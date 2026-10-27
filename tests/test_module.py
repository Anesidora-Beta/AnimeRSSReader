"""模块契约的单元测试（纯标准库 unittest）。

跑法： ``python -m unittest discover -s tests -v``
只看契约行为，不起服务；端到端的检查在仓库根的 ``smoke.py``。
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import settings as CFG          # noqa: E402
import store as ST              # noqa: E402
from anime_rss import API_VERSION                       # noqa: E402
from anime_rss.discovery import discover, merge_unique   # noqa: E402
from anime_rss.kernel.context import Context            # noqa: E402
from anime_rss.kernel.http import Router                # noqa: E402
from anime_rss.module import Module, ModuleError, load_modules   # noqa: E402
from anime_rss.modules import BUILTIN_MODULES           # noqa: E402


class _Tmp:
    """每个用例一份临时数据目录，互不干扰。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-test-")
        self.store = ST.Store(os.path.join(self.tmp, "t.db"))
        self.settings = CFG.Settings(self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ctx(self, **kw):
        kw.setdefault("store", self.store)
        kw.setdefault("settings", self.settings)
        kw.setdefault("router", Router())
        kw.setdefault("data_dir", self.tmp)
        return Context(**kw)


class ModuleLoadingTest(_Tmp, unittest.TestCase):
    def test_builtin_schedule_loads(self):
        ctx = self.ctx()
        reg = load_modules(ctx, BUILTIN_MODULES)
        self.assertTrue(reg.has("schedule"))
        self.assertIs(ctx.schedule, reg.get("schedule").service)

    def test_all_builtin_modules_load(self):
        reg = load_modules(self.ctx(), BUILTIN_MODULES)
        self.assertEqual(sorted(reg.ids()),
                         ["feeds", "reader", "schedule", "settings"])

    def test_route_table_is_stable(self):
        """模块化前后对外路由必须一模一样，少一条就是破坏兼容。"""
        ctx = self.ctx()
        load_modules(ctx, BUILTIN_MODULES)
        expected = {
            "DELETE /api/feeds/{fid}",
            "DELETE /api/watches/{wid}",
            "GET /api/item/{iid}",
            "GET /api/items",
            "GET /api/mirrors",
            "GET /api/refresh",
            "GET /api/schedule",
            "GET /api/schedule/progress",
            "GET /api/schedule/status",
            "GET /api/settings",
            "GET /api/watches",
            "GET /feed.xml",
            "GET /feed/{fid}",
            "GET /feeds.opml",
            "GET /opml.xml",
            "GET /rss",
            "GET /rss.xml",
            "PATCH /api/feeds/{fid}",
            "PATCH /api/watches/{wid}",
            "POST /api/feeds",
            "POST /api/maintenance",
            "POST /api/read",
            "POST /api/read-all",
            "POST /api/refresh",
            "POST /api/schedule/refresh",
            "POST /api/settings",
            "POST /api/settings/reset",
            "POST /api/star",
            "POST /api/watches",
            "POST /api/watches/{wid}/scan",
        }
        got = set(ctx.router.paths())
        self.assertEqual(sorted(expected - got), [], "模块少注册了路由")
        self.assertEqual(sorted(got - expected), [], "模块多注册了路由")

    def test_reader_depends_on_feeds(self):
        order = []
        load_modules(self.ctx(), BUILTIN_MODULES,
                     log=lambda msg: order.append(msg.strip().split()[1]))
        self.assertLess(order.index("feeds"), order.index("reader"))

    def test_classes_instantiated_per_load(self):
        a = load_modules(self.ctx(), BUILTIN_MODULES).get("schedule")
        b = load_modules(self.ctx(), BUILTIN_MODULES).get("schedule")
        self.assertIsNot(a, b)

    def test_duplicate_id_rejected(self):
        class Dup(Module):
            id = "schedule"
            name = "重复"

            def setup(self, ctx):
                pass

        with self.assertRaises(ModuleError):
            load_modules(self.ctx(), [Dup, Dup])

    def test_api_version_mismatch_rejected(self):
        class Old(Module):
            id = "old"
            name = "旧"
            api_version = "0"

            def setup(self, ctx):
                pass

        with self.assertRaises(ModuleError) as cm:
            load_modules(self.ctx(), [Old])
        self.assertIn("api_version", str(cm.exception))

    def test_missing_dependency_rejected(self):
        class Needs(Module):
            id = "needs"
            name = "需要别人"
            depends = ("ghost",)

            def setup(self, ctx):
                pass

        with self.assertRaises(ModuleError) as cm:
            load_modules(self.ctx(), [Needs])
        self.assertIn("ghost", str(cm.exception))

    def test_dependency_cycle_rejected(self):
        class A(Module):
            id = "a"
            name = "A"
            depends = ("b",)

            def setup(self, ctx):
                pass

        class B(Module):
            id = "b"
            name = "B"
            depends = ("a",)

            def setup(self, ctx):
                pass

        with self.assertRaises(ModuleError):
            load_modules(self.ctx(), [A, B])

    def test_dependency_loaded_first(self):
        order = []

        class First(Module):
            id = "first"
            name = "先"

            def setup(self, ctx):
                order.append("first")

        class Second(Module):
            id = "second"
            name = "后"
            depends = ("first",)

            def setup(self, ctx):
                order.append("second")

        load_modules(self.ctx(), [Second, First])
        self.assertEqual(order, ["first", "second"])

    def test_setup_error_wrapped(self):
        class Boom(Module):
            id = "boom"
            name = "会炸"

            def setup(self, ctx):
                raise ValueError("故意的")

        with self.assertRaises(ModuleError) as cm:
            load_modules(self.ctx(), [Boom])
        self.assertIn("故意的", str(cm.exception))

    def test_registry_exposes_module(self):
        ctx = self.ctx()
        load_modules(ctx, BUILTIN_MODULES)
        self.assertIsNotNone(ctx.module("schedule"))
        self.assertIsNone(ctx.module("nope"))
        self.assertIsNone(self.ctx().module("schedule"))   # 未加载时安全返回 None


class ScheduleModuleTest(_Tmp, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.ctx_obj = self.ctx()
        self.reg = load_modules(self.ctx_obj, BUILTIN_MODULES)
        self.mod = self.reg.get("schedule")

    def test_brief_shape(self):
        brief = self.mod.brief()
        self.assertEqual(set(brief), {"total", "today", "updated", "running"})
        self.assertIsInstance(brief["total"], int)

    def test_apply_settings_sources(self):
        self.mod.apply_settings({"scheduleSources": ["bangumi"], "scheduleTtl": 24})
        self.assertEqual(self.mod.service.enabled, ["bangumi"])
        self.assertEqual(self.mod.service.ttl, 24 * 3600)

    def test_apply_settings_empty_sources_falls_back_to_all(self):
        all_ids = list(self.mod.service.enabled)
        self.mod.apply_settings({"scheduleSources": [], "scheduleTtl": 6})
        self.assertEqual(len(self.mod.service.enabled), len(all_ids))
        self.assertEqual(self.mod.service.ttl, 6 * 3600)

    def test_api_version_matches_kernel(self):
        self.assertEqual(self.mod.api_version, API_VERSION)


class BootstrapFragmentTest(_Tmp, unittest.TestCase):
    """首屏聚合靠各模块的 bootstrap(ctx) 片段，字段不能丢也不能变味。"""

    def setUp(self):
        super().setUp()
        self.ctx_obj = self.ctx()
        self.reg = load_modules(self.ctx_obj, BUILTIN_MODULES)

    def test_feeds_fragment(self):
        frag = self.reg.get("feeds").bootstrap(self.ctx_obj)
        self.assertEqual(set(frag), {"sources", "feeds", "mirrors"})
        self.assertGreaterEqual(len(frag["feeds"]), 1)
        self.assertIn("sourceName", frag["feeds"][0])

    def test_reader_fragment(self):
        frag = self.reg.get("reader").bootstrap(self.ctx_obj)
        self.assertEqual(set(frag), {"watches"})
        self.assertIsInstance(frag["watches"], list)

    def test_settings_fragment(self):
        frag = self.reg.get("settings").bootstrap(self.ctx_obj)
        self.assertEqual(set(frag), {"settings"})
        self.assertIn("refreshInterval", frag["settings"])

    def test_schedule_fragment(self):
        frag = self.reg.get("schedule").bootstrap(self.ctx_obj)
        self.assertEqual(set(frag), {"schedule"})
        self.assertEqual(set(frag["schedule"]),
                         {"total", "today", "updated", "running"})

    def test_broken_module_does_not_break_bootstrap(self):
        class Broken(Module):
            id = "broken"
            name = "会炸"
            version = "1.0.0"

            def setup(self, ctx):
                pass

            def bootstrap(self, ctx):
                raise RuntimeError("故意的")

        reg = load_modules(self.ctx_obj, list(BUILTIN_MODULES) + [Broken])
        self.assertIn("broken", reg.ids())      # 加载不受影响
        # 聚合时出错的模块被跳过，其它片段照旧
        self.assertEqual(set(reg.get("settings").bootstrap(self.ctx_obj)),
                         {"settings"})


class DiscoveryTest(unittest.TestCase):
    """插件发现：单文件、包、坏文件、重名，四种情况都要按预期处理。"""

    GOOD = """
from anime_rss.module import Module
from anime_rss.kernel.http import Response


class DemoPlugin(Module):
    id = "demo_plugin"
    name = "演示插件"
    version = "0.1.0"

    def setup(self, ctx):
        ctx.router.get("/api/demo_plugin/ping", self.ping)

    def ping(self, req):
        return Response.json({"ok": True})
"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="arr-plugins-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(path) else None
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_single_file_plugin(self):
        self.write("demo.py", self.GOOD)
        mods, problems = discover([self.dir], use_entry_points=False)
        self.assertEqual(problems, [])
        self.assertEqual([m.id for m in mods], ["demo_plugin"])

    def test_package_plugin(self):
        self.write(os.path.join("pkgdemo", "__init__.py"), self.GOOD)
        mods, problems = discover([self.dir], use_entry_points=False)
        self.assertEqual(problems, [])
        self.assertEqual([m.id for m in mods], ["demo_plugin"])

    def test_broken_plugin_does_not_raise(self):
        self.write("broken.py", "import 不存在的模块\n")
        mods, problems = discover([self.dir], use_entry_points=False)
        self.assertEqual(mods, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("broken.py", problems[0])

    def test_file_without_module_reported(self):
        self.write("plain.py", "X = 1\n")
        mods, problems = discover([self.dir], use_entry_points=False)
        self.assertEqual(mods, [])
        self.assertIn("没有 Module 子类", problems[0])

    def test_underscore_files_ignored(self):
        self.write("_private.py", self.GOOD)
        mods, problems = discover([self.dir], use_entry_points=False)
        self.assertEqual((mods, problems), ([], []))

    def test_missing_directory_is_fine(self):
        mods, problems = discover([os.path.join(self.dir, "nope")],
                                  use_entry_points=False)
        self.assertEqual((mods, problems), ([], []))

    def test_duplicate_id_dropped(self):
        class Builtin(Module):
            id = "demo_plugin"
            name = "内置"

            def setup(self, ctx):
                pass

        self.write("demo.py", self.GOOD)
        extra, _ = discover([self.dir], use_entry_points=False)
        merged = merge_unique([Builtin], extra)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].name, "内置")      # 内置优先

    def test_plugin_without_id_dropped(self):
        class NoId(Module):
            name = "没 id"

            def setup(self, ctx):
                pass

        self.assertEqual(merge_unique([], [NoId()]), [])


class SettingsDefineTest(_Tmp, unittest.TestCase):
    """模块自带设置项：define() 之后要能读、能存、能校验、能控制位置。"""

    GROUP = {
        "group": "demo",
        "title": "演示模块",
        "items": [
            {"k": "demoSwitch", "type": "switch", "label": "开关", "default": False},
            {"k": "demoNumber", "type": "number", "label": "数字", "default": 5,
             "min": 1, "max": 10, "step": 1},
            {"k": "demoPick", "type": "select", "label": "选项", "default": "a",
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
        ],
    }

    def test_define_adds_group_and_defaults(self):
        self.settings.define(self.GROUP, after="read")
        groups = [g["group"] for g in self.settings.schema()]
        self.assertIn("demo", groups)
        self.assertEqual(groups[groups.index("read") + 1], "demo")

        values = self.settings.get()
        self.assertIs(values["demoSwitch"], False)
        self.assertEqual(values["demoNumber"], 5)
        self.assertEqual(values["demoPick"], "a")

    def test_define_values_are_validated(self):
        self.settings.define(self.GROUP)
        out = self.settings.save({"demoNumber": 999, "demoPick": "zzz"})
        self.assertEqual(out["demoNumber"], 10)     # 夹到 max
        self.assertEqual(out["demoPick"], "a")      # 非法值回落默认

    def test_define_merges_same_group(self):
        self.settings.define(self.GROUP)
        self.settings.define({"group": "demo", "title": "改过名",
                              "items": [{"k": "demoExtra", "type": "switch",
                                         "label": "新增", "default": True}]})
        group = [g for g in self.settings.schema() if g["group"] == "demo"][0]
        keys = [it["k"] for it in group["items"]]
        self.assertEqual(keys, ["demoSwitch", "demoNumber", "demoPick", "demoExtra"])
        self.assertEqual(group["title"], "改过名")
        self.assertIs(self.settings.get()["demoExtra"], True)

    def test_schema_is_per_instance(self):
        other = CFG.Settings(self.store)
        self.settings.define({"group": "onlyhere", "title": "只在这",
                              "items": [{"k": "onlyKey", "type": "switch",
                                         "default": True}]})
        self.assertIn("onlyhere", [g["group"] for g in self.settings.schema()])
        self.assertNotIn("onlyhere", [g["group"] for g in other.schema()])
        self.assertNotIn("onlyKey", other.get())

    def test_define_rejects_bad_item(self):
        with self.assertRaises(ValueError):
            self.settings.define({"group": "bad", "items": [{"type": "switch"}]})
        with self.assertRaises(ValueError):
            self.settings.define({"items": []})

    def test_schedule_group_comes_from_module(self):
        load_modules(self.ctx(), BUILTIN_MODULES)
        groups = [g["group"] for g in self.settings.schema()]
        self.assertEqual(groups, ["fetch", "read", "schedule", "store", "app"])
        self.assertEqual(self.settings.get()["scheduleTtl"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
