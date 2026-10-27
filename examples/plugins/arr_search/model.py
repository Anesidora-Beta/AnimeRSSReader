# -*- coding: utf-8 -*-
"""统一条目结构：把 6 个源站的不同形状收敛成同一种 dict。

这一层是**纯函数**：不碰网络、不碰数据库、不依赖 anime_rss 包，
所以能脱离主程序单测（见 ``tests/test_search.py``）。

字段与主程序 ``store.items`` 对齐（``source/title/link/magnet/torrent/infohash/
size/seeders/pubdate/ts/category/author/summary``），另外补了三个只在这里用的：
``sourceName``（显示名）、``sizeBytes``（排序用）、``local``（是否来自本地库）。
"""
import hashlib
import re
import time
from email.utils import mktime_tz, parsedate_tz

__all__ = [
    "make_item", "to_ts", "size_bytes", "human_size", "norm_title",
    "item_key", "infohash_of", "dedupe", "sort_items", "rank_relevance",
]

#: 排序 / 打分时用得上的全部字段
FIELDS = (
    "id", "source", "sourceName", "title", "link", "magnet", "torrent",
    "infohash", "size", "sizeBytes", "seeders", "pubdate", "ts",
    "category", "author", "summary", "local",
)

#: 认 ``1.4 GiB`` / ``1.4GB`` / ``500M`` / ``1024 B``；``1.4`` 这种纯数字不当体积
_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*([KMGT])?\s*(i?B)?\s*$", re.I)
_UNITS = {
    "B": 1, "KB": 1 << 10, "KIB": 1 << 10,
    "MB": 1 << 20, "MIB": 1 << 20,
    "GB": 1 << 30, "GIB": 1 << 30,
    "TB": 1 << 40, "TIB": 1 << 40,
    "PB": 1 << 50, "PIB": 1 << 50,
}


def _clean(s):
    return "" if s is None else str(s).strip()


# ------------------------------------------------------------------ 时间

def to_ts(value):
    """把各种时间写法折成 Unix 秒；认不出来返回 0。

    支持的形状（都是这几个站点真实出现的）：
    ``Thu, 06 Aug 2026 03:29:02 +0800`` / ``2026-08-06T03:33:55.313604`` /
    ``2026-08-06T03:33:55Z`` / ``2026-08-06 03:33`` / ``1793117373``。
    """
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v / 1000) if v > 1e11 else int(v)     # 毫秒自动降级
    s = _clean(value)
    if not s:
        return 0
    if s.isdigit():
        return to_ts(int(s))

    # RFC 822 / 2822
    if "," in s or re.match(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}", s):
        try:
            t = parsedate_tz(s)
            if t:
                return int(mktime_tz(t))
        except Exception:
            pass

    t = s.replace("Z", "+00:00").replace("/", "-")
    m = re.match(
        r"^(\d{4})-(\d{1,2})-(\d{1,2})"
        r"(?:[T ](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
        r"(?:([+-])(\d{2}):?(\d{2}))?)?", t)
    if not m:
        return 0
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hh = int(m.group(4) or 0)
        mi = int(m.group(5) or 0)
        ss = int(m.group(6) or 0)
        # 无时区标记时按本地时区处理 —— 与 sites 的“北京时间”一致
        if m.group(7):
            import calendar
            base = calendar.timegm((y, mo, d, hh, mi, ss, 0, 0, 0))
            off = (int(m.group(8)) * 3600 + int(m.group(9)) * 60)
            return int(base - off if m.group(7) == "+" else base + off)
        return int(time.mktime((y, mo, d, hh, mi, ss, 0, 0, -1)))
    except Exception:
        return 0


# ------------------------------------------------------------------ 体积

def size_bytes(text):
    """``1.3 GiB`` / ``12.2GB`` / ``13099650048`` -> 字节数；认不出返回 0。"""
    if text is None or text == "":
        return 0
    if isinstance(text, (int, float)):
        return int(text)
    s = _clean(text).replace(",", "")
    if s.isdigit():
        return int(s)
    m = _SIZE_RE.match(s)
    if not m:
        return 0
    unit = (m.group(2) or "").upper()
    if not unit and not m.group(3):
        return 0                      # 「1080p」里的 1080 不是体积
    try:
        return int(float(m.group(1)) * (_UNITS.get(unit + "B", 0) if unit else 1))
    except (TypeError, ValueError):
        return 0


def human_size(n):
    """字节数 -> ``1.3 GiB``（与主程序展示保持一致）。"""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return ""
    if f <= 0:
        return ""
    for unit, cap in (("TiB", 1 << 40), ("GiB", 1 << 30), ("MiB", 1 << 20), ("KiB", 1 << 10)):
        if f >= cap:
            return "%.1f %s" % (f / cap, unit)
    return "%d B" % f


# ------------------------------------------------------------------ 指纹与去重

def infohash_of(magnet):
    """从磁力里取 infohash（40 位 hex 或 32 位 base32 都认）。"""
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", magnet or "")
    if not m:
        return ""
    v = m.group(1)
    if len(v) == 40:
        return v.lower()
    try:
        import base64
        return base64.b32decode(v.upper()).hex()
    except Exception:
        return ""


