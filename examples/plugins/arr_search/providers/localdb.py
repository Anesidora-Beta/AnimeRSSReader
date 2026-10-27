# -*- coding: utf-8 -*-
"""本地库：把「已经抓到的帖子」也当成一个源。

意义不在于快，而在于**组合**：同一句查询可以同时看「本地已有的」和「源站历史里的」，
结果集合并去重后是一份完整的清单，而不是两套割裂的搜索。

它也是唯一不联网的源 —— 断网、源站全挂的时候仍然可用。
"""
from ..model import make_item
from .base import Page, Provider

#: 单页最多取多少条（本地查询是 O(n) LIKE，别一次拉太多）
PAGE_CAP = 200


class LocalDB(Provider):
    id = "local"
    name = "本地库"
    home = ""
    note = "只查已抓取入库的条目，不联网；断网时的兜底"
    kind = "local"
    caps = frozenset({"local"})
    page_size = 100
    max_pages = 20

    def search(self, q, page):
        if self.store is None:
            return Page([], has_more=False, note="没有可用的本地库")
        limit = min(PAGE_CAP, max(20, q.limit))
        offset = (page - 1) * limit
        kw = q.server_keyword()
        try:
            rows = self.store.query_items(feed="all", filt="all", q=kw,
                                          limit=limit, offset=offset)
        except Exception as e:                       # noqa: BLE001 - 旧库版本差异
            raise RuntimeError("本地库查询失败：%s" % e)

        items = []
        for r in rows or ():
            it = make_item(
                r.get("source") or "local", r.get("title") or "",
                r.get("link") or "", source_name=self.name,
                id=r.get("id") or "",
                magnet=r.get("magnet") or "", torrent=r.get("torrent") or "",
                infohash=r.get("infohash") or "", size=r.get("size") or "",
                seeders=r.get("seeders"), pubdate=r.get("pubdate") or "",
                ts=int(r.get("ts") or 0), category=r.get("category") or "",
                author=r.get("author") or "", summary=r.get("summary") or "",
            )
            it["local"] = True
            it["starred"] = bool(r.get("starred"))
            items.append(it)
        return Page(items, has_more=len(items) >= limit, note="第 %d 页" % page)
