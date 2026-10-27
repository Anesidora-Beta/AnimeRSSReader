# -*- coding: utf-8 -*-
"""AnimeGarden（动漫花园官方镜像 + BT 聚合站）。

这个源是「找旧帖」的主力，原因只有一个：它是这批站点里唯一一个
**同时支持服务端时间区间和无限翻页**的公开 API。

    GET https://api.animes.garden/resources
        ?search=鬼灭&page=1&pageSize=100&after=2022-01-01&before=2022-12-31

``after`` / ``before`` 收 ``YYYY-MM-DD``，实测直接就能捞出 2022 年的帖子 ——
主程序「只拉最新 200 条」的天花板在这里不存在。
"""
import json
import urllib.parse

from ..model import human_size, to_ts
from .base import Page, Provider, build

API = "https://api.animes.garden/resources"

#: provider 字段是原始站点机器名，翻成中文给页面看
PROVIDERS = {"dmhy": "动漫花园", "moe": "萌番组", "ani": "ANi", "other": "其它"}


class AnimeGarden(Provider):
    id = "animegarden"
    name = "AnimeGarden"
    home = "https://animes.garden"
    note = "聚合动漫花园/萌番组，支持时间区间 + 深度翻页，找旧帖首选"
    caps = frozenset({"keyword", "date", "paging", "fansub"})
    page_size = 100
    max_pages = 40

    def search(self, q, page):
        params = {
            "page": str(page),
            "pageSize": str(max(20, min(q.limit or 100, 100))),
        }
        if q.server_keyword():
            params["search"] = q.server_keyword()
        # 只有这个源能把时间区间下推下去
        if q.after:
            params["after"] = q.after
        if q.before:
            params["before"] = q.before
        if q.fansub:
            params["fansub"] = q.fansub

        url = API + "?" + urllib.parse.urlencode(params)
        from .. import net
        return self.parse_json(net.fetch(url, timeout=18), page)

    # 抽出来是为了能用固定 JSON 做离线单测
    def parse_json(self, raw, page=1):
        data = json.loads(raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else raw)
        if not isinstance(data, dict):
            raise ValueError("AnimeGarden 返回结构异常")

        items = []
        for r in data.get("resources") or []:
            fansub = ((r.get("fansub") or {}).get("name") or "").strip()
            publisher = ((r.get("publisher") or {}).get("name") or "").strip()
            prov = (r.get("provider") or "").strip()
            rtype = (r.get("type") or "").strip()
            bits = [PROVIDERS.get(prov, prov), rtype, fansub or publisher]
            items.append(build(
                self.id, self.name,
                r.get("title") or "",
                r.get("href") or "",
                magnet=r.get("magnet") or "",
                size=human_size(r.get("size")),
                pubdate=r.get("createdAt") or "",
                category=rtype,
                author=fansub or publisher,
                summary=" · ".join(x for x in bits if x),
                ts=to_ts(r.get("createdAt")),
            ))

        pag = data.get("pagination") or {}
        has_more = bool(items) and not pag.get("complete", False)
        if page >= self.max_pages:
            has_more = False
        return Page(items, has_more=has_more,
                    note="page=%s/%s" % (pag.get("page"), pag.get("pageSize")))
