# -*- coding: utf-8 -*-
"""动漫花园：``/topics/rss?keyword=`` 一次给 500 条，且能一路回溯好几年。

这是「旧帖也能找」的第二个关键点，很多人以为它只给最新几条，其实带 keyword 的
搜索 RSS 是一个 500 条的大窗口（实测覆盖 2022 年至今），只是不能翻页。

所以这个源 ``max_pages = 1``：拿一次就够，翻页交给 AnimeGarden。
三个镜像域名轮着试，谁通用谁 —— 国内经常只有其中一个能连上。
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import net
from ..model import to_ts
from .base import Page, Provider, build

HOSTS = ["https://share.dmhy.org", "https://dmhy.org", "https://www.dmhy.org"]


class Dmhy(Provider):
    id = "dmhy"
    name = "动漫花园"
    home = "https://share.dmhy.org"
    note = "搜索 RSS 一次 500 条、可回溯数年，但不支持翻页"
    caps = frozenset({"keyword"})
    page_size = 500
    max_pages = 1

    def search(self, q, page):
        if page > 1:
            return Page([], has_more=False, note="该源无翻页")
        kw = q.server_keyword()
        params = {}
        if kw:
            params["keyword"] = kw
            path = "/topics/rss"
        else:
            path = "/topics/rss/rss.xml"

        suffix = path + ("?" + urllib.parse.urlencode(params) if params else "")
        last = ""
        for host in HOSTS:
            try:
                raw = net.fetch(host + suffix, timeout=18)
                return self.parse_xml(raw)
            except Exception as e:                 # noqa: BLE001 - 换个镜像再试
                last = "%s: %s" % (type(e).__name__, e)
        raise RuntimeError("三个镜像都没通（%s）" % last)

    def parse_xml(self, raw):
        ch = ET.fromstring(raw).find("channel")
        if ch is None:
            raise ValueError("动漫花园返回的不是 RSS")
        items = []
        for it in ch.findall("item"):
            enc = it.find("enclosure")
            magnet = (enc.get("url") or "") if enc is not None else ""
            desc = it.findtext("description") or ""
            size = ""
            m = re.search(r"大小[:：]?\s*([\d.]+\s*[KMGT]i?B)", desc, re.I)
            if m:
                size = m.group(1)
            items.append(build(
                self.id, self.name,
                it.findtext("title") or "",
                it.findtext("link") or "",
                magnet=magnet,
                size=size,
                pubdate=(it.findtext("pubDate") or "").strip(),
                category=(it.findtext("category") or "").strip(),
                author=(it.findtext("author") or "").strip(),
                summary=re.sub(r"<[^>]+>", " ", desc)[:300].strip(),
                ts=to_ts(it.findtext("pubDate")),
            ))
        return Page(items, has_more=False, note="单页 500 条窗口")
