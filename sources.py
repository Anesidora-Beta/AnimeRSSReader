# -*- coding: utf-8 -*-
"""资源站适配层。

来源：https://github.com/Anesidora-Beta/anime-rss （原仓库 sources.py，未改动解析逻辑）
阅读器只在其上包了一层：抓取结果落库、去重、供页面消费。

每个站点只需两件事：
    url_xxx(params) -> 抓取地址
    parse_xxx(raw, params) -> 统一 item 列表

统一 item 结构（缺省用空串）：
    source, title, link(详情页), magnet, torrent(.torrent直链),
    infohash, size, seeders, pubdate(RFC822), category, author, desc_html

新增站点：在文件末尾按同样的写法加两个函数，再注册到 SOURCES 即可。
"""

import base64
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from urllib.parse import quote

# 构造磁力时补充的公共 tracker
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://9.rarbg.com:2810/announce",
]


# ------------------------------------------------------------------ 工具

def norm_infohash(h):
    """统一成 40 位小写 hex。

    动漫花园给的是 base32（32 位大写），蜜柑/Nyaa/萌番组给 hex，
    不统一就永远无法跨站认出是同一种子。
    """
    h = (h or "").strip()
    if len(h) == 32:
        try:
            return base64.b32decode(h.upper()).hex()
        except Exception:
            return h.lower()
    return h.lower()


def infohash_of(magnet):
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", magnet or "")
    return norm_infohash(m.group(1)) if m else ""


def build_magnet(infohash):
    h = (infohash or "").strip()
    if not h:
        return ""
    tr = "&".join("tr=" + quote(t, safe="") for t in TRACKERS)
    return f"magnet:?xt=urn:btih:{h}&{tr}"


def human_size(n):
    try:
        n = int(str(n).strip())
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    v, i = float(n), 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024.0
        i += 1
    return f"{v:.2f} {units[i]}" if i else f"{int(v)} B"


def iso_to_rfc822(s, tz_hours=8):
    """ISO8601 -> RFC822；无时区的按 tz_hours 处理"""
    if not s:
        return ""
    txt = str(s).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=tz_hours)))
    return format_datetime(dt)


def _int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def item(source, title, link, auto_magnet=True, **kw):
    d = {
        "source": source,
        "title": (title or "").strip(),
        "link": (link or "").strip(),
        "magnet": "",
        "torrent": "",
        "infohash": "",
        "size": "",
        "seeders": None,
        "pubdate": "",
        "category": "",
        "author": "",
        "desc_html": "",
    }
    d.update({k: v for k, v in kw.items() if v is not None})
    d["infohash"] = norm_infohash(d["infohash"])
    if not d["magnet"] and d["infohash"] and auto_magnet:
        d["magnet"] = build_magnet(d["infohash"])
    if not d["infohash"] and d["magnet"]:
        d["infohash"] = infohash_of(d["magnet"])
    return d


# ------------------------------------------------------------------ 动漫花园

DMHY_BASE = "https://share.dmhy.org"

# 动漫花园主站在国内经常被阻断、海外反而稳定，所以按「多入口」处理：
# 抓取时依次尝试，谁通用谁，并把可用的入口记下来给下次优先用。
# 想接自己的反代：设环境变量 ANIME_RSS_DMHY_HOST=https://你的域名
DMHY_HOSTS = [
    "https://share.dmhy.org",
    "https://dmhy.org",
    "https://www.dmhy.org",
]

# 动漫花园全站 RSS 的分类实测分布（500 条）：
#   動畫 364 / 日劇 49 / 季度全集 39 / 音樂 27 / 特攝 8 / 其他 7 /
#   ＲＡＷ 3 / 動漫音樂 2 / 電腦遊戲 1
# 订阅「动漫资源」时只保留下面三类；其余属噪音（有声书就落在「其他」）。
# 想放宽：dmhy_cat=動畫,季度全集,特攝  或 dmhy_all=1 拿全站。
DMHY_ANIME_CATS = {"動畫", "季度全集", "ＲＡＷ"}


def url_dmhy(p, host=None):
    kw = (p.get("keyword") or "").strip()
    q = {}
    for k in ("sort_id", "team_id"):
        if (p.get(k) or "").strip():
            q[k] = p[k].strip()
    if kw:
        q["keyword"] = kw
        path = "/topics/rss"
    else:
        path = "/topics/rss/rss.xml"
    from urllib.parse import urlencode

    base = (host or DMHY_BASE).rstrip("/")
    return base + path + ("?" + urlencode(q) if q else "")


