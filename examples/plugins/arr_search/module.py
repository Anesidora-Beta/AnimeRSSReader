"""深度搜索模块：把「只能看最新 200 条」这件事补上。

主程序刷新时每个源只保留最新 N 条（``feeds.prepare`` 默认 200、上限 500），
旧帖子一旦滑出窗口就再也回不来了。要找回三年前某版 BDRip，只能去源站翻 ——
这个模块就是那只手：**按需检索、能往历史里走、按需入库**。

三条设计取舍
------------
1. **按需而不入库**：搜索走「现查现给」，不把结果写进主库。理由是老帖量级大、
   命中率低，全存进去会把「全部更新」冲垮。要留的帖由用户点「收藏到本地」，
   这时才写库并打星（``starred=1`` 的条目不受 30 天清理影响）。
2. **源可组合**：一次查询可以同时打 N 个源，结果合并去重后统一排序。
   每个源声明自己的能力，查询层只下推它认得的部分，其余本地兜底。
3. **不占公共路径**：路由全部在 ``/api/search/`` 下，表前缀 ``mod_search_``，
   前端视图走 ``/plug/search/`` —— 符合 ``docs/ARCHITECTURE.md`` §4 的三条边界。
"""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from anime_rss.kernel.http import Response
from anime_rss.module import Module

from . import net
from . import watchbridge
from .model import dedupe, item_key, sort_items
from .providers import DEFAULT_IDS, build as build_providers
from .query import Query
from .storage import SearchStore

KEEP_FEED = "search_keep"
KEEP_NAME = "搜索收藏"


