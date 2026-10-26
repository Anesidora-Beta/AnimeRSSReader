# -*- coding: utf-8 -*-
"""新番放送时间表：把多个平台的放送安排合并成同一张周历。

数据来自四个平台，各有各的口径：

    哔哩哔哩      官方追番时间表，精确到分钟，中文标题 + 封面（UTC+8）
    Bangumi     番组计划每周放送表，只有「星期几」，但中日文名齐全
    AniList      全球放送数据，带准确时间戳、下一集集数与总集数
    MyAnimeList  通过 Jikan 公共接口取放送日（英/日文标题）

同一部番在不同平台叫法不一样，所以先用「中文名 / 日文名 / 罗马字名」三套键
做并查集合并（中文名在 Bangumi 的 name_cn 和 B 站标题之间能对上，日文名在
Bangumi 的 name 和 AniList 的 native 之间能对上，于是三方串成一条链），
再用中文标题相似度兜一遍，最终一部番只出现一次，卡片上列出所有平台入口。

对外只暴露三件事：Service.payload() 拿现成的周历，Service.start() 触发刷新，
Service.status() 看进度 —— 其它都是内部实现。
"""

import gzip
import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 全站统一按北京时间排表 —— 用户看得懂的就是本机时间
TZ = timezone(timedelta(hours=8))
TZ_LABEL = "UTC+8"

WEEK = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
WEEK_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

TTL = 6 * 3600            # 周历缓存 6 小时：番剧表一天最多变几次
COOLDOWN_FAILS = 3        # 连续失败几次后暂时跳过该平台
COOLDOWN_SEC = 20 * 60    # 跳过多长时间

# 平台注册表：id 同时用作条目前缀与前端筛选键
PLATFORMS = [
    {"id": "bilibili", "name": "哔哩哔哩", "short": "B站", "color": "#fb7299",
     "note": "官方追番时间表，精确到分钟"},
    {"id": "bangumi", "name": "Bangumi 番组计划", "short": "番组", "color": "#f09199",
     "note": "每周放送表，中日文标题、评分与追番人数"},
    {"id": "anilist", "name": "AniList", "short": "AniList", "color": "#02a9ff",
     "note": "全球放送时间，带下一集与总集数"},
    {"id": "mal", "name": "MyAnimeList", "short": "MAL", "color": "#2e51a2",
     "note": "Jikan 公共接口，英/日文标题"},
]
PLATFORM_BY_ID = {p["id"]: p for p in PLATFORMS}

# 合并时的封面优先级：有版权的官方站封面最好看
COVER_RANK = {"bilibili": 4, "bangumi": 3, "anilist": 2, "mal": 1}


# ---------------------------------------------------------------- 网络

def _http_json(url, data=None, headers=None, timeout=20, retries=1):
    """取 JSON。带一次重试，失败抛出最后一次的异常。"""
    hdr = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    if headers:
        hdr.update(headers)
    body = None
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        hdr["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, data=body, headers=hdr,
                method="POST" if body is not None else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception as e:                 # 超时 / 证书 / 状态码都走这里
            last = e
            if attempt < retries:
                time.sleep(0.6)
    raise last


# ---------------------------------------------------------------- 时间工具

def local_of_ts(ts):
    """时间戳 -> (本地星期 0=周一, "HH:MM")"""
    dt = datetime.fromtimestamp(int(ts), TZ)
    return dt.weekday(), "%02d:%02d" % (dt.hour, dt.minute)


def ts_of(weekday, hhmm, after=None):
    """把「每周 weekday 的 hhmm」换算成下一次的时间戳（本地时区）"""
    if weekday is None or weekday < 0 or not hhmm:
        return 0
    try:
        hh, mm = [int(x) for x in str(hhmm).split(":")[:2]]
    except (TypeError, ValueError):
        return 0
    now = datetime.fromtimestamp(after or time.time(), TZ)
    days = (weekday - now.weekday()) % 7
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=days)
    if cand <= now:
        cand += timedelta(days=7)
    return int(cand.timestamp())


def _ep_no(text):
    """从「第7话」「第 7 集」「EP7」里抠出集数"""
    if text is None:
        return None
    if isinstance(text, int):
        return text
    m = re.search(r"(\d{1,4})", str(text))
    return int(m.group(1)) if m else None


