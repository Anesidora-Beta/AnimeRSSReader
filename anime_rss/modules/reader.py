"""阅读模块：条目列表与详情、已读/收藏、关注规则。

关注规则会牵到抓取（本地没命中时去补搜一次），所以它依赖 feeds 模块 ——
依赖写在 ``depends`` 里，由内核保证 feeds 先加载。
"""
import time

import store as ST
from ..common import source_names
from ..kernel.http import Response
from ..module import Module


class ReaderModule(Module):
    id = "reader"
    name = "阅读与关注"
    version = "1.0.0"
    depends = ("feeds",)

    def setup(self, ctx):
        r = ctx.router
        r.get("/api/items", self.api_items)
        r.get("/api/item/{iid}", self.api_item)
        r.post("/api/read", self.api_read)
        r.post("/api/star", self.api_star)
        r.post("/api/read-all", self.api_read_all)

        r.get("/api/watches", self.api_watches)
        r.post("/api/watches", self.api_create_watch)
        r.patch("/api/watches/{wid}", self.api_update_watch)
        r.delete("/api/watches/{wid}", self.api_delete_watch)
        r.post("/api/watches/{wid}/scan", self.api_scan_watch)

    # ------------------------------------------------------------ bootstrap 片段
    def bootstrap(self, ctx):
        return {"watches": self.watches_payload()}

    # ------------------------------------------------------------ 条目
    def api_items(self, req):
        ctx = self.ctx
        q = req.query
        try:
            limit = max(1, min(int(q.get("limit") or 60), 300))
        except ValueError:
            limit = 60
        try:
            offset = max(0, int(q.get("offset") or 0))
        except ValueError:
            offset = 0
        feed = q.get("feed") or "all"
        filt = q.get("filter") or "all"
        kw = (q.get("q") or "").strip()
        rows = ctx.store.query_items(feed=feed, filt=filt, q=kw,
                                     limit=limit + 1, offset=offset)
        has_more = len(rows) > limit
        rows = rows[:limit]
        names = source_names(ctx)
        for it in rows:
            it["sourceName"] = names.get(it["source"], it["source"])
        return Response.json({"ok": True, "items": rows, "hasMore": has_more,
                              "offset": offset, "counts": ctx.store.counts()})

    def api_item(self, req):
        iid = req.params["iid"]
        it = self.ctx.store.get_item(iid)
        if not it:
            return Response.err("条目不存在", 404)
        it["sourceName"] = source_names(self.ctx).get(it["source"], it["source"])
        return Response.json({"ok": True, "item": it})

    def api_read(self, req):
        body = req.json_body()
        ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
        n = self.ctx.store.set_read(ids, bool(body.get("read", True)))
        return Response.json({"ok": True, "changed": n,
                              "counts": self.ctx.store.counts()})

    def api_star(self, req):
        body = req.json_body()
        it = self.ctx.store.set_star(body.get("id") or "",
                                     bool(body.get("starred", True)))
        return Response.json({"ok": True, "item": it,
                              "counts": self.ctx.store.counts()})

    def api_read_all(self, req):
        body = req.json_body()
        n = self.ctx.store.read_all(body.get("feed") or "all")
        return Response.json({"ok": True, "changed": n,
                              "counts": self.ctx.store.counts()})

    # ------------------------------------------------------------ 关注
    def api_watches(self, req):
        return Response.json({"ok": True, "watches": self.watches_payload()})

    def api_create_watch(self, req):
        return Response.json(self._create_watch(req.json_body()))

    def api_update_watch(self, req):
        ctx = self.ctx
        wid = req.params["wid"]
        if not ctx.store.watch(wid):
            return Response.err("关注不存在", 404)
        body = req.json_body()
        ctx.store.update_watch(
            wid,
            name=body.get("name"),
            include=body.get("include"),
            exclude=body.get("exclude"),
            fansub=body.get("fansub"),
            sources=body.get("sources"),
            enabled=body.get("enabled"),
        )
        hits = ctx.store.scan_watches()["per"].get(wid, 0)
        if not hits and ctx.store.watch(wid)["enabled"]:
            hits = self._backfill_watch(wid)      # 改了关键词后本地没命中，去补搜
        return Response.json({"ok": True, "hits": hits,
                              "watches": self.watches_payload(),
                              "counts": ctx.store.counts()})

    def api_delete_watch(self, req):
        ctx = self.ctx
        wid = req.params["wid"]
        if not ctx.store.watch(wid):
            return Response.err("关注不存在", 404)
        ctx.store.delete_watch(wid)
        return Response.json({"ok": True, "watches": self.watches_payload(),
                              "counts": ctx.store.counts()})

    def api_scan_watch(self, req):
        ctx = self.ctx
        wid = req.params["wid"]
        if not ctx.store.watch(wid):
            return Response.err("关注不存在", 404)
        res = ctx.store.scan_watches()
        return Response.json({"ok": True, "hits": res["per"].get(wid, 0),
                              "watches": self.watches_payload(),
                              "counts": ctx.store.counts()})

    # ------------------------------------------------------------ 内部
    def watches_payload(self):
        now = int(time.time())
        out = []
        for w in self.ctx.store.watches():
            keep = 0
            if w["lastHit"]:
                keep = max(0, ST.KEEP_WATCH_DAYS - (now - w["lastHit"]) // 86400)
            out.append({**w, "remainDays": keep})
        return out

    def _create_watch(self, body):
        ctx = self.ctx
        include = (body.get("include") or "").strip()
        if not include:
            return {"ok": False, "error": "请至少填写一个关注关键词"}
        name = (body.get("name") or "").strip() or include
        w = ctx.store.add_watch(name, include,
                                body.get("exclude") or "",
                                body.get("fansub") or "",
                                body.get("sources") or "")
        hits = ctx.store.scan_watches()["per"].get(w["id"], 0)
        # 本地一条都没有，立刻去 AnimeGarden 搜一次，别让用户对着空列表等下一轮刷新
        if not hits:
            hits = self._backfill_watch(w["id"])
        return {"ok": True, "watch": ctx.store.watch(w["id"]), "hits": hits,
                "watches": self.watches_payload(),
                "counts": ctx.store.counts()}

    def _backfill_watch(self, wid):
        ctx = self.ctx
        w = ctx.store.watch(wid)
        if not w:
            return 0
        try:
            got = ctx.refresher.search_for_watch(w)
        except Exception:
            return 0
        if not got:
            return 0
        ctx.store.upsert_items(None, got, link=False)
        return ctx.store.attach_watch(wid, got)