class SearchModule(Module):
    id = "search"
    name = "深度搜索"
    version = "1.0.0"

    views = ({
        "id": "search",
        "title": "深度搜索",
        "icon": "⌕",
        "order": 15,
        "description": "跨源站检索历史帖子，突破「只拉最新 200 条」",
        "script": "view.js",
        "style": "view.css",
    },)
    assets = "static"

    def __init__(self):
        self._store = None            # SearchStore（组合 / 历史）
        self._cfg = {}
        self._health = {}             # 源 id -> 最近一次运行情况
        self._cache = {}              # 结果缓存：签名 -> (expire, payload)
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 生命周期
    def setup(self, ctx):
        ctx.settings.define({
            "group": "search",
            "title": "深度搜索",
            "note": "跨源站检索历史帖子；条数上限只影响列表，不影响能查到多旧。",
            "items": [
                {"k": "searchSources", "type": "text", "label": "默认源",
                 "default": ",".join(DEFAULT_IDS),
                 "desc": "用逗号分隔；留空表示全部。可在搜索页临时改。"},
                {"k": "searchPages", "type": "number", "label": "默认深度（页）",
                 "default": 3, "min": 1, "max": 30,
                 "desc": "每个源往后翻几页；1 页最快，10 页已经是「归档检索」。"},
                {"k": "searchTimeout", "type": "number", "label": "单源超时（秒）",
                 "default": 18, "min": 5, "max": 60},
                {"k": "searchCache", "type": "switch", "label": "结果缓存",
                 "default": True, "desc": "同一查询 90 秒内直接复用，翻页更快。"},
                {"k": "searchKeepStar", "type": "switch", "label": "收藏即加星",
                 "default": True,
                 "desc": "加星条目不受主库 30 天清理影响，旧帖才能留得住。"},
            ],
        }, after="read")

        r = ctx.router
        r.get("/api/search", self.api_search)
        r.get("/api/search/providers", self.api_providers)
        r.get("/api/search/presets", self.api_presets)
        r.post("/api/search/presets", self.api_save_preset)
        r.delete("/api/search/presets/{pid}", self.api_del_preset)
        r.get("/api/search/history", self.api_history)
        r.post("/api/search/history/clear", self.api_clear_history)
        r.post("/api/search/keep", self.api_keep)
        r.post("/api/search/watch", self.api_watch)
        r.get("/api/search/watches", self.api_watches)
        r.post("/api/search/watch/status", self.api_watch_status)
        r.post("/api/search/cache/clear", self.api_clear_cache)

        ctx.every(86400, self._trim, name="search.trim", run_now=False)
        self.apply_settings(ctx.settings.get() if ctx.settings else {})
        self._open_store()

    def teardown(self):
        if self._store is not None:
            self._store.close()
            self._store = None
        net.clear_cache()

    def apply_settings(self, values):
        v = values or {}
        self._cfg = {
            "sources": [x.strip() for x in str(v.get("searchSources")
                                                or ",".join(DEFAULT_IDS)).split(",")
                        if x.strip()],
            "pages": _int(v.get("searchPages"), 3, 1, 30),
            "timeout": _int(v.get("searchTimeout"), 18, 5, 60),
            "cache": bool(v.get("searchCache", True)),
            "keepStar": bool(v.get("searchKeepStar", True)),
        }

    # ------------------------------------------------------------ 内部
    def _open_store(self):
        if self._store is not None:
            return self._store
        data_dir = getattr(self.ctx, "data_dir", "") or "."
        try:
            self._store = SearchStore(os.path.join(data_dir, "search.db"))
        except Exception as e:                       # noqa: BLE001 - 存储坏了只丢历史
            self.say("搜索历史库打不开：%s" % e)
            self._store = None
        return self._store

    def say(self, msg):
        if getattr(self.ctx, "log", None):
            try:
                self.ctx.log("  [search] %s" % msg)
            except Exception:
                pass

    def _providers(self):
        """内置源 + 第三方通过 ``ctx.extra`` 追加的源（同 id 覆盖）。"""
        extra = []
        try:
            extra = list((self.ctx.extra or {}).get("search.providers") or ())
        except Exception:
            extra = []
        return build_providers(extra, store=getattr(self.ctx, "store", None))

    def _trim(self):
        if self._store is not None:
            self._store.trim(200)

    # ------------------------------------------------------------ 搜索核心
    def search(self, query="", src=None, after="", before="", sort="", limit=0,
               pages=0, page=1, mode="", use_cache=True,
               must=None, exclude=None, fansub="", category="",
               size_min=0, size_max=0):
        """供其它模块直接调用的 Python 接口，返回与 HTTP 接口同构的 dict。

        ``ctx.modules.get("search").search("鬼灭 after:2022-01-01")`` 即可，
        不必绕 HTTP —— 这是「本项目也能当插件」那条约束带来的便宜。

        ``must`` / ``exclude`` / ``fansub`` / ``size_min`` 这些是给图形筛选
        面板留的结构化入口，语义和语法里的 ``+词`` / ``-词`` / ``字幕组:`` 一样。
        """
        started = time.time()
        q = Query.parse(query, src=src, after=after, before=before, sort=sort,
                        limit=limit or 30, pages=pages or self._cfg.get("pages", 3),
                        page=page, mode=mode, must=must, exclude=exclude,
                        fansub=fansub, category=category,
                        size_min=size_min, size_max=size_max)

        table = self._providers()
        wanted = q.src or self._cfg.get("sources") or list(DEFAULT_IDS)
        chosen = [table[i] for i in wanted if i in table]
        if not chosen:
            chosen = [table[i] for i in DEFAULT_IDS if i in table]

        sig = q.signature(p.id for p in chosen)
        if use_cache and self._cfg.get("cache", True):
            with self._lock:
                hit = self._cache.get(sig)
                if hit and hit[0] > time.time():
                    payload = dict(hit[1])
                    payload["cached"] = True
                    payload["took"] = int((time.time() - started) * 1000)
                    return payload

        results, health = self._fanout(q, chosen)
        merged = dedupe([it for r in results for it in r])
        kept = [it for it in merged if q.match(it)]
        ordered = sort_items(kept, q.sort, q.terms)
        cut = ordered[:q.limit]
        has_more = bool(len(ordered) > q.limit or any(h.get("hasMore") for h in health))

        payload = {
            "ok": True,
            "query": {
                "raw": q.raw, "sources": [p.id for p in chosen],
                "after": q.after, "before": q.before, "sort": q.sort,
                "limit": q.limit, "pages": q.pages, "page": q.page,
                "anyTerms": list(q.any_terms), "must": list(q.must),
                "exclude": list(q.exclude), "fansub": q.fansub,
                "category": q.category, "unknown": list(q.unknown),
                "sizeMin": q.size_min, "sizeMax": q.size_max,
            },
            "items": [_public(it) for it in cut],
            "count": len(cut),
            "raw": len(merged),
            "matched": len(kept),
            "hasMore": has_more,
            "cached": False,
            "took": int((time.time() - started) * 1000),
            "sources": health,
            "explain": [{"id": p.id, "name": p.name, "caps": sorted(p.caps),
                         **q.explain(p.caps)} for p in chosen],
        }

        if self._cfg.get("cache", True):
            with self._lock:
                if len(self._cache) > 40:
                    self._cache.clear()
                self._cache[sig] = (time.time() + 90, payload)

        try:
            self.ctx.emit("search.done", {
                "query": q.raw, "count": len(cut),
                "sources": [p.id for p in chosen], "took": payload["took"]})
        except Exception:
            pass
        return payload

    def _fanout(self, q, chosen):
        """并发打各个源，每个源内部按 ``q.pages`` 顺序翻页。"""
        if not chosen:
            return [], []
        overall = self._cfg.get("timeout", 18) + 6

        ex = ThreadPoolExecutor(max_workers=min(6, len(chosen)))
        futs = {ex.submit(self._one, p, q): p for p in chosen}
        done, pending = wait(futs.keys(), timeout=overall + q.pages * 2)

        items, health = [], []
        for fut, p in futs.items():
            if fut in pending:
                fut.cancel()
                health.append({"id": p.id, "name": p.name, "ok": False,
                               "ms": overall * 1000, "count": 0,
                               "error": "超时（%ds）" % overall, "hasMore": False})
                continue
            try:
                got, stat = fut.result()
            except Exception as e:                   # noqa: BLE001
                got, stat = [], {"id": p.id, "name": p.name, "ok": False,
                                 "ms": 0, "count": 0, "error": "%s: %s"
                                 % (type(e).__name__, e), "hasMore": False}
            items.append(got)
            health.append(stat)
            with self._lock:
                self._health[p.id] = dict(stat, ts=int(time.time()))
        ex.shutdown(wait=False)
        health.sort(key=lambda h: (not h["ok"], -int(h.get("count") or 0)))
        return items, health

    def _one(self, provider, q):
        """一个源：从 ``q.page`` 开始翻 ``q.pages`` 页，遇到 has_more=False 就停。"""
        began = time.time()
        out, note, more = [], "", False
        try:
            budget = provider.page_budget(q, q.pages)
        except Exception:                            # noqa: BLE001 - 老插件没实现就按默认
            budget = q.pages
        last_page = q.page + max(1, budget) - 1
        limit_page = min(last_page, int(getattr(provider, "max_pages", last_page) or 1))
        page = q.page
        error = ""
        while page <= limit_page:
            try:
                pg = provider.search(q, page)
            except Exception as e:                   # noqa: BLE001 - 单源失败不影响别的源
                error = "%s: %s" % (type(e).__name__, e)
                break
            out.extend(pg.items)
            note = pg.note or note
            more = pg.has_more
            if not pg.has_more:
                break
            page += 1
        ok = not error or bool(out)
        return out, {
            "id": provider.id, "name": provider.name,
            "ok": ok, "ms": int((time.time() - began) * 1000),
            "count": len(out), "pages": max(0, page - q.page + (1 if out else 0)),
            "hasMore": bool(more), "note": note, "error": error,
        }

    # ------------------------------------------------------------ 收藏入库
    def _keep(self, items):
        """把选中的帖子写进一个「搜索收藏」频道，并加星抗清理。"""
        store = getattr(self.ctx, "store", None)
        if store is None:
            raise RuntimeError("没有可用的本地库")
        if not store.feed(KEEP_FEED):
            store.add_feed(KEEP_FEED, KEEP_NAME, "custom", {}, "")
            # enabled=0：这个频道只装手工收藏的老帖，不参与自动抓取
            store.update_feed(KEEP_FEED, enabled=False)

        payload = []
        for it in items or ():
            payload.append({
                "source": it.get("source") or "search",
                "title": it.get("title") or "",
                "link": it.get("link") or "",
                "magnet": it.get("magnet") or "",
                "torrent": it.get("torrent") or "",
                "infohash": it.get("infohash") or "",
                "size": it.get("size") or "",
                "seeders": it.get("seeders"),
                "pubdate": it.get("pubdate") or "",
                "ts": int(it.get("ts") or 0),
                "category": it.get("category") or "",
                "author": it.get("author") or "",
                "summary": it.get("summary") or "",
                "links": [],
            })
        added = store.upsert_items(KEEP_FEED, payload)

        starred = 0
        if self._cfg.get("keepStar", True):
            for it in payload:
                try:
                    store.set_star(item_key(it), True)
                    starred += 1
                except Exception:                    # noqa: BLE001 - 星标失败不影响入库
                    pass
        return {"added": added, "starred": starred, "feed": KEEP_FEED,
                "feedName": KEEP_NAME}

    # ------------------------------------------------------------ HTTP
    def api_search(self, req):
        kw = self._query_kwargs(req)
        raw = req.q("q")
        q = Query.parse(raw, **kw)
        if q.empty and not q.src:
            return Response.err("至少给一个关键词，或指定时间范围 / 数据源")
        # 分页数用解析后的值再传一遍：``search()`` 对空串有自己的默认（30），
        # 而 Query 的默认是 60，不传会悄悄把默认条数改小。
        data = self.search(raw, **dict(kw, limit=q.limit, pages=q.pages, page=q.page))
        if self._store is not None and raw.strip():
            try:
                self._store.add_history(raw, data["query"]["sources"],
                                        data["count"], data["took"])
            except Exception:
                pass
        return Response.json(data)

    def _query_kwargs(self, req):
        """把「语法串 + 图形面板」两套入口合并成一份参数。

        面板传的是 ``must`` / ``exclude`` / ``fansub`` / ``sizeMin`` 这些
        结构化字段，和语法里的 ``+词`` / ``-词`` / ``字幕组:`` / ``size>``
        等价 —— 两条路最后落在同一个 Query 上，所以怎么混用都不打架。
        """
        return {
            "src": req.q("src"), "after": req.q("after"), "before": req.q("before"),
            "sort": req.q("sort"), "limit": req.q("limit"), "pages": req.q("pages"),
            "page": req.q("page"), "mode": req.q("mode"),
            "must": req.q("must"), "exclude": req.q("exclude"),
            "fansub": req.q("fansub"), "category": req.q("category"),
            "size_min": req.q("sizeMin") or req.q("size_min") or 0,
            "size_max": req.q("sizeMax") or req.q("size_max") or 0,
        }

    def api_providers(self, req):
        table = self._providers()
        out = []
        for pid, p in table.items():
            d = p.describe()
            d["health"] = self._health.get(pid, {})
            out.append(d)
        order = {pid: i for i, pid in enumerate(DEFAULT_IDS)}
        out.sort(key=lambda d: (order.get(d["id"], 99), d["id"]))
        return Response.json({
            "ok": True, "providers": out,
            "defaults": self._cfg.get("sources") or list(DEFAULT_IDS),
            "pages": self._cfg.get("pages", 3),
            "net": net.cache_stats(),
            "tips": _TIPS,
        })

    def api_presets(self, req):
        st = self._open_store()
        return Response.json({
            "ok": True,
            "presets": st.presets() if st else [],
            "history": st.history(20) if st else [],
        })

    def api_save_preset(self, req):
        st = self._open_store()
        if st is None:
            return Response.err("历史库不可用", 500)
        body = req.json_body()
        p = st.save_preset(body.get("name") or "", body.get("query") or "",
                           body.get("sources") or (), body.get("mode") or "deep",
                           body.get("sort") or "date")
        return Response.json({"ok": True, "preset": p, "presets": st.presets()})

    def api_del_preset(self, req):
        st = self._open_store()
        if st is None:
            return Response.err("历史库不可用", 500)
        ok = st.delete_preset(req.params["pid"])
        return Response.json({"ok": ok, "presets": st.presets()})

    def api_history(self, req):
        st = self._open_store()
        try:
            limit = max(1, min(int(req.q("limit") or 50), 200))
        except ValueError:
            limit = 50
        return Response.json({"ok": True, "history": st.history(limit) if st else []})

    def api_clear_history(self, req):
        st = self._open_store()
        n = st.clear_history() if st else 0
        return Response.json({"ok": True, "removed": n})

    def api_keep(self, req):
        body = req.json_body()
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            return Response.err("没有要收藏的条目")
        if len(items) > 200:
            items = items[:200]
        try:
            return Response.json({"ok": True, **self._keep(items)})
        except Exception as e:                       # noqa: BLE001
            return Response.err("收藏失败：%s" % e, 500)

    def api_clear_cache(self, req):
        with self._lock:
            self._cache.clear()
        net.clear_cache()
        return Response.json({"ok": True})

    # ------------------------------------------------------------ 接入关注
    def api_watch(self, req):
        """把搜到的条目挂进主程序的关注列表。

        两种用法（前端两个按钮各用一种）：

        * 给了 ``items``：把这几条挂上去（结果卡片上的「关注」按钮）；
        * 给 ``all=true`` 不给 items：按同一套参数重跑一次检索，把命中的全部挂上
          （顶部「整次搜索存为关注」）。走缓存，所以几乎不额外请求源站。

        真正干活的是 ``watchbridge.link()``，它复刻了主程序自己补搜老番的
        ``upsert_items(link=False) + attach_watch()`` 四行 —— 所以关注页里看到的
        和主程序自己补回来的一模一样。
        """
        st = getattr(self.ctx, "store", None)
        if st is None:
            return Response.err("主程序的数据存储不可用，关注功能需要宿主环境", 503)

        body = req.json_body() or {}
        kw = {
            "src": body.get("src") if isinstance(body.get("src"), str)
            else ",".join(body.get("src") or ()),
            "after": body.get("after") or "", "before": body.get("before") or "",
            "sort": body.get("sort") or "", "pages": body.get("pages") or 0,
            "must": body.get("must"), "exclude": body.get("exclude"),
            "fansub": body.get("fansub") or "", "category": body.get("category") or "",
            "size_min": body.get("sizeMin") or 0, "size_max": body.get("sizeMax") or 0,
        }
        raw = str(body.get("q") or "")
        q = Query.parse(raw, **kw)

        items = body.get("items")
        if not items and body.get("all"):
            # 重跑一次拿全集：缓存还在的话这一步是毫秒级
            got = self.search(raw, **dict(kw, limit=300, pages=q.pages or 3))
            items = got.get("items") or []
        if not isinstance(items, list) or not items:
            return Response.err("没有要加入关注的条目")

        try:
            # items 模式 = 点了某条结果的「关注」，只想盯这部番：
            # loose 让 1080p 这类规格词不进关注规则（整批存关注才严格照抄条件）。
            res = watchbridge.link(st, q, items[:300],
                                   name=body.get("name") or "",
                                   loose=not body.get("all"))
        except watchbridge.WatchBridgeError as e:
            return Response.err(str(e), 400)
        except Exception as e:                       # noqa: BLE001
            return Response.err("加入关注失败：%s" % e, 500)

        self.say("加入关注「%s」：挂上 %d 条" % (res["watch"]["name"], res["attached"]))
        return Response.json(res)

    def api_watches(self, req):
        """列出主程序已有的关注，供前端做「挂到哪个关注」的选择。"""
        st = getattr(self.ctx, "store", None)
        if st is None:
            return Response.json({"ok": True, "watches": []})
        out = []
        for w in st.watches():
            out.append({
                "id": w.get("id"), "name": w.get("name"),
                "include": w.get("include"), "exclude": w.get("exclude"),
                "fansub": w.get("fansub"), "sources": w.get("sources"),
                "enabled": bool(w.get("enabled")),
                "total": w.get("total", 0), "unread": w.get("unread", 0),
            })
        return Response.json({"ok": True, "watches": out})

    def api_watch_status(self, req):
        """这批结果里哪些已经在关注列表里 —— 前端拿它把按钮点亮。

        只读：查不到就当没关注，不影响搜索本身。
        """
        st = getattr(self.ctx, "store", None)
        body = req.json_body() or {}
        items = body.get("items") or []
        if not isinstance(items, list) or not items:
            return Response.json({"ok": True, "watched": {}})
        try:
            return Response.json({"ok": True,
                                  "watched": watchbridge.watched_map(st, items[:300])})
        except Exception as e:                       # noqa: BLE001
            return Response.json({"ok": True, "watched": {}, "error": str(e)})


