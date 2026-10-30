# -*- coding: utf-8 -*-
"""接口层测试：真起一个 ``create_app()``，走 HTTP 语义发请求。

这一层要盯住的是「接对了没有」：路由挂没挂上、过滤档位对不对、
唤醒护栏挡不挡得住、配置里的口令会不会漏出去。规则怎么判、进程怎么起，
在 ``test_rules`` / ``test_wake`` 里各自验过，这里不重复。

**测试绝不真的启动程序**：凡是会调 ``open`` / ``app`` 的用例，都先把自己的
假 ``Waker`` 换进去，记录「想打开什么」而不是真打开。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, PLUGIN)          # 让 rules / wake / clients 可导入
sys.path.insert(0, HERE)            # 让 _env 可导入（dotted 运行时也要）

import _env  # noqa: E402
import wake  # noqa: E402


def setUpModule():
    if not _env.have_host():
        raise unittest.SkipTest(
            "找不到主程序仓库（需要带 anime_rss/ 的目录）；设 ARR_REPO 指定")


# 三个条目，故意覆盖不同链接形态：
#   1) 磁力 + 一条详情页 —— 默认档（dl）应该只留磁力
#   2) 网盘 + 摘要里写提取码 —— 验自动补 pwd
#   3) 只有纯网页链接 —— 默认档要把它筛掉，all 档要留
ITEMS = [
    {"title": "[Sub] 某番 01 [1080p][简繁外挂]",
     "link": "https://bgm.tv/subject/1",
     "magnet": "magnet:?xt=urn:btih:aaa&dn=x",
     "size": "1.2 GB", "pubdate": "2026-10-01", "ts": 1759280000,
     "source": "t-feed", "summary": "字幕组作品"},
    {"title": "[Sub] 某番 02 [1080p]",
     "link": "https://bgm.tv/subject/2",
     "summary": "百度网盘：https://pan.baidu.com/s/1abc 提取码: k9x2",
     "size": "1.3 GB", "pubdate": "2026-10-02", "ts": 1759370000,
     "source": "t-feed"},
    {"title": "资源站公告",
     "link": "https://example.com/notice",
     "summary": "本周维护", "size": "", "pubdate": "2026-10-03",
     "ts": 1759460000, "source": "t-feed"},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lw-mod-")
        _env.seed_items(self.tmp, ITEMS)
        self.app = _env.make_app(self.tmp)
        self.mod = self._find()
        self.opened = []
        self.spawned = []
        self._real_waker = self.mod._waker
        # 插件包内的 wake 与顶层 wake 是两个模块对象（异常类也不同源），
        # 必须跟着实例拿它自己那一份，否则假 Waker 抛的错 module 捕不住。
        self.wake = sys.modules[type(self.mod._waker).__module__]
        # 换掉真启动器：记录意图，不碰系统
        self.mod._waker = self.wake.Waker(
            opener=lambda u, t=0: self.opened.append(u),
            spawner=lambda argv, **kw: self.spawned.append(list(argv)))

    def tearDown(self):
        self.mod._waker = self._real_waker
        try:
            self.app.stop()
        except Exception:                             # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _find(self):
        for m in self.app.modules:
            if getattr(m, "id", "") == "linkwake":
                return m
        self.skipTest("linkwake 没有被发现/加载（插件目录不对？）")

    def get(self, path, headers=None):
        return _env.call(self.app, "GET", path, None, headers)

    def post(self, path, body, headers=None):
        return _env.call(self.app, "POST", path, body, headers)


class Index(Base):
    def test_shape(self):
        st, d = self.get("/api/linkwake")
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertIsInstance(d["entries"], list)
        self.assertIn("counts", d)
        self.assertIn("scanned", d)
        self.assertGreaterEqual(d["scanned"], 1)
        self.assertTrue(d["providers"])
        self.assertTrue(d["modes"])
        self.assertTrue(d["env"]["canWake"])
        self.assertEqual(d["cfg"]["qb"]["passwordSet"], False)

    def test_default_kind_hides_pure_web(self):
        """默认「只看可下载的」：纯网页条目不该出现。"""
        st, d = self.get("/api/linkwake")
        self.assertEqual(st, 200)
        self.assertEqual(d["kind"], "dl")
        ids = [e["id"] for e in d["entries"]]
        self.assertEqual(len(ids), 2)
        for e in d["entries"]:
            self.assertTrue(e["links"])
            for ln in e["links"]:
                self.assertIn(ln["kind"], ("bt", "pan", "file"))

    def test_kind_all_keeps_web(self):
        st, d = self.get("/api/linkwake?kind=all")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["entries"]), 3)

    def test_kind_bt_only(self):
        st, d = self.get("/api/linkwake?kind=bt")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["entries"]), 1)
        self.assertEqual(d["entries"][0]["links"][0]["kind"], "bt")
        # 磁力排在组里第一位
        self.assertEqual(d["entries"][0]["groups"][0]["kind"], "bt")

    def test_kind_pan_and_pwd(self):
        st, d = self.get("/api/linkwake?kind=pan")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["entries"]), 1)
        ln = d["entries"][0]["links"][0]
        self.assertEqual(ln["pid"], "baidu")
        self.assertEqual(ln["pwd"], "k9x2")
        self.assertIn("pwd=k9x2", ln["url"])

    def test_search_hit_and_miss(self):
        st, d = self.get("/api/linkwake?kind=all&q=%E5%85%AC%E5%91%8A")
        self.assertEqual(st, 200)
        self.assertEqual(len(d["entries"]), 1)
        self.assertIn("公告", d["entries"][0]["title"])

        st, d = self.get("/api/linkwake?kind=all&q=zzz-nothing")
        self.assertEqual(st, 200)
        self.assertEqual(d["entries"], [])
        self.assertEqual(d["counts"]["items"], 0)

    def test_counts_match_entries(self):
        st, d = self.get("/api/linkwake?kind=all")
        self.assertEqual(st, 200)
        n = sum(e["count"] for e in d["entries"])
        self.assertEqual(d["counts"]["links"], n)
        self.assertEqual(d["counts"]["items"], len(d["entries"]))

    def test_page_size_capped(self):
        st, d = self.get("/api/linkwake?limit=9999")
        self.assertEqual(st, 200)
        self.assertLessEqual(len(d["entries"]), 200)

    def test_remote_cannot_wake(self):
        st, d = self.get("/api/linkwake", dict(_env.REMOTE_HOST))
        self.assertEqual(st, 200)
        self.assertFalse(d["env"]["canWake"])

    def test_no_store_still_answers(self):
        """主库没就绪时也要回 200 空列表，而不是 500。"""
        real = self.app.store
        self.mod.ctx.store = None
        try:
            st, d = self.get("/api/linkwake")
        finally:
            self.mod.ctx.store = real
        self.assertEqual(st, 200)
        self.assertEqual(d["entries"], [])


class Item(Base):
    def test_detail_has_page_link(self):
        st, d = self.get("/api/linkwake?kind=all")
        iid = d["entries"][0]["id"]
        st, d = self.get("/api/linkwake/item/%s" % iid)
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        e = d["entry"]
        self.assertIsNotNone(e)
        self.assertTrue(e["page"])
        urls = [ln["url"] for ln in e["links"]]
        self.assertIn(e["page"], urls)

    def test_missing_item(self):
        st, d = self.get("/api/linkwake/item/no-such-id")
        self.assertEqual(st, 404)
        self.assertFalse(d["ok"])


class Wake(Base):
    def test_copy_does_not_launch(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "mode": "copy"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "copy")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.spawned, [])

    def test_magnet_goes_to_system(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "pid": "magnet"})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.opened, ["magnet:?xt=urn:btih:aaa"])

    def test_pan_defaults_to_web(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "https://pan.baidu.com/s/1abc?pwd=k9x2",
                           "pid": "baidu"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "web")

    def test_dangerous_scheme_rejected(self):
        for bad in ("javascript:alert(1)", "file:///C:/Windows/win.ini",
                    r"\\evil\share\x.exe", "", "data:text/html,<script>"):
            with self.subTest(url=bad):
                st, d = self.post("/api/linkwake/wake",
                                  {"url": bad, "mode": "open"})
                self.assertEqual(st, 400)
                self.assertFalse(d["ok"])
        self.assertEqual(self.opened, [])

    def test_remote_blocked(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa"},
                          dict(_env.REMOTE_HOST))
        self.assertEqual(st, 403)
        self.assertIn("本机", d["error"])
        self.assertEqual(self.opened, [])

    def test_remote_allowed_when_switched_on(self):
        self.mod._opt = dict(self.mod._opt, allowRemote=True)
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa"},
                          dict(_env.REMOTE_HOST))
        self.assertEqual(st, 200)
        self.assertEqual(len(self.opened), 1)

    def test_unknown_mode(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "mode": "format-c"})
        self.assertEqual(st, 400)
        self.assertIn("唤醒方式", d["error"])

    def test_app_mode_missing_exe(self):
        self.mod._cfg["apps"]["magnet"] = {"mode": "app", "exe": "",
                                           "args": "{url}"}
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "pid": "magnet"})
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])

    def test_app_mode_spawns(self):
        self.mod._cfg["apps"]["magnet"] = {
            "mode": "app", "exe": sys.executable, "args": '--x "{url}"'}
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "pid": "magnet"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "app")
        self.assertEqual(self.spawned[0][-1], "magnet:?xt=urn:btih:aaa")


class Prepare(Base):
    """``/prepare`` 是给别的插件用的：只递一个 URL，就得回答完整。"""

    def test_magnet_shape(self):
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa", "title": "某番 01"})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(d["pid"], "magnet")
        self.assertEqual(d["kind"], "bt")
        self.assertEqual(d["title"], "某番 01")
        self.assertFalse(d["ask"])
        self.assertTrue(d["canWake"])
        ids = [t["id"] for t in d["targets"]]
        for want in ("auto", "qb", "aria2", "open", "web", "copy", "ask"):
            self.assertIn(want, ids)

    def test_pan_targets_exclude_torrent_clients(self):
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "https://pan.baidu.com/s/1abc"})
        self.assertEqual(st, 200)
        self.assertEqual(d["pid"], "baidu")
        self.assertEqual(d["kind"], "pan")
        ids = [t["id"] for t in d["targets"]]
        self.assertNotIn("qb", ids)
        self.assertNotIn("aria2", ids)
        self.assertIn("web", ids)

    def test_unconfigured_client_is_marked_not_ready(self):
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa"})
        by = {t["id"]: t for t in d["targets"]}
        self.assertFalse(by["qb"]["ready"])
        self.assertIn("qB", by["qb"]["note"])
        self.assertTrue(by["copy"]["ready"])
        self.assertTrue(by["auto"]["ready"])

    def test_ask_pref_turns_on_chooser(self):
        self.mod._cfg["prefs"]["bt"] = "ask"
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa"})
        self.assertEqual(st, 200)
        self.assertTrue(d["ask"])
        self.assertEqual(d["pref"], "ask")

    def test_explicit_ask_flag(self):
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa", "ask": True})
        self.assertTrue(d["ask"])

    def test_pref_decides_default(self):
        self.mod._cfg["prefs"]["bt"] = "aria2"
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa"})
        self.assertEqual(d["default"], "aria2")
        self.assertFalse(d["ask"])

    def test_bad_pref_falls_back_to_auto(self):
        self.mod._cfg["prefs"]["bt"] = "qb"       # 合法，但接着被弄坏
        self.mod._cfg["prefs"]["pan"] = "qb"      # pan 不该接受 qb
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "https://pan.baidu.com/s/1abc"})
        self.assertEqual(d["default"], "auto")

    def test_empty_url(self):
        st, d = self.post("/api/linkwake/prepare", {"url": "  "})
        self.assertEqual(st, 400)

    def test_remote_cannot_wake(self):
        st, d = self.post("/api/linkwake/prepare",
                          {"url": "magnet:?xt=urn:btih:aaa"},
                          dict(_env.REMOTE_HOST))
        self.assertEqual(st, 200)
        self.assertFalse(d["canWake"])
        self.assertTrue(d["reason"])


class WakeByTarget(Base):
    """弹层里点的那一项，直接以 target 落库执行。"""

    def test_target_copy(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa",
                           "target": "copy"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "copy")
        self.assertEqual(self.opened, [])

    def test_target_open(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa",
                           "target": "open"})
        self.assertEqual(st, 200)
        self.assertEqual(self.opened, ["magnet:?xt=urn:btih:aaa"])

    def test_target_web(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "https://pan.baidu.com/s/1abc?pwd=k9x2",
                           "target": "web"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "web")
        self.assertEqual(self.opened, ["https://pan.baidu.com/s/1abc?pwd=k9x2"])

    def test_target_qb_without_url_explains(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa", "target": "qb"})
        self.assertEqual(st, 400)
        self.assertIn("地址", d["error"])
        self.assertEqual(self.opened, [])

    def test_target_unset_uses_pref(self):
        self.mod._cfg["prefs"]["bt"] = "copy"
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa"})
        self.assertEqual(st, 200)
        self.assertEqual(d["action"], "copy")

    def test_target_unknown_falls_back_to_auto(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "magnet:?xt=urn:btih:aaa",
                           "target": "format-c"})
        self.assertEqual(st, 200)
        self.assertEqual(self.opened, ["magnet:?xt=urn:btih:aaa"])

    def test_dangerous_url_still_guarded(self):
        st, d = self.post("/api/linkwake/wake",
                          {"url": "file:///C:/Windows/win.ini",
                           "target": "open"})
        self.assertEqual(st, 400)
        self.assertEqual(self.opened, [])


class Batch(Base):
    MAG = ["magnet:?xt=urn:btih:a", "magnet:?xt=urn:btih:b"]

    def test_copy_returns_text(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "copy", "urls": self.MAG})
        self.assertEqual(st, 200)
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["text"].split("\n"), self.MAG)
        self.assertEqual(self.opened, [])

    def test_copy_works_from_remote(self):
        """复制不启动任何东西，远端也该能用。"""
        st, d = self.post("/api/linkwake/batch",
                          {"action": "copy", "urls": self.MAG},
                          dict(_env.REMOTE_HOST))
        self.assertEqual(st, 200)
        self.assertEqual(d["count"], 2)

    def test_open_launches_each(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "open", "urls": self.MAG})
        self.assertEqual(st, 200)
        self.assertEqual(d["count"], 2)
        self.assertEqual(self.opened, self.MAG)

    def test_open_skips_bad_and_reports(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "open",
                           "urls": ["magnet:?xt=urn:btih:a",
                                    "javascript:alert(1)"]})
        self.assertEqual(st, 200)
        self.assertEqual(d["count"], 1)
        self.assertEqual(len(d["failed"]), 1)
        self.assertEqual(self.opened, ["magnet:?xt=urn:btih:a"])

    def test_open_blocked_from_remote(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "open", "urls": self.MAG},
                          dict(_env.REMOTE_HOST))
        self.assertEqual(st, 403)
        self.assertEqual(self.opened, [])

    def test_empty(self):
        st, d = self.post("/api/linkwake/batch", {"action": "copy", "urls": []})
        self.assertEqual(st, 400)
        self.assertIn("没有", d["error"])

    def test_unknown_action(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "rm -rf", "urls": self.MAG})
        self.assertEqual(st, 400)

    def test_too_many(self):
        st, d = self.post("/api/linkwake/batch",
                          {"action": "copy",
                           "urls": ["magnet:?xt=urn:btih:%d" % i
                                    for i in range(201)]})
        self.assertEqual(st, 400)
        self.assertIn("最多", d["error"])

    def test_qb_without_config(self):
        """没配 qB 地址时给可读的报错，而不是抛异常。"""
        st, d = self.post("/api/linkwake/batch",
                          {"action": "qb", "urls": self.MAG})
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])
        self.assertTrue(d["error"])


class Apps(Base):
    def test_get_hides_secrets(self):
        st, d = self.post("/api/linkwake/apps",
                          {"qb": {"url": "http://127.0.0.1:8080",
                                  "user": "admin", "password": "s3cret",
                                  "savepath": "/dl"},
                           "aria2": {"url": "http://127.0.0.1:6800/jsonrpc",
                                     "token": "tk", "dir": "/dl"}})
        self.assertEqual(st, 200)
        st, d = self.get("/api/linkwake/apps")
        self.assertEqual(st, 200)
        cfg = d["cfg"]
        self.assertTrue(cfg["qb"]["passwordSet"])
        self.assertTrue(cfg["aria2"]["tokenSet"])
        self.assertNotIn("password", cfg["qb"])
        self.assertNotIn("token", cfg["aria2"])
        # 整份响应里不该出现明文
        blob = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("s3cret", blob)
        self.assertNotIn('"tk"', blob)

    def test_blank_password_keeps_old(self):
        self.post("/api/linkwake/apps",
                  {"qb": {"url": "http://127.0.0.1:8080", "password": "old-one"}})
        # 模拟前端：拿到的是空密码，直接原样提交
        self.post("/api/linkwake/apps",
                  {"qb": {"url": "http://127.0.0.1:9090", "password": ""}})
        path = os.path.join(self.tmp, "data", "linkwake.json")
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["qb"]["password"], "old-one")
        self.assertEqual(saved["qb"]["url"], "http://127.0.0.1:9090")

    def test_save_per_provider_mode(self):
        st, d = self.post("/api/linkwake/apps",
                          {"apps": {"baidu": {"mode": "scheme",
                                              "scheme": "baiduyun"}}})
        self.assertEqual(st, 200)
        tgt = self.mod._cfg["apps"]["baidu"]
        self.assertEqual(tgt["mode"], "scheme")
        self.assertEqual(tgt["scheme"], "baiduyun")

    def test_bad_mode_falls_back(self):
        self.post("/api/linkwake/apps", {"apps": {"magnet": {"mode": "shutdown"}}})
        self.assertIn(self.mod._cfg["apps"]["magnet"]["mode"],
                      ("open", "web", "copy", "scheme", "app"))

    def test_unknown_provider_ignored(self):
        st, d = self.post("/api/linkwake/apps",
                          {"apps": {"not-a-real-one": {"mode": "copy"}}})
        self.assertEqual(st, 200)
        self.assertNotIn("not-a-real-one", self.mod._cfg["apps"])

    def test_save_prefs(self):
        st, d = self.post("/api/linkwake/apps",
                          {"prefs": {"bt": "qb", "pan": "ask"}})
        self.assertEqual(st, 200)
        self.assertEqual(self.mod._cfg["prefs"]["bt"], "qb")
        self.assertEqual(self.mod._cfg["prefs"]["pan"], "ask")
        self.assertEqual(d["cfg"]["prefs"]["bt"], "qb")

    def test_prefs_reject_impossible_pairing(self):
        """网盘不可能交给 qB —— 写进去也不该生效。"""
        self.post("/api/linkwake/apps", {"prefs": {"pan": "qb"}})
        self.assertEqual(self.mod._cfg["prefs"]["pan"], "auto")

    def test_prefs_reject_unknown(self):
        self.post("/api/linkwake/apps",
                  {"prefs": {"bt": "shutdown", "file": "qb"}})
        self.assertEqual(self.mod._cfg["prefs"]["bt"], "auto")
        self.assertEqual(self.mod._cfg["prefs"]["file"], "qb")

    def test_prefs_survive_reload(self):
        self.post("/api/linkwake/apps", {"prefs": {"bt": "copy"}})
        cfg = self.mod._load()
        self.assertEqual(cfg["prefs"]["bt"], "copy")
        # 其它三种保持默认，不会被一起写坏
        self.assertEqual(cfg["prefs"]["pan"], "auto")

    def test_corrupt_file_still_boots(self):
        """配置文件是半个 JSON 时，插件要能用默认值起来。"""
        path = os.path.join(self.tmp, "data", "linkwake.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not json")
        cfg = self.mod._load()
        self.assertIn("apps", cfg)
        self.assertTrue(cfg["apps"])

    def test_no_temp_left_behind(self):
        self.post("/api/linkwake/apps", {"qb": {"url": "http://x"}})
        left = [f for f in os.listdir(os.path.join(self.tmp, "data"))
                if f.endswith(".tmp")]
        self.assertEqual(left, [])


class Probe(Base):
    def test_needs_target(self):
        st, d = self.post("/api/linkwake/probe", {})
        self.assertEqual(st, 400)
        self.assertIn("which", d["error"])

    def test_qb_bad_url(self):
        self.post("/api/linkwake/apps", {"qb": {"url": "ftp://x/y"}})
        st, d = self.post("/api/linkwake/probe", {"which": "qb"})
        self.assertEqual(st, 400)

    def test_qb_nothing_listening(self):
        self.post("/api/linkwake/apps",
                  {"qb": {"url": "http://127.0.0.1:1", "password": "x"}})
        st, d = self.post("/api/linkwake/probe", {"which": "qb"})
        self.assertEqual(st, 400)
        self.assertIn("连不上", d["error"])

    def test_aria2_nothing_listening(self):
        self.post("/api/linkwake/apps",
                  {"aria2": {"url": "http://127.0.0.1:1/jsonrpc"}})
        st, d = self.post("/api/linkwake/probe", {"which": "aria2"})
        self.assertEqual(st, 400)


class Settings(Base):
    def test_group_defined(self):
        schema = self.mod.ctx.settings.schema()
        grp = next((g for g in schema if g["group"] == "linkwake"), None)
        self.assertIsNotNone(grp, "设置里没有 linkwake 分组")
        keys = [i["k"] for i in grp["items"]]
        for k in ("lwAllowRemote", "lwAutoPwd", "lwOnlyDownloadable",
                  "lwPageSize"):
            self.assertIn(k, keys)
        # 登记之后立刻要有默认值，否则设置页渲染成 undefined
        vals = self.mod.ctx.settings.get()
        self.assertEqual(vals["lwPageSize"], 40)
        self.assertIs(vals["lwAutoPwd"], True)

    def test_switch_changes_behaviour(self):
        self.mod.apply_settings({"lwOnlyDownloadable": False})
        st, d = self.get("/api/linkwake")
        self.assertEqual(st, 200)
        self.assertEqual(d["kind"], "all")
        self.assertEqual(len(d["entries"]), 3)

    def test_page_size_clamped(self):
        self.mod.apply_settings({"lwPageSize": 9999})
        self.assertEqual(self.mod._opt["pageSize"], 200)
        self.mod.apply_settings({"lwPageSize": "x"})
        self.assertEqual(self.mod._opt["pageSize"], 40)

    def test_auto_pwd_switch(self):
        self.mod.apply_settings({"lwAutoPwd": False})
        st, d = self.get("/api/linkwake?kind=pan")
        self.assertEqual(st, 200)
        self.assertEqual(d["entries"][0]["links"][0]["pwd"], "")


class Static(unittest.TestCase):
    """静态资源要真的能取到，否则界面一片空白。"""

    def test_files_exist(self):
        for name in ("view.js", "view.css"):
            p = os.path.join(PLUGIN, "static", name)
            self.assertTrue(os.path.isfile(p), p)
            self.assertGreater(os.path.getsize(p), 500, p)


if __name__ == "__main__":
    unittest.main()
