# -*- coding: utf-8 -*-
"""蜜柑计划：``/RSS/Search?searchstr=`` 一次 100 条，不能翻页。

蜜柑以「番剧订阅」为主，同一集通常只有一个种子（它自己重新打包过），
所以它给的磁力可能下不动，这里只给种子直链（与主程序的处理一致）。

条数固定 100，跨季度找旧帖时靠别源补；它更多是「补上中文番剧这一块」。
"""
import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import net
from ..model import human_size
from .base import Page, Provider, build

BASE = "https://mikanani.me"
MK = "{https://mikanani.me/0.1/}"


class Mikan(Provider):
    id = "mikan"
    name = "蜜柑计划"
    home = BASE
    note = "搜索一次 100 条、不能翻页；只给种子直链（磁力需自行转换）"
    caps = frozenset({"keyword"})
    page_size = 100
    max_pages = 1

    def search(self, q, page):
        if page > 1:
            return Page([], has_more=False, note="该源无翻页")
        kw = q.server_keyword()
        if not kw:
            # 没有关键词时蜜柑只能给全站最新，对「找旧帖」没有意义
            return Page([], has_more=False, note="蜜柑只在有关键词时检索")
        url = BASE + "/RSS/Search?" + urllib.parse.urlencode({"searchstr": kw})
        return self.parse_xml(net.fetch(url, timeout=18))

    def parse_xml(self, raw):
        ch = ET.fromstring(raw).find("channel")
        if ch is None:
            raise ValueError("蜜柑返回的不是 RSS")
        items = []
        for it in ch.findall("item"):
            enc = it.find("enclosure")
            torrent = (enc.get("url") or "") if enc is not None else ""
            length = (enc.get("length") or "") if enc is not None else ""
            link = (it.findtext("link") or "").strip()
            tor = it.find(MK + "torrent")
            pub = ""
            if tor is not None:
                pub = (tor.findtext(MK + "pubDate") or "").strip()
                length = length or (tor.findtext(MK + "contentLength") or "")
            pub = pub or (it.findtext("pubDate") or "").strip()
            m = re.search(r"/([0-9a-fA-F]{40})(?:\.torrent)?/?$", link or torrent)
            items.append(build(
                self.id, self.name,
                it.findtext("title") or "",
                link,
                torrent=torrent,
                infohash=m.group(1) if m else "",
                size=human_size(length) if length.strip().isdigit() else "",
                pubdate=pub,
                summary=re.sub(r"<[^>]+>", " ", it.findtext("description") or "")[:300].strip(),
            ))
        return Page(items, has_more=False, note="单页 100 条")