_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_num(text):
    text = (text or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGIT.get(text[1:2], 0)
    if text.endswith("十"):
        return _CN_DIGIT.get(text[0], 0) * 10
    if "十" in text:
        a, b = text.split("十", 1)
        return _CN_DIGIT.get(a, 0) * 10 + _CN_DIGIT.get(b, 1)
    return _CN_DIGIT.get(text, 0)


def _season_no(title):
    """第几季。0 表示没写（当作第一季），99 表示最终季。"""
    s = unicodedata.normalize("NFKC", (title or "")).lower()
    m = re.search(r"第\s*([0-9一二两三四五六七八九十]+)\s*[季期部章]", s)
    if m:
        return _cn_num(m.group(1)) or 0
    m = re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)\s*season", s) or \
        re.search(r"season\s*(\d{1,2})", s) or \
        re.search(r"シーズン\s*(\d{1,2})", s) or \
        re.search(r"(\d{1,2})\s*(?:st|nd|rd|th)\s*stage", s)
    if m:
        return int(m.group(1))
    if re.search(r"最终季|final season", s):
        return 99
    return 0


# ---------------------------------------------------------------- 标题归一

_BRACKETS = re.compile(r"[\(（\[【][^\)）\]】]*[\)）\]】]")
_NOISE = re.compile(r"[\s:：·・!！?？,，.。、_\-–—~～'\"“”‘’「」『』/\\|＆&]+")


def _norm_title(s):
    """拿去比对的归一形式：小写、去括号内容、去季数、只留文字数字"""
    s = unicodedata.normalize("NFKC", (s or "")).lower()
    s = _BRACKETS.sub(" ", s)
    s = re.sub(r"\d{1,2}\s*(?:st|nd|rd|th)\s*(?:season|stage)", " ", s)
    s = re.sub(r"\bseason\s*\d{1,2}\b", " ", s)
    s = re.sub(r"シーズン\s*\d{1,2}", " ", s)
    s = re.sub(r"(第)?\s*[0-9一二两三四五六七八九十]+\s*(?:クール|期|季)", " ", s)
    s = re.sub(r"(全|共)\s*\d+\s*[话話集]", " ", s)
    s = _NOISE.sub("", s)
    return s


def _keys(entry):
    """一部番的所有可比对键；键里带季数，避免第 1 季和第 3 季被并成一条"""
    out = set()
    for t in (entry.get("titleCn"), entry.get("titleJp"), entry.get("titleEn")):
        k = _norm_title(t)
        if len(k) >= 3:
            out.add("%s#%d" % (k, _season_no(t)))
    return out


def _similar(a, b):
    """中文标题相似度（0~1），用来兜住平台之间多出来的副标题"""
    import difflib
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------- 条目构造

def _entry(platform, ident, **kw):
    d = {
        "id": "%s:%s" % (platform, ident),
        "platform": platform,
        "titleCn": "", "titleJp": "", "titleEn": "",
        "cover": "", "url": "",
        "weekday": None, "time": "", "airTs": 0,
        "episode": None, "episodes": None,
        "score": None, "heat": 0, "kind": "",
    }
    d.update({k: v for k, v in kw.items() if v is not None})
    return d


# ---------------------------------------------------------------- 各平台适配

def fetch_bilibili():
    """哔哩哔哩追番时间表：types 1=番剧 2=国创，两边都要"""
    out = []
    seen = set()
    for t in ("1", "2"):
        raw = _http_json(
            "https://api.bilibili.com/pgc/web/timeline?types=%s&before=0&after=7" % t,
            headers={"Referer": "https://www.bilibili.com/",
                     "Origin": "https://www.bilibili.com"},
            timeout=15)
        for day in raw.get("result") or []:
            for ep in day.get("episodes") or []:
                title = (ep.get("title") or "").strip()
                if not title:
                    continue
                sid = ep.get("season_id") or ep.get("episode_id")
                key = str(sid)
                ts = int(ep.get("pub_ts") or 0)
                if key in seen:
                    continue
                seen.add(key)
                wd, hm = local_of_ts(ts) if ts else (None, ep.get("pub_time") or "")
                ep_no = _ep_no(ep.get("pub_index"))
                if ts and ts < time.time() - 86400:      # 已经播完一天以上的旧集不要
                    continue
                out.append(_entry(
                    "bilibili", key,
                    titleCn=title,
                    cover=(ep.get("cover") or ep.get("square_cover") or "")
                          .replace("http://", "https://"),
                    url=("https://www.bilibili.com/bangumi/play/ep%s" % ep["episode_id"]
                         if ep.get("episode_id") else
                         "https://www.bilibili.com/bangumi/play/ss%s" % sid),
                    weekday=wd,
                    time=hm,
                    airTs=ts,
                    episode=ep_no,
                ))
    return out


