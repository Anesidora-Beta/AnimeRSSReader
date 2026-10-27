import io
import json
import shutil
import tempfile
import unittest

from arrkit import Module
from arrkit.hosting import export, wsgi_app
from arrkit.testing import Client


class Ping(Module):
    id = "ping"
    name = "ping"
    version = "1.0.0"

    def setup(self, ctx):
        ctx.router.get("/api/ping", lambda req: {"ok": True, "pong": 1})


class MountHost:
    def __init__(self):
        self.router = None

    def mount(self, router):
        self.router = router


class AddRouteHost:
    def __init__(self):
        self.routes = []

    def add_route(self, method, path, fn):
        self.routes.append((method, path, fn))


class RouteHost:
    def __init__(self):
        self.routes = []

    def route(self, method, path, fn):
        self.routes.append((method, path, fn))


class BadHost:
    pass


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arrkit-h-")
        self.kw = dict(data_dir=self.tmp, no_plugins=True, modules=[Ping])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mount_host(self):
        host = MountHost()
        k = export(host, **self.kw)
        try:
            self.assertIs(host.router, k.router)
            self.assertEqual(Client(k).get("/api/ping").json["pong"], 1)
        finally:
            k.stop()

    def test_add_route_host(self):
        host = AddRouteHost()
        k = export(host, start=False, **self.kw)
        try:
            paths = [p for _, p, _ in host.routes]
            self.assertIn("/api/ping", paths)
            self.assertTrue(any(p.startswith("/_kit/") for p in paths))
            self.assertFalse(k._started)          # start=False 不启任务表
        finally:
            k.stop()

    def test_route_host(self):
        host = RouteHost()
        k = export(host, **self.kw)
        try:
            self.assertTrue(host.routes)
        finally:
            k.stop()

    def test_bad_host_raises_and_cleans_up(self):
        with self.assertRaises(TypeError):
            export(BadHost(), **self.kw)

    def test_none_host_returns_kernel(self):
        k = export(None, **self.kw)
        try:
            self.assertTrue(k._started)
        finally:
            k.stop()


class WsgiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arrkit-w-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, site, path, method="GET", body=b""):
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
            "HTTP_HOST": "127.0.0.1:8899",
        }
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        chunks = site(environ, start_response)
        return captured, b"".join(chunks)

    def test_wsgi_roundtrip(self):
        site = wsgi_app(data_dir=self.tmp, no_plugins=True, modules=[Ping])
        try:
            got, body = self._call(site, "/api/ping")
            self.assertTrue(got["status"].startswith("200"))
            self.assertEqual(json.loads(body), {"ok": True, "pong": 1})
            self.assertIn("Content-Length", got["headers"])
        finally:
            site.stop()

    def test_wsgi_404_and_head(self):
        site = wsgi_app(data_dir=self.tmp, no_plugins=True, modules=[Ping])
        try:
            got, _ = self._call(site, "/nope")
            self.assertTrue(got["status"].startswith("404"))
            got, body = self._call(site, "/api/ping", method="HEAD")
            self.assertTrue(got["status"].startswith("200"))
            self.assertEqual(body, b"")
        finally:
            site.stop()


if __name__ == "__main__":
    unittest.main()
