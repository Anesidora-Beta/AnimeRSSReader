# -*- coding: utf-8 -*-
"""离线单测：不联网、不要主程序，直接跑。

    python -m unittest discover -s plugins/arr_search/tests -v
    # 或者在插件目录里： python -m unittest test_search -v

覆盖四层里最容易被改坏的三层：查询语法 / 下推与兜底 / 各源解析。
第四层（模块与路由）在 ``test_integration.py`` 里，需要主程序在场。
"""
import json
import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(ROOT))          # 让 `import arr_search` 成立

from arr_search import model as M                    # noqa: E402
from arr_search import providers as P                 # noqa: E402
from arr_search.providers.acgrip import AcgRip        # noqa: E402
from arr_search.providers.animegarden import AnimeGarden   # noqa: E402
from arr_search.providers.bangumi import Bangumi      # noqa: E402
from arr_search.providers.dmhy import Dmhy            # noqa: E402
from arr_search.providers.mikan import Mikan          # noqa: E402
from arr_search.providers.nyaa import Nyaa            # noqa: E402
from arr_search.query import Query                    # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


class TestModel(unittest.TestCase):
    def test_to_ts_shapes(self):
        # RFC822 带时区要按 +0800 折算成 UTC
        self.assertEqual(
            M.to_ts("Thu, 06 Aug 2026 03:29:02 +0800"),
            int(datetime(2026, 8, 5, 19, 29, 2, tzinfo=timezone.utc).timestamp()))
        self.assertEqual(
            M.to_ts("2026-08-06T03:33:55Z"),
            int(datetime(2026, 8, 6, 3, 33, 55, tzinfo=timezone.utc).timestamp()))
        for raw in ("2026-08-06T03:33:55.313604", "2026-08-06 03:33"):
            self.assertGreater(M.to_ts(raw), 1600000000, raw)
        for raw in ("", None, "看不懂"):
            self.assertEqual(M.to_ts(raw), 0, raw)

    def test_to_ts_milliseconds(self):
        self.assertEqual(M.to_ts(1754441342000), 1754441342)

    def test_size(self):
        self.assertEqual(M.size_bytes("1.4 GiB"), 1503238553)
        self.assertEqual(M.size_bytes("1G"), 1 << 30)
        self.assertEqual(M.size_bytes("13099650048"), 13099650048)
        self.assertEqual(M.size_bytes("1080p"), 0)
        self.assertEqual(M.size_bytes(""), 0)
        self.assertEqual(M.human_size(1 << 30), "1.0 GiB")

    def test_make_item_fills_derived(self):
        it = M.make_item("dmhy", "标题", "http://x", size="1.4 GiB",
                         pubdate="Mon, 03 Jan 2022 10:00:00 +0800",
                         magnet="magnet:?xt=urn:btih:" + "a" * 40)
        self.assertEqual(it["sizeBytes"], 1503238553)
        self.assertGreater(it["ts"], 1641000000)
        self.assertTrue(it["id"].startswith("h:"))
        self.assertEqual(it["sourceName"], "dmhy")

    def test_dedupe_merges_and_keeps_best(self):
        a = M.make_item("dmhy", "[组A] 鬼灭 01", "http://a", infohash="b" * 40)
        b = M.make_item("nyaa", "[组A] 鬼灭 01", "http://b", infohash="b" * 40,
                        size="1.2 GiB", seeders=8)
        c = M.make_item("acgrip", "[组A] 鬼灭 01", "http://c")   # 只有标题相同
        out = M.dedupe([a, b, c])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["size"], "1.2 GiB")
        self.assertEqual(out[0]["seeders"], 8)
        self.assertIn("dmhy", out[0]["alsoIn"])

    def test_sort(self):
        items = [
            M.make_item("a", "x", size="2 GiB", ts=100),
            M.make_item("a", "y", size="1 GiB", ts=300),
            M.make_item("a", "z", size="3 GiB", ts=200, seeders=50),
        ]
        self.assertEqual([i["title"] for i in M.sort_items(items, "date")], ["y", "z", "x"])
        self.assertEqual([i["title"] for i in M.sort_items(items, "size")], ["z", "x", "y"])
        self.assertEqual(M.sort_items(items, "seeders")[0]["title"], "z")


