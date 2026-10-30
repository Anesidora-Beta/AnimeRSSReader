"""链接识别：把一堆杂链接归到「哪个网盘、哪种下载方式」。

这一层只做纯计算 —— 给一个 URL 或一个条目，回答三个问题：

1. 这是哪个网盘 / 哪种下载方式？（``classify``）
2. 这个条目里一共有哪些能下的东西？（``extract``）
3. 哪些链接能一起喂给同一个客户端？（``group``）

不联网、不碰文件系统、不启动任何程序。真正动手的是 ``wake.py``，
所以这一层可以随便测。

为什么自己写规则而不是查表
--------------------------
网盘的分享域名换得比版本号还勤（123 云盘就换过 123pan/123684/123865/123912
好几个），而且同一个网盘有 ``pan./yun./www.`` 多种前缀。把「域名 → 提供方」
写成紧凑的正则表，改一行就能跟上一个新域名，比维护一堆 if 实在。
"""
import re

__all__ = ["PROVIDERS", "KIND_TEXT", "provider", "classify", "extract",
           "group", "target_of", "parse_size"]

# ---------------------------------------------------------------- 提供方表
#
# kind 决定前端怎么筛、默认怎么唤醒：
#   bt   —— 磁力 / 电驴 / 迅雷专用链 / 种子文件 → 交给 BT 客户端，最可靠
#   pan  —— 网盘分享页 → 默认开网页（客户端协议各家不统一，见 scheme 说明）
#   file —— http 直链的媒体/压缩包 → 交给浏览器或 IDM
#   web  —— 资料站、群链接等 → 只能打开 / 复制
#
# scheme 是「如果装了对应客户端，Windows 里注册的协议名」。有的客户端注册了、
# 有的没注册，所以它只是「app 模式」的一种实现，默认不用 —— 见 README 的
# 「为什么网盘默认不直接拉客户端」。
PROVIDERS = (
    # ---------------- 国内网盘
    {"id": "baidu", "name": "百度网盘", "badge": "度", "kind": "pan",
     "color": "#2f6bff", "scheme": "baiduyun",
     "note": "分享页认 ?pwd= 提取码；没有会从摘要里找"},
    {"id": "alipan", "name": "阿里云盘", "badge": "阿", "kind": "pan",
     "color": "#6c5ce7", "scheme": "aliyundrive",
     "note": "新版域名 alipan.com 与旧版 aliyundrive.com 都认"},
    {"id": "quark", "name": "夸克网盘", "badge": "夸", "kind": "pan",
     "color": "#3b82f6", "scheme": "quark",
     "note": "quark.cn 与 pan.quark.cn 都认"},
    {"id": "pan123", "name": "123 云盘", "badge": "123", "kind": "pan",
     "color": "#00a3ff", "scheme": "123pan",
     "note": "换过 123pan / 123684 / 123865 / 123912 多个域名"},
    {"id": "xunlei", "name": "迅雷云盘", "badge": "迅", "kind": "pan",
     "color": "#1e90ff", "scheme": "thunder",
     "note": "pan.xunlei.com 的分享页；磁力另见 bt"},
    {"id": "pan115", "name": "115 网盘", "badge": "115", "kind": "pan",
     "color": "#f0b400", "scheme": "115",
     "note": "115.com / 115cdn.com / anxia.com"},
    {"id": "tianyi", "name": "天翼云盘", "badge": "翼", "kind": "pan",
     "color": "#e60012", "scheme": "cloud189",
     "note": "cloud.189.cn 的 /t/ 短链"},
    {"id": "cmcc", "name": "移动云盘", "badge": "移", "kind": "pan",
     "color": "#0089ff", "scheme": "cmcccloud",
     "note": "caiyun.139.com / yun.139.com"},
    {"id": "weiyun", "name": "腾讯微云", "badge": "微", "kind": "pan",
     "color": "#12b7f5", "scheme": "weiyun",
     "note": "share.weiyun.com"},
    {"id": "uc", "name": "UC 网盘", "badge": "UC", "kind": "pan",
     "color": "#ff7a00", "scheme": "uc",
     "note": "drive.uc.cn"},
    {"id": "lanzou", "name": "蓝奏云", "badge": "蓝", "kind": "pan",
     "color": "#4aa8ff", "scheme": "",
     "note": "没有通用客户端协议，走网页"},
    {"id": "pikpak", "name": "PikPak", "badge": "P", "kind": "pan",
     "color": "#7b61ff", "scheme": "pikpak",
     "note": "mypikpak.com"},
    {"id": "terabox", "name": "TeraBox", "badge": "T", "kind": "pan",
     "color": "#2f6bff", "scheme": "terabox",
     "note": "terabox.com / 1024tera.com"},
    {"id": "cowtransfer", "name": "奶牛快传", "badge": "奶", "kind": "pan",
     "color": "#00c2b3", "scheme": "",
     "note": "cowtransfer.com，多为临时链接"},
    {"id": "wenshushu", "name": "文叔叔", "badge": "文", "kind": "pan",
     "color": "#ffb400", "scheme": "",
     "note": "wenshushu.cn"},

    # ---------------- 国外网盘
    {"id": "gdrive", "name": "Google Drive", "badge": "G", "kind": "pan",
     "color": "#4285f4", "scheme": "",
     "note": "drive.google.com / docs.google.com（含表格形式的资源清单）"},
    {"id": "onedrive", "name": "OneDrive", "badge": "O", "kind": "pan",
     "color": "#0078d4", "scheme": "",
     "note": "1drv.ms 短链"},
    {"id": "mega", "name": "MEGA", "badge": "M", "kind": "pan",
     "color": "#d9272e", "scheme": "mega",
     "note": "mega.nz，装了 MEGAsync 可用 app 模式"},
    {"id": "dropbox", "name": "Dropbox", "badge": "D", "kind": "pan",
     "color": "#0061ff", "scheme": "",
     "note": "dropbox.com"},

    # ---------------- 下载方式
    {"id": "magnet", "name": "磁力链接", "badge": "BT", "kind": "bt",
     "color": "#e74c3c", "scheme": "",
     "note": "交给系统默认 BT 客户端（qB / 迅雷 / 比特彗星都行）"},
    {"id": "ed2k", "name": "eD2k 电驴", "badge": "驴", "kind": "bt",
     "color": "#8e44ad", "scheme": "",
     "note": "需要 eMule 或迅雷"},
    {"id": "thunder", "name": "迅雷专用链", "badge": "雷", "kind": "bt",
     "color": "#1e90ff", "scheme": "",
     "note": "thunder:// 开头的加密链，只有迅雷认"},
    {"id": "torrent", "name": "种子文件", "badge": "种", "kind": "bt",
     "color": "#2ecc71", "scheme": "",
     "note": ".torrent 直链"},

    # ---------------- 兜底
    {"id": "file", "name": "直链文件", "badge": "链", "kind": "file",
     "color": "#16a085", "scheme": "",
     "note": "http 上的 mkv/mp4/zip 等，可交给浏览器或 IDM"},
    {"id": "web", "name": "网页", "badge": "网", "kind": "web",
     "color": "#8a8f98", "scheme": "",
     "note": "资料站、群链接等，只做打开与复制"},
)

