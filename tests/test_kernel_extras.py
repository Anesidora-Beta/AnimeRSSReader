"""内核扩展能力的单元测试：事件总线 / 定时任务 / 前端视图。

跑法： ``python -m unittest discover -s tests -v``
这些用例不联网、不起服务，几百毫秒跑完。
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anime_rss.kernel.bus import Bus                       # noqa: E402
from anime_rss.kernel.context import Context              # noqa: E402
from anime_rss.kernel.jobs import Jobs                    # noqa: E402
from anime_rss.kernel.views import ViewError, ViewRegistry  # noqa: E402
from anime_rss.module import Module, load_modules, unload_modules  # noqa: E402


# ---------------------------------------------------------------- 内核自带路由

class CoreRoutesTest(unittest.TestCase):
    def test_core_route_table(self):
        """外壳路由就那么几条，多一条少一条都要有意识地改这个清单。"""
        from anime_rss.api.core import CoreApi
        from anime_rss.kernel.http import Router
        router = Router()
        CoreApi(Context()).register(router)
        self.assertEqual(sorted(router.paths()), sorted([
            "GET /",
            "GET /index.html",
            "GET /favicon.ico",
            "GET /apple-touch-icon.png",
            "GET /healthz",
            "GET /api/health",
            "GET /api/bootstrap",
            "GET /api/views",
            "GET /api/jobs",
            "POST /api/open-window",
            "GET /plug/{mid}/{name:path}",
        ]))

    def test_bootstrap_carries_views(self):
        """没有视图注册表时也不能炸，返回空清单即可。"""
        from anime_rss.api.core import CoreApi
        from anime_rss.kernel.http import Request

        class _Store:
            def counts(self):
                return {"total": 0, "unread": 0, "starred": 0, "feeds": {}, "watches": {}}

            def stats(self):
                return {}

        class _Refresher:
            def status(self):
                return {}

        ctx = Context(store=_Store(), refresher=_Refresher())
        body = CoreApi(ctx).bootstrap(Request("GET", "/api/bootstrap")).body
        self.assertIn(b'"views": []', body)


# ---------------------------------------------------------------- 事件总线

class BusTest(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()
        self.got = []

    def test_emit_delivers_payload(self):
        self.bus.on("x.y", lambda p: self.got.append(p))
        n = self.bus.emit("x.y", {"a": 1})
        self.assertEqual(n, 1)
        self.assertEqual(self.got, [{"a": 1}])

    def test_no_subscriber_is_not_an_error(self):
        self.assertEqual(self.bus.emit("nothing.here", {}), 0)

    def test_once_only_fires_first_time(self):
        seen = []
        self.bus.once("boot", lambda p: seen.append(p))
        self.bus.emit("boot", 1)
        self.bus.emit("boot", 2)
        self.assertEqual(seen, [1])

    def test_wildcard_receives_event_and_payload(self):
        seen = []
        self.bus.on("*", lambda e, p: seen.append((e, p)))
        self.bus.emit("a.b", 7)
        self.assertEqual(seen, [("a.b", 7)])

    def test_handler_error_does_not_break_others(self):
        def boom(_):
            raise RuntimeError("坏了")
        self.bus.on("x", boom)
        self.bus.on("x", lambda p: self.got.append("ok"))
        n = self.bus.emit("x")
        self.assertEqual(n, 1)                 # 好的那个仍然收到
        self.assertEqual(self.got, ["ok"])
        self.assertEqual(self.bus.status()["errors"], 1)

    def test_off_and_cancel(self):
        def h(_):
            self.got.append(1)
        sub = self.bus.on("x", h)
        self.bus.off("x", h)
        self.bus.emit("x")
        self.assertEqual(self.got, [])
        sub2 = self.bus.on("y", h)
        sub2.cancel()
        self.bus.emit("y")
        self.assertEqual(self.got, [])
        self.assertTrue(sub.cancelled)

    def test_off_without_args_clears_all(self):
        self.bus.on("a", lambda p: None)
        self.bus.on("b", lambda p: None)
        self.assertEqual(len(self.bus), 2)
        self.assertEqual(self.bus.off(), 2)
        self.assertEqual(len(self.bus), 0)

    def test_reentrant_emit_does_not_deadlock(self):
        def relay(p):
            self.bus.emit("second", p + 1)
        self.bus.on("first", relay)
        self.bus.on("second", lambda p: self.got.append(p))
        self.bus.emit("first", 1)
        self.assertEqual(self.got, [2])

    def test_emit_bad_name(self):
        with self.assertRaises(ValueError):
            self.bus.emit("", {})

    def test_status_shape(self):
        self.bus.on("x", lambda p: None)
        self.bus.emit("x")
        st = self.bus.status()
        for key in ("events", "subscriptions", "emitted", "errors"):
            self.assertIn(key, st)
        self.assertEqual(st["events"]["x"], 1)
        self.assertEqual(st["emitted"], 1)


# ---------------------------------------------------------------- 定时任务

class JobsTest(unittest.TestCase):
    def setUp(self):
        self.jobs = Jobs()
        self.runs = []

    def tearDown(self):
        self.jobs.stop(timeout=2.0)

    def test_every_requires_positive_interval(self):
        with self.assertRaises(ValueError):
            self.jobs.every(0, lambda: None, name="bad")

    def test_run_pending_fires_due_job(self):
        self.jobs.every(0.01, lambda: self.runs.append(1), name="tick")
        time.sleep(0.03)
        self.jobs.run_pending()
        self.jobs.wait_idle(2)
        self.assertEqual(len(self.runs), 1)
        job = self.jobs.get("tick")
        self.assertEqual(job.runs, 1)
        self.assertEqual(job.errors, 0)

    def test_duplicate_name_rejected_but_reusable_after_cancel(self):
        self.jobs.every(10, lambda: None, name="dup")
        with self.assertRaises(ValueError):
            self.jobs.every(10, lambda: None, name="dup")
        self.jobs.cancel("dup")
        self.jobs.every(10, lambda: None, name="dup")
        self.assertIn("dup", self.jobs.names())

    def test_later_runs_once(self):
        self.jobs.later(0, lambda: self.runs.append("once"), name="one")
        self.jobs.run_pending()
        self.jobs.wait_idle(2)
        self.jobs.run_pending()
        self.jobs.wait_idle(2)
        self.assertEqual(self.runs, ["once"])
        self.assertNotIn("one", self.jobs.names())

    def test_failure_is_recorded_not_raised(self):
        def boom():
            raise ValueError("炸了")
        self.jobs.every(0.01, boom, name="bad")
        self.jobs.every(0.01, lambda: self.runs.append("ok"), name="good")
        time.sleep(0.03)
        self.jobs.run_pending()
        self.jobs.wait_idle(2)
        self.assertEqual(self.runs, ["ok"])          # 别的任务照跑
        self.assertEqual(self.jobs.get("bad").errors, 1)
        self.assertIn("炸了", self.jobs.get("bad").last_error)

    def test_start_and_stop(self):
        self.jobs.every(0.02, lambda: self.runs.append("t"), name="loop")
        self.assertTrue(self.jobs.start())
        self.assertFalse(self.jobs.start())          # 重复启动无副作用
        deadline = time.time() + 2
        while len(self.runs) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.runs), 2)
        self.jobs.stop()
        self.assertFalse(self.jobs.running)

    def test_trigger_runs_immediately(self):
        self.jobs.every(3600, lambda: self.runs.append("manual"), name="slow")
        self.assertTrue(self.jobs.trigger("slow"))
        self.jobs.wait_idle(2)
        self.assertEqual(self.runs, ["manual"])
        self.assertFalse(self.jobs.trigger("missing"))

    def test_status_lists_jobs(self):
        self.jobs.every(5, lambda: None, name="a")
        st = self.jobs.status()
        names = [j["name"] for j in st["jobs"]]
        self.assertEqual(names, ["a"])
        self.assertIn("nextIn", st["jobs"][0])
        self.assertFalse(st["running"])


# ---------------------------------------------------------------- 前端视图

class ViewRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-view-")
        os.makedirs(os.path.join(self.tmp, "js"))
        with open(os.path.join(self.tmp, "view.js"), "w", encoding="utf-8") as f:
            f.write("ARR.registerView({id:'demo',mount:function(){}});")
        with open(os.path.join(self.tmp, "js", "deep.js"), "w", encoding="utf-8") as f:
            f.write("// deep")
        with open(os.path.join(self.tmp, "view.css"), "w", encoding="utf-8") as f:
            f.write(".x{}")
        self.reg = ViewRegistry()
        self.reg.mount("demo", self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_register_and_manifest(self):
        v = self.reg.register("demo", {"id": "demo", "title": "演示", "icon": "◆",
                                       "script": "view.js", "style": "view.css",
                                       "order": 20})
        self.assertEqual(v.script, "/plug/demo/view.js")
        self.assertEqual(v.style, "/plug/demo/view.css")
        man = self.reg.manifest()
        self.assertEqual(len(man), 1)
        self.assertEqual(man[0]["title"], "演示")
        self.assertEqual(man[0]["order"], 20)

    def test_duplicate_id_rejected(self):
        self.reg.register("demo", {"id": "demo", "script": "view.js"})
        with self.assertRaises(ViewError):
            self.reg.register("demo", {"id": "demo", "script": "view.js"})

    def test_bad_id_rejected(self):
        # id 为空会用模块 id 兜底（不算错），这里只测真不合法的
        for bad in ("Demo", "1x", "a b", "x/y", "带中文"):
            with self.assertRaises(ViewError):
                self.reg.register("demo", {"id": bad, "script": "view.js"})

    def test_empty_id_falls_back_to_module_id(self):
        v = self.reg.register("demo", {"script": "view.js"})
        self.assertEqual(v.id, "demo")

    def test_missing_script_rejected(self):
        with self.assertRaises(ViewError):
            self.reg.register("demo", {"id": "demo2", "script": "nope.js"})

    def test_mount_missing_dir(self):
        self.assertFalse(self.reg.mount("ghost", os.path.join(self.tmp, "nope")))

    def test_read_flat_and_nested(self):
        data, ctype = self.reg.read("demo", "view.js")
        self.assertIn(b"registerView", data)
        self.assertTrue(ctype.startswith("application/javascript"))
        nested, _ = self.reg.read("demo", "js/deep.js")
        self.assertEqual(nested, b"// deep")

    def test_read_blocks_traversal(self):
        for bad in ("../view.js", "..%2fview.js", "/etc/passwd",
                    "js/../../view.js", "..\\view.js", ""):
            self.assertIsNone(self.reg.read("demo", bad), bad)

    def test_order_and_iteration(self):
        self.reg.register("demo", {"id": "b", "script": "view.js", "order": 5})
        self.reg.register("demo", {"id": "a", "script": "view.js", "order": 1})
        self.assertEqual([v.id for v in self.reg], ["a", "b"])
        self.assertIn("a", self.reg)
        self.assertEqual(len(self.reg), 2)


# ---------------------------------------------------------------- 与模块契约的衔接

class _ViewModule(Module):
    id = "demo"
    name = "视图演示"
    version = "1.0.0"

    def __init__(self, script="view.js", with_dir=True):
        self.script = script
        self.with_dir = with_dir
        self.torn = False

    def setup(self, ctx):
        self.views = ({"id": "demo", "title": "演示", "script": self.script},)
        self.assets = "assets" if self.with_dir else ""

    def teardown(self):
        self.torn = True


class ModuleViewsTest(unittest.TestCase):
    """模块声明的视图要能被内核自动登记；坏的视图只跳过自己。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-mod-")
        self.pkg = os.path.join(self.tmp, "demo_pkg")
        os.makedirs(os.path.join(self.pkg, "assets"))
        with open(os.path.join(self.pkg, "assets", "view.js"), "w", encoding="utf-8") as f:
            f.write("// ok")
        with open(os.path.join(self.pkg, "mod.py"), "w", encoding="utf-8") as f:
            f.write("from anime_rss.module import Module\n"
                    "class M(Module):\n"
                    "    id = 'demopkg'\n"
                    "    name = '包内模块'\n"
                    "    views = ({'id': 'demopkg', 'title': '包内视图',"
                    " 'script': 'view.js'},)\n"
                    "    assets = 'assets'\n"
                    "    def setup(self, ctx):\n"
                    "        pass\n")
        sys.path.insert(0, self.pkg)
        self.ctx = Context(views=ViewRegistry())

    def tearDown(self):
        sys.path.remove(self.pkg)
        sys.modules.pop("mod", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.ctx.views.clear()

    def test_module_declared_view_is_registered(self):
        import mod                                    # noqa: F401 - 刚写出来的测试模块
        load_modules(self.ctx, [mod.M])
        self.assertEqual(self.ctx.views.ids(), ["demopkg"])
        man = self.ctx.views.manifest()[0]
        self.assertEqual(man["script"], "/plug/demopkg/view.js")
        data, _ = self.ctx.views.read("demopkg", "view.js")
        self.assertEqual(data, b"// ok")

    def test_bad_view_does_not_block_module(self):
        mod = _ViewModule(script="missing.js")
        # 类清单里的实例：目录是 tests/，assets 不存在，视图脚本也不存在
        load_modules(self.ctx, [mod])
        self.assertEqual(len(self.ctx.views), 0)      # 视图被跳过

    def test_unload_calls_teardown_reverse(self):
        a, b = _ViewModule(), _ViewModule()
        a.id, b.id = "a", "b"
        reg = load_modules(self.ctx, [a, b])
        self.assertEqual(unload_modules(reg), 2)
        self.assertTrue(a.torn and b.torn)


# ---------------------------------------------------------------- ctx 快捷方式

class ContextShortcutTest(unittest.TestCase):
    def test_shortcuts_work(self):
        bus, jobs = Bus(), Jobs()
        ctx = Context(bus=bus, jobs=jobs)
        seen = []
        ctx.on("ping", lambda p: seen.append(p))
        self.assertEqual(ctx.emit("ping", 1), 1)
        self.assertEqual(seen, [1])
        job = ctx.every(60, lambda: None, name="x")
        self.assertIs(job, jobs.get("x"))
        self.assertIsNotNone(ctx.later(5, lambda: None, name="y"))
        jobs.cancel("y")
        jobs.stop(1)

    def test_shortcuts_are_safe_without_parts(self):
        ctx = Context()
        self.assertIsNone(ctx.on("x", lambda p: None))
        self.assertEqual(ctx.emit("x"), 0)
        self.assertIsNone(ctx.every(1, lambda: None))
        self.assertIsNone(ctx.view("nope"))


if __name__ == "__main__":
    unittest.main()
