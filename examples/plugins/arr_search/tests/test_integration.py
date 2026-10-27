# -*- coding: utf-8 -*-
"""集成测试：在真实主程序里加载插件，验证「模块 + 路由 + 组合 + 入库」。

需要主程序在场（在仓库根目录跑，且插件已放进 ``plugins/``）：

    python -m unittest discover -s plugins/arr_search/tests -v

不需要网络：两个假源代替真实站点，所以它测的是**编排**而不是站点。
真实站点的连通性交给 ``live_smoke.py``（那个会联网）。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_PARENT = os.path.dirname(ROOT)
REPO = os.path.dirname(PLUGIN_PARENT)

# 单测按目录发现时，sys.path 里没有插件目录与仓库根，这里补上两行
for _p in (PLUGIN_PARENT, REPO):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from anime_rss.app import create_app
    from arr_search.providers.base import Page, Provider
    from arr_search.model import make_item
    HAVE_HOST = True
except Exception:                                    # noqa: BLE001
    HAVE_HOST = False
    Provider = object                                 # 占位，让类定义能通过


class FakeA(Provider):
    id = "fake_a"
    name = "假源 A"
    caps = frozenset({"keyword", "paging"})
    page_size = 10
    max_pages = 5

    def search(self, q, page):
        if page > 2:
            return Page([], has_more=False)
        items = [
            make_item(self.id, "[组A] 鬼灭之刃 %02d [1080p]" % page,
                      "http://a/%d" % page, source_name=self.name,
                      infohash=("%02d" % page) * 20, size="1.5 GiB",
                      pubdate="2022-03-%02d" % (10 + page),
                      magnet="magnet:?xt=urn:btih:" + ("%02d" % page) * 20),
            make_item(self.id, "[组A] 鬼灭之刃 1080p 粤语版 %d" % page,
                      "http://a/cant/%d" % page, source_name=self.name,
                      size="900 MiB", pubdate="2022-03-05"),
        ]
        return Page(items, has_more=page < 2)


class FakeB(Provider):
    id = "fake_b"
    name = "假源 B"
    caps = frozenset({"keyword", "paging"})
    page_size = 10
    max_pages = 5

    def search(self, q, page):
        if page > 1:
            return Page([], has_more=False)
        # 与 A 的第一条同 infohash：应当被去重合并
        return Page([
            make_item(self.id, "[组A] 鬼灭之刃 01 [1080p]", "http://b/1",
                      source_name=self.name, infohash="01" * 20,
                      size="1.5 GiB", seeders=42, pubdate="2022-03-11"),
            make_item(self.id, "[组B] 别的番 01", "http://b/2",
                      source_name=self.name),
        ], has_more=False)


@unittest.skipUnless(HAVE_HOST, "主程序不在 sys.path 里（这个用例要在仓库根目录跑）")
class TestIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="arrsearch_")
        cls.app = create_app(data_dir=cls.tmp, base_dir=os.path.dirname(ROOT),
                             plugin_dirs=[PLUGIN_PARENT], fetch_on_start=False,
                             auto_refresh=False)
        # 用假源替换掉所有真实源，避免联网
        cls.app.ctx.extra.setdefault("search.providers", []).extend([FakeA, FakeB])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.app.stop()
        finally:
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        r = self.app.handle("GET", path)
        self.assertEqual(r.status, 200, r.body[:200])
        return json.loads(r.body)

    def post(self, path, body):
        r = self.app.handle("POST", path, body)
        self.assertEqual(r.status, 200, r.body[:200])
        return json.loads(r.body)

    # ------------------------------------------------------------ 模块本身
    def test_module_registered(self):
        from anime_rss.module import API_VERSION
        mod = self.app.ctx.modules.get("search")
        self.assertIsNotNone(mod)
        self.assertEqual(mod.api_version, API_VERSION)
        self.assertEqual(mod.id, "search")

    def test_view_and_assets(self):
        boot = self.get("/api/bootstrap")
        ids = [v["id"] for v in boot["views"]]
        self.assertIn("search", ids)
        js = self.app.handle("GET", "/plug/search/view.js")
        self.assertEqual(js.status, 200)
        self.assertIn(b"registerView", js.body)

    def test_settings_group(self):
        boot = self.get("/api/bootstrap")
        self.assertIn("searchSources", boot["settings"])
        self.assertIn("searchPages", boot["settings"])

    # ------------------------------------------------------------ 组合
    def test_providers_list(self):
        d = self.get("/api/search/providers")
        self.assertTrue(d["ok"])
        ids = [p["id"] for p in d["providers"]]
        self.assertIn("fake_a", ids)
        self.assertIn("local", ids)
        fake = [p for p in d["providers"] if p["id"] == "fake_a"][0]
        self.assertIn("paging", fake["caps"])

    def test_search_merge_dedupe_filter(self):
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a,fake_b"
                     "&pages=2&limit=20")
        self.assertTrue(d["ok"])
        titles = [i["title"] for i in d["items"]]
        # 同 infohash 的两条合并成一条，并且做了字段补齐（B 的 seeders 应被带过来）
        merged = [i for i in d["items"] if i["title"].endswith("01 [1080p]")]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["seeders"], 42)
        # 「别的番」不该出现
        self.assertFalse(any("别的番" in t for t in titles))
        self.assertTrue(d["explain"])

    def test_search_exclude(self):
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD%20-%E7%B2%A4%E8%AF%AD"
                     "&src=fake_a,fake_b&pages=1&limit=20")
        self.assertTrue(all("粤语" not in i["title"] for i in d["items"]), "排除没生效")
        self.assertGreater(d["count"], 0)

    def test_search_has_more_and_paging(self):
        d1 = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a&pages=1&page=1&limit=1")
        self.assertTrue(d1["hasMore"])
        d2 = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a&pages=1&page=2&limit=1")
        self.assertNotEqual(d1["items"][0]["title"], d2["items"][0]["title"])

    def test_empty_query_rejected(self):
        r = self.app.handle("GET", "/api/search")
        self.assertEqual(r.status, 400)

    def test_time_window(self):
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a,fake_b"
                     "&after=2022-03-11&limit=20")
        floor = int(time.mktime((2022, 3, 11, 0, 0, 0, 0, 0, -1)))
        self.assertTrue(d["items"])
        self.assertTrue(all(i["ts"] >= floor for i in d["items"]), "时间下界没生效")

    # ------------------------------------------------------------ 入库与组合收藏
    def test_keep_then_local_hit(self):
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a&limit=1")
        item = d["items"][0]
        k = self.post("/api/search/keep", {"items": [item]})
        self.assertTrue(k["ok"])
        self.assertGreaterEqual(k["added"], 1)
        self.assertEqual(k["starred"], 1)

        feed = self.app.store.feed("search_keep")
        self.assertIsNotNone(feed)
        self.assertFalse(feed["enabled"], "收藏频道不该参与自动抓取")

        loc = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=local&limit=5")
        self.assertGreaterEqual(loc["count"], 1)
        self.assertTrue(loc["items"][0]["local"])

    def test_presets_roundtrip(self):
        p = self.post("/api/search/presets", {
            "name": "集成测试组合", "query": "鬼灭 after:2022-01-01",
            "sources": ["fake_a"], "mode": "deep", "sort": "date"})
        self.assertTrue(p["ok"])
        pid = p["preset"]["id"]
        lst = self.get("/api/search/presets")
        self.assertTrue(any(x["id"] == pid for x in lst["presets"]))

        r = self.app.handle("DELETE", "/api/search/presets/%d" % pid)
        self.assertEqual(r.status, 200)
        self.assertTrue(json.loads(r.body)["ok"])

    # ------------------------------------------------------------ 接入关注
    def test_structured_params_over_http(self):
        """图形面板那套参数（不走语法）也要能筛。"""
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a"
                     "&must=1080p&pages=1&limit=10")
        self.assertGreaterEqual(d["count"], 1)
        self.assertEqual(d["query"]["must"], ["1080p"])
        self.assertTrue(all("1080p" in i["title"] for i in d["items"]))

        d2 = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a"
                      "&exclude=%E7%B2%A4%E8%AF%AD&pages=1&limit=10")
        self.assertTrue(all("粤语" not in i["title"] for i in d2["items"]), "排除词没生效")

    def test_watch_from_items(self):
        """单条结果 -> 关注：建关注、挂条目、给 feed。"""
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD%E4%B9%8B%E5%88%83"
                     "&src=fake_a&pages=1&limit=5")
        self.assertGreaterEqual(d["count"], 1)
        r = self.post("/api/search/watch", {
            "q": "鬼灭之刃", "src": "fake_a", "items": d["items"][:1]})
        self.assertTrue(r["ok"])
        self.assertTrue(r["created"])
        self.assertGreaterEqual(r["attached"], 1)
        self.assertEqual(r["watch"]["name"], "鬼灭之刃")
        self.assertEqual(r["feed"], "watch:" + r["watch"]["id"])
        self.assertGreaterEqual(r["watch"]["total"], 1)

        # 主程序的关注页确实能读到
        rows = self.app.store.query_items(feed=r["feed"])
        self.assertGreaterEqual(len(rows), 1)

    def test_watch_all_from_query(self):
        """整条查询 -> 关注：不给 items，让后端按条件重跑再挂。"""
        r = self.post("/api/search/watch", {"q": "鬼灭", "src": "fake_a",
                                            "all": True, "pages": 1})
        self.assertTrue(r["ok"])
        self.assertGreaterEqual(r["attached"], 2, "重跑检索没把命中挂上")

    def test_watch_idempotent(self):
        """同一套规则点两次不该长出两条关注。"""
        body = {"q": "鬼灭之刃", "src": "fake_a", "all": True, "pages": 1}
        a = self.post("/api/search/watch", body)
        b = self.post("/api/search/watch", body)
        self.assertEqual(a["watch"]["id"], b["watch"]["id"])
        self.assertFalse(b["created"])
        self.assertEqual(b["attached"], 0, "重复挂载不该再计入条数")

    def test_watch_status_lights_up(self):
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_a&pages=1&limit=5")
        item = d["items"][0]
        self.post("/api/search/watch", {"q": "鬼灭", "src": "fake_a", "items": [item]})
        s = self.post("/api/search/watch/status", {"items": d["items"]})
        self.assertIn(item["id"], s["watched"])

    def test_watch_keeps_out_of_updates(self):
        """挂进关注的老番不该涌进「全部更新」（link=False）。"""
        before = len(self.app.store.query_items(feed="all"))
        d = self.get("/api/search?q=%E9%AC%BC%E7%81%AD&src=fake_b&pages=1&limit=5")
        self.post("/api/search/watch", {"q": "鬼灭", "src": "fake_b",
                                        "items": d["items"]})
        after = len(self.app.store.query_items(feed="all"))
        self.assertEqual(before, after, "关注挂载污染了全部更新")

    def test_watches_listed(self):
        w = self.get("/api/search/watches")
        self.assertTrue(w["ok"])
        self.assertTrue(any(x["name"] == "鬼灭之刃" for x in w["watches"]))
        self.assertTrue(all("total" in x for x in w["watches"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