KIND_TEXT = {"pan": "网盘", "bt": "磁力 / 种子", "file": "直链", "web": "网页"}
KIND_ORDER = ("bt", "pan", "file", "web")

_BY_ID = {p["id"]: p for p in PROVIDERS}


def provider(pid):
    """按 id 取提供方；未知 id 返回 web 那条，调用方不用判空。"""
    return _BY_ID.get(pid) or _BY_ID["web"]


# ---------------------------------------------------------------- 域名规则
#
# 顺序有意义：先认协议头（magnet/ed2k/thunder），再认各网盘主机，
# 最后才是 .torrent / 文件后缀 / 网页兜底。加新网盘就往这里插一条。
_RULES = (
    ("magnet",  r"^magnet:\?"),
    ("ed2k",    r"^ed2k://"),
    ("thunder", r"^thunder://"),

    ("baidu",   r"^https?://(?:pan|yun)\.baidu\.com/"),
    ("baidu",   r"^https?://(?:www\.)?baidu\.com/s/"),
    ("alipan",  r"^https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/"),
    ("quark",   r"^https?://(?:pan\.)?quark\.cn/"),
    ("pan123",  r"^https?://(?:www\.)?(?:123pan|123684|123865|123912)\.(?:com|cn)/"),
    ("xunlei",  r"^https?://pan\.xunlei\.com/s/"),
    ("pan115",  r"^https?://(?:www\.)?(?:115|115cdn|anxia)\.com/s/"),
    ("tianyi",  r"^https?://cloud\.189\.cn/"),
    ("cmcc",    r"^https?://(?:caiyun|yun)\.139\.com/"),
    ("weiyun",  r"^https?://share\.weiyun\.com/"),
    ("uc",      r"^https?://drive\.uc\.cn/"),
    ("lanzou",  r"^https?://(?:[\w-]+\.)*(?:lanzou[a-z]?|lanzn|ilanzou)\.(?:com|cn)/"),
    ("pikpak",  r"^https?://(?:mypikpak|pikpak)\.com/"),
    ("terabox", r"^https?://(?:[\w-]+\.)*(?:terabox|1024tera)\.com/"),
    ("cowtransfer", r"^https?://(?:www\.)?cowtransfer\.com/"),
    ("wenshushu",   r"^https?://(?:www\.)?wenshushu\.cn/"),
    ("gdrive",  r"^https?://(?:drive|docs)\.google\.com/"),
    ("onedrive", r"^https?://(?:1drv\.ms|onedrive\.live\.com)/"),
    ("mega",    r"^https?://(?:mega\.nz|mega\.co\.nz)/"),
    ("dropbox", r"^https?://(?:www\.)?dropbox\.com/"),
)

