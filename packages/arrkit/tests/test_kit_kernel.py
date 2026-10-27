import json
import os
import shutil
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.request

from arrkit import Module
from arrkit.adapters import MemoryAssets
from arrkit.kernel import KernelConfig, create_kernel
from arrkit.testing import Client


class Counter(Module):
    id = "counter"
    name = "计数器"
    version = "1.0.0"

    def setup(self, ctx):
        self.n = 0
        self.calls = []
        ctx.router.get("/api/counter", self.read)
        ctx.router.post("/api/counter", self.bump)
        ctx.settings.define("counter", [
            {"key": "counter.step", "type": "int", "default": 1, "min": 1, "max": 9},
        ])

    def read(self, req):
        return {"ok": True, "n": self.n}

    def bump(self, req):
        step = self.ctx.settings.get("counter.step") or 1
        self.n += step
        self.ctx.emit("counter.bumped", {"n": self.n})
        return {"ok": True, "n": self.n}

    def bootstrap(self, ctx):
        return {"counter": {"n": self.n}}

    def apply_settings(self, values):
        self.calls.append(values.get("counter.step"))


class Boom(Module):
    id = "boom"
    name = "坏模块"

    def bootstrap(self, ctx):
        raise RuntimeError("bootstrap 故意失败")


class KernelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arrkit-k-")
        self.k = create_kernel(data_dir=self.tmp, no_plugins=True,
                               modules=[Counter], app_name="测试")
        self.c = Client(self.k)

    def tearDown(self):
        self.k.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_admin_endpoints(self):
        self.assertEqual(self.c.get("/_kit/health").json["app"], "测试")
        self.assertEqual(self.c.get("/_kit/status").status, 200)
        self.assertEqual(self.c.get("/_kit/modules").json["modules"][0]["id"], "counter")
        self.assertEqual(self.c.get("/_kit/views").json["views"], [])
        self.assertEqual(self.c.get("/_kit/jobs").json["ok"], True)
        self.assertEqual(self.c.get("/_kit/nope").status, 404)

    def test_custom_admin_prefix(self):
        tmp = tempfile.mkdtemp(prefix="arrkit-k2-")
        try:
            k = create_kernel(data_dir=tmp, no_plugins=True, modules=[Counter],
                              admin_prefix="/admin")
            self.assertEqual(Client(k).get("/admin/health").status, 200)
            self.assertEqual(Client(k).get("/_kit/health").status, 404)
            k.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_bootstrap_merges_and_isolates(self):
        tmp = tempfile.mkdtemp(prefix="arrkit-k3-")
        try:
            k = create_kernel(data_dir=tmp, no_plugins=True, modules=[Counter, Boom])
            boot = Client(k).get("/_kit/bootstrap").json
            self.assertEqual(boot["counter"], {"n": 0})
            self.assertEqual(boot["app"], "arrkit")      # 坏模块没把整页搞挂
            self.assertIn("views", boot)
            self.assertIn("settings", boot)
            k.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_settings_save_calls_apply_and_emits(self):
        seen = []
        self.k.bus.on("settings.changed", lambda p: seen.append(p["keys"]))
        reply = self.c.post("/_kit/settings", {"counter.step": 5})
        self.assertEqual(reply.json["changed"], ["counter.step"])
        mod = self.k.ctx.modules.get("counter")
        self.assertEqual(mod.calls, [5])
        self.assertEqual(seen, [["counter.step"]])
        self.assertEqual(self.c.post("/api/counter", {}).json["n"], 5)

    def test_settings_reset(self):
        self.c.post("/_kit/settings", {"counter.step": 4})
        r = self.c.post("/_kit/settings/reset", {})
        self.assertEqual(r.json["values"]["counter.step"], 1)

    def test_jobs_endpoint_runs_job(self):
        hits = []
        self.k.jobs.every(3600, lambda: hits.append(1), name="manual")
        r = self.c.post("/_kit/jobs/manual/run", {})
        self.assertEqual(r.status, 200)
        self.k.jobs.wait_idle(2)
        self.assertEqual(hits, [1])
        self.assertEqual(self.c.post("/_kit/jobs/nope/run", {}).status, 409)

    def test_static_mount_missing(self):
        # 没给 static_dir 时不该挂 /assets/，也不该报错
        self.assertEqual(self.c.get("/assets/index.html").status, 404)

    def test_memory_assets_port(self):
        tmp = tempfile.mkdtemp(prefix="arrkit-k4-")
        try:
            assets = MemoryAssets({"/pages/index.html": b"<h1>hi</h1>"})
            k = create_kernel(data_dir=tmp, no_plugins=True, modules=[Counter],
                              static_dir="/pages", asset_port=assets)
            reply = Client(k).get("/assets/index.html")
            self.assertEqual(reply.status, 200)
            self.assertEqual(reply.body, b"<h1>hi</h1>")
            k.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_plugin_discovery_from_dir(self):
        tmp = tempfile.mkdtemp(prefix="arrkit-k5-")
        plug = os.path.join(tmp, "modules")
        os.makedirs(plug)
        with open(os.path.join(plug, "extra.py"), "w", encoding="utf-8") as f:
            f.write(textwrap.dedent('''
                from arrkit import Module

                class Extra(Module):
                    id = "extra"
                    name = "外挂"
                    version = "0.1.0"

                    def setup(self, ctx):
                        ctx.router.get("/api/extra", lambda req: {"ok": True})
            '''))
        try:
            k = create_kernel(data_dir=tmp, modules=[Counter])
            self.assertIn("extra", k.ctx.modules.ids())
            self.assertEqual(Client(k).get("/api/extra").status, 200)
            k.stop()

            k2 = create_kernel(data_dir=tmp, modules=[Counter], no_plugins=True)
            self.assertNotIn("extra", k2.ctx.modules.ids())
            k2.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_instances_are_isolated(self):
        other = tempfile.mkdtemp(prefix="arrkit-k6-")
        try:
            k2 = create_kernel(data_dir=other, no_plugins=True, modules=[Counter])
            Client(self.k).post("/api/counter", {})
            self.assertEqual(Client(self.k).get("/api/counter").json["n"], 1)
            self.assertEqual(Client(k2).get("/api/counter").json["n"], 0)
            k2.stop()
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_serve_on_random_port(self):
        tmp = tempfile.mkdtemp(prefix="arrkit-k7-")
        try:
            k = create_kernel(data_dir=tmp, no_plugins=True, modules=[Counter])
            t = threading.Thread(target=k.serve, kwargs={"port": 0, "quiet": True},
                                 daemon=True)
            t.start()
            for _ in range(100):
                if k.server is not None:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(k.server)
            _, port = k.server.address
            self.assertGreater(port, 0)

            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open("http://127.0.0.1:%d/_kit/health" % port, timeout=5) as r:
                self.assertEqual(json.loads(r.read())["ok"], True)

            k.server.shutdown()
            t.join(5)
            self.assertFalse(t.is_alive())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_status_shape(self):
        st = self.k.status()
        for key in ("ok", "app", "version", "dataDir", "modules",
                    "views", "jobs", "adminPrefix"):
            self.assertIn(key, st)

    def test_context_helpers(self):
        ctx = self.k.ctx
        self.assertIs(ctx.module("counter"), ctx.modules.get("counter"))
        self.assertIsNone(ctx.module("nope"))
        self.assertEqual(ctx.service("nope", 3), 3)
        self.assertIsNotNone(ctx.every(3600, lambda: None, name="ctx.job"))
        self.assertIsNotNone(ctx.later(3600, lambda: None, name="ctx.later"))
        ctx.jobs.cancel("ctx.job")
        ctx.jobs.cancel("ctx.later")

    def test_config_resolution(self):
        c = KernelConfig(data_dir=self.tmp)
        self.assertEqual(c.resolve_data_dir(), os.path.abspath(self.tmp))
        self.assertEqual(c.resolve_plugin_dirs(), [os.path.join(os.path.abspath(self.tmp),
                                                                "modules")])
        self.assertEqual(KernelConfig().resolve_static_dir(), "")


if __name__ == "__main__":
    unittest.main()