class TestQuerySyntax(unittest.TestCase):
    def test_operators(self):
        q = Query.parse("鬼灭 无限城 -粤语 +1080p")
        self.assertEqual(q.any_terms, ["鬼灭", "无限城"])
        self.assertEqual(q.must, ["1080p"])
        self.assertEqual(q.exclude, ["粤语"])

    def test_fields(self):
        q = Query.parse("字幕组:樱都 类型:动画 src:dmhy,nyaa sort:seeders pages:8")
        self.assertEqual(q.fansub, "樱都")
        self.assertEqual(q.category, "动画")
        self.assertEqual(q.src, ["dmhy", "nyaa"])
        self.assertEqual(q.sort, "seeders")
        self.assertEqual(q.pages, 8)

    def test_quote_keeps_space(self):
        q = Query.parse('"鬼灭 无限城" 1080p')
        self.assertEqual(q.any_terms, ["鬼灭 无限城", "1080p"])

    def test_size_and_dates(self):
        q = Query.parse("size>1G after:2022-01-01 before:2023-06-30")
        self.assertEqual(q.size_min, 1 << 30)
        self.assertEqual(q.after, "2022-01-01")
        self.assertEqual(q.before, "2023-06-30")
        self.assertGreater(q._after_ts, q._before_ts - 10 ** 9)
        self.assertFalse(q.empty)

    def test_relative_date(self):
        q = Query.parse("after:3y")
        self.assertRegex(q.after, r"^\d{4}-\d{2}-\d{2}$")

    def test_mode_and_params(self):
        q = Query.parse("鬼灭", mode="fast", limit=999)
        self.assertEqual(q.pages, 1)
        self.assertEqual(q.limit, 300)          # 夹到上限

    def test_unknown_field_is_kept_as_term(self):
        q = Query.parse("foo:bar 鬼灭")
        self.assertEqual(q.unknown, ["foo"])
        self.assertIn("foo:bar", q.any_terms)

    def test_empty(self):
        self.assertTrue(Query.parse("").empty)
        self.assertFalse(Query.parse("after:2020-01-01").empty)


class TestPushdown(unittest.TestCase):
    def test_keyword_keeps_must_and_any(self):
        # 必须词和任选词都要下推：只推 +1080p 会把站点整片「1080p 资源」捞回来，
        # 本地再按番名筛掉 —— 白抓一堆，一条都对不上（这个 bug 真踩过）。
        q = Query.parse("鬼灭 无限城 +1080p")
        self.assertEqual(q.server_keyword(), "1080p 无限城")

    def test_keyword_longest_any(self):
        q = Query.parse("鬼灭 1080p 无限城篇第一章")
        self.assertEqual(q.server_keyword(), "无限城篇第一章")

    def test_serverside_by_caps(self):
        q = Query.parse("鬼灭 after:2022-01-01 字幕组:樱都")
        self.assertEqual(q.serverside(frozenset({"keyword"})), {"keyword": "鬼灭"})
        got = q.serverside(frozenset({"keyword", "date", "fansub"}))
        self.assertEqual(got["after"], "2022-01-01")
        self.assertEqual(got["fansub"], "樱都")

    def test_explain_mentions_local_filter(self):
        q = Query.parse("鬼灭 -粤语")
        ex = q.explain(frozenset({"keyword"}))
        self.assertIn("pushed", ex)
        self.assertTrue(any("排除" in s for s in ex["local"]))


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.item = M.make_item(
            "dmhy", "[樱都字幕组][鬼灭之刃][01][1080p]", "http://x",
            size="1.4 GiB", pubdate="Mon, 03 Jan 2022 10:00:00 +0800",
            author="樱都", category="動畫")

    def test_or_any(self):
        self.assertTrue(Query.parse("鬼灭").match(self.item))
        self.assertTrue(Query.parse("海贼 鬼灭").match(self.item))
        self.assertFalse(Query.parse("海贼").match(self.item))

    def test_and_not(self):
        self.assertTrue(Query.parse("+鬼灭 +1080p").match(self.item))
        self.assertFalse(Query.parse("+鬼灭 +2160p").match(self.item))
        self.assertFalse(Query.parse("鬼灭 -1080p").match(self.item))

    def test_fields(self):
        self.assertTrue(Query.parse("字幕组:樱都").match(self.item))
        self.assertFalse(Query.parse("字幕组:喵萌").match(self.item))
        self.assertTrue(Query.parse("类型:動畫").match(self.item))

    def test_size_window(self):
        self.assertTrue(Query.parse("size>1G").match(self.item))
        self.assertFalse(Query.parse("size<1G").match(self.item))

    def test_time_window(self):
        self.assertFalse(Query.parse("鬼灭", after="2023-01-01").match(self.item))
        self.assertTrue(Query.parse("鬼灭", after="2021-01-01").match(self.item))
        self.assertFalse(Query.parse("鬼灭", before="2021-12-31").match(self.item))

    def test_unknown_date_dropped_only_when_window(self):
        it = M.make_item("nyaa", "鬼灭 01", "http://y")
        self.assertTrue(Query.parse("鬼灭").match(it))
        self.assertFalse(Query.parse("鬼灭", after="2021-01-01").match(it))