_RULES_C = tuple((pid, re.compile(pat, re.I)) for pid, pat in _RULES)

# 直链文件的后缀。加上就不能算「网页」了，IDM 之类的才认得出来。
_FILE_EXT = re.compile(
    r"\.(?:mkv|mp4|avi|rmvb|wmv|flv|mov|ts|m2ts|webm|mp3|flac|wav|"
    r"zip|rar|7z|tar|gz|iso|exe|apk|pdf|epub|cbz|cbr)$", re.I)

_TORRENT_EXT = re.compile(r"\.torrent(?:$|\?)", re.I)

# 摘要里抓 URL。中英文标点都当断点，否则「链接见 https://x.com/a，提取码」
# 会把中文逗号一起吃进去。
_URL_RE = re.compile(
    r"(?:magnet:\?|ed2k://|thunder://|https?://)"
    r"[^\s\u3000\"'<>“”‘’()\[\]{}，。；：、！？（）【】《》]+", re.I)

# 「提取码: abcd」「密码：1234」「pwd=xx」
_PWD_RE = re.compile(
    r"(?:提取码|提取碼|访问码|訪問碼|验证码|驗證碼|密码|密碼|口令|pwd|password)"
    r"\s*[:：=]?\s*([A-Za-z0-9]{4,8})", re.I)

_TRAIL = ".,;:!?'\")]}>\u3002\uff0c\u3001\uff1b\uff1a"


def _clean(url):
    """去掉首尾空白与句末标点，还原 &amp; 这类 HTML 转义。"""
    u = (url or "").strip().strip(_TRAIL)
    u = u.replace("&amp;", "&").replace("&quot;", '"')
    return u


def classify(url):
    """判断一个链接属于谁。返回 (provider_id, 规范化后的 url)。

    认不出来一律当 web —— 宁可只给「打开/复制」，也不猜。
    """
    u = _clean(url)
    if not u:
        return "web", ""
    low = u.lower()
    if low.startswith("#") or low.startswith("javascript:"):
        return "web", u
    for pid, rx in _RULES_C:
        if rx.match(low):
            return pid, u
    if low.startswith(("http://", "https://")):
        if _TORRENT_EXT.search(low):
            return "torrent", u
        if _FILE_EXT.search(low.split("?")[0]):
            return "file", u
        return "web", u
    return "web", u


def parse_size(text):
    """把「1.2GB」「800 MB」解析成字节数；认不出返回 0。只用于排序展示。"""
    m = re.match(r"^\s*([\d.]+)\s*([KMGT]?)i?B?\s*$", str(text or ""), re.I)
    if not m:
        return 0
    try:
        n = float(m.group(1))
    except ValueError:
        return 0
    unit = (m.group(2) or "").upper()
    mul = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[unit]
    return int(n * mul)


