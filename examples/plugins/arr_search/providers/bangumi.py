# -*- coding: utf-8 -*-
"""萌番组：JSON API ``/api/torrent/page/{N}``，每页 30 条，可一直往后翻。

它没有可用的搜索接口（``/api/torrent/search`` 现在返回 500，正文是
``Expected a string but received a undefined``），主程序的做法也是「拿页 +
本地按关键词筛」。这里沿用并加强：

* 有关键词：本地筛，但会一直翻到命中足够多或者翻过时间下界为止；
* 没关键词：就是「按时间往回翻」，配合 ``after:`` / ``before:`` 用正好；
* ``publish_time`` 是倒序的，所以一旦本页最旧一条已经早于 ``after``，
  就没必要再往后翻了 —— 这是把「深挖」限制在必要范围内的关键。
"""
import json

from .. import net
from ..model import to_ts
from .base import Page, Provider, build

BASE = "https://bangumi.moe"


class Bangumi(Provider):
    id = "bangumi"
    name = "萌番组"
    home = BASE
    note = "没有搜索接口：有关键词时只在最近几页里筛，配合时间区间才好用"
    caps = frozenset({"paging"})
    page_size = 30
    max_pages = 60

    def page_budget(self, q, pages):
        # 有关键词时它只能在拿到的 30 条里本地筛，多翻几页也筛不出几个命中，
        # 纯属给源站添堵；没有关键词（就是按时间往回翻）时才值得深挖。
        if q.server_keyword():
            return 1
        return pages

    def search(self, q, page):
        url = "%s/api/torrent/page/%d" % (BASE, page)
        raw = net.fetch(url, timeout=15)
        page_obj = self.parse_json(raw, q, page)
        return page_obj

    # 抽出来是为了能用固定 JSON 做离线单测
    def parse_json(self, raw, q, page=1):
        data = json.loads(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
        if not isinstance(data, dict):
            raise ValueError("萌番组返回结构异常")

        items = []
        for t in data.get("torrents") or []:
            items.append(build(
                self.id, self.name,
                t.get("title") or "",
                "%s/torrent/%s" % (BASE, t.get("_id") or ""),
                magnet=t.get("magnet") or "",
                infohash=t.get("infoHash") or "",
                size=t.get("size") or "",
                seeders=t.get("seeders"),
                pubdate=t.get("publish_time") or "",
                summary=(t.get("introduction") or "")[:300],
                ts=to_ts(t.get("publish_time")),
            ))

        has_more = len(items) >= self.page_size and page < self.max_pages
        if has_more and q.after:
            from ..query import _date_ts
            oldest = min((i["ts"] for i in items if i.get("ts")), default=0)
            if oldest and oldest <= _date_ts(q.after):
                has_more = False          # 已经翻过时间下界，停
        return Page(items, has_more=has_more, note="第 %d 页" % page)
