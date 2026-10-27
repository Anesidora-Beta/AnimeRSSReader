# -*- coding: utf-8 -*-
"""状态机测试：磁盘与内存的六种组合，必须落在预期状态上。

这一层的价值全在「不一致」上 —— 用户最怕界面说已停用、其实还在跑。
所以每个 case 都是先造出一个矛盾，再看它有没有被如实标出来。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from plugin_manager import probe, registry  # noqa: E402


def mk_probe(bucket, dirname, pid="", error="", api_version="1", size=1000,
             assets="static", depends=()):
    p = probe.Probe(bucket, dirname, os.path.join("F:/plugins", bucket, dirname))
    p.id = pid or dirname
    p.title = pid or dirname
    p.version = "1.0.0"
    p.api_version = api_version
    p.error = error
    p.size = size
    p.files = 3
    p.mtime = 1700000000
    p.assets = assets
    p.depends = tuple(depends)
    p.kind = "package"
    return p


def mk_runtime(pid, builtin=False, routes=(), views=(), api_version="1",
               depends=(), file="F:/x.py"):
    return {
        "id": pid, "name": pid, "version": "1.0.0", "apiVersion": api_version,
        "builtin": builtin, "depends": list(depends), "routes": list(routes),
        "views": list(views), "assets": True, "settings": [],
        "file": file, "hasTeardown": True,
    }


class RouteOwnership(unittest.TestCase):
    def test_owns_pattern(self):
        self.assertTrue(registry.owns_pattern("search", ["api", "search"]))
        self.assertTrue(registry.owns_pattern("search", ["api", "search", "x"]))
        self.assertTrue(registry.owns_pattern("search", ["api", "v1", "search"]))
        self.assertFalse(registry.owns_pattern("search", ["api", "searchx"]))
        self.assertFalse(registry.owns_pattern("search", ["api", "items"]))
        self.assertFalse(registry.owns_pattern("search", []))
        # 老路径（不带模块前缀）不该被误认
        self.assertFalse(registry.owns_pattern("reader", ["feed.xml"]))

    def test_owned_routes_by_method_owner(self):
        """handler 是绑定在本模块实例上的方法 —— 最准的那条判据。"""
        class Mod:
            id = "reader"

            def api_items(self, req):
                return None

            def api_item(self, req):
                return None

        mod = Mod()
        routes = [
            ("GET", ["api", "items"], mod.api_items),
            ("GET", ["api", "item", "{iid}"], mod.api_item),
            ("GET", ["api", "other"], lambda req: None),
        ]
        got = registry.owned_routes(mod, "reader", routes)
        self.assertEqual(len(got), 2)
        self.assertNotIn(("GET", ["api", "other"], routes[2][2]), got)

    def test_owned_routes_by_package(self):
        """插件内部再分包时，按模块名前缀认领。"""
        class Fake:
            id = "x"
            __module__ = "anime_rss_plugin_foo.providers"

        def handler(req):
            return None

        handler.__module__ = "anime_rss_plugin_foo.providers.acg"
        routes = [("GET", ["api", "weird"], handler)]
        got = registry.owned_routes(Fake(), "x", routes)
        self.assertEqual(len(got), 1)

    def test_builtin_by_module_name(self):
        class Fake:
            builtin = True
        Fake.__module__ = "anime_rss.modules.feeds"
        self.assertTrue(registry.is_builtin(Fake()))

        class Plug:
            builtin = True                       # 插件常忘记写 False
        Plug.__module__ = "anime_rss_plugin_arr_search.module"
        self.assertFalse(registry.is_builtin(Plug()))

        class Plain:
            builtin = False
        Plain.__module__ = "something_else"
        self.assertFalse(registry.is_builtin(Plain()))


class StateMachine(unittest.TestCase):
    def _one(self, rt=None, buckets=None, soft=()):
        entries = registry.merge(
            {rt["id"]: rt} if rt else {},
            buckets or {probe.BUCKET_ACTIVE: [], probe.BUCKET_DISABLED: [],
                        probe.BUCKET_REMOVED: []},
            soft_disabled=soft, api_version="1", plugins_root="F:/plugins")
        return entries[0] if entries else None

    def test_normal(self):
        e = self._one(mk_runtime("search"),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE,
                                                      "arr_search", "search")],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "normal")
        self.assertTrue(e["enabled"])
        self.assertTrue(e["canDisable"])
        self.assertFalse(e["canEnable"])
        self.assertEqual(e["warnings"], [])

    def test_stale(self):
        """目录正常但没加载：多半是刚放进去没重启。"""
        e = self._one(None, {probe.BUCKET_ACTIVE: [mk_probe(
            probe.BUCKET_ACTIVE, "newbie", "newbie")],
            probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "stale")
        self.assertFalse(e["enabled"])
        self.assertTrue(any("重启" in w for w in e["warnings"]))

    def test_broken(self):
        e = self._one(None, {probe.BUCKET_ACTIVE: [mk_probe(
            probe.BUCKET_ACTIVE, "baddie", "baddie", error="语法错误（第 3 行）")],
            probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "broken")
        self.assertEqual(e["warnings"][0], "语法错误（第 3 行）")

    def test_disabled(self):
        e = self._one(None, {probe.BUCKET_ACTIVE: [],
                             probe.BUCKET_DISABLED: [mk_probe(
                                 probe.BUCKET_DISABLED, "old", "old")],
                             probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "disabled")
        self.assertTrue(e["canEnable"])
        self.assertFalse(e["canDisable"])

    def test_removed(self):
        e = self._one(None, {probe.BUCKET_ACTIVE: [], probe.BUCKET_DISABLED: [],
                             probe.BUCKET_REMOVED: [mk_probe(
                                 probe.BUCKET_REMOVED, "gone@20260101-0000",
                                 "gone")]})
        self.assertEqual(e["state"], "removed")
        self.assertTrue(e["canRestore"])

    def test_partial_when_still_running(self):
        """目录进了 .disabled，进程里却还在跑 —— 必须说清楚，不能谎报已停用。"""
        e = self._one(mk_runtime("ghost"),
                      {probe.BUCKET_ACTIVE: [],
                       probe.BUCKET_DISABLED: [mk_probe(probe.BUCKET_DISABLED,
                                                        "ghost", "ghost")],
                       probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "partial")
        self.assertFalse(e["enabled"])
        self.assertTrue(any("重启后彻底消失" in w for w in e["warnings"]))

    def test_soft_disabled(self):
        """目录不可写：只在内存里挡住，重启会自动再挡一次。"""
        e = self._one(mk_runtime("stuck"),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE,
                                                      "stuck", "stuck")],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []},
                      soft=["stuck"])
        self.assertEqual(e["state"], "soft")
        self.assertEqual(e["stateText"], "本次停用")
        self.assertTrue(e["soft"])
        self.assertFalse(e["enabled"])
        self.assertTrue(e["canEnable"])

    def test_external(self):
        """已加载但磁盘上找不到（pip 入口点装的）。"""
        e = self._one(mk_runtime("ep_plugin", file="C:/py/site-packages/ep.py"))
        self.assertEqual(e["state"], "external")
        self.assertTrue(e["enabled"])
        self.assertFalse(e["canRemove"])

    def test_core(self):
        e = self._one(mk_runtime("feeds", builtin=True))
        self.assertEqual(e["state"], "core")
        self.assertFalse(e["canDisable"])
        self.assertFalse(e["canRemove"])

    # -------------------------------------------------- 体检项
    def test_contract_version_mismatch(self):
        e = self._one(mk_runtime("old", api_version="2"),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE, "old",
                                                      "old", api_version="2")],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertTrue(any("契约版本" in w for w in e["warnings"]))

    def test_missing_dep(self):
        e = self._one(mk_runtime("needy", depends=("nothere",)),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE, "needy",
                                                      "needy")],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertTrue(any("缺少依赖" in w and "nothere" in w for w in e["warnings"]))

    def test_dep_present_is_not_a_warning(self):
        rt = mk_runtime("needy", depends=("feeds",))
        buckets = {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE, "needy",
                                                  "needy")],
                   probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []}
        entries = registry.merge({"needy": rt, "feeds": mk_runtime("feeds", builtin=True)},
                                 buckets, api_version="1", plugins_root="F:/plugins")
        needy = [e for e in entries if e["id"] == "needy"][0]
        self.assertEqual([w for w in needy["warnings"] if "依赖" in w], [])

    def test_dirname_mismatch_is_not_an_error(self):
        """目录名和 id 不一致很常见（arr_search / search），只提示不报错。"""
        e = self._one(mk_runtime("search"),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE,
                                                      "arr_search", "search")],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertEqual(e["state"], "normal")
        self.assertEqual([w for w in e["warnings"] if "不一致" in w], [])

    def test_big_plugin_warns(self):
        e = self._one(mk_runtime("fat"),
                      {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE, "fat",
                                                      "fat", size=9 * 1024 * 1024)],
                       probe.BUCKET_DISABLED: [], probe.BUCKET_REMOVED: []})
        self.assertTrue(any("体积偏大" in w for w in e["warnings"]))

    # -------------------------------------------------- 排序与计数
    def test_order_and_counts(self):
        buckets = {
            probe.BUCKET_ACTIVE: [
                mk_probe(probe.BUCKET_ACTIVE, "ok", "ok"),
                mk_probe(probe.BUCKET_ACTIVE, "bad", "bad", error="坏了"),
            ],
            probe.BUCKET_DISABLED: [mk_probe(probe.BUCKET_DISABLED, "off", "off")],
            probe.BUCKET_REMOVED: [mk_probe(probe.BUCKET_REMOVED, "gone@1", "gone")],
        }
        entries = registry.merge({"ok": mk_runtime("ok"),
                                  "feeds": mk_runtime("feeds", builtin=True)},
                                 buckets, api_version="1", plugins_root="F:/plugins")
        states = [e["state"] for e in entries]
        self.assertEqual(states[0], "broken")        # 有问题的排最前
        self.assertEqual(states[-1], "core")         # 内置垫底
        self.assertIn("disabled", states)
        self.assertIn("removed", states)


class SnapshotShape(unittest.TestCase):
    """Snapshot 依赖 ctx，这里只测不碰 ctx 的那部分逻辑（trash / issues）。"""

    def test_trash_and_issues(self):
        from plugin_manager.registry import Snapshot
        snap = Snapshot.__new__(Snapshot)
        snap.root = "F:/plugins"
        snap.self_id = "pluginmgr"
        snap.entries = registry.merge(
            {}, {probe.BUCKET_ACTIVE: [mk_probe(probe.BUCKET_ACTIVE, "bad", "bad",
                                                error="坏了")],
                 probe.BUCKET_DISABLED: [mk_probe(probe.BUCKET_DISABLED, "off", "off")],
                 probe.BUCKET_REMOVED: [mk_probe(probe.BUCKET_REMOVED, "g@1", "g")]},
            api_version="1", plugins_root="F:/plugins")
        d = snap.as_dict()
        self.assertEqual(len(d["entries"]), 3)
        self.assertEqual(len(d["trash"]), 2)         # 停用 + 回收站
        self.assertTrue(d["issues"])
        self.assertEqual(d["issues"][0]["level"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
