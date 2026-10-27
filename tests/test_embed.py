"""可嵌入组装的单元测试：create_app / handle / wsgi / export（P4）。

跑法： ``python -m unittest discover -s tests -v``
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anime_rss.app import App, AppConfig, create_app, alias_api_v1   # noqa: E402
from anime_rss.host import export, wsgi_app                          # noqa: E402
from anime_rss.kernel.http import Router                             # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-embed-")
        self.app = None

    def tearDown(self):
        if self.app is not None:
            self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, **kw):
        kw.setdefault("data_dir", os.path.join(self.tmp, "data"))
        kw.setdefault("log", None)
        self.app = create_app(**kw)
        return self.app


class CreateAppTest(_Base):
    def test_build_without_server(self):
        app = self.make()
        self.assertTrue(app.status()["ok"])
        self.assertIn("feeds", app.status()["modules"])
        self.assertTrue(os.path.isdir(app.config.resolve_data_dir()))

    def test_handle_returns_response(self):
        app = self.make()
        resp = app.handle("GET", "/healthz")
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["app"], "anime-rss-reader")

    def test_handle_serves_static(self):
        app = self.make()
        resp = app.handle("GET", "/assets/app.js")
        self.assertEqual(resp.status, 200)
        self.assertIn("javascript", resp.ctype)
        self.assertIn(b"registerView", resp.body)

    def test_handle_unknown_path(self):
        app = self.make()
        self.assertEqual(app.handle("GET", "/nope").status, 404)

    def test_handle_json_body(self):
        app = self.make()
        resp = app.handle("POST", "/api/settings", body={"values": {"theme": "dark"}})
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["values"]["theme"], "dark")

    def test_two_apps_are_isolated(self):
        a = create_app(data_dir=os.path.join(self.tmp, "a"))
        b = create_app(data_dir=os.path.join(self.tmp, "b"))
        try:
            self.assertNotEqual(a.store.path, b.store.path)
            a.handle("POST", "/api/settings", body={"values": {"theme": "dark"}})
            got_b = json.loads(b.handle("GET", "/api/settings").body)["values"]["theme"]
            self.assertNotEqual(got_b, "dark")
        finally:
            a.stop()
            b.stop()


class ApiV1Test(_Base):
    def test_v1_alias_same_body(self):
        app = self.make()
        old = app.handle("GET", "/api/items?limit=1")
        new = app.handle("GET", "/api/v1/items?limit=1")
        self.assertEqual(old.status, 200)
        self.assertEqual(new.status, 200)
        self.assertEqual(json.loads(old.body)["items"], json.loads(new.body)["items"])

    def test_v1_alias_post(self):
        app = self.make()
        resp = app.handle("POST", "/api/v1/settings", body={"values": {"theme": "dark"}})
        self.assertEqual(resp.status, 200)

    def test_can_disable_alias(self):
        app = self.make(api_v1=False)
        self.assertEqual(app.handle("GET", "/api/v1/health").status, 404)
        self.assertEqual(app.handle("GET", "/api/health").status, 200)

    def test_alias_helper_is_idempotent(self):
        router = Router()
        router.get("/api/demo", lambda req: None)
        first = alias_api_v1(router)
        second = alias_api_v1(router)
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertIn("GET /api/v1/demo", router.paths())


class LifecycleTest(_Base):
    def test_start_registers_jobs(self):
        app = self.make(fetch_on_start=True)
        app.start()
        names = app.jobs.names()
        self.assertIn("auto-refresh", names)
        self.assertIn("boot-fetch", names)       # 空库必然过期，应安排首次抓取
        self.assertTrue(app.jobs.running)

    def test_stop_is_idempotent(self):
        app = self.make()
        app.start()
        app.stop()
        app.stop()
        self.assertFalse(app.jobs.running)
        self.assertFalse(app.status()["ok"])

    def test_no_fetch_on_start(self):
        app = self.make(fetch_on_start=False)
        app.start()
        self.assertNotIn("boot-fetch", app.jobs.names())


class WsgiTest(_Base):
    def _call(self, callable_, path, query="", method="GET", body=b""):
        import io
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "CONTENT_LENGTH": str(len(body)) if body else "",
            "wsgi.input": io.BytesIO(body),
            "HTTP_HOST": "127.0.0.1:8899",
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = callable_(environ, start_response)
        return captured, b"".join(chunks)

    def test_wsgi_get(self):
        app = self.make()
        got, body = self._call(app.wsgi, "/api/v1/health")
        self.assertTrue(got["status"].startswith("200"))
        self.assertEqual(json.loads(body)["ok"], True)
        self.assertEqual(int(got["headers"]["Content-Length"]), len(body))

    def test_wsgi_head_has_no_body(self):
        app = self.make()
        got, body = self._call(app.wsgi, "/healthz", method="HEAD")
        self.assertTrue(got["status"].startswith("200"))
        self.assertEqual(body, b"")

    def test_wsgi_app_helper(self):
        site = wsgi_app(data_dir=os.path.join(self.tmp, "wsgi"))
        try:
            got, body = self._call(site, "/api/health")
            self.assertTrue(got["status"].startswith("200"))
            self.assertIn(b"anime-rss-reader", body)
            self.assertTrue(site.app.status()["ok"])
        finally:
            site.stop()


class _RouteHost:
    def __init__(self):
        self.routes = []

    def add_route(self, method, path, fn):
        self.routes.append((method, path, fn))


class _MountHost:
    def __init__(self):
        self.router = None

    def mount(self, router):
        self.router = router


class ExportTest(_Base):
    def test_export_to_route_host(self):
        host = _RouteHost()
        app = export(host, data_dir=os.path.join(self.tmp, "h"),
                     start=False, static_dir=None)
        self.app = app
        paths = [p for _, p, _ in host.routes]
        self.assertIn("/api/items", paths)
        self.assertIn("/api/v1/items", paths)
        self.assertIn("/healthz", paths)

    def test_export_to_mount_host(self):
        host = _MountHost()
        app = export(host, data_dir=os.path.join(self.tmp, "h2"), start=False)
        self.app = app
        self.assertIsNotNone(host.router)
        self.assertIn("GET /api/bootstrap", host.router.paths())

    def test_export_rejects_unknown_host(self):
        class _Nothing:
            pass
        with self.assertRaises(TypeError):
            export(_Nothing(), data_dir=os.path.join(self.tmp, "h3"), start=False)

    def test_export_start_false_keeps_jobs_stopped(self):
        host = _RouteHost()
        app = export(host, data_dir=os.path.join(self.tmp, "h4"), start=False)
        self.app = app
        self.assertFalse(app.jobs.running)


class ConfigTest(unittest.TestCase):
    def test_defaults(self):
        cfg = AppConfig()
        self.assertFalse(cfg.fetch_on_start)     # 嵌入默认不抓网络
        self.assertTrue(cfg.auto_refresh)
        self.assertTrue(cfg.api_v1)
        self.assertTrue(cfg.resolve_data_dir().endswith("data"))

    def test_repr(self):
        self.assertIn("AppConfig", repr(AppConfig(port=9)))
        self.assertIn("App", repr(App()))

    def test_data_dir_from_env(self):
        old = os.environ.get("ANIME_RSS_DATA")
        os.environ["ANIME_RSS_DATA"] = os.path.join(tempfile.gettempdir(), "arr-env")
        try:
            self.assertTrue(AppConfig().resolve_data_dir().endswith("arr-env"))
        finally:
            if old is None:
                os.environ.pop("ANIME_RSS_DATA", None)
            else:
                os.environ["ANIME_RSS_DATA"] = old


if __name__ == "__main__":
    unittest.main()