def norm_title(title):
    """标题归一化：去掉标点/空白/大小写差异，用来做跨源同帖判定。"""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _clean(title).lower())


def item_key(it):
    """跨源指纹。与主程序 ``store.item_key`` 同一算法，保证能命中同一条库记录。"""
    ih = _clean(it.get("infohash")).lower()
    if ih:
        return "h:" + ih
    base = _clean(it.get("link")) + "|" + _clean(it.get("title"))
    return "g:" + hashlib.sha1(base.encode("utf-8", "ignore")).hexdigest()[:24]


def _quality(it):
    """同一条帖子出现在多个源时，谁的字段更全就留谁。"""
    s = 0
    s += 4 if it.get("magnet") else 0
    s += 2 if it.get("torrent") else 0
    s += 2 if it.get("size") else 0
    s += 1 if it.get("seeders") is not None else 0
    s += 1 if it.get("pubdate") else 0
    return s


def dedupe(items, by_title=True):
    """跨源去重：同 infohash 或同标题视为一条，保留字段最全的并把零散字段补齐。

    与主程序 ``feeds._dedupe`` 的并查集写法等价，这里改成更直白的哈希分桶，
    因为搜索结果量级小（几百条），可读性优先。
    """
    kept, index = [], {}
    for it in items:
        keys = []
        ih = _clean(it.get("infohash")).lower()
        if ih:
            keys.append("h:" + ih)
        if by_title:
            t = norm_title(it.get("title"))
            if t:
                keys.append("t:" + t)
        hit = next((index[k] for k in keys if k in index), None)
        if hit is None:
            for k in keys:
                index[k] = len(kept)
            kept.append(it)
            continue
        cur = kept[hit]
        keep, drop = (cur, it) if _quality(cur) >= _quality(it) else (it, cur)
        for f in ("magnet", "torrent", "size", "sizeBytes", "seeders",
                  "category", "author", "pubdate", "infohash"):
            if not keep.get(f) and drop.get(f):
                keep[f] = drop[f]
        if int(drop.get("ts") or 0) > int(keep.get("ts") or 0):
            keep["ts"] = drop["ts"]
        keep["alsoIn"] = sorted(set((keep.get("alsoIn") or []) + [cur.get("source")])
                                - {keep.get("source")})
        kept[hit] = keep
    return kept


# ------------------------------------------------------------------ 排序

def rank_relevance(it, terms):
    """相关度：命中词数 > 命中位置（标题开头） > 是否带磁力。"""
    title = _clean(it.get("title")).lower()
    score = 0
    for t in terms or ():
        if not t:
            continue
        if t in title:
            score += 2
            if title.find(t) <= 2:
                score += 1
    if it.get("magnet"):
        score += 1
    return score


def sort_items(items, sort="date", terms=()):
    """按指定维度排序；``date`` 默认新的在前，其它维度降序。"""
    if sort == "size":
        return sorted(items, key=lambda x: int(x.get("sizeBytes") or 0), reverse=True)
    if sort == "seeders":
        # 没有做种数（None）的一律排在最后，而不是因为 True > False 跑到最前面
        return sorted(items, key=lambda x: (0 if x.get("seeders") is not None else 1,
                                            -int(x.get("seeders") or 0)))
    if sort == "relevance":
        return sorted(items, key=lambda x: (rank_relevance(x, terms), int(x.get("ts") or 0)),
                      reverse=True)
    return sorted(items, key=lambda x: (int(x.get("ts") or 0), _quality(x)), reverse=True)


# ------------------------------------------------------------------ 构造

def make_item(source, title, link="", source_name="", **kw):
    """按统一结构造一条；未知字段忽略，缺省补空串。

    ``size`` 既接受 ``"1.3 GiB"`` 也接受字节数 —— 顺手把 ``sizeBytes`` 算好，
    这样排序不用每次再解析一遍字符串。
    """
    it = {f: "" for f in FIELDS}
    it.update({"seeders": None, "ts": 0, "sizeBytes": 0, "local": False})
    it["source"] = _clean(source)
    it["sourceName"] = _clean(source_name) or _clean(source)
    it["title"] = _clean(title)
    it["link"] = _clean(link)
    for k, v in kw.items():
        if k in FIELDS:
            it[k] = v
    it["magnet"] = _clean(it.get("magnet"))
    it["torrent"] = _clean(it.get("torrent"))
    it["infohash"] = _clean(it.get("infohash")).lower()
    if not it["infohash"] and it["magnet"]:
        it["infohash"] = infohash_of(it["magnet"])

    raw_size = it.get("size")
    n = size_bytes(raw_size)
    it["sizeBytes"] = n
    it["size"] = _clean(raw_size) if not isinstance(raw_size, (int, float)) else human_size(n)
    if n and not it["size"]:
        it["size"] = human_size(n)

    it["ts"] = int(it.get("ts") or 0) or to_ts(it.get("pubdate"))
    if it.get("seeders") not in (None, ""):
        try:
            it["seeders"] = int(it["seeders"])
        except (TypeError, ValueError):
            it["seeders"] = None
    else:
        it["seeders"] = None
    if not it.get("id"):
        it["id"] = item_key(it)
    return it
