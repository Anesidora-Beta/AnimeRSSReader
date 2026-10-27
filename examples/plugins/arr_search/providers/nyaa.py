# -*- coding: utf-8 -*-
"""Nyaa：RSS 是「一次给最新的 75 条、翻页参数被忽略」，要挖历史只能读列表页。

实测 ``?page=rss&p=3`` 与 ``p=1`` 返回完全相同的条目，说明 RSS 不支持翻页；
但 HTML 列表页 ``?q=..&p=N`` 每页 75 条、可以一直往后翻。所以这里抓 HTML 用正则
解析 —— 不引入 bs4/lxml，继续保持零第三方依赖。

Nyaa 是国际站（RAW / 英译为主），找中文字幕组的旧帖通常靠 AnimeGarden，
但很多冷门 RAW 只在 Nyaa 留档，值得单独一条腿。
"""
import html
import re
import urllib.parse

from .. import net
from ..model import to_ts
from .base import Page, Provider, build

BASE = "https://nyaa.si"

_ROW_RE = re.compile(r'<tr class="(?:default|success|danger)">(.*?)</tr>', re.S)
_VIEW_RE = re.compile(r'href="/view/(\d+)"[^>]*title="([^"]*)"')
_CAT_RE = re.compile(r'href="/\?c=([\d_]+)"\s+title="([^"]*)"')
_MAGNET_RE = re.compile(r'href="(magnet:\?[^"]+)"')
_SIZE_RE = re.compile(r'<td class="text-center">([\d.]+\s*[KMGTP]?i?B)</td>')
_TS_RE = re.compile(r'data-timestamp="(\d+)"')
_SEED_RE = re.compile(
    r'data-timestamp="\d+">[^<]*</td>\s*<td class="text-center">(\d+)</td>')


class Nyaa(Provider):
    id = "nyaa"
    name = "Nyaa"
    home = BASE
    note = "国际站；RSS 不翻页，这里读列表页，每页 75 条可持续往后翻"
    caps = frozenset({"keyword", "paging"})
    page_size = 75
    max_pages = 40

    def search(self, q, page):
        params = {"c": "1_0", "f": "0", "p": str(page)}
        kw = q.server_keyword()
        if kw:
            params["q"] = kw
        url = BASE + "/?" + urllib.parse.urlencode(params)
        items = self.parse_html(net.fetch(url, timeout=18).decode("utf-8", "ignore"))
        # 满页才认为还有下一页；Nyaa 每页 75 条，被分类过滤后可能少几条
        has_more = len(items) >= self.page_size * 0.6 and page < self.max_pages
        return Page(items, has_more=has_more, note="第 %d 页" % page)

    # 下面两个方法单独抽出来，是为了能用固定 HTML 做离线单测
    def parse_html(self, html_text):
        return self.parse_rows(_ROW_RE.findall(html_text))

    def parse_rows(self, rows):
        out = []
        for row in rows:
            m = _VIEW_RE.search(row)
            if not m:
                continue
            vid, title = m.group(1), html.unescape(m.group(2))
            cat = _CAT_RE.search(row)
            mag = _MAGNET_RE.search(row)
            size = _SIZE_RE.search(row)
            ts = _TS_RE.search(row)
            seed = _SEED_RE.search(row)
            out.append(build(
                self.id, self.name,
                title,
                "%s/view/%s" % (BASE, vid),
                magnet=html.unescape(mag.group(1)) if mag else "",
                torrent="%s/download/%s.torrent" % (BASE, vid),
                size=size.group(1) if size else "",
                seeders=int(seed.group(1)) if seed else None,
                pubdate="",
                ts=int(ts.group(1)) if ts else 0,
                category=html.unescape(cat.group(2)) if cat else "",
            ))
        for it in out:
            if it["ts"] and not it["pubdate"]:
                import time
                it["pubdate"] = time.strftime("%a, %d %b %Y %H:%M:%S +0000",
                                              time.gmtime(it["ts"]))
        return out