class TestStructuredParams(unittest.TestCase):
    """图形面板走的那套结构化参数，和语法必须落在同一个 Query 上。"""

    def test_params_apply(self):
        q = Query.parse("鬼灭", must="1080p", exclude="粤语",
                        fansub="樱都", size_min="1G")
        self.assertEqual(q.must, ["1080p"])
        self.assertEqual(q.exclude, ["粤语"])
        self.assertEqual(q.fansub, "樱都")
        self.assertEqual(q.size_min, 1 << 30)

    def test_params_merge_with_syntax(self):
        # 搜索框里写 +简日、面板里填 1080p，两条都该生效（合并而非覆盖）
        q = Query.parse("鬼灭 +简日", must="1080p")
        self.assertEqual(q.must, ["简日", "1080p"])

    def test_params_dedup(self):
        q = Query.parse("+1080p", must="1080p")
        self.assertEqual(q.must, ["1080p"])

    def test_params_affect_match(self):
        hit = M.make_item("dmhy", "[樱都][鬼灭之刃][01][1080p][简日]", "http://a/1")
        miss = M.make_item("dmhy", "[樱都][鬼灭之刃][01][720p][粤语]", "http://a/2")
        q = Query.parse("鬼灭", must="1080p", exclude="粤语")
        self.assertTrue(q.match(hit))
        self.assertFalse(q.match(miss))

    def test_size_drops_unknown(self):
        it = M.make_item("dmhy", "鬼灭 01", "http://a")          # 没有体积信息
        self.assertFalse(Query.parse("鬼灭", size_min="1G").match(it))

    def test_signature_separates_filters(self):
        # 换筛选条件必须换缓存键，否则用户看到「改了没反应」
        base = Query.parse("鬼灭").signature(["dmhy"])
        self.assertNotEqual(base, Query.parse("鬼灭", must="1080p").signature(["dmhy"]))
        self.assertNotEqual(base, Query.parse("鬼灭", exclude="粤语").signature(["dmhy"]))
        self.assertNotEqual(base, Query.parse("+1080p").signature(["dmhy"]))


class TestWatchMapping(unittest.TestCase):
    """Query -> 主程序 watches 表四个字段的映射（关注列表只认这四个）。"""

    def test_must_and_any_both_kept(self):
        f, _ = Query.parse("鬼灭 +简日").to_watch()
        self.assertEqual(f["include"], "简日 鬼灭")
        # 名字带上番名之外的词，免得关注列表出现两条同名不同规则
        self.assertEqual(f["name"], "鬼灭（简日）")

    def test_or_terms_degrade_to_first(self):
        f, notes = Query.parse("鬼灭 无限城 1080p").to_watch()
        # 关注只能长期盯一个词，取第一个（番名在前），不是最长的 1080p
        self.assertEqual(f["include"], "鬼灭")
        self.assertTrue(any("只能" in n for n in notes), notes)

    def test_exclude_fansub_sources(self):
        f, _ = Query.parse("鬼灭", exclude="粤语 合集", fansub="樱都",
                           src="dmhy,nyaa").to_watch()
        self.assertEqual(f["exclude"], "粤语 合集")
        self.assertEqual(f["fansub"], "樱都")
        self.assertEqual(f["sources"], "dmhy,nyaa")

    def test_time_window_warns(self):
        f, notes = Query.parse("鬼灭", after="2021-01-01").to_watch()
        self.assertTrue(any("时间" in n for n in notes), notes)

    def test_fansub_only(self):
        f, _ = Query.parse("", fansub="樱都").to_watch()
        self.assertEqual(f["include"], "")
        self.assertEqual(f["name"], "樱都")

    def test_name_override(self):
        f, _ = Query.parse("鬼灭").to_watch(name="鬼灭 存档")
        self.assertEqual(f["name"], "鬼灭 存档")

    def test_loose_drops_spec_words(self):
        # 点某条结果的「关注」= 想盯这部番，1080p/简日 这类规格词不该写进规则，
        # 否则以后出 2160p 版本就永远盯不到
        q = Query.parse("鬼灭", must="1080p", fansub="樱都")
        strict, _ = q.to_watch()
        self.assertEqual(strict["include"], "1080p 鬼灭")
        loose, _ = q.to_watch(loose=True)
        self.assertEqual(loose["include"], "鬼灭")
        self.assertEqual(loose["fansub"], "樱都", "显式指定的字幕组该保留")

    def test_loose_falls_back_to_must(self):
        # 只填了「必含」没有关键词时，loose 也不能把条件清空
        f, _ = Query.parse("", must="鬼灭").to_watch(loose=True)
        self.assertEqual(f["include"], "鬼灭")


