# -*- coding: utf-8 -*-
"""关注桥接：把搜到的历史条目挂进主程序的关注列表。

主程序自己就有这条链路（``reader._backfill_watch``）：:

    got = ctx.refresher.search_for_watch(w)
    ctx.store.upsert_items(None, got, link=False)
    ctx.store.attach_watch(wid, got)

这里做的是**同一件事**，只是 ``got`` 来自多源检索而不是单一源，所以关注页
里看到的东西和主程序自己补搜回来的完全一致 —— 不需要改主程序一行代码。

两个必须小心的地方
------------------
1. **字段名不同**：插件侧叫 ``sourceName``，store 侧要 ``author``；转换在
   ``to_store_item()`` 里一次做完，别让调用方拼字段。
2. **id 必须让 store 自己算**：``attach_watch`` 用 ``store.item_key(it)`` 生成
   ``watch_items.item_id``，如果这里塞一个自己算的 id，关注页 JOIN items 时
   一条都查不出来 —— 这是最容易踩且最难发现的坑。所以转出来的 dict **不带 id**。

``upsert_items(..., link=False)`` 是故意的：几年前的资源不该涌进「全部更新」
和各个资源站频道，它们只服务关注列表。
"""
import time

__all__ = ["WatchBridgeError", "to_store_item", "to_store_items",
           "find_watch", "ensure_watch", "attach", "link", "watched_map"]


class WatchBridgeError(Exception):
    """关注这条链路走不通（宿主没 store、字段缺失之类）。"""


#: 插件 item -> store item 的字段映射。``sourceName`` 在 store 里叫 ``author``。
_FIELD_MAP = (
    ("source", "source"),
    ("title", "title"),
    ("link", "link"),
    ("magnet", "magnet"),
    ("torrent", "torrent"),
    ("infohash", "infohash"),
    ("size", "size"),
    ("sizeBytes", "sizeBytes"),
    ("seeders", "seeders"),
    ("pubdate", "pubdate"),
    ("ts", "ts"),
    ("category", "category"),
    ("author", "author"),
    ("summary", "summary"),
)


def to_store_item(it):
    """插件 item -> 能直接喂给 ``store.upsert_items`` 的 dict。

    注意不写 ``id``：让 ``store.item_key()`` 自己算，才能和 ``attach_watch``
    生成的 ``watch_items.item_id`` 对上。
    """
    out = {}
    for src, dst in _FIELD_MAP:
        v = it.get(src)
        out[dst] = "" if v is None else v
    if not out.get("author"):
        # 插件把「谁发的」放在 sourceName（源站名）和 author（字幕组）两处，
        # 关注规则里 fansub 是拿 title 或 author 匹配的，这里补一句让字幕组稳一点。
        out["author"] = it.get("sourceName") or ""
    out["seeders"] = it.get("seeders")
    out["ts"] = int(it.get("ts") or 0)
    out["links"] = []
    out["local"] = False
    if not out["title"]:
        raise WatchBridgeError("条目没有标题，无法挂到关注")
    return out


