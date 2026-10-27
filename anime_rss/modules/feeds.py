"""订阅源模块：自定义订阅的增删改、抓取状态、镜像健康、RSS / OPML 导出。

路由说明：管理接口挂在自己的 ``/api/feeds`` 前缀下；导出接口
（``/feed.xml``、``/rss``、``/opml.xml`` …）必须留在根路径 —— 那是用户
填进阅读器里的订阅地址，换前缀会让已有订阅全部失效，属于契约允许的例外。
"""
import time
import urllib.parse

import feeds as F
import sources as S
from ..common import source_names
from ..kernel.http import Response
from ..module import Module


class FeedsModule(Module):
    id = "feeds"
    name = "订阅源"
    version = "1.0.0"

    # ------------------------------------------------------------ 生命周期
    def setup(self, ctx):
        r = ctx.router
        r.get("/api/mirrors", self.api_mirrors)
        r.get("/api/refresh", self.api_refresh_status)
        r.post("/api/refresh", self.api_refresh_start)
        r.post("/api/feeds", self.api_create)
        r.patch("/api/feeds/{fid}", self.api_update)
        r.delete("/api/feeds/{fid}", self.api_delete)

        # 导出：根路径，保持老订阅地址可用
        r.get("/feed.xml", self.feed_all)
        r.get("/rss.xml", self.feed_all)
        r.get("/rss", self.feed_all)
        r.get("/feed/{fid}", self.feed_one)
        r.get("/opml.xml", self.opml)
        r.get("/feeds.opml", self.opml)

    # ------------------------------------------------------------ bootstrap 片段
    def bootstrap(self, ctx):
        feeds = ctx.store.feeds()
        names = source_names(ctx)
        for f in feeds:
            f["sourceName"] = names.get(f["id"], f["title"])
        return {
            "sources": [{"id": s["id"], "name": s["name"], "home": s["home"],
                         "note": s["note"]} for s in S.SOURCES],
            "feeds": feeds,
            "mirrors": F.host_status(),
        }

    # ------------------------------------------------------------ 订阅管理
    def api_create(self, req):
        return Response.json(self._create(req.json_body()))

    def api_update(self, req):
        ctx = self.ctx
        fid = req.params["fid"]
        if not ctx.store.feed(fid):
            return Response.err("订阅源不存在", 404)
        body = req.json_body()
        ctx.store.update_feed(
            fid,
            title=body.get("title"),
            url=body.get("url"),
            enabled=body.get("enabled"),
            params=body.get("params"),
        )
        return Response.json({"ok": True, "feeds": ctx.store.feeds(),
                              "counts": ctx.store.counts()})

    def api_delete(self, req):
        ctx = self.ctx
        fid = req.params["fid"]
        feed = ctx.store.feed(fid)
        if not feed:
            return Response.err("订阅源不存在", 404)
        if feed["kind"] == "builtin":
            return Response.err("内置频段不能删除，可以关闭它", 400)
        ctx.store.delete_feed(fid)
        return Response.json({"ok": True, "feeds": ctx.store.feeds(),
                              "counts": ctx.store.counts()})

    def _create(self, body):
        ctx = self.ctx
        url = (body.get("url") or "").strip()
        title = (body.get("title") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "订阅地址必须以 http:// 或 https:// 开头"}
        fid = "c" + str(int(time.time() * 1000))
        if not title:
            title = urllib.parse.urlsplit(url).hostname or "自定义订阅"
        feed = ctx.store.add_feed(fid, title, "custom", {}, url)
        # 立刻抓一次，让用户加完就能看到内容
        try:
            items, _ = F.collect_custom(feed)
            added = ctx.store.upsert_items(fid, items[:300])
            ctx.store.feed_stat(fid, True)
        except Exception as e:
            ctx.store.feed_stat(fid, False, "%s: %s" % (type(e).__name__, e))
            return {"ok": True, "feeds": ctx.store.feeds(), "counts": ctx.store.counts(),
                    "warning": "订阅已添加，但首次抓取失败：%s" % e}
        return {"ok": True, "feeds": ctx.store.feeds(), "counts": ctx.store.counts(),
                "added": added}

    # ------------------------------------------------------------ 抓取与镜像
    def api_refresh_status(self, req):
        return Response.json({"ok": True, **self.ctx.refresher.status()})

    def api_refresh_start(self, req):
        body = req.json_body()
        ids = body.get("feeds") or None
        started = self.ctx.refresher.start(ids)
        return Response.json({"ok": True, "started": started,
                              **self.ctx.refresher.status()})

    def api_mirrors(self, req):
        return Response.json({"ok": True, "sources": F.host_status()})

    # ------------------------------------------------------------ 导出
    def feed_all(self, req):
        return self.serve(req, None)

    def feed_one(self, req):
        fid = req.params["fid"]
        if fid.endswith(".xml"):
            fid = fid[:-4]
        return self.serve(req, fid)

    def serve(self, req, fid):
        ctx = self.ctx
        if fid and not ctx.store.feed(fid):
            return Response.text("订阅源不存在", 404)
        data = F.build_feed_xml(ctx.store, fid or "all", ctx.base_url(req))
        return Response(data, "application/rss+xml; charset=utf-8",
                        cache="public, max-age=120")

    def opml(self, req):
        data = F.build_opml(self.ctx.store, self.ctx.base_url(req))
        return Response(data, "text/x-opml; charset=utf-8")
