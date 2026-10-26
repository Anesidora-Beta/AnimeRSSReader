# -*- coding: utf-8 -*-
"""聚合引擎：抓取 5 个资源站或任意自定义 RSS，统一清洗后写入本地库。

上半部分是站点聚合（复用上游 sources.py 的适配层），
下半部分是通用 RSS / Atom 解析，让「阅读器」也能订阅任何其它订阅源。
"""

import gzip
import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import sources as S
import textutil as T

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
CACHE_TTL = 300

ET.register_namespace("atom", "http://www.w3.org/2005/Atom")


# ---------------------------------------------------------------- 网络

def _force_ipv4():
    """部分云环境只解析到 IPv6 会报 Network is unreachable，收敛到 IPv4 优先。"""
    orig = socket.getaddrinfo

    def v4(host, port, family=0, type=0, proto=0, flags=0):
        try:
            res = orig(host, port, socket.AF_INET, type, proto, flags)
            if res:
                return res
        except socket.gaierror:
            pass
        return orig(host, port, family, type, proto, flags)

    socket.getaddrinfo = v4


try:
    _force_ipv4()
except Exception:
    pass

_cache = {}
_cache_lock = threading.Lock()


def fetch_bytes(url, timeout=25):
    """带 TTL 缓存的抓取；失败时回退到过期缓存。"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit["ts"] < CACHE_TTL:
            return hit["data"]
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml,"
                  " application/json, text/xml, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read()
            enc = (r.headers.get("Content-Encoding") or "").lower()
        if enc == "gzip":
            data = gzip.decompress(data)
        elif enc == "deflate":
            data = zlib.decompress(data, -zlib.MAX_WBITS)
        with _cache_lock:
            _cache[url] = {"ts": now, "data": data}
        return data
    except Exception:
        with _cache_lock:
            hit = _cache.get(url)
            if hit:
                return hit["data"]
        raise


# ---------------------------------------------------------------- 站点频道

def params_for(feed):
    """订阅源里存的 JSON -> sources.py 需要的查询参数（全部是字符串）"""
    p = dict(feed.get("params") or {})
    out = {}
    for k in ("src", "keyword", "include", "exclude", "cat", "limit",
              "sort_id", "team_id", "dmhy_all", "dmhy_cat", "fansub",
              "ag_pages", "ag_all", "ag_cat"):
        v = p.get(k)
        if isinstance(v, (list, tuple)):
            v = ",".join(str(x).strip() for x in v if str(x).strip())
        v = ("" if v is None else str(v)).strip()
        if v:
            out[k] = v
    if "src" not in out:
        out["src"] = ",".join(S.DEFAULT_IDS)
    out.setdefault("limit", "200")
    return out


def _normalize(items):
    """补齐派生字段：正文外链、摘要、磁力瘦身"""
    for it in items:
        html_src = it.get("desc_html") or ""
        it["links"] = T.extract_links(html_src)
        it["summary"] = T.clean_text(html_src, 500)
        it["magnet"] = T.slim_magnet(it.get("magnet") or "", it.get("title") or "")
        if not it.get("infohash") and it["magnet"]:
            it["infohash"] = T.infohash_of(it["magnet"])
        it["ts"] = T.to_ts(it.get("pubdate"))
    return items


def _filter(items, params):
    """按 include / exclude / keyword / cat 过滤（与原项目一致的语义）"""
    include = [w.strip().lower() for w in (params.get("include") or "").split(",") if w.strip()]
    exclude = [w.strip().lower() for w in (params.get("exclude") or "").split(",") if w.strip()]
    cats = [w.strip() for w in (params.get("cat") or "").split(",") if w.strip()]
    keyword = (params.get("keyword") or "").strip().lower()
    out = []
    for it in items:
        low = (it.get("title") or "").lower()
        if include and not any(w in low for w in include):
            continue
        if exclude and any(w in low for w in exclude):
            continue
        if cats and it.get("source") == "dmhy" and it.get("category") not in cats:
            continue
        if keyword and it.get("source") == "bangumi" and keyword not in low:
            continue
        out.append(it)
    return out


def _dedupe(items):
    """双键去重：infohash 相同或标题相同都算同一条，保留信息最全的。"""
    n = len(items)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    def quality(it):
        s = 0
        s += 4 if it.get("magnet") else 0
        s += 2 if it.get("torrent") else 0
        s += 1 if it.get("size") else 0
        s += 1 if it.get("seeders") is not None else 0
        return s

    by_hash, by_title = {}, {}
    for i, it in enumerate(items):
        h = (it.get("infohash") or "").lower()
        if h:
            if h in by_hash:
                union(by_hash[h], i)
            else:
                by_hash[h] = i
        t = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (it.get("title") or "").lower())
        if t:
            if t in by_title:
                union(by_title[t], i)
            else:
                by_title[t] = i

    groups, order = {}, []
    for i, it in enumerate(items):
        r = find(i)
        if r not in groups:
            groups[r] = it
            order.append(r)
            continue
        a, b = groups[r], it
        keep, drop = (a, b) if quality(a) >= quality(b) else (b, a)
        for f in ("size", "torrent", "seeders", "category"):
            if not keep.get(f) and drop.get(f):
                keep[f] = drop[f]
        if not keep.get("magnet") and drop.get("magnet"):
            keep["magnet"] = drop["magnet"]
            keep["infohash"] = drop.get("infohash") or keep.get("infohash")
        groups[r] = keep
    return [groups[r] for r in order]


# 镜像健康状态：host -> {"ok": 最近成功时间, "fail": 连续失败次数, "downUntil": 冷却截止}
# 国内直连花园主站常常直接超时，失败过的入口先冷处理，下次优先用能通的。
_host_state = {}
_host_lock = threading.Lock()
_HOST_COOLDOWN = 600          # 失败后冷却 10 分钟，仍有别的入口可用时不浪费时间重试

STATE_PATH = ""              # 由 app.py 启动时指向 data/hosts.json，用来跨重启记住入口状态
_state_loaded = False


def _load_host_state():
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    if not STATE_PATH:
        return
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            with _host_lock:
                for k, v in data.items():
                    if isinstance(v, dict):
                        _host_state.setdefault(k, {}).update(v)
    except Exception:
        pass


def _save_host_state():
    if not STATE_PATH:
        return
    try:
        d = os.path.dirname(STATE_PATH)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with _host_lock:
            snap = {k: dict(v) for k, v in _host_state.items()}
        tmp = STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False)
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _host_label(url):
    return urllib.parse.urlsplit(url).netloc or url


def host_mark(host, ok, error=""):
    now = time.time()
    with _host_lock:
        st = _host_state.setdefault(host, {})
        if ok:
            st["ok"] = now
            st["fail"] = 0
            st["downUntil"] = 0
            st["error"] = ""
        else:
            st["fail"] = st.get("fail", 0) + 1
            st["downUntil"] = now + _HOST_COOLDOWN
            st["error"] = str(error)[:160]
    _save_host_state()


def _host_rank(sid):
    """可用入口优先、最近成功过的优先，冷却中的排到最后（全部挂掉时还能兜底）"""
    _load_host_state()
    now = time.time()
    with _host_lock:
        def key(h):
            st = _host_state.get(h) or {}
            return (1 if st.get("downUntil", 0) > now else 0,
                    -st.get("ok", 0),
                    st.get("fail", 0))
        return sorted(S.hosts_of(sid), key=key)


def host_status():
    """给页面展示：每个入口现在能不能用"""
    _load_host_state()
    out = []
    now = time.time()
    with _host_lock:
        st = {k: dict(v) for k, v in _host_state.items()}
    for s in S.SOURCES:
        rows = []
        for h in S.hosts_of(s["id"]):
            d = st.get(h) or {}
            rows.append({
                "host": _host_label(h),
                "url": h,
                "ok": bool(d.get("ok")),
                "fail": d.get("fail", 0),
                "down": d.get("downUntil", 0) > now,
                "lastOk": d.get("ok", 0),
                "error": d.get("error", ""),
            })
        out.append({"id": s["id"], "name": s["name"], "hosts": rows})
    return out


def _mirror_fallback(sid, params):
    """直连全挂时走 AnimeGarden 镜像：把 provider=dmhy 的资源和直连结果等价看待。"""
    if sid != "dmhy":
        return []
    base = S.AG_BASE
    try:
        pages = int(params.get("ag_pages") or 2)
    except (TypeError, ValueError):
        pages = 2
    got = []
    for n in range(1, max(1, min(pages, 5)) + 1):
        url = f"{base}/resources/dmhy?page={n}&pageSize=100"
        raw = fetch_bytes(url, timeout=20)
        got.extend(S.parse_animegarden(raw, params))
    for it in got:
        # 内容本来就是花园的，归到 dmhy 频道，用户看到的仍是「动漫花园」
        it["source"] = "dmhy"
    return _normalize(got)


def _collect_one(sid, params):
    """抓一个源：按入口优先级逐个尝试，返回 (items, errors)"""
    name = S.BY_ID[sid]["name"]
    errors = []
    reachable_empty = False
    short_timeout = 12 if len(S.hosts_of(sid)) > 1 else 25

    for host in _host_rank(sid):
        urls = S.candidate_urls(sid, params, host)
        try:
            got = []
            for u in urls:
                raw = fetch_bytes(u, timeout=short_timeout)
                got.extend(S.parse_items(sid, raw, params))
            host_mark(host, True)
            if got:
                if errors:
                    errors.append(f"{name}: 已切换到 {_host_label(host)}")
                return _normalize(got), errors
            reachable_empty = True          # 通了但没数据，可能确实没更新
        except Exception as e:
            host_mark(host, False, f"{e.__class__.__name__} {e}")
            errors.append(f"{name}（{_host_label(host)}）: {e.__class__.__name__} {e}")
            continue

    if reachable_empty:
        return [], errors

    # 直连全部失败 -> 花园镜像兜底
    try:
        got = _mirror_fallback(sid, params)
        if got:
            errors.append(f"{name}: 直连不可用，已用镜像 AnimeGarden 取到 {len(got)} 条")
            return got, errors
    except Exception as e:
        errors.append(f"{name}: 镜像 AnimeGarden 也失败（{e.__class__.__name__}）")
    return [], errors


def collect_builtin(feed):
    """抓取内置频道（可能是多个站合并），每个源内部再做镜像切换"""
    params = params_for(feed)
    sids = S.resolve_ids(params.get("src"))

    items, errors = [], []
    workers = min(6, max(1, len(sids)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_collect_one, sid, params): sid for sid in sids}
        for f in as_completed(futs):
            try:
                got, errs = f.result()
                items.extend(got)
                errors.extend(errs)
            except Exception as e:
                errors.append(f"{S.BY_ID[futs[f]]['name']}: {e.__class__.__name__} {e}")
    return items, errors


# ---------------------------------------------------------------- 通用 RSS

def _ln(tag):
    """去掉命名空间，只留小写本地名"""
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(node, *names):
    for ch in node:
        if _ln(ch.tag) in names:
            return ch
    return None


def _text(node, *names):
    ch = _child(node, *names)
    if ch is None:
        return ""
    return "".join(ch.itertext()).strip()


def parse_generic(raw, feed_id, fallback_title=""):
    """解析任意 RSS 2.0 / Atom / RDF，返回统一 item 列表。

    下载型字段（磁力、种子、做种数、体积）能认出来就带上，
    认不出就当普通文章处理，阅读器照常显示正文与链接。
    """
    root = ET.fromstring(raw)
    out = []
    rname = _ln(root.tag)

    if rname == "feed":                      # Atom
        entries = [n for n in root if _ln(n.tag) == "entry"]
        channel_title = _text(root, "title")
        for e in entries:
            title = _text(e, "title")
            link = ""
            enclosure = ""
            for ln in [n for n in e if _ln(n.tag) == "link"]:
                rel = (ln.get("rel") or "alternate").lower()
                href = ln.get("href") or ""
                if rel == "enclosure" and href:
                    enclosure = href
                elif rel == "alternate" and href and not link:
                    link = href
                elif href and not link:
                    link = href
            guid = _text(e, "id") or link or title
            body = ""
            for name in ("content", "summary"):
                ch = _child(e, name)
                if ch is not None:
                    txt = "".join(ch.itertext())
                    if len(txt) > len(body):
                        body = txt
            pub = _text(e, "published", "updated", "date")
            anode = _child(e, "author")
            author = (_text(anode, "name") if anode is not None else "") or _text(e, "author")
            out.append(_mk_item(feed_id, title, link, guid, body, pub, author,
                                enclosure, ""))
        return out, channel_title

    # RSS 2.0 / RDF
    channel = None
    for n in root.iter():
        if _ln(n.tag) == "channel":
            channel = n
            break
    if channel is None:
        channel = root
    channel_title = _text(channel, "title")

    nodes = [n for n in channel if _ln(n.tag) == "item"]
    if not nodes:                                   # RDF 把 item 放在根节点下
        nodes = [n for n in root if _ln(n.tag) == "item"]

    for e in nodes:
        title = _text(e, "title")
        link = _text(e, "link")
        guid = _text(e, "guid", "id") or link or title
        body = _text(e, "encoded", "description", "summary", "content")
        pub = _text(e, "pubdate", "date", "published", "updated", "issued")
        author = _text(e, "author", "creator")
        enclosure = ""
        etype = ""
        enc = _child(e, "enclosure")
        if enc is not None:
            enclosure = enc.get("url") or ""
            etype = (enc.get("type") or "").lower()
        size = _text(e, "size", "contentlength")
        seeders = _text(e, "seeders")
        infohash = _text(e, "infohash")
        out.append(_mk_item(feed_id, title, link, guid, body, pub, author,
                            enclosure, size, seeders, infohash, etype))
    return out, channel_title


def _mk_item(feed_id, title, link, guid, body, pub, author, enclosure,
             size="", seeders="", infohash="", enclosure_type=""):
    """把任意订阅源的一条内容映射成统一 item 结构"""
    magnet = T.find_magnet(body) or T.find_magnet(enclosure)
    torrent = ""
    if enclosure and not enclosure.lower().startswith("magnet:"):
        low = enclosure.lower().split("?")[0]
        if ("bittorrent" in (enclosure_type or "")) or low.endswith(".torrent") \
                or "torrent" in enclosure.lower() or "/download" in enclosure.lower():
            torrent = enclosure

    ih = T.norm_infohash(infohash) if infohash else (T.infohash_of(magnet) or T.infohash_of(torrent))
    seed = None
    try:
        seed = int(str(seeders).strip()) if str(seeders).strip() else None
    except (TypeError, ValueError):
        seed = None

    return {
        "source": feed_id,
        "title": title,
        "link": link,
        "magnet": magnet,
        "torrent": torrent,
        "infohash": ih,
        "size": (size or "").strip(),
        "seeders": seed,
        "pubdate": pub,
        "category": "",
        "author": (author or "").strip(),
        "desc_html": body or "",
        "guid": guid,
    }


def collect_custom(feed):
    url = (feed.get("url") or "").strip()
    if not url:
        raise ValueError("订阅地址为空")
    raw = fetch_bytes(url)
    items, _chan = parse_generic(raw, feed["id"])
    return _normalize(items), []


# ---------------------------------------------------------------- 刷新

def prepare(items, params):
    items = _filter(items, params)
    items = _dedupe(items)
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    try:
        limit = int(params.get("limit") or 200)
    except ValueError:
        limit = 200
    return items[:max(1, min(limit, 500))]


class Refresher:
    """后台刷新任务，前端轮询 /api/refresh 拿进度。"""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.state = {
            "running": False, "feed": "", "done": 0, "total": 0,
            "added": 0, "kept": 0, "errors": [], "started": 0,
            "finished": 0, "message": "就绪", "watchHits": 0,
        }

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self, feed_ids=None):
        with self.lock:
            if self.state["running"]:
                return False
            self.state.update({
                "running": True, "done": 0, "total": 0, "added": 0,
                "kept": 0, "errors": [], "started": time.time(),
                "finished": 0, "message": "准备中…", "feed": "", "watchHits": 0,
            })
        t = threading.Thread(target=self._run, args=(feed_ids,), daemon=True)
        t.start()
        return True

    def refresh_sync(self, feed_ids=None):
        self._run(feed_ids)
        return self.status()

    def search_for_watch(self, w, limit=100):
        """关注本地一条都没命中时，去 AnimeGarden 在线搜一次。

        订阅源的 RSS 只有最近几十条，老番或冷门番根本没收录，
        只靠本地匹配会出现「关注了却永远空着」的情况。
        """
        words = [x for x in str(w.get("include") or "").split() if x]
        if not words:
            return []
        q = " ".join(words[:3])
        url = (f"{S.AG_BASE}/resources?search={urllib.parse.quote(q)}"
               f"&page=1&pageSize={limit}")
        raw = fetch_bytes(url, timeout=20)
        return _normalize(S.parse_animegarden(raw, {"ag_all": "1"}))

    def _run(self, feed_ids=None):
        try:
            feeds = [f for f in self.store.feeds(only_enabled=True)
                     if not feed_ids or f["id"] in feed_ids]
            with self.lock:
                self.state["total"] = len(feeds)
            for i, feed in enumerate(feeds):
                with self.lock:
                    self.state["feed"] = feed["title"]
                    self.state["message"] = f"抓取 {feed['title']}…"
                try:
                    if feed["kind"] == "custom":
                        items, errors = collect_custom(feed)
                    else:
                        items, errors = collect_builtin(feed)
                        items = prepare(items, params_for(feed))
                        if errors:
                            # 有源失败但也有数据时，仍然写入并记录错误
                            pass
                    if feed["kind"] == "builtin":
                        added = self.store.upsert_items(feed["id"], items)
                    else:
                        added = self.store.upsert_items(feed["id"], items[:300])
                    self.store.feed_stat(feed["id"], True)
                    with self.lock:
                        self.state["added"] += added
                        self.state["kept"] += len(items)
                        self.state["errors"] += errors
                        self.state["message"] = f"{feed['title']} +{added}"
                except Exception as e:
                    msg = f"{feed['title']}: {e.__class__.__name__} {e}"
                    self.store.feed_stat(feed["id"], False, msg)
                    with self.lock:
                        self.state["errors"].append(msg)
                        self.state["message"] = msg
                with self.lock:
                    self.state["done"] = i + 1
            try:
                self.store.prune()
            except Exception:
                pass
            # 抓完再跑关注匹配：新条目和老条目（关注是后加的）都能命中
            hit = 0
            try:
                with self.lock:
                    self.state["message"] = "匹配关注列表…"
                hit = self.store.scan_watches().get("hits", 0)
            except Exception as e:
                with self.lock:
                    self.state["errors"].append(f"关注匹配失败: {e}")

            # 本地一条都没命中的关注，去 AnimeGarden 在线补搜（新关注 30 天内才做）
            try:
                now = time.time()
                missing = [w for w in self.store.watches()
                           if w["enabled"] and not w.get("total")
                           and now - (w.get("created") or 0) < 30 * 86400]
                if missing:
                    with self.lock:
                        self.state["message"] = "为关注补搜…"
                    filled = 0
                    for w in missing:
                        try:
                            got = self.search_for_watch(w)
                        except Exception:
                            continue
                        if not got:
                            continue
                        self.store.upsert_items(None, got, link=False)
                        filled += self.store.attach_watch(w["id"], got)
                    if filled:
                        hit += filled
            except Exception as e:
                with self.lock:
                    self.state["errors"].append(f"关注补搜失败: {e}")
            with self.lock:
                self.state["watchHits"] = hit
                self.state["message"] = (
                    f"完成，新增 {self.state['added']} 条"
                    + (f"，关注命中 {hit} 条" if hit else "")
                    + (f"，{len(self.state['errors'])} 个源出错"
                       if self.state["errors"] else "")
                )
        finally:
            with self.lock:
                self.state["running"] = False
                self.state["finished"] = time.time()


# ---------------------------------------------------------------- 导出

def build_feed_xml(store, feed_id, base_url=""):
    """把库里已有条目导出成标准 RSS，方便丢进别的阅读器。"""
    feed = store.feed(feed_id) if feed_id not in ("", "all") else None
    items = store.query_items(feed=feed_id or "all", limit=200)
    channel_title = feed["title"] if feed else "动漫资源 · 全部更新"

    root = ET.Element("rss", {"version": "2.0"})
    ch = ET.SubElement(root, "channel")
    ET.SubElement(ch, "title").text = channel_title
    ET.SubElement(ch, "link").text = base_url or "http://localhost/"
    ET.SubElement(ch, "description").text = (
        "由 「动漫资源 RSS 阅读器」 聚合生成；每条自带磁力链接 / 种子直链、帖子页与正文外链。")
    ET.SubElement(ch, "language").text = "zh-cn"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(
        datetime.now(timezone(timedelta(hours=8))))
    ET.SubElement(ch, "generator").text = "anime-rss-reader"
    ET.SubElement(ch, "ttl").text = "30"
    ET.SubElement(ch, "{http://www.w3.org/2005/Atom}link", {
        "href": base_url, "rel": "self", "type": "application/rss+xml"})

    for it in items:
        e = ET.SubElement(ch, "item")
        ET.SubElement(e, "title").text = it["title"]
        ET.SubElement(e, "link").text = it["link"] or it["magnet"]
        ET.SubElement(e, "guid", {"isPermaLink": "false"}).text = it["id"]
        if it["pubdate"]:
            ET.SubElement(e, "pubDate").text = it["pubdate"]
        elif it["ts"]:
            ET.SubElement(e, "pubDate").text = T.rfc822(it["ts"])
        name = S.BY_ID.get(it["source"], {}).get("name") or it["source"]
        ET.SubElement(e, "category").text = it["category"] or name
        ET.SubElement(e, "description").text = _desc_html(it, name)
        enc = it["magnet"] or it["torrent"]
        if enc:
            ET.SubElement(e, "enclosure", {
                "url": enc, "type": "application/x-bittorrent", "length": "0"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _desc_html(it, src_name):
    import html as H

    esc = H.escape
    bits = [f'<b style="color:#0969da">{esc(src_name)}</b>']
    if it["category"]:
        bits.append(esc(it["category"]))
    if it["size"]:
        bits.append(esc(it["size"]))
    if it["seeders"] is not None:
        bits.append(f"做种 {it['seeders']}")
    h = [f'<div style="font:14px/1.65 -apple-system,Segoe UI,Roboto,sans-serif;color:#1f2328">'
         f'<p style="margin:0 0 10px;color:#57606a;font-size:13px">{" · ".join(bits)}</p>']
    if it["magnet"]:
        h.append('<p style="margin:0 0 6px;font-size:13px"><b>磁力链接</b><br>'
                 f'<a href="{esc(it["magnet"], quote=True)}">▶ 点此打开</a></p>'
                 f'<p style="margin:0 0 12px;word-break:break-all;color:#57606a;'
                 f'font-size:11.5px">{esc(it["magnet"])}</p>')
    if it["torrent"]:
        h.append(f'<p style="margin:0 0 12px;font-size:13px"><b>种子文件</b> '
                 f'<a href="{esc(it["torrent"], quote=True)}">下载 .torrent</a></p>')
    if it["link"]:
        h.append(f'<p style="margin:0 0 12px;font-size:13px"><b>帖子页</b> '
                 f'<a href="{esc(it["link"], quote=True)}">{esc(it["link"])}</a></p>')
    for u in it["links"][:8]:
        h.append(f'<p style="margin:0 0 4px;font-size:12.5px">'
                 f'<a href="{esc(u, quote=True)}">{esc(u[:80])}</a></p>')
    if it["summary"]:
        h.append(f'<p style="margin:10px 0 0;color:#57606a;font-size:12.5px;'
                 f'white-space:pre-wrap">{esc(it["summary"][:600])}</p>')
    h.append("</div>")
    return "".join(h)


def build_opml(store, base_url=""):
    root = ET.Element("opml", {"version": "2.0"})
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = "动漫资源 RSS 订阅"
    body = ET.SubElement(root, "body")
    base = (base_url or "").rstrip("/")
    for f in store.feeds():
        if f["kind"] == "custom" and f["url"]:
            url = f["url"]
        else:
            url = base + f"/feed/{f['id']}.xml"
        ET.SubElement(body, "outline", {
            "text": f["title"], "title": f["title"], "type": "rss", "xmlUrl": url})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