def to_store_items(items):
    """批量转换 + 去重（同一 infohash 只留一条，多个源重复发的不重复挂）。"""
    out, seen = [], set()
    for it in items or ():
        try:
            one = to_store_item(it)
        except WatchBridgeError:
            continue
        key = (one.get("infohash") or "").lower() or \
              ((one.get("link") or "") + "|" + (one.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(one)
    return out


def _same_rule(watch, fields):
    """两条关注是不是同一套规则（名字不参与，改名不该长出第二条）。"""
    return (str(watch.get("include") or "").strip() == fields["include"].strip()
            and str(watch.get("exclude") or "").strip() == fields["exclude"].strip()
            and str(watch.get("fansub") or "").strip() == fields["fansub"].strip()
            and str(watch.get("sources") or "").strip() == fields["sources"].strip())


def find_watch(store, fields):
    """找已有的同规则关注；找到就复用，避免每点一次「关注」就建一条。"""
    for w in store.watches():
        if _same_rule(w, fields):
            return w
    return None


def ensure_watch(store, fields):
    """返回 ``(watch, created)``。"""
    w = find_watch(store, fields)
    if w:
        return w, False
    w = store.add_watch(fields["name"], fields.get("include", ""),
                        fields.get("exclude", ""), fields.get("fansub", ""),
                        fields.get("sources", ""))
    return w, True


def attach(store, wid, items):
    """把条目写进 items 表，再挂到关注下。返回 ``(入库新增数, 挂上条数)``。

    顺序不能反：``watch_items.item_id`` 要 JOIN ``items``，先入库才挂得上。
    """
    rows = to_store_items(items)
    if not rows:
        return 0, 0
    added = store.upsert_items(None, rows, link=False)
    n = store.attach_watch(wid, rows)
    return added, n


def link(store, query, items, name="", loose=False):
    """一站式：查询条件 -> 关注规则 -> 建/复用关注 -> 挂条目。

    ``query`` 是 ``Query`` 实例，``items`` 是本次搜到的插件条目。
    ``loose=True`` 表示「只想盯这部番」，规格词不进关注规则（见
    ``Query.to_watch``）。返回的 dict 直接可以当接口响应体用。
    """
    if store is None:
        raise WatchBridgeError("主程序的数据存储不可用，关注功能需要宿主环境")

    fields, notes = query.to_watch(name=name, loose=loose)
    w, created = ensure_watch(store, fields)
    added, attached = attach(store, w["id"], items)
    w = store.watch(w["id"]) or w

    return {
        "ok": True,
        "watch": {
            "id": w["id"],
            "name": w.get("name"),
            "include": w.get("include"),
            "exclude": w.get("exclude"),
            "fansub": w.get("fansub"),
            "sources": w.get("sources"),
            "total": w.get("total", 0),
            "unread": w.get("unread", 0),
        },
        "created": created,
        "addedToStore": added,
        "attached": attached,
        "skipped": max(0, len(items or ()) - attached),
        "notes": notes,
        "feed": "watch:" + w["id"],
        "ts": int(time.time()),
    }


def watched_map(store, items):
    """这批条目里哪些已经在关注列表里（给前端把按钮点亮用）。

    返回 ``{插件 item 的 id: watch_id}``。查的时候**不自己算 store 的 item_id**，
    而是拿 infohash / (link,title) 反查 —— 主程序哪天改了 id 生成规则也不会错。

    只有公开接口不够用时才碰 ``_db``：主程序没提供「查这批条目在哪些关注里」
    的方法，而改主程序又违背插件的前提，所以这里加锁直查，只读不写。
    """
    db = getattr(store, "_db", None) if store is not None else None
    lock = getattr(store, "_lock", None)
    if db is None:
        return {}

    now = int(time.time())
    out = {}
    sql_ih = ("SELECT w.watch_id wid FROM items i"
              " JOIN watch_items w ON w.item_id = i.id"
              " WHERE i.infohash = ? AND w.expires > ? LIMIT 1")
    sql_gt = ("SELECT w.watch_id wid FROM items i"
              " JOIN watch_items w ON w.item_id = i.id"
              " WHERE i.link = ? AND i.title = ? AND w.expires > ? LIMIT 1")

    def ask(sql, args):
        try:
            return db.execute(sql, args).fetchone()
        except Exception:                        # noqa: BLE001 - 只读探测，失败就当没关注
            return None

    if lock is not None:
        lock.acquire()
    try:
        for it in items or ():
            ih = (it.get("infohash") or "").lower()
            link = it.get("link") or ""
            title = it.get("title") or ""
            row = None
            if ih:
                row = ask(sql_ih, (ih, now))
            if row is None and link:
                row = ask(sql_gt, (link, title, now))
            if row is not None and it.get("id"):
                out[it["id"]] = row["wid"]
    finally:
        if lock is not None:
            lock.release()
    return out