def fetch_bangumi():
    """Bangumi 番组计划：只有星期几，集数按首播日期推"""
    raw = _http_json(
        "https://api.bgm.tv/calendar",
        headers={"User-Agent": "AnimeRSSReader/1.4 (https://github.com/Anesidora-Beta/AnimeRSSReader)"},
        timeout=20)
    today = datetime.now(TZ).date()
    out = []
    for day in raw or []:
        wd = ((day.get("weekday") or {}).get("id") or 0) - 1     # 1..7 -> 0..6
        if wd < 0:
            continue
        for it in day.get("items") or []:
            name = (it.get("name") or "").strip()
            cn = (it.get("name_cn") or "").strip()
            if not name and not cn:
                continue
            # 集数：首播日 + 每周一集推算，超出合理范围就当不知道
            ep = None
            try:
                air = datetime.strptime((it.get("air_date") or "")[:10], "%Y-%m-%d").date()
                if air <= today:
                    ep = (today - air).days // 7 + 1
                    if ep < 1 or ep > 500:
                        ep = None
            except ValueError:
                ep = None
            rating = (it.get("rating") or {}).get("score")
            heat = ((it.get("collection") or {}).get("doing") or 0)
            out.append(_entry(
                "bangumi", it.get("id"),
                titleCn=cn, titleJp=name,
                cover=((it.get("images") or {}).get("large") or "")
                      .replace("http://", "https://"),
                url="https://bgm.tv/subject/%s" % it.get("id"),
                weekday=wd, time="",
                episode=ep,
                score=rating or None, heat=heat,
                kind="TV" if it.get("type") == 2 else "",
            ))
    return out