# ---------------------------------------------------------------- 工具

def _int(value, default, lo=None, hi=None):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _public(it):
    """只把前端要用的字段发出去，顺手补一个「在哪几个源出现过」。"""
    return {
        "id": it.get("id") or "",
        "source": it.get("source") or "",
        "sourceName": it.get("sourceName") or it.get("source") or "",
        "title": it.get("title") or "",
        "link": it.get("link") or "",
        "magnet": it.get("magnet") or "",
        "torrent": it.get("torrent") or "",
        "infohash": it.get("infohash") or "",
        "size": it.get("size") or "",
        "sizeBytes": int(it.get("sizeBytes") or 0),
        "seeders": it.get("seeders"),
        "pubdate": it.get("pubdate") or "",
        "ts": int(it.get("ts") or 0),
        "category": it.get("category") or "",
        "author": it.get("author") or "",
        "summary": it.get("summary") or "",
        "local": bool(it.get("local")),
        "alsoIn": it.get("alsoIn") or [],
    }


_TIPS = [
    "空格是「任一命中」，+ 是「必须含」，- 是「排除」",
    "字幕组:桜都  /  类型:动画  /  src:dmhy,nyaa",
    "after:2022-01-01 也可以写 after:3y（三年前）",
    "size>1G 只留大于 1G 的；sort:seeders 按做种数排",
    "找不到就加深度：pages:10，或直接圈时间区间",
]
