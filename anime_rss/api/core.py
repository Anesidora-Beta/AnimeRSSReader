"""核心 API：把原先 app.py 里那条 if/elif 链拆成显式路由表。

行为与原实现保持一致（URL、状态码、缓存头、错误文案都不变），
只是从「一个巨型函数按字符串分支」变成「注册表 + 小函数」。
"""
import time
import urllib.parse

import feeds as F
import schedule as SCH
import settings as CFG
import sources as S
import store as ST
from ..kernel.http import Response


# ---------------------------------------------------------------- 应用层小工具

def source_names(ctx):
    """source id -> 显示名（内置站点 + 自定义订阅源标题）"""
    names = {s["id"]: s["name"] for s in S.SOURCES}
    try:
        for f in ctx.store.feeds():
            names.setdefault(f["id"], f["title"])
    except Exception:
        pass
    return names


def apply_settings(ctx, values=None):
    """把设置同步到各个组件：各模块自己的配置 + 关窗行为。"""
    vals = values or (ctx.settings.get() if ctx.settings else {})
    # 谁有 apply_settings 就通知谁，核心不需要知道具体有哪几个模块
    if ctx.modules is not None:
        for mod in ctx.modules:
            hook = getattr(mod, "apply_settings", None)
            if callable(hook):
                try:
                    hook(vals)
                except Exception:
                    pass
    if ctx.ui is not None:
        ctx.ui.on_close = vals.get("closeAction") or "ask"
    return vals


def settings_payload(ctx):
    vals = ctx.settings.get() if ctx.settings else CFG.normalize({})
    info = {
        "version": ctx.version,
        "port": ctx.port,
        "lan": ctx.lan_mode,
        "dataDir": ctx.data_dir,
        "dbPath": ctx.db_path,
        "uptime": ctx.uptime,
        "stats": ctx.store.stats() if ctx.store else {},
        "nextRefresh": ctx.auto.next_run_at() if ctx.auto else 0,
        "refreshing": bool(ctx.refresher.status()["running"]) if ctx.refresher else False,
        "platforms": [{"id": p["id"], "name": p["name"]} for p in SCH.PLATFORMS],
    }
    if ctx.lan_mode:
        info["lanUrl"] = ctx.lan_url()
    return {"ok": True, "values": vals,
            "schema": ctx.settings.schema() if ctx.settings else CFG.SCHEMA,
            "info": info}


# ---------------------------------------------------------------- 路由实现