def parse_dmhy(raw, p):
    # 动漫花园的 RSS 是全站流，实测 500 条里混着 日劇 49、音樂 27、其他 7
    # （例如 Japanese Audiobooks Archive 这类有声书）、電腦遊戲 1 等非动漫内容。
    # 默认只保留动漫分类；需要全量时传 dmhy_all=1，也可用 dmhy_cat=動畫,特攝 自选。
    if str(p.get("dmhy_all") or "") in ("1", "true", "yes"):
        allow = None
    elif (p.get("dmhy_cat") or "").strip():
        allow = {x.strip() for x in p["dmhy_cat"].split(",") if x.strip()}
    elif (p.get("cat") or "").strip():
        allow = None          # 调用方已显式指定分类，交给上层过滤
    else:
        allow = DMHY_ANIME_CATS

    ch = ET.fromstring(raw).find("channel")
    out = []
    for it in ch.findall("item"):
        cat = (it.findtext("category") or "").strip()
        # 分类为空的条目保守保留，避免因字段缺失误删
        if allow is not None and cat and cat not in allow:
            continue
        enc = it.find("enclosure")
        mag = (enc.get("url") or "") if enc is not None else ""
        out.append(
            item(
                "dmhy",
                it.findtext("title"),
                it.findtext("link"),
                magnet=mag,
                infohash=infohash_of(mag),
                pubdate=(it.findtext("pubDate") or "").strip(),
                category=cat,
                author=(it.findtext("author") or "").strip(),
                desc_html=it.findtext("description") or "",
            )
        )
    return out


# ------------------------------------------------------------------ 蜜柑计划

MIKAN_BASE = "https://mikanani.me"
MK_NS = "{https://mikanani.me/0.1/}"


def url_mikan(p, host=None):
    base = (host or MIKAN_BASE).rstrip("/")
    kw = (p.get("keyword") or "").strip()
    if kw:
        return f"{base}/RSS/Search?searchstr={quote(kw)}"
    return f"{base}/RSS/Classic"


