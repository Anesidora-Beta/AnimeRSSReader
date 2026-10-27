# -*- coding: utf-8 -*-
"""集成测试：真的起一个应用，把插件装上、停掉、卸掉、再装回来。

**这一步不能用假对象糊过去**。停用如果只改了标志位、路由还在，单测照样全绿，
用户却会发现「界面说停了，接口还能打」。所以这里全程走 HTTP：

    app.handle("POST", "/api/pluginmgr/action", {...})   → 停用
    app.handle("GET",  "/api/mini/ping")                → 必须 404

需要主程序在场（找不到就整类 skip）：

    python -m unittest discover -s plugins/plugin_manager/tests -v
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _env  # noqa: E402

if _env.have_host():
    from anime_rss.app import create_app
else:                                             # pragma: no cover
    create_app = None


@unittest.skipUnless(_env.have_host(), "需要主程序在场（设置 ARR_REPO 或装进 plugins/）")
class PluginManagerApi(unittest.TestCase):
    def setUp(self):
        self.plugins = tempfile.mkdtemp(prefix="pm-plugins-")
        self.data = tempfile.mkdtemp(prefix="pm-data-")
        _env.copy_plugin(self.plugins)                     # 管理器自己
        _env.write_minimal_plugin(self.plugins, "mini_plug")
        self.app = create_app(data_dir=self.data, base_dir=_env.REPO,
                              plugin_dirs=[self.plugins], log=None)
        self.assertTrue(self.app.ctx.modules.get("pluginmgr"),
                        "插件管理器没加载起来，后面的测试没有意义")

    def tearDown(self):
        try:
            self.app.stop()
        finally:
            shutil.rmtree(self.plugins, ignore_errors=True)
            shutil.rmtree(self.data, ignore_errors=True)

    # -------------------------------------------------- 小工具
    def call(self, method, path, body=None, query=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        resp = self.app.handle(method, path, body=body or b"", query=query)
        try:
            return resp.status, json.loads(resp.body)
        except Exception:                              # noqa: BLE001 - zip 之类的二进制
            return resp.status, resp

    def act(self, action, ident, **extra):
        body = {"action": action, "id": ident}
        body.update(extra)
        return self.call("POST", "/api/pluginmgr/action", body)

    def snapshot(self):
        status, data = self.call("GET", "/api/pluginmgr")
        self.assertEqual(status, 200)
        return data

    def entry(self, ident):
        for e in self.snapshot()["entries"]:
            if e["id"] == ident:
                return e
        return None

    def module_ids(self):
        return list(self.app.ctx.modules.ids())

    def view_ids(self):
        return [v.id for v in self.app.views]

    def job_names(self):
        return sorted(j["name"] for j in self.app.jobs.status()["jobs"])

    # -------------------------------------------------- 列表
    def test_list_shape(self):
        data = self.snapshot()
        self.assertTrue(data["ok"])
        self.assertEqual(data["env"]["root"], self.plugins)
        self.assertTrue(data["env"]["apiVersion"])
        ids = [e["id"] for e in data["entries"]]
        self.assertIn("mini", ids)
        self.assertIn("pluginmgr", ids)
        self.assertIn("feeds", ids)                    # 内置也列出来（只读）

        mini = self.entry("mini")
        self.assertEqual(mini["state"], "normal")
        self.assertIn("GET /api/mini/ping", mini["routes"])
        self.assertIn("mini", mini["views"])
        self.assertTrue(mini["assets"])
        self.assertFalse(mini["builtin"])

        mgr = self.entry("pluginmgr")
        self.assertTrue(mgr["isSelf"])

        core = self.entry("feeds")
        self.assertTrue(core["builtin"])
        self.assertEqual(core["state"], "core")
        self.assertFalse(core["canDisable"])

    # -------------------------------------------------- 停用 / 启用
    def test_disable_really_stops(self):
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 200)

        status, res = self.act("disable", "mini")
        self.assertEqual(status, 200, res)
        self.assertNotIn("mini", self.module_ids())
        self.assertNotIn("mini", self.view_ids())
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 404)
        self.assertEqual(self.call("GET", "/plug/mini/view.js")[0], 404)
        self.assertNotIn("mini.tick", self.job_names())

        # 目录已经移进 .disabled，重启也不会加载它
        self.assertTrue(os.path.isdir(
            os.path.join(self.plugins, ".disabled", "mini_plug")))
        self.assertFalse(os.path.isdir(os.path.join(self.plugins, "mini_plug")))
        self.assertEqual(self.entry("mini")["state"], "disabled")

        # /api/bootstrap 的视图清单也不能再有它
        _, boot = self.call("GET", "/api/bootstrap")
        self.assertNotIn("mini", [v["id"] for v in boot.get("views") or []])

    def test_enable_brings_it_back(self):
        self.act("disable", "mini")
        status, res = self.act("enable", "mini")
        self.assertEqual(status, 200, res)
        self.assertIn("mini", self.module_ids())
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 200)
        self.assertEqual(self.call("GET", "/api/mini/echo?q=hi")[1]["q"], "hi")
        self.assertIn("mini", self.view_ids())
        self.assertIn("mini.tick", self.job_names())
        self.assertEqual(self.entry("mini")["state"], "normal")

    def test_disable_enable_twice_no_leak(self):
        """反复开关不能把路由/任务越堆越多。"""
        before_routes = len(self.app.router.routes)
        before_jobs = len(self.job_names())
        for _ in range(3):
            self.act("disable", "mini")
            self.act("enable", "mini")
        self.assertEqual(len(self.app.router.routes), before_routes)
        self.assertEqual(len(self.job_names()), before_jobs)
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 200)

    def test_soft_disable_when_dir_not_movable(self):
        """目录不可写时只能软停用 —— 状态要如实说「仅本次运行」。"""
        status, res = self.act("disable", "mini", move=False)
        self.assertEqual(status, 200, res)
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 404)
        self.assertTrue(os.path.isdir(os.path.join(self.plugins, "mini_plug")))

        e = self.entry("mini")
        self.assertEqual(e["state"], "soft")
        self.assertEqual(e["stateText"], "本次停用")
        self.assertTrue(e["soft"])
        self.assertTrue(e["canEnable"])
        self.assertTrue(any("不可写" in w for w in e["warnings"]))

        # 软停用要能在下次启动时自动再摘一次
        with open(self.app.ctx.modules.get("pluginmgr").state_path(),
                  encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertIn("mini", st["softDisabled"])

    # -------------------------------------------------- 重载
    def test_reload_picks_up_new_code(self):
        path = os.path.join(self.plugins, "mini_plug", "__init__.py")
        with open(path, encoding="utf-8") as fh:
            body = fh.read().replace('"pong": True', '"pong": "v2"')
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)

        status, res = self.act("reload", "mini")
        self.assertEqual(status, 200, res)
        self.assertEqual(self.call("GET", "/api/mini/ping")[1]["pong"], "v2")
        self.assertEqual(self.job_names().count("mini.tick"), 1)
        self.assertEqual(self.call("GET", "/api/v1/mini/ping")[0], 200)

    # -------------------------------------------------- 卸载 / 恢复 / 删除
    def test_remove_restore_purge(self):
        status, res = self.act("remove", "mini")
        self.assertEqual(status, 200, res)
        token = res["token"]
        self.assertIn("@", token)
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 404)
        self.assertEqual(self.entry("mini")["state"], "removed")

        status, res = self.act("restore", token)
        self.assertEqual(status, 200, res)
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 200)

        # 再卸一次，然后彻底删
        _, res = self.act("remove", "mini")
        token = res["token"]
        status, _ = self.act("purge", token)
        self.assertEqual(status, 200)
        self.assertFalse(os.path.isdir(
            os.path.join(self.plugins, ".removed", token)))
        self.assertIsNone(self.entry("mini"))

    def test_purge_rejects_traversal(self):
        status, res = self.act("purge", "../../mini_plug")
        self.assertEqual(status, 400)
        self.assertIn("不合法", res["error"])

    # -------------------------------------------------- 保护
    def test_builtin_is_protected(self):
        status, res = self.act("disable", "feeds")
        self.assertEqual(status, 400)
        self.assertIn("内置", res["error"])
        self.assertIn("feeds", self.module_ids())

    def test_self_is_protected(self):
        status, res = self.act("disable", "pluginmgr")
        self.assertEqual(status, 400)
        self.assertIn("自己", res["error"])
        self.assertIn("pluginmgr", self.module_ids())

    def test_unknown_action(self):
        status, res = self.call("POST", "/api/pluginmgr/action",
                                {"action": "explode", "id": "mini"})
        self.assertEqual(status, 400)
        self.assertIn("不认识的动作", res["error"])

    # -------------------------------------------------- 安装
    def _zip(self, files, top="pack_plug"):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, text in files.items():
                zf.writestr((top + "/" + name) if top else name, text)
        return buf.getvalue()

    def test_install_zip(self):
        data = self._zip({"__init__.py": _env.minimal_source("mini2", "二号"),
                          "static/view.js": _env.minimal_view("mini2", "二号")})
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "pack.zip"})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["id"], "mini2", res.get("steps"))
        self.assertTrue(os.path.isdir(os.path.join(self.plugins, "pack_plug")))
        self.assertIn("mini2", self.module_ids())
        self.assertEqual(self.call("GET", "/api/mini2/ping")[0], 200)

    def test_install_rejects_duplicate_without_overwrite(self):
        data = self._zip({"__init__.py": _env.MINIMAL_PLUGIN})
        self.call("POST", "/api/pluginmgr/install", data, query={"name": "p.zip"})
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "p.zip"})
        self.assertEqual(status, 400)
        self.assertIn("已存在", res["error"])

        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "p.zip", "overwrite": "1"})
        self.assertEqual(status, 200, res)
        self.assertTrue(os.path.isdir(os.path.join(self.plugins, ".removed")))
        self.assertEqual(self.call("GET", "/api/mini/ping")[0], 200)

    def test_install_rejects_traversal(self):
        data = self._zip({"../evil.py": "x = 1"})
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "bad.zip"})
        self.assertEqual(status, 400)
        self.assertIn("越界", res["error"])

    def test_install_rejects_symlink(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("evil/__init__.py")
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
        status, res = self.call("POST", "/api/pluginmgr/install", buf.getvalue(),
                                query={"name": "evil.zip"})
        self.assertEqual(status, 400)
        self.assertIn("符号链接", res["error"])

    def test_install_rejects_non_zip(self):
        status, res = self.call("POST", "/api/pluginmgr/install", b"hello",
                                query={"name": "x.zip"})
        self.assertEqual(status, 400)
        self.assertIn("有效的 zip", res["error"])

    def test_install_rejects_empty_pack(self):
        data = self._zip({"README.md": "# 说明"})
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "doc.zip"})
        self.assertEqual(status, 400)
        self.assertIn("__init__.py", res["error"])

    def test_install_flat_zip_uses_filename(self):
        """zip 里没有顶层目录时，用文件名当插件目录名。"""
        data = self._zip({"__init__.py": _env.MINIMAL_PLUGIN}, top="")
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "flatplug.zip"})
        self.assertEqual(status, 200, res)
        self.assertTrue(os.path.isdir(os.path.join(self.plugins, "flatplug")))

    def test_install_refuses_to_overwrite_itself(self):
        """装到插件管理器自己的目录上：落盘前就拒绝，别把运行中的自己弄没。"""
        data = self._zip({"__init__.py": _env.minimal_source("other", "别的")},
                         top="plugin_manager")
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "self.zip", "overwrite": "1"})
        self.assertEqual(status, 400)
        self.assertIn("自己", res["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.plugins, ".removed")))
        self.assertEqual(self.call("GET", "/api/pluginmgr")[0], 200)

    def test_install_refuses_duplicate_module_id(self):
        """包里的 id 与本插件重名：解包后、落盘前拒绝，临时目录要清掉。"""
        data = self._zip({"__init__.py": _env.minimal_source("pluginmgr", "冒名")})
        status, res = self.call("POST", "/api/pluginmgr/install", data,
                                query={"name": "fake.zip"})
        self.assertEqual(status, 400)
        self.assertIn("冲突", res["error"])
        self.assertFalse(os.path.isdir(os.path.join(self.plugins, "pack_plug")))
        left = [n for n in os.listdir(self.plugins) if n.startswith(".staging-")]
        self.assertEqual(left, [])

    def test_staging_dir_is_cleaned(self):
        self.test_install_rejects_traversal()
        left = [n for n in os.listdir(self.plugins) if n.startswith(".staging-")]
        self.assertEqual(left, [])

    # -------------------------------------------------- 导出 / 详情 / 文件
    def test_export_roundtrip(self):
        resp = self.app.handle("GET", "/api/pluginmgr/export",
                               query={"id": "mini"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.ctype, "application/zip")
        zf = zipfile.ZipFile(io.BytesIO(resp.body))
        names = zf.namelist()
        self.assertIn("mini_plug/__init__.py", names)
        self.assertIn("mini_plug/static/view.js", names)
        self.assertFalse([n for n in names if "__pycache__" in n])

    def test_detail_and_file(self):
        status, d = self.call("GET", "/api/pluginmgr/detail", query={"id": "mini"})
        self.assertEqual(status, 200)
        self.assertTrue(d["fileList"])
        self.assertEqual(d["files"], len(d["fileList"]))   # 数量与列表别打架
        self.assertIn("source", d)

        status, f = self.call("GET", "/api/pluginmgr/file",
                              query={"id": "mini", "path": "__init__.py"})
        self.assertEqual(status, 200)
        self.assertIn("MiniModule", f["text"])

        status, res = self.call("GET", "/api/pluginmgr/file",
                                query={"id": "mini", "path": "../../etc/passwd"})
        self.assertEqual(status, 400)

    def test_note_persists(self):
        status, _ = self.call("POST", "/api/pluginmgr/note",
                              {"id": "mini", "text": "等上游修 bug"})
        self.assertEqual(status, 200)
        self.assertEqual(self.snapshot()["notes"]["mini"], "等上游修 bug")
        self.assertEqual(self.snapshot()["notes"]["mini"], "等上游修 bug")

    def test_doctor_lists_issues_of_broken_plugin(self):
        bad = os.path.join(self.plugins, "broken_plug")
        os.makedirs(bad, exist_ok=True)
        with open(os.path.join(bad, "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write("class X(Module)\n  pass\n")
        data = self.snapshot()
        broken = [e for e in data["entries"] if e["dirname"] == "broken_plug"]
        self.assertTrue(broken)
        self.assertEqual(broken[0]["state"], "broken")
        self.assertTrue(broken[0]["warnings"])
        self.assertTrue(any(i["level"] == "error" for i in data["issues"]))

    # -------------------------------------------------- 顺序
    def test_view_static_served_while_enabled(self):
        status, resp = self.call("GET", "/plug/mini/view.js")
        self.assertEqual(status, 200)
        self.assertIn(b"registerView", resp.body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
