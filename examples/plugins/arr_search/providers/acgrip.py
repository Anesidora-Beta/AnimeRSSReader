# -*- coding: utf-8 -*-
"""ACG.RIP：``/.xml?term=关键词&page=N`` 每页 30 条，翻页正常，历史很深。

WebRip / BDRip 画质向的字幕组聚集地，找「当年那版」的概率比聚合站高，
所以它虽然条数少，仍然值得作为独立一条腿（组合起来才不漏）。
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import net
from ..model import human_size
from .base import Page, Provider, build

BASE = "https://acg.rip"
EZ = "{http://xmlns.ezrss.it/0.1/}"


class AcgRip(Provider):
    id = "acgrip"
    name = "ACG.RIP"
    home = BASE
    note = "每页 30 条、可翻页；画质向字幕组多，旧番留档较全"
    caps = frozenset({"keyword", "paging"})
    page_size = 30
    max_pages = 40

    def search(self, q, page):
        params = {"page": str(page)}
        kw = q.server_keyword()
        if kw:
            params["term"] = kw
        url = BASE + "/.xml?" + urllib.parse.urlencode(params)
        raw = net.fetch(url, timeout=18)
        return self.parse_xml(raw, page)

    def parse_xml(self, raw, page=1):
        ch = ET.fromstring(raw).find("channel")
        if ch is None:
            raise ValueError("ACG.RIP 返回的不是 RSS")
        items = []
        for it in ch.findall("item"):
            enc = it.find("enclosure")
            torrent = (enc.get("url") or "") if enc is not None else ""
            length = it.findtext(EZ + "contentLength") or ""
            desc = it.findtext("description") or ""
            items.append(build(
                self.id, self.name,
                it.findtext("title") or "",
                it.findtext("link") or "",
                torrent=torrent,
                size=human_size(length) if length.strip().isdigit() else (length or ""),
                pubdate=(it.findtext("pubDate") or "").strip(),
                summary=re.sub(r"<[^>]+>", " ", desc)[:300].strip(),
            ))
        return Page(items, has_more=len(items) >= self.page_size,
                    note="第 %d 页" % page)
