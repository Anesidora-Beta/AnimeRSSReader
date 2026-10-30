#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按频道的计数：``counts()`` 里 feeds / watches 那两个字典。

这两个数不只是给徽章用的 —— 列表头部那排「全部 / 未读 / 收藏」直接读它们。
少一个 ``starred`` 字段，界面上就会永远显示 0，而条目其实收藏了。

跑法： ``python -m unittest tests.test_counts -v``
"""
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store as ST                                     # noqa: E402


ROWS = [
    {"id": "t0", "title": "某番 01", "ts": 1000,
     "link": "https://example.com/0"},
    {"id": "t1", "title": "某番 02", "ts": 1001,
     "link": "https://example.com/1"},
    {"id": "t2", "title": "别的片子", "ts": 1002,
     "link": "https://example.com/2"},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-counts-")
        self.st = ST.Store(os.path.join(self.tmp, "reader.db"))
        self.st.add_feed("custom:1", "测试源", "custom")
        self.st.upsert_items("custom:1", ROWS)
        self.ids = [it["id"] for it in self.st.query_items(feed="custom:1")]

    def tearDown(self):
        self.st.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def id_of(self, title):
        """按标题找条目 id：库里是哈希 id，顺序也不是插入顺序。"""
        for it in self.st.query_items(feed="custom:1", limit=50):
            if it["title"] == title:
                return it["id"]
        raise AssertionError("没有这一条：" + title)

    def feed_counts(self):
        return self.st.counts()["feeds"]["custom:1"]


class FeedCounts(Base):
    def test_total_and_unread(self):
        c = self.feed_counts()
        self.assertEqual(c["total"], 3)
        self.assertEqual(c["unread"], 3)

    def test_starred_counted_per_feed(self):
        self.st.set_star(self.ids[0], True)
        self.assertEqual(self.feed_counts()["starred"], 1)
        self.st.set_star(self.ids[1], True)
        self.assertEqual(self.feed_counts()["starred"], 2)

    def test_unstar_goes_back(self):
        self.st.set_star(self.ids[0], True)
        self.st.set_star(self.ids[0], False)
        self.assertEqual(self.feed_counts()["starred"], 0)

    def test_read_does_not_affect_starred(self):
        self.st.set_star(self.ids[0], True)
        self.st.set_read([self.ids[0]], True)
        c = self.feed_counts()
        self.assertEqual(c["unread"], 2)
        self.assertEqual(c["starred"], 1)

    def test_filter_and_count_agree(self):
        """数量说有几条，按这个筛选就真能拿到几条 —— 界面上的数字点下去
        必须对得上，否则用户会以为丢了东西。"""
        self.st.set_star(self.ids[0], True)
        self.st.set_star(self.ids[2], True)
        c = self.feed_counts()
        got = self.st.query_items(feed="custom:1", filt="starred", limit=50)
        self.assertEqual(len(got), c["starred"])
        self.assertEqual(len(got), 2)

    def test_empty_feed_absent(self):
        self.assertIsNone(self.st.counts()["feeds"].get("custom:nope"))

    def test_global_counts_still_right(self):
        self.st.set_star(self.ids[0], True)
        c = self.st.counts()
        self.assertEqual(c["starred"], 1)
        self.assertEqual(c["total"], 3)


class WatchCounts(Base):
    def setUp(self):
        super().setUp()
        self.w = self.st.add_watch("某番", include="某番")
        # 传的字段要和 upsert 时一致：条目 id 是 link+title 的哈希
        n = self.st.attach_watch(self.w["id"], [
            {"title": "某番 01", "link": "https://example.com/0"},
            {"title": "某番 02", "link": "https://example.com/1"},
        ])
        self.assertEqual(n, 2)

    def wcount(self):
        return self.st.counts()["watches"][self.w["id"]]

    def test_basics(self):
        c = self.wcount()
        self.assertEqual(c["total"], 2)
        self.assertEqual(c["unread"], 2)

    def test_starred_counted_per_watch(self):
        self.st.set_star(self.id_of("某番 01"), True)
        self.assertEqual(self.wcount()["starred"], 1)

    def test_star_outside_watch_not_counted(self):
        # 「别的片子」不匹配「某番」，不算进这个关注
        self.st.set_star(self.id_of("别的片子"), True)
        self.assertEqual(self.wcount()["starred"], 0)
        self.assertEqual(self.st.counts()["starred"], 1)

    def test_watch_filter_agrees_with_count(self):
        self.st.set_star(self.id_of("某番 02"), True)
        got = self.st.query_items(feed="watch:" + self.w["id"],
                                  filt="starred", limit=50)
        self.assertEqual(len(got), self.wcount()["starred"])


if __name__ == "__main__":
    unittest.main()