def parse_mikan(raw, p):
    ch = ET.fromstring(raw).find("channel")
    out = []
    for it in ch.findall("item"):
        enc = it.find("enclosure")
        turl = (enc.get("url") or "") if enc is not None else ""
        tlen = (enc.get("length") or "") if enc is not None else ""
        link = (it.findtext("link") or "").strip()
        tor = it.find(MK_NS + "torrent")
        pub = (tor.findtext(MK_NS + "pubDate") if tor is not None else "") or ""
        if not pub:
            pub = it.findtext("pubDate") or ""
        if "T" in pub and not pub.startswith(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            pub = iso_to_rfc822(pub, 8)

        # Episode hash 实为"原始种子"的 infohash：可用来跨站去重，
        # 但蜜柑自己下发的 .torrent 是重新打包的（真实 infohash 不同），
        # 直接拿它拼磁力可能下不动，所以只给种子直链。
        m = re.search(r"/([0-9a-fA-F]{40})(?:\.torrent)?/?$", link or turl)
        ih = m.group(1) if m else ""

        out.append(
            item(
                "mikan",
                it.findtext("title"),
                link,
                auto_magnet=False,
                infohash=ih,
                torrent=turl,
                size=human_size(tlen),
                pubdate=pub.strip(),
                desc_html=it.findtext("description") or "",
            )
        )
    return out


# ------------------------------------------------------------------ ACG.RIP

ACGRIP_BASE = "https://acg.rip"
TR_NS = "{http://xmlns.ezrss.it/0.1/}"


def url_acgrip(p, host=None):
    base = (host or ACGRIP_BASE).rstrip("/")
    kw = (p.get("keyword") or "").strip()
    if kw:
        return f"{base}/.xml?term={quote(kw)}"
    return f"{base}/.xml"


def parse_acgrip(raw, p):
    ch = ET.fromstring(raw).find("channel")
    out = []
    for it in ch.findall("item"):
        enc = it.find("enclosure")
        turl = (enc.get("url") or "") if enc is not None else ""
        clen = it.findtext(TR_NS + "contentLength") or ""
        out.append(
            item(
                "acgrip",
                it.findtext("title"),
                it.findtext("link"),
                torrent=turl,
                size=human_size(clen),
                pubdate=(it.findtext("pubDate") or "").strip(),
                desc_html=it.findtext("description") or "",
            )
        )
    return out


# ------------------------------------------------------------------ Nyaa

NYAA_BASE = "https://nyaa.si"
NY_NS = "{https://nyaa.si/xmlns/nyaa}"


def url_nyaa(p, host=None):
    base = (host or NYAA_BASE).rstrip("/")
    kw = (p.get("keyword") or "").strip()
    q = "?page=rss&c=1_0&f=0"  # 1_0 = 全部 Anime
    if kw:
        q += "&q=" + quote(kw)
    return base + "/" + q


def parse_nyaa(raw, p):
    ch = ET.fromstring(raw).find("channel")
    out = []
    for it in ch.findall("item"):
        out.append(
            item(
                "nyaa",
                it.findtext("title"),
                it.findtext("guid"),
                infohash=(it.findtext(NY_NS + "infoHash") or "").strip(),
                torrent=(it.findtext("link") or "").strip(),
                size=(it.findtext(NY_NS + "size") or "").strip(),
                seeders=_int(it.findtext(NY_NS + "seeders")),
                pubdate=(it.findtext("pubDate") or "").strip(),
                category=(it.findtext(NY_NS + "category") or "").strip(),
                desc_html=it.findtext("description") or "",
            )
        )
    return out


# ------------------------------------------------------------------ 萌番组

BANGUMI_BASE = "https://bangumi.moe"


def url_bangumi(p, host=None):
    # 该站没有 RSS，用 JSON API；关键词在本地过滤
    base = (host or BANGUMI_BASE).rstrip("/")
    return f"{base}/api/torrent/page/1"


def parse_bangumi(raw, p):
    d = json.loads(raw)
    out = []
    for t in d.get("torrents") or []:
        out.append(
            item(
                "bangumi",
                t.get("title", ""),
                f"{BANGUMI_BASE}/torrent/{t.get('_id', '')}",
                magnet=t.get("magnet", ""),
                infohash=t.get("infoHash", ""),
                size=t.get("size", ""),
                seeders=_int(t.get("seeders")),
                pubdate=iso_to_rfc822(t.get("publish_time", "")),
                desc_html=t.get("introduction", "") or "",
            )
        )
    return out


# ------------------------------------------------------------------ AnimeGarden（动漫花园镜像）

# AnimeGarden 是动漫花园的第三方镜像站 + BT 聚合站，本体由 Cloudflare 承载，
# 国内直连通常比 share.dmhy.org 稳，作为「花园镜像分支」接入。
# 文档：https://animes.garden/docs/api   说明：https://animespace.onekuma.cn/animegarden/
AG_BASE = "https://api.animes.garden"

# provider 字段是原始站点的机器名，翻译一下给页面显示
AG_PROVIDERS = {
    "dmhy": "动漫花园",
    "moe": "萌番组",
    "ani": "ANi",
    "other": "其它",
}

# 资源类型：AnimeGarden 的 type 是中文，且不同 provider 简繁不一，两套都收
# （实测 dmhy 给「動畫」，moe 给「动画」）。日劇 / 音樂 / 其他 属噪音，默认剔除。
AG_ANIME_TYPES = {"動畫", "动画", "合集", "季度全集", "RAW", "ＲＡＷ"}


def url_animegarden(p, host=None):
    kw = (p.get("keyword") or "").strip()
    try:
        page = int(p.get("_page") or 1)
    except (TypeError, ValueError):
        page = 1
    q = {"page": str(max(1, page)), "pageSize": "100"}
    if kw:
        q["search"] = kw
    fansub = (p.get("fansub") or "").strip()
    if fansub:
        q["fansub"] = fansub
    from urllib.parse import urlencode

    base = (host or AG_BASE).rstrip("/")
    return base + "/resources?" + urlencode(q)


def parse_animegarden(raw, p):
    d = json.loads(raw)
    # 和趴花园一样默认只要动漫类；ag_all=1 拿全站，ag_cat=动画,合集 自选
    if str(p.get("ag_all") or "") in ("1", "true", "yes"):
        allow = None
    elif (p.get("ag_cat") or "").strip():
        allow = {x.strip() for x in p["ag_cat"].split(",") if x.strip()}
    elif (p.get("cat") or "").strip():
        allow = None
    else:
        allow = AG_ANIME_TYPES

    out = []
    for r in d.get("resources") or []:
        mag = (r.get("magnet") or "").strip()
        fansub = (r.get("fansub") or {}).get("name") or ""
        provider = (r.get("provider") or "").strip()
        rtype = (r.get("type") or "").strip()
        if allow is not None and rtype and rtype not in allow:
            continue
        publisher = (r.get("publisher") or {}).get("name") or ""
        bits = [AG_PROVIDERS.get(provider, provider), rtype, fansub, publisher]
        desc = "<p>" + " · ".join(x for x in bits if x) + "</p>"
        out.append(
            item(
                "animegarden",
                r.get("title"),
                r.get("href") or "",
                magnet=mag,
                infohash=infohash_of(mag),
                size=human_size(r.get("size")),
                pubdate=iso_to_rfc822(r.get("createdAt")),
                category=rtype,
                author=fansub or publisher,
                desc_html=desc,
            )
        )
    return out


# ------------------------------------------------------------------ 注册表

SOURCES = [
    {"id": "dmhy", "name": "动漫花园", "home": DMHY_BASE, "url": url_dmhy, "parse": parse_dmhy,
     "hosts": DMHY_HOSTS, "note": "中文资源最全，磁力直连；三个镜像域名自动切换"},
    {"id": "animegarden", "name": "花园镜像 AnimeGarden", "home": "https://animes.garden",
     "url": url_animegarden, "parse": parse_animegarden, "hosts": [AG_BASE],
     "note": "动漫花园官方镜像 + 聚合站，国内可达性更好"},
    {"id": "mikan", "name": "蜜柑计划", "home": MIKAN_BASE, "url": url_mikan, "parse": parse_mikan,
     "note": "番剧订阅为主，提供种子直链"},
    {"id": "acgrip", "name": "ACG.RIP", "home": ACGRIP_BASE, "url": url_acgrip, "parse": parse_acgrip,
     "note": "WebRip 画质好，字幕组集中"},
    {"id": "nyaa", "name": "Nyaa", "home": NYAA_BASE, "url": url_nyaa, "parse": parse_nyaa,
     "note": "国际站，RAW 与英译，带做种数"},
    {"id": "bangumi", "name": "萌番组", "home": BANGUMI_BASE, "url": url_bangumi, "parse": parse_bangumi,
     "note": "磁力链接完整，带做种数"},
]

BY_ID = {s["id"]: s for s in SOURCES}
DEFAULT_IDS = [s["id"] for s in SOURCES]


def resolve_ids(raw_param):
    """解析 src=mikan,nyaa -> 合法 id 列表"""
    if not raw_param:
        return list(DEFAULT_IDS)
    wanted = [x.strip().lower() for x in str(raw_param).split(",") if x.strip()]
    ids = [x for x in wanted if x in BY_ID]
    return ids or list(DEFAULT_IDS)


def hosts_of(sid):
    """该源的全部候选入口（含用户自定义反代）"""
    src = BY_ID.get(sid) or {}
    out = list(src.get("hosts") or [])
    if not out:
        home = src.get("home") or ""
        if home:
            out = [home]
    # 允许用环境变量把自己搭的反代插到最前面
    import os as _os
    env = (_os.environ.get("ANIME_RSS_%s_HOST" % sid.upper()) or "").strip()
    if env:
        out = [env.rstrip("/")] + [h for h in out if h.rstrip("/") != env.rstrip("/")]
    return out


def candidate_urls(sid, params, host):
    """同一入口下要抓的地址（萌番组搜索时翻页、AnimeGarden 翻页）"""
    if sid == "bangumi" and (params.get("keyword") or "").strip():
        return [f"{host.rstrip('/')}/api/torrent/page/{n}" for n in (1, 2)]
    if sid == "animegarden":
        try:
            pages = int(params.get("ag_pages") or 2)
        except (TypeError, ValueError):
            pages = 2
        pages = max(1, min(pages, 5))
        return [BY_ID[sid]["url"](dict(params, _page=n), host)
                for n in range(1, pages + 1)]
    return [BY_ID[sid]["url"](params, host)]


def fetch_candidates(sid, params):
    """返回 [(host, [url, ...]), ...]，按优先级排列，抓取时逐个尝试"""
    return [(h, candidate_urls(sid, params, h)) for h in hosts_of(sid)]


def fetch_urls(sid, params):
    """兼容旧调用：第一个入口的全部地址"""
    c = fetch_candidates(sid, params)
    return c[0][1] if c else []


def fetch_url(sid, params):
    u = fetch_urls(sid, params)
    return u[0] if u else ""


def parse_items(sid, raw, params):
    return BY_ID[sid]["parse"](raw, params)
