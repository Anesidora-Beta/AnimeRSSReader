# -*- coding: utf-8 -*-
"""SQLite 存储层：订阅源、条目、已读与收藏状态。

单文件数据库，零外部依赖；条目按「跨站指纹」去重（同一 infohash 只存一条），
再用 item_feeds 维护「哪条属于哪个订阅源」的多对多关系，
所以同一个种子在「全部」和自己的站点频道里都能看到。
"""

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import time

#: 库结构版本，写进 ``PRAGMA user_version``。
#: 导入备份时靠它挡掉「来自更新版本程序」的数据。
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'builtin',   -- builtin / custom
    params      TEXT NOT NULL DEFAULT '{}',        -- JSON: src/keyword/include/exclude/limit
    url         TEXT NOT NULL DEFAULT '',          -- 自定义订阅的 RSS 地址
    enabled     INTEGER NOT NULL DEFAULT 1,
    sort        INTEGER NOT NULL DEFAULT 100,
    last_fetch  INTEGER NOT NULL DEFAULT 0,
    last_ok     INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT NOT NULL DEFAULT '',
    added       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS items (
    id       TEXT PRIMARY KEY,
    source   TEXT NOT NULL DEFAULT '',
    title    TEXT NOT NULL DEFAULT '',
    link     TEXT NOT NULL DEFAULT '',
    magnet   TEXT NOT NULL DEFAULT '',
    torrent  TEXT NOT NULL DEFAULT '',
    infohash TEXT NOT NULL DEFAULT '',
    size     TEXT NOT NULL DEFAULT '',
    seeders  INTEGER,
    pubdate  TEXT NOT NULL DEFAULT '',
    ts       INTEGER NOT NULL DEFAULT 0,
    category TEXT NOT NULL DEFAULT '',
    author   TEXT NOT NULL DEFAULT '',
    summary  TEXT NOT NULL DEFAULT '',
    links    TEXT NOT NULL DEFAULT '[]',
    read     INTEGER NOT NULL DEFAULT 0,
    starred  INTEGER NOT NULL DEFAULT 0,
    added    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS item_feeds (
    item_id TEXT NOT NULL,
    feed_id TEXT NOT NULL,
    PRIMARY KEY (item_id, feed_id)
);

-- 关注列表：按关键词长期盯一部番 / 一个字幕组 / 一个分辨率
CREATE TABLE IF NOT EXISTS watches (
    id        TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    include   TEXT NOT NULL DEFAULT '',   -- 关键词，空格分隔，需要全部命中
    exclude   TEXT NOT NULL DEFAULT '',   -- 排除词，命中任一即跳过
    fansub    TEXT NOT NULL DEFAULT '',   -- 字幕组，逗号/空格分隔，任一命中
    sources   TEXT NOT NULL DEFAULT '',   -- 限定来源 id，空=全部
    enabled   INTEGER NOT NULL DEFAULT 1,
    created   INTEGER NOT NULL DEFAULT 0,
    last_scan INTEGER NOT NULL DEFAULT 0,
    last_hit  INTEGER NOT NULL DEFAULT 0
);

-- 关注命中的条目：独立记 30 天，源站翻页滚掉的也能继续看到
CREATE TABLE IF NOT EXISTS watch_items (
    watch_id TEXT NOT NULL,
    item_id  TEXT NOT NULL,
    seen     INTEGER NOT NULL DEFAULT 0,   -- 首次命中时间
    expires  INTEGER NOT NULL DEFAULT 0,   -- 保留截止
    PRIMARY KEY (watch_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_items_ts ON items (ts DESC);
CREATE INDEX IF NOT EXISTS idx_items_added ON items (added DESC);
CREATE INDEX IF NOT EXISTS idx_if_feed ON item_feeds (feed_id, item_id);
CREATE INDEX IF NOT EXISTS idx_wi_watch ON watch_items (watch_id, seen DESC);
CREATE INDEX IF NOT EXISTS idx_wi_exp ON watch_items (expires);

-- 新番放送表：各平台抓回来、跨平台合并之后的一条番一行（payload 存整条 JSON）
CREATE TABLE IF NOT EXISTS schedule (
    id      TEXT PRIMARY KEY,
    weekday INTEGER NOT NULL DEFAULT -1,
    air_ts  INTEGER NOT NULL DEFAULT 0,
    payload TEXT NOT NULL DEFAULT '{}'
);

-- 上一次抓放送表时各平台的结果，用来在界面上标「哪个源没取到」
CREATE TABLE IF NOT EXISTS schedule_source (
    id     TEXT PRIMARY KEY,
    ok     INTEGER NOT NULL DEFAULT 0,
    count  INTEGER NOT NULL DEFAULT 0,
    error  TEXT NOT NULL DEFAULT '',
    ms     INTEGER NOT NULL DEFAULT 0
);

-- 杂项键值（目前只放 schedule_updated）
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_schedule_wd ON schedule (weekday, air_ts);
"""

# 内置订阅源：等于把上游 feeds.json 的频道 + 分站频道一次性铺好
DEFAULT_FEEDS = [
    ("all",     "全部更新",     "builtin", {"src": "", "limit": "200"}, 10),
    ("dmhy",    "动漫花园",     "builtin", {"src": "dmhy", "limit": "150"}, 20),
    ("animegarden", "花园镜像 AnimeGarden", "builtin",
     {"src": "animegarden", "limit": "200", "ag_pages": "2"}, 25),
    ("mikan",   "蜜柑计划",     "builtin", {"src": "mikan", "limit": "150"}, 30),
    ("acgrip",  "ACG.RIP",      "builtin", {"src": "acgrip", "limit": "150"}, 40),
    ("nyaa",    "Nyaa",         "builtin", {"src": "nyaa", "limit": "150"}, 50),
    ("bangumi", "萌番组",       "builtin", {"src": "bangumi", "limit": "150"}, 60),
    ("hd",      "1080p 合集",   "builtin", {"src": "", "include": "1080p", "limit": "150"}, 70),
    ("magnet",  "只带磁力的站", "builtin", {"src": "dmhy,nyaa,bangumi", "limit": "150"}, 80),
]

KEEP_DAYS = 30          # 未收藏条目保留天数
KEEP_WATCH_DAYS = 30    # 关注命中的条目，从命中之日起保留天数
KEEP_MAX = 30000        # 条目总量上限，超出后淘汰最旧的未收藏条目


def watch_id_of(feed):
    """把 feed 参数里的关注标识拆出来：watch:w:abc 和 w:abc 都认"""
    if not feed or not isinstance(feed, str):
        return None
    if feed.startswith("watch:"):
        return feed[len("watch:"):]
    if feed.startswith("w:"):
        return feed
    return None


def item_key(it):
    """跨源指纹：优先 infohash（同一种子只有一条），否则按链接+标题哈希"""
    ih = (it.get("infohash") or "").strip().lower()
    if ih:
        return "h:" + ih
    base = (it.get("link") or "") + "|" + (it.get("title") or "")
    return "g:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:24]


class Store:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.RLock()
        self._connect()

    def _connect(self):
        """建连接 + 建表 + 补内置频道。``__init__`` 和整体换库都走这里。"""
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA user_version=%d" % SCHEMA_VERSION)
            self._seed()

    def reload_from(self, db_file):
        """整体换成另一个库文件（导入用户数据用）。

        整段都拿着 ``self._lock``：别的线程要么在换库前查完，要么等换完
        用新库继续，不会撞上「连接已关闭」。调用方负责事先备份。
        """
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass
            # WAL / SHM 是旧库的残留，留着会被 SQLite 当成新库的日志，必须删掉
            for suffix in ("-wal", "-shm"):
                p = self.path + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            shutil.copyfile(db_file, self.path)
            self._connect()

    # ------------------------------------------------------------ 初始化
    def _seed(self):
        """补齐缺失的内置频道（老版本数据库升级后也能拿到新频道）"""
        have = {r["id"] for r in self._db.execute("SELECT id FROM feeds").fetchall()}
        changed = False
        for fid, title, kind, params, sort in DEFAULT_FEEDS:
            if fid in have:
                continue
            self._db.execute(
                "INSERT INTO feeds (id,title,kind,params,url,enabled,sort) VALUES (?,?,?,?,?,1,?)",
                (fid, title, kind, json.dumps(params, ensure_ascii=False), "", sort),
            )
            changed = True
        if changed:
            self._db.commit()

    def close(self):
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    # ------------------------------------------------------------ 订阅源
    def feeds(self, only_enabled=False):
        sql = "SELECT * FROM feeds"
        if only_enabled:
            sql += " WHERE enabled=1"
        sql += " ORDER BY sort ASC, title ASC"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        out = []
        for r in rows:
            try:
                params = json.loads(r["params"] or "{}")
            except Exception:
                params = {}
            out.append({
                "id": r["id"], "title": r["title"], "kind": r["kind"],
                "params": params, "url": r["url"], "enabled": bool(r["enabled"]),
                "sort": r["sort"], "lastFetch": r["last_fetch"],
                "lastOk": r["last_ok"], "lastError": r["last_error"],
            })
        return out

    def feed(self, fid):
        for f in self.feeds():
            if f["id"] == fid:
                return f
        return None

    def add_feed(self, fid, title, kind, params=None, url=""):
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO feeds (id,title,kind,params,url,enabled,sort)"
                " VALUES (?,?,?,?,?,1,?)",
                (fid, title, kind, json.dumps(params or {}, ensure_ascii=False), url, 500),
            )
            self._db.commit()
        return self.feed(fid)

    def update_feed(self, fid, **kw):
        fields, values = [], []
        for k in ("title", "url", "enabled", "sort"):
            if k in kw and kw[k] is not None:
                fields.append(f"{k}=?")
                values.append(int(bool(kw[k])) if k == "enabled" else kw[k])
        if "params" in kw and kw["params"] is not None:
            fields.append("params=?")
            values.append(json.dumps(kw["params"], ensure_ascii=False))
        if not fields:
            return self.feed(fid)
        values.append(fid)
        with self._lock:
            self._db.execute(f"UPDATE feeds SET {','.join(fields)} WHERE id=?", values)
            self._db.commit()
        return self.feed(fid)

    def delete_feed(self, fid):
        with self._lock:
            self._db.execute("DELETE FROM item_feeds WHERE feed_id=?", (fid,))
            self._db.execute("DELETE FROM feeds WHERE id=?", (fid,))
            self._db.execute(
                "DELETE FROM items WHERE id NOT IN (SELECT item_id FROM item_feeds)"
            )
            self._db.commit()

    def feed_stat(self, fid, ok=True, error=""):
        now = int(time.time())
        with self._lock:
            if ok:
                self._db.execute(
                    "UPDATE feeds SET last_fetch=?, last_ok=?, last_error='' WHERE id=?",
                    (now, now, fid),
                )
            else:
                self._db.execute(
                    "UPDATE feeds SET last_fetch=?, last_error=? WHERE id=?",
                    (now, error[:300], fid),
                )
            self._db.commit()

    # ------------------------------------------------------------ 条目写入
    def upsert_items(self, feed_id, items, link=True):
        """写入条目，返回新增条数（已存在的只补全字段，不动已读/收藏）

        link=False 时不建立频道归属 —— 关注补搜回来的历史资源只服务关注，
        不会涌进「全部更新」和各个资源站频道。
        """
        now = int(time.time())
        added = 0
        sql = """
        INSERT INTO items (id,source,title,link,magnet,torrent,infohash,size,seeders,
                           pubdate,ts,category,author,summary,links,read,starred,added)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?)
        ON CONFLICT(id) DO UPDATE SET
            magnet   = CASE WHEN items.magnet=''  THEN excluded.magnet  ELSE items.magnet  END,
            torrent  = CASE WHEN items.torrent='' THEN excluded.torrent ELSE items.torrent END,
            infohash = CASE WHEN items.infohash='' THEN excluded.infohash ELSE items.infohash END,
            size     = CASE WHEN items.size=''    THEN excluded.size    ELSE items.size     END,
            seeders  = COALESCE(excluded.seeders, items.seeders),
            category = CASE WHEN items.category='' THEN excluded.category ELSE items.category END,
            author   = CASE WHEN items.author=''   THEN excluded.author   ELSE items.author   END,
            summary  = CASE WHEN length(excluded.summary) > length(items.summary)
                            THEN excluded.summary ELSE items.summary END,
            links    = CASE WHEN length(excluded.links) > length(items.links)
                            THEN excluded.links ELSE items.links END,
            ts       = MAX(items.ts, excluded.ts)
        """
        with self._lock:
            cur = self._db.cursor()
            for it in items:
                key = item_key(it)
                before = cur.execute("SELECT 1 FROM items WHERE id=?", (key,)).fetchone()
                cur.execute(sql, (
                    key, it.get("source") or "", it.get("title") or "",
                    it.get("link") or "", it.get("magnet") or "",
                    it.get("torrent") or "", it.get("infohash") or "",
                    it.get("size") or "", it.get("seeders"),
                    it.get("pubdate") or "", int(it.get("ts") or 0),
                    it.get("category") or "", it.get("author") or "",
                    it.get("summary") or "",
                    json.dumps(it.get("links") or [], ensure_ascii=False),
                    now,
                ))
                if not before:
                    added += 1
                if link and feed_id:
                    cur.execute(
                        "INSERT OR IGNORE INTO item_feeds (item_id,feed_id) VALUES (?,?)",
                        (key, feed_id),
                    )
            self._db.commit()
        return added

    # ------------------------------------------------------------ 条目查询
    def query_items(self, feed="all", filt="all", q="", limit=60, offset=0):
        where, args = [], []
        wid = watch_id_of(feed)
        if wid:
            where.append("i.id IN (SELECT item_id FROM watch_items"
                         " WHERE watch_id=? AND expires>?)")
            args += [wid, int(time.time())]
        elif feed and feed != "all":
            where.append("i.id IN (SELECT item_id FROM item_feeds WHERE feed_id=?)")
            args.append(feed)
        else:
            # 只列正常的频道条目；关注补搜回来的历史资源（没有频道归属）不进这里
            where.append("i.id IN (SELECT item_id FROM item_feeds)")
        if filt == "unread":
            where.append("i.read=0")
        elif filt == "starred":
            where.append("i.starred=1")
        elif filt == "magnet":
            where.append("i.magnet!=''")
        if q:
            where.append("(i.title LIKE ? OR i.summary LIKE ?)")
            like = "%" + q + "%"
            args += [like, like]
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sql = (
            "SELECT i.* FROM items i " + clause +
            " ORDER BY i.ts DESC, i.added DESC LIMIT ? OFFSET ?"
        )
        with self._lock:
            rows = self._db.execute(sql, args + [int(limit), int(offset)]).fetchall()
        return [self._row(r) for r in rows]

    def get_item(self, iid):
        with self._lock:
            r = self._db.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
        return self._row(r) if r else None

    @staticmethod
    def _row(r):
        try:
            links = json.loads(r["links"] or "[]")
        except Exception:
            links = []
        return {
            "id": r["id"], "source": r["source"], "title": r["title"],
            "link": r["link"], "magnet": r["magnet"], "torrent": r["torrent"],
            "infohash": r["infohash"], "size": r["size"], "seeders": r["seeders"],
            "pubdate": r["pubdate"], "ts": r["ts"], "category": r["category"],
            "author": r["author"], "summary": r["summary"], "links": links,
            "read": bool(r["read"]), "starred": bool(r["starred"]),
            "added": r["added"],
        }

    def counts(self):
        """总量 / 未读 / 收藏 / 分频道未读"""
        with self._lock:
            total = self._db.execute(
                "SELECT COUNT(*) c FROM items"
                " WHERE id IN (SELECT item_id FROM item_feeds)").fetchone()["c"]
            unread = self._db.execute(
                "SELECT COUNT(*) c FROM items WHERE read=0"
                " AND id IN (SELECT item_id FROM item_feeds)").fetchone()["c"]
            starred = self._db.execute(
                "SELECT COUNT(*) c FROM items WHERE starred=1").fetchone()["c"]
            per = self._db.execute(
                "SELECT feed_id, COUNT(*) c, SUM(read=0) u, SUM(starred=1) s"
                " FROM item_feeds JOIN items ON items.id=item_feeds.item_id"
                " GROUP BY feed_id"
            ).fetchall()
        return {
            "total": total, "unread": unread, "starred": starred,
            "feeds": {r["feed_id"]: {"total": r["c"], "unread": r["u"] or 0,
                                     "starred": r["s"] or 0} for r in per},
            "watches": self.watch_counts(),
        }

    def newest_ts(self, feed="all"):
        with self._lock:
            if feed == "all":
                r = self._db.execute("SELECT MAX(ts) t FROM items").fetchone()
            else:
                r = self._db.execute(
                    "SELECT MAX(i.ts) t FROM items i JOIN item_feeds m ON m.item_id=i.id"
                    " WHERE m.feed_id=?", (feed,)).fetchone()
        return r["t"] or 0

    # ------------------------------------------------------------ 键值配置
    def meta_get(self, k, default=""):
        with self._lock:
            r = self._db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r is not None else default

    def meta_set(self, k, v):
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO meta (k,v) VALUES (?,?)",
                             (k, v if isinstance(v, str) else str(v)))
            self._db.commit()

    def meta_prefix(self, prefix=""):
        """取某个前缀下的全部键值（设置项都存在 meta 里，前缀 s.）"""
        with self._lock:
            rows = self._db.execute(
                "SELECT k, v FROM meta WHERE k LIKE ?", (prefix + "%",)).fetchall()
        return {r["k"]: r["v"] for r in rows}

    # ------------------------------------------------------------ 关注列表
    @staticmethod
    def _words(s):
        """关键词拆分：空格 / 逗号 / 顿号都算分隔符"""
        if not s:
            return []
        norm = str(s).replace("，", " ").replace(",", " ").replace("、", " ")
        return [w.lower() for w in norm.split() if w]

    def watches(self):
        now = int(time.time())
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM watches ORDER BY created ASC").fetchall()
            stat = self._db.execute(
                "SELECT w.watch_id wid, COUNT(*) c, SUM(i.read=0) u, MAX(w.seen) s"
                " FROM watch_items w JOIN items i ON i.id=w.item_id"
                " WHERE w.expires>? GROUP BY w.watch_id", (now,)).fetchall()
        by = {r["wid"]: {"total": r["c"], "unread": r["u"] or 0, "lastHit": r["s"] or 0}
              for r in stat}
        out = []
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            st = by.get(d["id"]) or {"total": 0, "unread": 0, "lastHit": 0}
            d["enabled"] = bool(d["enabled"])
            d.update(st)
            out.append(d)
        return out

    def watch(self, wid):
        for w in self.watches():
            if w["id"] == wid:
                return w
        return None

    def add_watch(self, name, include="", exclude="", fansub="", sources=""):
        name = (name or "").strip() or "未命名关注"
        wid = "w:" + hashlib.sha1(
            (name + repr(time.time())).encode("utf-8", "ignore")).hexdigest()[:10]
        now = int(time.time())
        with self._lock:
            self._db.execute(
                "INSERT INTO watches (id,name,include,exclude,fansub,sources,enabled,created)"
                " VALUES (?,?,?,?,?,?,1,?)",
                (wid, name, (include or "").strip(), (exclude or "").strip(),
                 (fansub or "").strip(), (sources or "").strip(), now),
            )
            self._db.commit()
        return self.watch(wid)

    def update_watch(self, wid, **kw):
        fields, values = [], []
        for k in ("name", "include", "exclude", "fansub", "sources", "enabled"):
            if k in kw and kw[k] is not None:
                fields.append(f"{k}=?")
                values.append(int(bool(kw[k])) if k == "enabled" else str(kw[k]).strip())
        if fields:
            values.append(wid)
            with self._lock:
                self._db.execute(f"UPDATE watches SET {','.join(fields)} WHERE id=?", values)
                self._db.commit()
        return self.watch(wid)

    def delete_watch(self, wid):
        with self._lock:
            self._db.execute("DELETE FROM watch_items WHERE watch_id=?", (wid,))
            self._db.execute("DELETE FROM watches WHERE id=?", (wid,))
            self._db.commit()

    def scan_watches(self, window_days=60):
        """把近 window_days 的条目跑一遍关注规则，命中的记 30 天。

        返回 {"hits": 新命中总数, "per": {watch_id: 新增数}}
        """
        now = int(time.time())
        active = [w for w in self.watches() if w["enabled"]]
        if not active:
            return {"hits": 0, "per": {}}

        cutoff = now - window_days * 86400
        with self._lock:
            rows = self._db.execute(
                "SELECT id,title,source,author FROM items WHERE ts>=?", (cutoff,)
            ).fetchall()

        per, total = {}, 0
        with self._lock:
            cur = self._db.cursor()
            for w in active:
                inc = self._words(w["include"])
                exc = self._words(w["exclude"])
                fss = self._words(w["fansub"])
                srcs = set(self._words(w["sources"]))
                hit = 0
                for r in rows:
                    title = (r["title"] or "").lower()
                    if inc and not all(x in title for x in inc):
                        continue
                    if exc and any(x in title for x in exc):
                        continue
                    if srcs and r["source"] not in srcs:
                        continue
                    if fss:
                        author = (r["author"] or "").lower()
                        if not any(x in title or x in author for x in fss):
                            continue
                    cur.execute(
                        "INSERT OR IGNORE INTO watch_items (watch_id,item_id,seen,expires)"
                        " VALUES (?,?,?,?)",
                        (w["id"], r["id"], now, now + KEEP_WATCH_DAYS * 86400))
                    if cur.rowcount:
                        hit += 1
                per[w["id"]] = hit
                total += hit
                cur.execute("UPDATE watches SET last_scan=? WHERE id=?", (now, w["id"]))
                if hit:
                    cur.execute("UPDATE watches SET last_hit=? WHERE id=?", (now, w["id"]))
            self._db.commit()
        return {"hits": total, "per": per}

    def attach_watch(self, wid, items):
        """把一批条目直接挂到关注下（补搜用）。

        补搜回来的可能是几年前的番，走 scan_watches 的「近 60 天」窗口会全部漏掉，
        所以这里只按关注规则判定，不看发布时间。
        """
        w = self.watch(wid)
        if not w or not w["enabled"]:
            return 0
        inc = self._words(w["include"])
        exc = self._words(w["exclude"])
        fss = self._words(w["fansub"])
        srcs = set(self._words(w["sources"]))
        now = int(time.time())
        expires = now + KEEP_WATCH_DAYS * 86400
        n = 0
        with self._lock:
            cur = self._db.cursor()
            for it in items:
                title = (it.get("title") or "").lower()
                if inc and not all(x in title for x in inc):
                    continue
                if exc and any(x in title for x in exc):
                    continue
                if srcs and (it.get("source") or "") not in srcs:
                    continue
                if fss:
                    author = (it.get("author") or "").lower()
                    if not any(x in title or x in author for x in fss):
                        continue
                cur.execute(
                    "INSERT OR IGNORE INTO watch_items (watch_id,item_id,seen,expires)"
                    " VALUES (?,?,?,?)", (wid, item_key(it), now, expires))
                if cur.rowcount:
                    n += 1
            if n:
                cur.execute("UPDATE watches SET last_hit=?, last_scan=? WHERE id=?",
                            (now, now, wid))
            self._db.commit()
        return n

    def watch_counts(self):
        now = int(time.time())
        with self._lock:
            rows = self._db.execute(
                "SELECT w.watch_id wid, COUNT(*) c, SUM(i.read=0) u, SUM(i.starred=1) s"
                " FROM watch_items w JOIN items i ON i.id=w.item_id"
                " WHERE w.expires>? GROUP BY w.watch_id", (now,)).fetchall()
        return {r["wid"]: {"total": r["c"], "unread": r["u"] or 0,
                           "starred": r["s"] or 0} for r in rows}

    # ------------------------------------------------------------ 状态变更
    def set_read(self, ids, read=True):
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._db.execute(
                f"UPDATE items SET read=? WHERE id IN ({marks})",
                [1 if read else 0] + list(ids),
            )
            self._db.commit()
        return cur.rowcount

    def set_star(self, iid, star=True):
        with self._lock:
            self._db.execute(
                "UPDATE items SET starred=? WHERE id=?", (1 if star else 0, iid))
            self._db.commit()
        return self.get_item(iid)

    def read_all(self, feed="all"):
        wid = watch_id_of(feed)
        with self._lock:
            if wid:
                cur = self._db.execute(
                    "UPDATE items SET read=1 WHERE read=0 AND id IN (SELECT item_id"
                    " FROM watch_items WHERE watch_id=? AND expires>?)",
                    (wid, int(time.time())))
            elif feed and feed != "all":
                cur = self._db.execute(
                    "UPDATE items SET read=1 WHERE read=0 AND id IN"
                    " (SELECT item_id FROM item_feeds WHERE feed_id=?)", (feed,))
            else:
                cur = self._db.execute("UPDATE items SET read=1 WHERE read=0")
            self._db.commit()
        return cur.rowcount

    # ------------------------------------------------------------ 放送表
    def save_schedule(self, entries, sources, updated):
        """整表替换：周历是「当季快照」，每次抓取都是新的完整结果"""
        rows = [(e["id"], int(e.get("weekday", -1)), int(e.get("airTs") or 0),
                 json.dumps(e, ensure_ascii=False)) for e in entries]
        srcs = [(s["id"], 1 if s.get("ok") else 0, int(s.get("count") or 0),
                 (s.get("error") or "")[:200], int(s.get("ms") or 0)) for s in sources]
        with self._lock:
            self._db.execute("DELETE FROM schedule")
            self._db.executemany(
                "INSERT OR REPLACE INTO schedule (id,weekday,air_ts,payload)"
                " VALUES (?,?,?,?)", rows)
            self._db.execute("DELETE FROM schedule_source")
            self._db.executemany(
                "INSERT OR REPLACE INTO schedule_source (id,ok,count,error,ms)"
                " VALUES (?,?,?,?,?)", srcs)
            self._db.execute(
                "INSERT OR REPLACE INTO meta (k,v) VALUES ('schedule_updated',?)",
                (str(int(updated)),))
            self._db.commit()

    def load_schedule(self):
        with self._lock:
            rows = self._db.execute("SELECT payload FROM schedule").fetchall()
            srcs = self._db.execute(
                "SELECT * FROM schedule_source").fetchall()
            meta = self._db.execute(
                "SELECT v FROM meta WHERE k='schedule_updated'").fetchone()
        entries = []
        for r in rows:
            try:
                entries.append(json.loads(r["payload"]))
            except Exception:
                continue
        try:
            updated = int(meta["v"]) if meta else 0
        except (TypeError, ValueError):
            updated = 0
        sources = [{"id": s["id"], "ok": bool(s["ok"]), "count": s["count"],
                    "error": s["error"], "ms": s["ms"]} for s in srcs]
        return {"entries": entries, "sources": sources, "updated": updated}

    # ------------------------------------------------------------ 维护
    def prune(self, days=KEEP_DAYS, cap=KEEP_MAX):
        now = int(time.time())
        cutoff = now - days * 86400
        with self._lock:
            # 关注命中的条目按自己的保留期走，不受主表 30 天限制
            n0 = self._db.execute(
                "DELETE FROM watch_items WHERE expires<?", (now,)
            ).rowcount
            n1 = self._db.execute(
                "DELETE FROM items WHERE starred=0 AND ts>0 AND ts<?"
                " AND id NOT IN (SELECT item_id FROM watch_items WHERE expires>?)",
                (cutoff, now)).rowcount
            n2 = self._db.execute(
                "DELETE FROM items WHERE starred=0 AND id NOT IN ("
                " SELECT id FROM items ORDER BY ts DESC LIMIT ?)"
                " AND id NOT IN (SELECT item_id FROM watch_items WHERE expires>?)",
                (cap, now)).rowcount
            self._db.execute(
                "DELETE FROM item_feeds WHERE item_id NOT IN (SELECT id FROM items)")
            self._db.execute(
                "DELETE FROM watch_items WHERE item_id NOT IN (SELECT id FROM items)")
            self._db.commit()
        return n1 + n2, n0

    def vacuum(self):
        with self._lock:
            try:
                self._db.execute("VACUUM")
            except Exception:
                pass

    def stats(self):
        with self._lock:
            n_items = self._db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
            n_feeds = self._db.execute("SELECT COUNT(*) c FROM feeds").fetchone()["c"]
        size = os.path.getsize(self.path) if os.path.exists(self.path) else 0
        return {"items": n_items, "feeds": n_feeds, "dbBytes": size}
