# -*- coding: utf-8 -*-
"""文本清洗 / 磁力规整 / 时间处理。

从上游 anime-rss 的 server.py 抽出，聚合层和阅读器共用。
"""

import html
import re
import urllib.parse
from email.utils import parsedate_to_datetime

TAG_RE = re.compile(r"<[^>]+>")
IMG_RE = re.compile(r"<img[^>]*>", re.I)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)
BARE_URL_RE = re.compile(r"""https?://[^\s<>"']+""")
MAGNET_RE = re.compile(r"""magnet:\?[^\s"'<>\\)]+""", re.I)

SKIP_HOST_PARTS = (
    "tracker.", "t.acg.rip", "open.acgtracker", "nyaa.tracker",
    "tr.bangumi.moe", "bangumi.moe/tracker", "google-analytics",
    "googletagmanager", "doubleclick",
)
SKIP_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".ico",
    ".css", ".js", ".woff", ".woff2",
)
SITE_HOSTS = (
    "share.dmhy.org", "mikanani.me", "acg.rip", "nyaa.si", "bangumi.moe",
)


def clean_text(s, limit=None):
    """HTML -> 可读纯文本"""
    if not s:
        return ""
    s = IMG_RE.sub(" ", s)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|li|tr|h\d)>", "\n", s, flags=re.I)
    s = TAG_RE.sub("", s)
    s = html.unescape(s)
    s = TAG_RE.sub("", s)
    s = re.sub(r"<[^>]*$", "", s)          # 源站正文被截断时残留的半截标签
    s = s.replace("\u00a0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def extract_links(desc_html):
    """从正文里抽出有意义的外链（去重、去噪）"""
    if not desc_html:
        return []
    raw = HREF_RE.findall(desc_html) + BARE_URL_RE.findall(desc_html)
    out, seen = [], set()
    for h in raw:
        h = html.unescape(h).strip().rstrip(").,;）】")
        if not h.lower().startswith(("http://", "https://")):
            continue
        m = re.match(r"https?://([^/?#]+)", h)
        if not m:
            continue
        host = m.group(1).lower()
        if any(host.endswith(x) for x in SITE_HOSTS):
            continue
        if any(p in host for p in SKIP_HOST_PARTS):
            continue
        if h.lower().split("?")[0].endswith(SKIP_EXT):
            continue
        key = h.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return out[:12]


def find_magnet(text):
    """从任意 HTML / 文本里捞出第一个磁力链接"""
    if not text:
        return ""
    m = MAGNET_RE.search(text)
    return m.group(0) if m else ""


def slim_magnet(url, title="", keep_tr=3):
    """磁力规整：btih 统一成 40 位 hex、补上显示名称、只留前几个 tracker。

    源站给的磁力有两个毛病，直接转发会明显影响可用性：
      1. 动漫花园用 base32 编码 btih（32 位），部分下载器只认 hex，统一转换；
      2. dn 参数经常缺失或为空串，下载器里就只剩一串哈希，看不出是什么番。
    """
    if not url or not url.startswith("magnet:"):
        return url or ""
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", url, flags=re.I)
    if not m:
        return url
    ih = norm_infohash(m.group(1))
    if not ih or len(ih) != 40:
        return url

    dn = ""
    dm = re.search(r"[&?]dn=([^&]*)", url)
    if dm:
        dn = urllib.parse.unquote(dm.group(1)).strip()
    if not dn and title:
        dn = title.strip()

    parts = [f"magnet:?xt=urn:btih:{ih}"]
    if dn:
        parts.append("dn=" + urllib.parse.quote(dn, safe=""))
    for i, t in enumerate(re.findall(r"[&?]tr=([^&]*)", url)):
        if i >= keep_tr:
            break
        parts.append("tr=" + t)
    return "&".join(parts)


def norm_infohash(h):
    """统一成 40 位小写 hex（动漫花园给的是 base32）"""
    import base64

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


def to_ts(pubdate):
    """RFC822 / ISO8601 / 各种乱七八糟的时间 -> 秒级时间戳（失败返回 0）"""
    s = (pubdate or "").strip()
    if not s:
        return 0
    try:
        return int(parsedate_to_datetime(s).timestamp())
    except Exception:
        pass
    txt = s.replace("Z", "+00:00")
    from datetime import datetime

    for fmt in (None, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            if fmt:
                return int(datetime.strptime(txt, fmt).timestamp())
            dt = datetime.fromisoformat(txt)
            return int(dt.timestamp())
        except Exception:
            continue
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", txt)
    if m:
        from datetime import datetime as _dt

        try:
            return int(_dt(*[int(x) for x in m.groups()]).timestamp())
        except Exception:
            return 0
    return 0


def rfc822(ts):
    """时间戳 -> RSS 用的 RFC822"""
    from email.utils import formatdate

    try:
        return formatdate(int(ts), usegmt=False)
    except Exception:
        return ""
