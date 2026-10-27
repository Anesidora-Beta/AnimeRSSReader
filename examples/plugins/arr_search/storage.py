# -*- coding: utf-8 -*-
"""搜索收藏的组合（preset）与历史，落在自己的小库里。

为什么独立文件、而不是塞进主库
------------------------------
主程序的 ``Store`` 没有对外暴露连接（``ctx.db`` 在架构文档里是目标形态，
真实代码里只有 ``ctx.store``），插件不该去摸别人的 ``_db``。

于是这里用 ``data/search.db``，但**表名仍按 ``mod_<id>_`` 前缀规范**：
以后主程序真把 ``ctx.db`` 做出来了，把这份文件并进去只需改连接、不改表名。
这些都是可以随时重建的数据，丢了不影响主程序，所以单独一个文件也没有事务问题。
"""
import os
import sqlite3
import threading
import time

__all__ = ["SearchStore"]

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mod_search_preset (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        name     TEXT NOT NULL,
        query    TEXT NOT NULL DEFAULT '',
        sources  TEXT NOT NULL DEFAULT '',
        mode     TEXT NOT NULL DEFAULT 'deep',
        sort     TEXT NOT NULL DEFAULT 'date',
        created  INTEGER NOT NULL DEFAULT 0,
        used     INTEGER NOT NULL DEFAULT 0,
        last_use INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS mod_search_history (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        query   TEXT NOT NULL,
        sources TEXT NOT NULL DEFAULT '',
        count   INTEGER NOT NULL DEFAULT 0,
        took    INTEGER NOT NULL DEFAULT 0,
        ts      INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_search_hist_ts ON mod_search_history(ts DESC)",
]


class SearchStore:
    """搜索自己的持久层。调用方只需拿到 dict / list，不需要知道 SQL。"""

    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            for sql in SCHEMA:
                self._db.execute(sql)
            self._db.commit()

    def close(self):
        try:
            with self._lock:
                self._db.close()
        except Exception:
            pass

    # ------------------------------------------------------------ 组合
    def presets(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM mod_search_preset ORDER BY used DESC, id DESC"
            ).fetchall()
        return [{
            "id": r["id"], "name": r["name"], "query": r["query"],
            "sources": [s for s in (r["sources"] or "").split(",") if s],
            "mode": r["mode"], "sort": r["sort"],
            "created": r["created"], "used": r["used"], "lastUse": r["last_use"],
        } for r in rows]

    def save_preset(self, name, query="", sources=(), mode="deep", sort="date"):
        name = (name or "").strip() or "未命名组合"
        src = ",".join(x for x in (sources or ()) if x)
        now = int(time.time())
        with self._lock:
            same = self._db.execute(
                "SELECT id FROM mod_search_preset WHERE name=? AND query=?",
                (name, query or "")).fetchone()
            if same:
                self._db.execute(
                    "UPDATE mod_search_preset SET sources=?, mode=?, sort=? WHERE id=?",
                    (src, mode, sort, same["id"]))
                pid = same["id"]
            else:
                cur = self._db.execute(
                    "INSERT INTO mod_search_preset (name,query,sources,mode,sort,created)"
                    " VALUES (?,?,?,?,?,?)", (name, query or "", src, mode, sort, now))
                pid = cur.lastrowid
            self._db.commit()
        return next((p for p in self.presets() if p["id"] == pid), None)

    def delete_preset(self, pid):
        with self._lock:
            n = self._db.execute("DELETE FROM mod_search_preset WHERE id=?",
                                 (int(pid),)).rowcount
            self._db.commit()
        return n > 0

    def touch_preset(self, pid):
        with self._lock:
            self._db.execute(
                "UPDATE mod_search_preset SET used=used+1, last_use=? WHERE id=?",
                (int(time.time()), int(pid)))
            self._db.commit()

    # ------------------------------------------------------------ 历史
    def history(self, limit=30):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM mod_search_history ORDER BY id DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [{"id": r["id"], "query": r["query"],
                 "sources": [s for s in (r["sources"] or "").split(",") if s],
                 "count": r["count"], "took": r["took"], "ts": r["ts"]} for r in rows]

    def add_history(self, query, sources=(), count=0, took=0):
        q = (query or "").strip()
        if not q:
            return 0
        src = ",".join(x for x in (sources or ()) if x)
        with self._lock:
            # 同一句查询连按两次不必记两条，只更新时间
            last = self._db.execute(
                "SELECT id FROM mod_search_history ORDER BY id DESC LIMIT 1").fetchone()
            if last and self._db.execute(
                    "SELECT 1 FROM mod_search_history WHERE id=? AND query=?",
                    (last["id"], q)).fetchone():
                self._db.execute(
                    "UPDATE mod_search_history SET count=?, took=?, ts=? WHERE id=?",
                    (int(count), int(took), int(time.time()), last["id"]))
            else:
                self._db.execute(
                    "INSERT INTO mod_search_history (query,sources,count,took,ts)"
                    " VALUES (?,?,?,?,?)", (q, src, int(count), int(took),
                                            int(time.time())))
            self._db.commit()
        return 1

    def clear_history(self):
        with self._lock:
            n = self._db.execute("DELETE FROM mod_search_history").rowcount
            self._db.commit()
        return n

    def trim(self, keep=200):
        """只留最近 N 条历史，定时任务每天跑一次。"""
        with self._lock:
            n = self._db.execute(
                "DELETE FROM mod_search_history WHERE id NOT IN ("
                " SELECT id FROM mod_search_history ORDER BY id DESC LIMIT ?)",
                (int(keep),)).rowcount
            self._db.commit()
        return n

    def stats(self):
        with self._lock:
            p = self._db.execute("SELECT COUNT(*) c FROM mod_search_preset").fetchone()["c"]
            h = self._db.execute("SELECT COUNT(*) c FROM mod_search_history").fetchone()["c"]
        return {"presets": p, "history": h}