class TestWatchBridgeConvert(unittest.TestCase):
    """插件 item -> store item 的字段转换（真正的挂载在集成测试里）。"""

    def test_fields_mapped(self):
        from arr_search import watchbridge as WB
        it = M.make_item("dmhy", "[樱都][鬼灭之刃][01]", "http://a/1",
                         source_name="动漫花园", infohash="AA" * 20,
                         size="1.4 GiB", seeders=12, pubdate="2022-01-05",
                         author="樱都")
        row = WB.to_store_item(it)
        self.assertEqual(row["source"], "dmhy")
        self.assertEqual(row["title"], "[樱都][鬼灭之刃][01]")
        self.assertEqual(row["infohash"].lower(), "aa" * 20)
        self.assertEqual(row["seeders"], 12)
        self.assertGreater(row["ts"], 1600000000)

    def test_no_id_injected(self):
        # id 必须让 store.item_key() 自己算，否则 watch_items 挂上去 JOIN 不到
        from arr_search import watchbridge as WB
        row = WB.to_store_item(M.make_item("dmhy", "鬼灭 01", "http://a"))
        self.assertNotIn("id", row)

    def test_author_fallback(self):
        from arr_search import watchbridge as WB
        row = WB.to_store_item(M.make_item("nyaa", "鬼灭 01", "http://a",
                                           source_name="Nyaa"))
        self.assertEqual(row["author"], "Nyaa")

    def test_dedupe_by_infohash(self):
        from arr_search import watchbridge as WB
        a = M.make_item("dmhy", "鬼灭 01", "http://a", infohash="cc" * 20)
        b = M.make_item("nyaa", "鬼灭 01", "http://b", infohash="cc" * 20)
        self.assertEqual(len(WB.to_store_items([a, b])), 1)

    def test_empty_title_rejected(self):
        from arr_search import watchbridge as WB
        self.assertEqual(WB.to_store_items([{"title": "", "source": "x"}]), [])


class TestProviders(unittest.TestCase):
    def test_dmhy(self):
        page = Dmhy().parse_xml(fixture("dmhy.xml"))
        self.assertGreaterEqual(len(page.items), 1)
        it = page.items[0]
        self.assertEqual(it["source"], "dmhy")
        self.assertTrue(it["title"])
        self.assertGreater(it["ts"], 1600000000)
        self.assertFalse(page.has_more)

    def test_acgrip(self):
        page = AcgRip().parse_xml(fixture("acgrip.xml"))
        self.assertGreaterEqual(len(page.items), 1)
        self.assertTrue(page.items[0]["torrent"].endswith(".torrent"))
        self.assertTrue(page.items[0]["size"])

    def test_mikan(self):
        page = Mikan().parse_xml(fixture("mikan.xml"))
        self.assertGreaterEqual(len(page.items), 1)
        self.assertGreater(page.items[0]["ts"], 1600000000)

    def test_nyaa(self):
        page = Nyaa().parse_html(fixture("nyaa_rows.html").decode("utf-8"))
        self.assertGreaterEqual(len(page), 1)
        self.assertIn("/view/", page[0]["link"])
        self.assertTrue(page[0]["magnet"].startswith("magnet:"))
        self.assertIsNotNone(page[0]["seeders"])

    def test_animegarden(self):
        page = AnimeGarden().parse_json(fixture("animegarden.json"))
        self.assertGreaterEqual(len(page.items), 1)
        self.assertTrue(page.items[0]["title"])
        self.assertGreater(page.items[0]["ts"], 1600000000)

    def test_bangumi(self):
        q = Query.parse("鬼灭")
        page = Bangumi().parse_json(fixture("bangumi.json"), q, 1)
        self.assertGreaterEqual(len(page.items), 1)
        self.assertTrue(page.items[0]["magnet"].startswith("magnet:"))

    def test_registry(self):
        table = P.build()
        for pid in ("animegarden", "dmhy", "nyaa", "acgrip", "mikan", "bangumi", "local"):
            self.assertIn(pid, table)
        # 第三方同 id 实现顶掉内置
        class Fake(Dmhy):
            id = "dmhy"
            name = "假花园"
        table2 = P.build([Fake])
        self.assertEqual(table2["dmhy"].name, "假花园")


if __name__ == "__main__":
    unittest.main(verbosity=2)