ANILIST_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    media(type: ANIME, status: RELEASING, sort: [POPULARITY_DESC]) {
      id
      title { romaji native english }
      coverImage { large extraLarge }
      siteUrl
      format
      episodes
      averageScore
      popularity
      nextAiringEpisode { airingAt episode }
    }
  }
}
"""


def fetch_anilist(pages=3):
    """AniList：唯一给得出「下一次放送的准确时刻」的源"""
    out = []
    for page in range(1, max(1, pages) + 1):
        raw = _http_json("https://graphql.anilist.co",
                         data={"query": ANILIST_QUERY,
                               "variables": {"page": page, "perPage": 50}},
                         timeout=20)
        media = (((raw.get("data") or {}).get("Page") or {}).get("media") or [])
        if not media:
            break
        for m in media:
            nxt = m.get("nextAiringEpisode") or {}
            ts = int(nxt.get("airingAt") or 0)
            if not ts:
                # 没有下一集时间的（PV / CM / 已完结的 OVA 之类）排不进周历，直接丢掉
                continue
            titles = m.get("title") or {}
            cover = (m.get("coverImage") or {})
            wd, hm = local_of_ts(ts)
            out.append(_entry(
                "anilist", m.get("id"),
                titleJp=(titles.get("native") or "").strip(),
                titleEn=(titles.get("romaji") or titles.get("english") or "").strip(),
                cover=(cover.get("extraLarge") or cover.get("large") or ""),
                url=m.get("siteUrl") or "",
                weekday=wd,
                time=hm,
                airTs=ts,
                episode=_ep_no(nxt.get("episode")),
                episodes=m.get("episodes"),
                score=(m.get("averageScore") or 0) / 10.0 or None,
                heat=m.get("popularity") or 0,
                kind=(m.get("format") or "").title(),
            ))
    return out


MAL_DAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6}


def fetch_mal(pages=1):
    """MyAnimeList（Jikan 公共接口）。被墙时整源跳过，不影响其它三个。"""
    out = []
    for page in range(1, max(1, pages) + 1):
        raw = _http_json(
            "https://api.jikan.moe/v4/seasons/now?limit=25&sfw=true&page=%d" % page,
            timeout=10, retries=0)
        for m in raw.get("data") or []:
            bc = m.get("broadcast") or {}
            day = (bc.get("day") or "").strip().lower()
            wd = MAL_DAYS.get(day)
            hm = ""
            # Jikan 的放送时间按 JST 给，换算成北京时间要减 1 小时，跨零点的星期跟着退一天
            mt = re.match(r"^(\d{1,2}):(\d{2})$", (bc.get("time") or "").strip())
            if mt:
                hh, mm = int(mt.group(1)), int(mt.group(2))
                hh -= 1
                if hh < 0:
                    hh += 24
                    if wd is not None:
                        wd = (wd - 1) % 7
                hm = "%02d:%02d" % (hh, mm)
            out.append(_entry(
                "mal", m.get("mal_id"),
                titleEn=(m.get("title_english") or m.get("title") or "").strip(),
                titleJp=(m.get("title_japanese") or "").strip(),
                cover=(((m.get("images") or {}).get("jpg") or {}).get("large_image_url")
                       or ((m.get("images") or {}).get("jpg") or {}).get("image_url") or ""),
                url=m.get("url") or "",
                weekday=wd, time=hm,
                episodes=m.get("episodes"),
                score=m.get("score"),
                heat=m.get("members") or 0,
                kind="TV",
            ))
    return out


FETCHERS = {
    "bilibili": fetch_bilibili,
    "bangumi": fetch_bangumi,
    "anilist": fetch_anilist,
    "mal": fetch_mal,
}


# ---------------------------------------------------------------- 合并

def merge(entries):
    """跨平台并查集：同一部番只留一条，其余信息按平台优先级补齐"""
    n = len(entries)
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

    by_key = {}
    keyed = []
    for i, e in enumerate(entries):
        ks = _keys(e)
        keyed.append(ks)
        for k in ks:
            if k in by_key:
                union(by_key[k], i)
            else:
                by_key[k] = i

    # 第二遍：中文名相似度兜底（0.86 足够区分「药屋少女的呢喃」和「药屋少女的呢喃 第三季」，
    # 又不会把完全不同的番并到一起），季数不同的一律不并
    groups_cn = {}
    for i, e in enumerate(entries):
        cn = _norm_title(e.get("titleCn"))
        if len(cn) < 4:
            continue
        groups_cn.setdefault(_season_no(e.get("titleCn")), []).append((i, cn))
    for _season, rows in groups_cn.items():
        for a in range(len(rows)):
            for b in range(a + 1, len(rows)):
                if find(rows[a][0]) == find(rows[b][0]):
                    continue
                if _similar(rows[a][1], rows[b][1]) >= 0.86:
                    union(rows[a][0], rows[b][0])

    buckets = {}
    order = []
    for i in range(n):
        r = find(i)
        if r not in buckets:
            buckets[r] = []
            order.append(r)
        buckets[r].append(entries[i])

    return [_combine(buckets[r]) for r in order]


def _combine(group):
    """一组同番条目 -> 一张卡片需要的数据"""
    # 排在前面的是信息更可信的平台（B 站/AniList 有准确时刻，Bangumi 有中日文名）
    group = sorted(group, key=lambda e: (COVER_RANK.get(e["platform"], 0)),
                   reverse=True)

    def pick(field):
        for e in group:
            v = e.get(field)
            if v not in (None, "", 0):
                return v
        return group[0].get(field)

    title_cn = pick("titleCn")
    title_jp = pick("titleJp")
    title_en = pick("titleEn")

    # 下次放送：带准确时间戳的平台里挑最近一次还没过太久的；
    # 全都过期就拿最近的那个时间做「每周几点」的参照。
    now = int(time.time())
    stampeds = [(int(e.get("airTs") or 0), e) for e in group]
    stampeds = [x for x in stampeds if x[0] > 0]
    upcoming = sorted([x for x in stampeds if x[0] >= now - 3 * 3600],
                      key=lambda x: x[0])
    if upcoming:
        anchor, air_ts = upcoming[0][1], upcoming[0][0]
    elif stampeds:
        anchor, air_ts = max(stampeds, key=lambda x: x[0])[1], 0
    else:
        anchor, air_ts = group[0], 0

    weekday = anchor.get("weekday")
    hhmm = anchor.get("time") or ""
    if air_ts:
        weekday, hhmm = local_of_ts(air_ts)
    for e in group:                       # 没时间的平台（Bangumi）补星期几
        if weekday is None and e.get("weekday") is not None:
            weekday = e["weekday"]
        if not hhmm and e.get("time"):
            hhmm = e["time"]

    episodes = None
    for e in group:
        if e.get("episodes"):
            episodes = max(episodes or 0, int(e["episodes"]))
    episode = anchor.get("episode")
    if not episode:
        for e in group:
            if e.get("episode"):
                episode = e["episode"]
                break
    episode = int(episode) if episode else None

    score = next((float(e["score"]) for e in group
                  if e.get("platform") == "bangumi" and e.get("score")), None)
    if score is None:
        score = next((float(e["score"]) for e in group
                      if e.get("score") and float(e["score"]) <= 10), None)

    links = []
    for e in sorted(group, key=lambda x: COVER_RANK.get(x["platform"], 0), reverse=True):
        p = PLATFORM_BY_ID.get(e["platform"]) or {}
        if e.get("url"):
            links.append({"id": e["platform"], "name": p.get("short") or e["platform"],
                          "full": p.get("name") or e["platform"], "url": e["url"]})
    via = [l["id"] for l in links]

    title = title_cn or title_jp or title_en
    ident = "%s#%s" % (_norm_title(title)[:40], _season_no(title))
    return {
        "id": "m:" + hashlib.sha1(ident.encode("utf-8", "ignore")).hexdigest()[:12],
        "title": title,
        "titleCn": title_cn or "",
        "titleJp": title_jp or "",
        "titleEn": title_en or "",
        "cover": pick("cover") or "",
        "weekday": weekday if weekday is not None else -1,
        "time": hhmm,
        "airTs": air_ts,
        "nextTs": air_ts if air_ts > int(time.time()) - 1800 else max(air_ts, 0),
        "episode": episode,
        "episodes": episodes,
        "score": round(score, 1) if score else None,
        "heat": max([int(e.get("heat") or 0) for e in group] or [0]),
        "kind": pick("kind") or "",
        "links": links,
        "via": via,
        "primaryUrl": (links[0]["url"] if links else ""),
    }


def build(entries):
    """合并 + 排序 + 补齐展示字段"""
    merged = merge(entries)
    now = int(time.time())
    for e in merged:
        # 每周重算「接下来这次」：过了就顺延到下周同一时间
        if e["weekday"] >= 0 and e["time"]:
            nxt = ts_of(e["weekday"], e["time"], now)
            if e["airTs"] and e["airTs"] > now - 1800:
                nxt = e["airTs"]
            e["nextTs"] = nxt
        elif e["airTs"]:
            e["nextTs"] = e["airTs"]
        else:
            e["nextTs"] = 0
        e["soon"] = 0 < e["nextTs"] - now <= 86400
    merged.sort(key=lambda e: (e["time"] or "99:99", -(e["heat"] or 0)))
    return merged


def collect(only=None):
    """并发抓所有平台，返回 (条目, 各来源状态)"""
    results, sources = [], []
    todo = [p for p in PLATFORMS if not only or p["id"] in only]
    with ThreadPoolExecutor(max_workers=max(1, len(todo))) as ex:
        futs = {ex.submit(_timed, p["id"]): p for p in todo}
        for f in as_completed(futs):
            p = futs[f]
            items, err, ms = f.result()
            sources.append({"id": p["id"], "name": p["name"], "short": p["short"],
                            "color": p["color"], "note": p["note"],
                            "ok": not err, "count": len(items),
                            "error": err, "ms": ms, "cached": False})
            results.extend(items)
    order = {p["id"]: i for i, p in enumerate(PLATFORMS)}
    sources.sort(key=lambda s: order.get(s["id"], 99))
    return results, sources


def _timed(sid):
    t0 = time.time()
    try:
        items = FETCHERS[sid]()
        return items, "", int((time.time() - t0) * 1000)
    except Exception as e:
        msg = "%s: %s" % (type(e).__name__, e)
        return [], msg[:200], int((time.time() - t0) * 1000)


# ---------------------------------------------------------------- 服务

class Service:
    """周历的读取 / 后台刷新。内存里留一份，SQLite 里留一份，断网也能看。"""

    def __init__(self, store, enabled=None, ttl=TTL):
        self.store = store
        self._lock = threading.Lock()
        self._data = None
        self._loaded = False
        self._last_attempt = 0
        self._fails = {}          # platform -> [连续失败次数, 冷却截止]
        self.enabled = list(enabled) if enabled else [p["id"] for p in PLATFORMS]
        self.ttl = ttl
        self.state = {"running": False, "done": 0, "total": 0,
                      "message": "就绪", "started": 0, "finished": 0}

    # ---------------- 设置联动
    def configure(self, enabled=None, ttl=None):
        """设置里改了来源开关 / 缓存时长后调用，下一次抓取按新配置走"""
        if enabled is not None:
            keep = [p["id"] for p in PLATFORMS if p["id"] in set(enabled)]
            self.enabled = keep or [p["id"] for p in PLATFORMS]
        if ttl is not None:
            self.ttl = max(60, int(ttl))

    def active_platforms(self):
        return [p for p in PLATFORMS if p["id"] in self.enabled]

    # ---------------- 读取
    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            self._data = self.store.load_schedule()
        except Exception:
            self._data = {"entries": [], "sources": [], "updated": 0}

    def data(self):
        self._ensure_loaded()
        return self._data or {"entries": [], "sources": [], "updated": 0}

    def stale(self, ttl=None):
        return (time.time() - (self.data().get("updated") or 0)) > (ttl or self.ttl)

    def today_count(self):
        wd = datetime.now(TZ).weekday()
        return sum(1 for e in self.data().get("entries") or []
                   if e.get("weekday") == wd)

    # ---------------- 刷新
    def status(self):
        with self._lock:
            s = dict(self.state)
        d = self.data()
        s["updated"] = d.get("updated") or 0
        s["age"] = int(time.time() - (d.get("updated") or 0)) if d.get("updated") else -1
        s["total"] = s.get("total") or len(d.get("entries") or [])
        return s

    def start(self, force=False):
        with self._lock:
            if self.state["running"]:
                return False
            self.state.update({"running": True, "done": 0,
                               "total": len(self.active_platforms()),
                               "started": time.time(),
                               "finished": 0, "message": "准备抓取放送表…"})
        threading.Thread(target=self._run, args=(force,), daemon=True).start()
        return True

    def should_auto_refresh(self):
        """打开页面时要不要顺手抓一次：没数据、或者缓存过期了就抓。
        刚失败过就等一会儿，免得每次请求都卡在同一个连不上的站上。"""
        if self.state["running"]:
            return False
        if time.time() - self._last_attempt < 120:
            return False
        return (not self.data().get("entries")) or self.stale()

    def refresh_sync(self):
        self._run(True)
        return self.status()

    def _cooling(self, sid, force=False):
        if force:
            self._fails.pop(sid, None)
            return False
        fails, until = self._fails.get(sid, (0, 0))
        return fails >= COOLDOWN_FAILS and until > time.time()

    def _run(self, force=False):
        self._last_attempt = time.time()
        try:
            wanted = self.active_platforms()
            todo = [p for p in wanted if not self._cooling(p["id"], force)]
            skipped = [p for p in wanted if p not in todo]
            entries, sources = collect([p["id"] for p in todo])
            for p in skipped:
                sources.append({"id": p["id"], "name": p["name"], "short": p["short"],
                                "color": p["color"], "note": p["note"],
                                "ok": False, "count": 0, "cached": True,
                                "error": "连续失败，已暂时跳过（手动刷新可重试）",
                                "ms": 0})
            order = {p["id"]: i for i, p in enumerate(PLATFORMS)}
            sources.sort(key=lambda s: order.get(s["id"], 99))
            for s in sources:
                if s["error"] and not s["cached"]:
                    fails, _ = self._fails.get(s["id"], (0, 0))
                    self._fails[s["id"]] = (fails + 1, time.time() + COOLDOWN_SEC)
                elif s["ok"]:
                    self._fails.pop(s["id"], None)

            merged = build(entries)
            if not merged:
                raise RuntimeError("各平台都没取到数据")

            now = int(time.time())
            try:
                self.store.save_schedule(merged, sources, now)
            except Exception:
                pass          # 落库失败不影响这次展示
            self._data = {"entries": merged, "sources": sources, "updated": now}
            with self._lock:
                self.state["message"] = "完成，共 %d 部" % len(merged)
                self.state["done"] = len(wanted)
        except Exception as e:
            with self._lock:
                self.state["message"] = "抓取失败：%s" % e
        finally:
            with self._lock:
                self.state["running"] = False
                self.state["finished"] = time.time()

    # ---------------- 给前端的结构
    def payload(self):
        data = self.data()
        entries = data.get("entries") or []
        # 设置里关掉的来源不再展示（数据要等下一次抓取才会真正清掉）
        if self.enabled:
            keep = set(self.enabled)
            entries = [e for e in entries
                       if keep & set(e.get("via") or [e.get("platform")])]
        now = int(time.time())
        today = datetime.now(TZ)
        monday = today.date() - timedelta(days=today.weekday())

        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            rows = [e for e in entries if e.get("weekday") == i]
            rows.sort(key=lambda e: (e.get("time") or "99:99",
                                     -int(e.get("nextTs") or 0),
                                     -(e.get("heat") or 0)))
            days.append({
                "weekday": i,
                "label": WEEK[i],
                "en": WEEK_EN[i],
                "date": "%02d-%02d" % (d.month, d.day),
                "isToday": i == today.weekday(),
                "count": len(rows),
                "items": rows,
            })

        unknown = sorted([e for e in entries if e.get("weekday") not in range(7)],
                         key=lambda e: -(e.get("heat") or 0))
        if unknown:
            days.append({"weekday": -1, "label": "时间待定", "en": "TBA",
                         "date": "", "isToday": False,
                         "count": len(unknown), "items": unknown[:60]})

        upcoming = sorted([e for e in entries if (e.get("nextTs") or 0) > now],
                          key=lambda e: e["nextTs"])[:10]

        by_platform = {}
        for e in entries:
            for pid in e.get("via") or [e["platform"]]:
                by_platform[pid] = by_platform.get(pid, 0) + 1

        # 落库时只存了 id / ok / count，名称和配色在这里补齐
        sources = []
        for s in data.get("sources") or []:
            p = PLATFORM_BY_ID.get(s["id"]) or {}
            sources.append({**s,
                            "name": p.get("name") or s["id"],
                            "short": p.get("short") or s["id"],
                            "color": p.get("color") or "#8d96a4",
                            "note": p.get("note") or ""})
        known = {s["id"] for s in sources}
        for p in self.active_platforms():         # 还没抓过的平台也列出来
            if p["id"] not in known:
                sources.append({"id": p["id"], "name": p["name"],
                                "short": p["short"], "color": p["color"],
                                "note": p["note"], "ok": False, "count": 0,
                                "error": "", "ms": 0, "cached": False})
        if self.enabled:
            sources = [s for s in sources if s["id"] in set(self.enabled)]

        return {
            "ok": True,
            "updated": data.get("updated") or 0,
            "age": int(now - (data.get("updated") or 0)) if data.get("updated") else -1,
            "stale": self.stale(),
            "refreshing": self.state["running"],
            "message": self.state["message"],
            "timezone": TZ_LABEL,
            "platforms": [{"id": p["id"], "name": p["name"], "short": p["short"],
                           "color": p["color"], "note": p["note"],
                           "on": p["id"] in self.enabled}
                          for p in PLATFORMS],
            "sources": sources,
            "days": days,
            "upcoming": upcoming,
            "stats": {
                "total": len(entries),
                "today": sum(1 for e in entries if e.get("weekday") == today.weekday()),
                "merged": sum(len(e.get("via") or []) for e in entries),
                "byPlatform": by_platform,
            },
        }


# ---------------------------------------------------------------- 自测

if __name__ == "__main__":
    # python schedule.py —— 直接跑一次抓取，看看合并效果（不进数据库）
    t0 = time.time()
    rows, srcs = collect()
    print("\n来源：")
    for s in srcs:
        print("  %-10s %s %3d 部  %5dms %s" % (
            s["id"], "OK  " if s["ok"] else "FAIL", s["count"], s["ms"], s["error"]))
    merged = build(rows)
    print("\n原始 %d 条 -> 合并后 %d 部（%.1fs）" % (len(rows), len(merged), time.time() - t0))
    days = {}
    for e in merged:
        days.setdefault(e["weekday"], []).append(e)
    for wd in sorted(days):
        label = WEEK[wd] if 0 <= wd < 7 else "待定"
        print("\n[%s] %d 部" % (label, len(days[wd])))
        for e in days[wd][:8]:
            bits = [e["time"] or "--:--", e["title"]]
            if e["episode"]:
                bits.append("第%d话" % e["episode"])
            if e["score"]:
                bits.append("★%s" % e["score"])
            print("   " + "  ".join(bits) + "   ← " + ",".join(e["via"]))