def _with_pwd(url, text):
    """百度网盘这类分享页，链接里没带 pwd 就从文字里补一个。

    真实数据里大量条目是这样：链接只有 ``/s/1abc``，提取码写在摘要的
    「提取码: orio」里。补上之后复制出去就能直接打开，省一步人工对。
    """
    if "pwd=" in url.lower():
        return url, ""
    m = _PWD_RE.search(text or "")
    if not m:
        return url, ""
    code = m.group(1)
    sep = "&" if "?" in url else "?"
    return url + sep + "pwd=" + code, code


def _text_blob(item):
    """把标题 / 摘要 / 作者拼起来，供提取码与 URL 扫描用。"""
    parts = [item.get("title") or "", item.get("summary") or "",
             item.get("author") or ""]
    return "\n".join(p for p in parts if p)


def extract(item, allow_pwd=True, include_page=True):
    """从一个条目里抽出全部可下载链接。

    顺序刻意排成「磁力 → 种子 → links[] → 摘要里的 URL → 详情页」：前面的是
    确实能直接下的，最后那个是资源站页面（可能还得再点一层）。前端按这个
    顺序展示，用户第一眼看到的就是最该点的那个。

    ``include_page=False`` 时丢掉详情页与 web 类，只要「真能下的」。
    """
    if not item:
        return []
    # 这段文本有两个用途：扫摘要里的 URL、找「提取码: xxxx」。
    # 关掉自动补提取码只该关掉后者 —— 早先这里把整段文本一起清空了，
    # 结果「从摘要里抽链接」也一起失效，条目凭空少了一半链接。
    text = _text_blob(item)
    out, seen = [], set()

    def add(raw, source, kind_hint=""):
        pid, url = classify(raw)
        if not url or url in seen:
            return
        if not include_page and (pid == "web" or source == "link"):
            return
        pwd = ""
        if allow_pwd and pid == "baidu":
            url, pwd = _with_pwd(url, text)
            if url in seen:
                return
        seen.add(url)
        p = provider(pid)
        out.append({
            "url": url,
            "pid": pid,
            "name": p["name"],
            "badge": p["badge"],
            "kind": p["kind"],
            "color": p["color"],
            "pwd": pwd,
            "from": source,
            "hint": kind_hint,
        })

    add(item.get("magnet") or "", "magnet")
    add(item.get("torrent") or "", "torrent")
    for u in item.get("links") or []:
        if isinstance(u, str):
            add(u, "links")
    for u in _URL_RE.findall(text):
        add(u, "summary")
    if include_page:
        add(item.get("link") or "", "link")
    return out


def group(links):
    """按提供方归堆，用于「一起丢给同一个客户端」。

    返回 [{pid, name, badge, kind, color, urls:[...], count}]，顺序按
    KIND_ORDER（先磁力后网盘），同类内按 provider 表里的原始顺序 —— 这样
    界面每次刷新不会跳来跳去。
    """
    buckets = {}
    for ln in links:
        buckets.setdefault(ln["pid"], []).append(ln)
    rank = {p["id"]: i for i, p in enumerate(PROVIDERS)}
    groups = []
    for pid, items in buckets.items():
        p = provider(pid)
        groups.append({
            "pid": pid, "name": p["name"], "badge": p["badge"],
            "kind": p["kind"], "color": p["color"], "note": p["note"],
            "urls": [x["url"] for x in items],
            "count": len(items),
            "pwd": next((x["pwd"] for x in items if x.get("pwd")), ""),
        })
    groups.sort(key=lambda g: (KIND_ORDER.index(g["kind"])
                               if g["kind"] in KIND_ORDER else 9,
                               rank.get(g["pid"], 99)))
    return groups


# 默认唤醒动作。bt 走系统关联最靠谱（磁力一定有人认领），
# 网盘给网页 + 提取码，直链交给浏览器。
_DEFAULT_MODE = {"bt": "open", "pan": "web", "file": "open", "web": "web"}


def target_of(pid):
    """这个提供方的默认唤醒方式。"""
    p = provider(pid)
    return {"mode": _DEFAULT_MODE.get(p["kind"], "web"),
            "scheme": p.get("scheme") or "",
            "exe": "", "args": "{url}"}