class CoreApi:
    def __init__(self, ctx):
        self.ctx = ctx

    # 注册全部核心路由（顺序即优先级）
    def register(self, router):
        r = router
        r.get("/", self.index)
        r.get("/index.html", self.index)
        r.get("/favicon.ico", self.favicon)
        r.get("/apple-touch-icon.png", self.favicon)
        r.get("/healthz", self.health)
        r.get("/api/health", self.health)

        r.get("/api/bootstrap", self.bootstrap)
        r.get("/api/items", self.items)
        r.get("/api/item/{iid}", self.item)
        r.get("/api/refresh", self.refresh_status)
        r.get("/api/watches", self.watches)
        r.get("/api/mirrors", self.mirrors)
        r.get("/api/settings", self.settings_get)
        r.get("/feed.xml", self.feed_all)
        r.get("/rss.xml", self.feed_all)
        r.get("/rss", self.feed_all)
        r.get("/feed/{fid}", self.feed_one)
        r.get("/opml.xml", self.opml)
        r.get("/feeds.opml", self.opml)

        r.post("/api/refresh", self.refresh_start)
        r.post("/api/settings", self.settings_save)
        r.post("/api/settings/reset", self.settings_reset)
        r.post("/api/maintenance", self.maintenance)
        r.post("/api/read", self.read)
        r.post("/api/star", self.star)
        r.post("/api/read-all", self.read_all)
        r.post("/api/feeds", self.create_feed)
        r.post("/api/watches", self.create_watch)
        r.post("/api/watches/{wid}/scan", self.scan_watch)
        r.post("/api/open-window", self.open_window)

        r.patch("/api/watches/{wid}", self.update_watch)
        r.patch("/api/feeds/{fid}", self.update_feed)
        r.delete("/api/watches/{wid}", self.delete_watch)
        r.delete("/api/feeds/{fid}", self.delete_feed)
        return self

    # ------------------------------------------------------------ 静态页
    def index(self, req):
        page = self.ctx.assets.read(self.ctx.static_dir, "index.html")
        if page is None:
            page = "static/index.html 缺失".encode("utf-8")
        page = page.replace(b"__VERSION__", self.ctx.version.encode())
        return Response(page, "text/html; charset=utf-8", cache="no-cache")

    def favicon(self, req):
        data = self.ctx.assets.read(self.ctx.static_dir, "icon.svg") or b""
        return Response(data, "image/svg+xml", cache="max-age=604800")

    def health(self, req):
        return Response.json({"ok": True, "app": "anime-rss-reader",
                              "version": self.ctx.version,
                              "uptime": self.ctx.uptime})

    # ------------------------------------------------------------ 读接口
    def bootstrap(self, req):
        ctx = self.ctx
        feeds = ctx.store.feeds()
        names = source_names(ctx)
        for f in feeds:
            f["sourceName"] = names.get(f["id"], f["title"])
        return Response.json({
            "ok": True,
            "app": ctx.app_name,
            "version": ctx.version,
            "sources": [{"id": s["id"], "name": s["name"], "home": s["home"],
                         "note": s["note"]} for s in S.SOURCES],
            "feeds": feeds,
            "counts": ctx.store.counts(),
            "stats": ctx.store.stats(),
            "refresh": ctx.refresher.status(),
            "lan": {"enabled": ctx.lan_mode,
                    "urls": [ctx.lan_url()] if ctx.lan_mode else [],
                    "ip": ctx.lan_url().split("//")[-1].split(":")[0]},
            "watches": self.watches_payload(),
            "mirrors": F.host_status(),
            "schedule": self._schedule_brief(),
            "settings": ctx.settings.get() if ctx.settings else {},
            "baseUrl": ctx.base_url(req),
            "serverTime": int(time.time()),
        })

    def items(self, req):
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

    def item(self, req):
        iid = req.params["iid"]
        it = self.ctx.store.get_item(iid)
        if not it:
            return Response.err("条目不存在", 404)
        it["sourceName"] = source_names(self.ctx).get(it["source"], it["source"])
        return Response.json({"ok": True, "item": it})

    def refresh_status(self, req):
        return Response.json({"ok": True, **self.ctx.refresher.status()})

    def watches(self, req):
        return Response.json({"ok": True, "watches": self.watches_payload()})

    def mirrors(self, req):
        return Response.json({"ok": True, "sources": F.host_status()})

    def settings_get(self, req):
        if self.ctx.settings is None:
            return Response.err("设置未初始化", 503)
        return Response.json(settings_payload(self.ctx))

    def feed_all(self, req):
        return self.serve_feed(req, None)

    def feed_one(self, req):
        fid = req.params["fid"]
        if fid.endswith(".xml"):
            fid = fid[:-4]
        return self.serve_feed(req, fid)

    def opml(self, req):
        data = F.build_opml(self.ctx.store, self.ctx.base_url(req))
        return Response(data, "text/x-opml; charset=utf-8")

    # ------------------------------------------------------------ 写接口
    def refresh_start(self, req):
        body = req.json_body()
        ids = body.get("feeds") or None
        started = self.ctx.refresher.start(ids)
        return Response.json({"ok": True, "started": started,
                              **self.ctx.refresher.status()})

    def settings_save(self, req):
        ctx = self.ctx
        if ctx.settings is None:
            return Response.err("设置未初始化", 503)
        body = req.json_body()
        patch = body.get("values") if isinstance(body.get("values"), dict) else body
        vals = ctx.settings.save(patch)
        apply_settings(ctx, vals)
        if ctx.auto is not None:
            ctx.auto.last_try = time.time()          # 改了间隔就重新计时
        return Response.json({"ok": True, "values": vals,
                              "info": settings_payload(ctx)["info"]})

    def settings_reset(self, req):
        ctx = self.ctx
        if ctx.settings is None:
            return Response.err("设置未初始化", 503)
        vals = ctx.settings.reset()
        apply_settings(ctx, vals)
        if ctx.auto is not None:
            ctx.auto.last_try = time.time()
        return Response.json({"ok": True, "values": vals,
                              "info": settings_payload(ctx)["info"]})

    def maintenance(self, req):
        ctx = self.ctx
        body = req.json_body()
        action = (body.get("action") or "clean").lower()
        if action == "vacuum":
            ctx.store.vacuum()
            return Response.json({"ok": True, "action": action,
                                  "stats": ctx.store.stats()})
        vals = ctx.settings.get() if ctx.settings else {}
        removed, expired = ctx.store.prune(
            days=int(vals.get("keepDays") or 30),
            cap=int(vals.get("keepMax") or 30000))
        if body.get("vacuum", True):
            ctx.store.vacuum()
        return Response.json({"ok": True, "action": "clean",
                              "removed": removed, "expiredWatches": expired,
                              "stats": ctx.store.stats(), "counts": ctx.store.counts()})

    def read(self, req):
        body = req.json_body()
        ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
        n = self.ctx.store.set_read(ids, bool(body.get("read", True)))
        return Response.json({"ok": True, "changed": n,
                              "counts": self.ctx.store.counts()})

    def star(self, req):
        body = req.json_body()
        it = self.ctx.store.set_star(body.get("id") or "",
                                     bool(body.get("starred", True)))
        return Response.json({"ok": True, "item": it,
                              "counts": self.ctx.store.counts()})

    def read_all(self, req):
        body = req.json_body()
        n = self.ctx.store.read_all(body.get("feed") or "all")
        return Response.json({"ok": True, "changed": n,
                              "counts": self.ctx.store.counts()})

    def create_feed(self, req):
        return Response.json(self._create_feed(req.json_body()))

    def create_watch(self, req):
        return Response.json(self._create_watch(req.json_body()))

    def scan_watch(self, req):
        wid = req.params["wid"]
        if not self.ctx.store.watch(wid):
            return Response.err("关注不存在", 404)
        res = self.ctx.store.scan_watches()
        return Response.json({"ok": True, "hits": res["per"].get(wid, 0),
                              "watches": self.watches_payload(),
                              "counts": self.ctx.store.counts()})

    def open_window(self, req):
        if self.ctx.ui is None:
            return Response.json({"ok": True, "opened": False,
                                  "reason": "当前实例没有窗口监督器"})
        return Response.json({"ok": True, "opened": self.ctx.ui.request_open()})

    def update_watch(self, req):
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

    def update_feed(self, req):
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

    def delete_watch(self, req):
        ctx = self.ctx
        wid = req.params["wid"]
        if not ctx.store.watch(wid):
            return Response.err("关注不存在", 404)
        ctx.store.delete_watch(wid)
        return Response.json({"ok": True, "watches": self.watches_payload(),
                              "counts": ctx.store.counts()})

    def delete_feed(self, req):
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

    # ------------------------------------------------------------ 内部实现
    def serve_feed(self, req, fid):
        ctx = self.ctx
        if fid and not ctx.store.feed(fid):
            return Response.text("订阅源不存在", 404)
        data = F.build_feed_xml(ctx.store, fid or "all", ctx.base_url(req))
        return Response(data, "application/rss+xml; charset=utf-8",
                        cache="public, max-age=120")

    def _schedule_brief(self):
        """放送表摘要由模块提供；模块没装就返回空壳，侧栏显示 0 而不是报错。"""
        mod = self.ctx.module("schedule")
        return mod.brief() if mod is not None else {
            "total": 0, "today": 0, "updated": 0, "running": False}

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

    def _create_feed(self, body):
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
