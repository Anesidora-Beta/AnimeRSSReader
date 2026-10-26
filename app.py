#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""动漫资源 RSS 阅读器 —— 单进程应用入口。

把「多站动漫资源聚合」和「RSS 阅读器」合成一个可直接运行的小应用：
  * 后端：纯 Python 标准库（无需 pip install），内置五个资源站适配 + 通用 RSS 订阅
  * 前端：单页三栏阅读器，静态资源全部本地，离线也能开
  * 数据：SQLite 单文件，放在 data/ 下，随包携带，复制走即可

用法:
    python app.py                    # 只监听本机，自动打开独立窗口
    python app.py --lan              # 同 WiFi 的手机 / 平板也能访问
    python app.py --port 9000 --no-browser
    python app.py --browser          # 改用默认浏览器的普通标签页
"""

import argparse
import gzip
import html
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import feeds as F
import sources as S
import store as ST

APP_NAME = "动漫资源 · RSS 阅读器"
VERSION = "1.3.0"

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):          # PyInstaller 单文件模式
    HERE = getattr(sys, "_MEIPASS", HERE) or HERE
    _BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE = HERE
DATA_DIR = os.environ.get("ANIME_RSS_DATA") or os.path.join(_BASE, "data")
STATIC_DIR = os.path.join(HERE, "static")

STORE = None
REFRESHER = None
START_TS = time.time()
LAN_MODE = False
PORT = 8899


# ---------------------------------------------------------------- 静态资源

_ASSET_CACHE = {}
ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".woff2": "font/woff2",
}


def read_asset(name):
    """读取 static/ 下的文件（按 mtime 缓存，改文件即时生效）"""
    safe = os.path.basename(name)
    path = os.path.join(STATIC_DIR, safe)
    if not os.path.isfile(path):
        return None
    mtime = os.path.getmtime(path)
    hit = _ASSET_CACHE.get(safe)
    if hit and hit[0] == mtime:
        return hit[1]
    with open(path, "rb") as f:
        data = f.read()
    _ASSET_CACHE[safe] = (mtime, data)
    return data


def local_ip():
    """拿一个本机在局域网里的地址，用来给手机扫码/输入"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def source_names():
    """source id -> 显示名（内置站点 + 自定义订阅源标题）"""
    names = {s["id"]: s["name"] for s in S.SOURCES}
    try:
        for f in STORE.feeds():
            names.setdefault(f["id"], f["title"])
    except Exception:
        pass
    return names


# ---------------------------------------------------------------- 独立窗口

WIN_MAC_EDGE = (
    r"Microsoft\Edge\Application\msedge.exe",
    r"Google\Chrome\Application\chrome.exe",
)


def find_chromium():
    """找一个 Chromium 内核浏览器（Edge / Chrome），用它开无地址栏的独立窗口"""
    if os.name == "nt":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        for root in roots:
            if not root:
                continue
            for rel in WIN_MAC_EDGE:
                path = os.path.join(root, rel)
                if os.path.isfile(path):
                    return path
        return None
    if sys.platform == "darwin":
        for path in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                     "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"):
            if os.path.isfile(path):
                return path
        return None
    for name in ("google-chrome-stable", "google-chrome", "chromium",
                 "chromium-browser", "microsoft-edge"):
        path = shutil.which(name)
        if path:
            return path
    return None


def ask_close_action():
    """关窗后的选择框：返回 True 表示退出，False 表示留在后台。

    用系统 MessageBox（YesNo + 问号图标），不引入任何 GUI 依赖。
    MessageBoxW 在部分环境下会「立刻返回 0」表示没能弹出来，
    所以带一次降级重试，确保每次关窗都真的问到人。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes
        MB_YESNO = 0x4
        MB_ICONQUESTION = 0x20
        MB_DEFBUTTON2 = 0x100        # 默认落在「否」，避免回车误退出
        MB_TOPMOST = 0x40000
        MB_SETFOREGROUND = 0x10000
        text = ("主窗口已关闭。\n\n"
                "是否退出程序？\n\n"
                "　【是】退出，服务停止\n"
                "　【否】不退出，留在后台继续运行\n"
                "　　　　（手机或其它浏览器仍可访问）")
        for attempt in range(3):
            flags = MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
            if attempt == 0:
                flags |= MB_TOPMOST | MB_SETFOREGROUND
            started = time.time()
            ret = ctypes.windll.user32.MessageBoxW(None, text, APP_NAME, flags)
            _debug(f"MessageBox attempt={attempt} ret={ret} "
                   f"elapsed={time.time() - started:.2f}s")
            if ret in (6, 7):            # IDYES / IDNO，真的有人点了
                return ret != 7
            time.sleep(0.15)             # 没弹出来，换个参数再来一次
        _debug("MessageBox 始终没弹出，按退出处理")
        return True
    except Exception as e:
        _debug(f"MessageBox 异常: {e!r}")
        return True


def _debug(msg):
    """ANIME_RSS_DEBUG=1 时把内部状态打到控制台，便于排查打包后的问题"""
    if os.environ.get("ANIME_RSS_DEBUG") == "1":
        print(f"\n  [debug] {msg}", flush=True)


def minimize_console():
    """把控制台窗口缩到任务栏，视觉上退到后台"""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)     # SW_MINIMIZE
    except Exception:
        pass


def open_app_window(url, size="1240,880"):
    """用 --app 模式启动浏览器：没有地址栏和标签页，看起来就是个桌面软件。

    独立 user-data-dir 有两个好处：不干扰用户日常浏览器，
    主题、滚动位置这些 localStorage 状态也能长期保留。
    成功返回 Popen 对象（用来感知窗口是否被关掉），失败返回 None。
    """
    exe = find_chromium()
    if not exe:
        return None
    profile = os.path.join(DATA_DIR, ".window")
    try:
        os.makedirs(profile, exist_ok=True)
    except OSError:
        return None

    cmd = [exe, "--app=" + url, "--window-size=" + size,
           "--user-data-dir=" + profile, "--no-first-run",
           "--no-default-browser-check", "--disable-features=Translate,MediaRouter",
           "--disable-background-networking"]
    try:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                  "stdin": subprocess.DEVNULL}
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000     # CREATE_NO_WINDOW
        return subprocess.Popen(cmd, **kwargs)
    except Exception:
        return None


class WindowSupervisor(threading.Thread):
    """「开窗 → 等关闭 → 询问 → 留在后台 → 等着被再次唤起」的常驻循环。

    之前这里是个一次性线程：问过一次就结束了，用户选了「留在后台」之后
    再怎么关窗都不会再问，看起来就像「沿用了第一次的选择」。
    现在循环不退出，每次窗口关闭都重新询问；
    再次双击程序时，新进程会 POST /api/open-window 把窗口接回来，
    于是又能正常触发询问。
    """

    def __init__(self, url, size, on_close, server):
        super().__init__(daemon=True, name="window-supervisor")
        self.url = url
        self.size = size
        self.on_close = on_close          # ask / exit / background
        self.server = server
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._want_open = False
        self._have_window = False
        self._stopping = False

    # ---------------- 外部调用
    def request_open(self):
        """有新进程来「唤起界面」时调用；已经有窗口就不重复开"""
        with self._lock:
            if self._have_window or self._stopping:
                return False
            self._want_open = True
        self._wake.set()
        return True

    def stop(self):
        with self._lock:
            self._stopping = True
        self._wake.set()

    # ---------------- 线程主体
    def run(self):
        self.request_open()
        while True:
            with self._lock:
                want = self._want_open
                self._want_open = False
                stopping = self._stopping
            if stopping:
                return
            if not want:
                self._wake.wait(60)
                self._wake.clear()
                continue
            self._wake.clear()
            verdict = self._show_once()
            if verdict == "exit":
                self._shutdown()
                return

    def _show_once(self):
        with self._lock:
            self._have_window = True
        try:
            proc = open_app_window(self.url, self.size) if self.on_close != "browser" else None
            if proc is None:
                try:
                    webbrowser.open(self.url)
                except Exception:
                    pass
                return "browser"          # 普通标签页感知不到关闭，只能靠 stop
            started = time.time()
            while proc.poll() is None:
                if self._stopping:
                    return "stop"
                time.sleep(1.0)
            if time.time() - started < 5:      # 窗口没起来（启动失败），不当作关窗
                return "abort"
            action = self.on_close
            if action == "ask":
                action = "exit" if ask_close_action() else "background"
            if action == "exit":
                return "exit"
            self._stays_in_background()
            return "background"
        finally:
            with self._lock:
                self._have_window = False

    def _stays_in_background(self):
        print("\n  窗口已关闭，服务仍在后台运行")
        print(f"  访问地址   {self.url}")
        print("  再次双击程序可重新打开窗口；关闭控制台窗口即可彻底退出\n")
        minimize_console()

    def _shutdown(self):
        print("\n  窗口已关闭，服务退出")
        try:
            self.server.shutdown()
        except Exception:
            pass


UI = None            # 全局窗口监督器，/api/open-window 用它把界面接回来


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AnimeRSSReader/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if os.environ.get("ANIME_RSS_QUIET") == "1":
            return
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------ 输出
    def _send(self, body, ctype="application/json; charset=utf-8", code=200,
              cache="no-store", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        headers = {
            "Content-Type": ctype,
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
        }
        use_gzip = (
            "gzip" in (self.headers.get("Accept-Encoding") or "")
            and len(body) > 1400
            and not ctype.startswith(("image/", "font/"))
        )
        if use_gzip:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(body))
        headers.update(extra or {})
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        self._send(obj, "application/json; charset=utf-8", code)

    def _err(self, msg, code=400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _base_url(self):
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or f"127.0.0.1:{PORT}"
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        return f"{proto}://{host}"

    # ------------------------------------------------------------ 路由
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        try:
            if path in ("/", "/index.html"):
                page = read_asset("index.html") or "static/index.html 缺失".encode("utf-8")
                page = page.replace(b"__VERSION__", VERSION.encode())
                return self._send(page, "text/html; charset=utf-8", cache="no-cache")

            if path.startswith("/assets/"):
                name = path[len("/assets/"):] or "index.html"
                data = read_asset(name)
                if data is None:
                    return self._send("Not Found", "text/plain; charset=utf-8", 404)
                ext = os.path.splitext(name)[1].lower()
                return self._send(
                    data, ASSET_TYPES.get(ext, "application/octet-stream"),
                    cache="public, max-age=86400")

            if path in ("/favicon.ico", "/apple-touch-icon.png"):
                data = read_asset("icon.svg")
                return self._send(data or b"", "image/svg+xml", cache="max-age=604800")

            if path in ("/healthz", "/api/health"):
                return self._json({"ok": True, "app": "anime-rss-reader",
                                   "version": VERSION,
                                   "uptime": int(time.time() - START_TS)})

            if path == "/api/bootstrap":
                return self._json(self._bootstrap())

            if path == "/api/items":
                return self._json(self._items(q))

            if path.startswith("/api/item/"):
                iid = urllib.parse.unquote(path[len("/api/item/"):])
                it = STORE.get_item(iid)
                if not it:
                    return self._err("条目不存在", 404)
                it["sourceName"] = source_names().get(it["source"], it["source"])
                return self._json({"ok": True, "item": it})

            if path == "/api/refresh":
                return self._json({"ok": True, **REFRESHER.status()})

            if path == "/api/watches":
                return self._json({"ok": True, "watches": self._watches_payload()})

            if path == "/api/mirrors":
                return self._json({"ok": True, "sources": F.host_status()})

            if path in ("/feed.xml", "/rss.xml", "/rss"):
                return self._serve_feed(None)
            if path.startswith("/feed/"):
                fid = path[len("/feed/"):]
                if fid.endswith(".xml"):
                    fid = fid[:-4]
                return self._serve_feed(urllib.parse.unquote(fid))
            if path in ("/opml.xml", "/feeds.opml"):
                data = F.build_opml(STORE, self._base_url())
                return self._send(data, "text/x-opml; charset=utf-8")

            return self._send("Not Found", "text/plain; charset=utf-8", 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._err(f"{type(e).__name__}: {e}", 500)
            except Exception:
                pass

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        body = self._body()
        try:
            if path == "/api/refresh":
                ids = body.get("feeds") or None
                started = REFRESHER.start(ids)
                return self._json({"ok": True, "started": started,
                                   **REFRESHER.status()})

            if path == "/api/read":
                ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
                n = STORE.set_read(ids, bool(body.get("read", True)))
                return self._json({"ok": True, "changed": n,
                                   "counts": STORE.counts()})

            if path == "/api/star":
                it = STORE.set_star(body.get("id") or "", bool(body.get("starred", True)))
                return self._json({"ok": True, "item": it, "counts": STORE.counts()})

            if path == "/api/read-all":
                n = STORE.read_all(body.get("feed") or "all")
                return self._json({"ok": True, "changed": n, "counts": STORE.counts()})

            if path == "/api/feeds":
                return self._json(self._create_feed(body))

            if path == "/api/watches":
                return self._json(self._create_watch(body))

            if path.startswith("/api/watches/") and path.endswith("/scan"):
                wid = urllib.parse.unquote(path[len("/api/watches/"):-len("/scan")])
                if not STORE.watch(wid):
                    return self._err("关注不存在", 404)
                res = STORE.scan_watches()
                return self._json({"ok": True, "hits": res["per"].get(wid, 0),
                                   "watches": self._watches_payload(),
                                   "counts": STORE.counts()})

            if path == "/api/open-window":
                if UI is None:
                    return self._json({"ok": True, "opened": False,
                                       "reason": "当前实例没有窗口监督器"})
                opened = UI.request_open()
                return self._json({"ok": True, "opened": opened})

            return self._send("Not Found", "text/plain; charset=utf-8", 404)
        except Exception as e:
            return self._err(f"{type(e).__name__}: {e}", 500)

    def do_PATCH(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path.rstrip("/")

        if path.startswith("/api/watches/"):
            wid = urllib.parse.unquote(path[len("/api/watches/"):])
            if not STORE.watch(wid):
                return self._err("关注不存在", 404)
            body = self._body()
            STORE.update_watch(
                wid,
                name=body.get("name"),
                include=body.get("include"),
                exclude=body.get("exclude"),
                fansub=body.get("fansub"),
                sources=body.get("sources"),
                enabled=body.get("enabled"),
            )
            hits = STORE.scan_watches()["per"].get(wid, 0)
            if not hits and STORE.watch(wid)["enabled"]:
                hits = self._backfill_watch(wid)    # 改了关键词后本地没命中，去补搜
            return self._json({"ok": True, "hits": hits,
                               "watches": self._watches_payload(),
                               "counts": STORE.counts()})

        if not path.startswith("/api/feeds/"):
            return self._send("Not Found", "text/plain; charset=utf-8", 404)
        fid = urllib.parse.unquote(path[len("/api/feeds/"):])
        if not STORE.feed(fid):
            return self._err("订阅源不存在", 404)
        body = self._body()
        STORE.update_feed(
            fid,
            title=body.get("title"),
            url=body.get("url"),
            enabled=body.get("enabled"),
            params=body.get("params"),
        )
        return self._json({"ok": True, "feeds": STORE.feeds(),
                           "counts": STORE.counts()})

    def do_DELETE(self):
        u = urllib.parse.urlsplit(self.path)
        path = u.path.rstrip("/")

        if path.startswith("/api/watches/"):
            wid = urllib.parse.unquote(path[len("/api/watches/"):])
            if not STORE.watch(wid):
                return self._err("关注不存在", 404)
            STORE.delete_watch(wid)
            return self._json({"ok": True, "watches": self._watches_payload(),
                               "counts": STORE.counts()})

        if not path.startswith("/api/feeds/"):
            return self._send("Not Found", "text/plain; charset=utf-8", 404)
        fid = urllib.parse.unquote(path[len("/api/feeds/"):])
        feed = STORE.feed(fid)
        if not feed:
            return self._err("订阅源不存在", 404)
        if feed["kind"] == "builtin":
            return self._err("内置频段不能删除，可以关闭它", 400)
        STORE.delete_feed(fid)
        return self._json({"ok": True, "feeds": STORE.feeds(),
                           "counts": STORE.counts()})

    # ------------------------------------------------------------ 具体实现
    def _serve_feed(self, fid):
        if fid and not STORE.feed(fid):
            return self._send("订阅源不存在", "text/plain; charset=utf-8", 404)
        data = F.build_feed_xml(STORE, fid or "all", self._base_url())
        return self._send(data, "application/rss+xml; charset=utf-8",
                          cache="public, max-age=120")

    def _bootstrap(self):
        feeds = STORE.feeds()
        names = source_names()
        for f in feeds:
            f["sourceName"] = names.get(f["id"], f["title"])
        lan_urls = []
        if LAN_MODE:
            lan_urls = [f"http://{local_ip()}:{PORT}/"]
        return {
            "ok": True,
            "app": APP_NAME,
            "version": VERSION,
            "sources": [{"id": s["id"], "name": s["name"], "home": s["home"],
                         "note": s["note"]} for s in S.SOURCES],
            "feeds": feeds,
            "counts": STORE.counts(),
            "stats": STORE.stats(),
            "refresh": REFRESHER.status(),
            "lan": {"enabled": LAN_MODE, "urls": lan_urls, "ip": local_ip()},
            "watches": self._watches_payload(),
            "mirrors": F.host_status(),
            "baseUrl": self._base_url(),
            "serverTime": int(time.time()),
        }

    def _items(self, q):
        try:
            limit = max(1, min(int(q.get("limit") or 60), 300))
        except ValueError:
            limit = 60
        try:
            offset = max(0, int(q.get("offset") or 0))
        except ValueError:
            offset = 0
        feed = q.get("feed") or "all"
        filt = q.get("filter") or "all"
        kw = (q.get("q") or "").strip()
        items = STORE.query_items(feed=feed, filt=filt, q=kw,
                                 limit=limit + 1, offset=offset)
        has_more = len(items) > limit
        items = items[:limit]
        names = source_names()
        for it in items:
            it["sourceName"] = names.get(it["source"], it["source"])
        return {"ok": True, "items": items, "hasMore": has_more,
                "offset": offset, "counts": STORE.counts()}

    def _watches_payload(self):
        now = int(time.time())
        out = []
        for w in STORE.watches():
            keep = 0
            if w["lastHit"]:
                keep = max(0, ST.KEEP_WATCH_DAYS - (now - w["lastHit"]) // 86400)
            out.append({**w, "remainDays": keep})
        return out

    def _create_watch(self, body):
        include = (body.get("include") or "").strip()
        if not include:
            return self._err("请至少填写一个关注关键词", 400)
        name = (body.get("name") or "").strip() or include
        w = STORE.add_watch(name, include,
                            body.get("exclude") or "",
                            body.get("fansub") or "",
                            body.get("sources") or "")
        hits = STORE.scan_watches()["per"].get(w["id"], 0)
        # 本地一条都没有，立刻去 AnimeGarden 搜一次，别让用户对着空列表等下一轮刷新
        if not hits:
            hits = self._backfill_watch(w["id"])
        return {"ok": True, "watch": STORE.watch(w["id"]), "hits": hits,
                "watches": self._watches_payload(),
                "counts": STORE.counts()}

    def _backfill_watch(self, wid):
        w = STORE.watch(wid)
        if not w:
            return 0
        try:
            got = REFRESHER.search_for_watch(w)
        except Exception:
            return 0
        if not got:
            return 0
        STORE.upsert_items(None, got, link=False)
        return STORE.attach_watch(wid, got)

    def _create_feed(self, body):
        url = (body.get("url") or "").strip()
        title = (body.get("title") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "error": "订阅地址必须以 http:// 或 https:// 开头"}
        fid = "c" + str(int(time.time() * 1000))
        if not title:
            title = urllib.parse.urlsplit(url).hostname or "自定义订阅"
        feed = STORE.add_feed(fid, title, "custom", {}, url)
        # 立刻抓一次，让用户加完就能看到内容
        try:
            items, _ = F.collect_custom(feed)
            added = STORE.upsert_items(fid, items[:300])
            STORE.feed_stat(fid, True)
        except Exception as e:
            STORE.feed_stat(fid, False, f"{type(e).__name__}: {e}")
            return {"ok": True, "feeds": STORE.feeds(), "counts": STORE.counts(),
                    "warning": f"订阅已添加，但首次抓取失败：{e}"}
        return {"ok": True, "feeds": STORE.feeds(), "counts": STORE.counts(),
                "added": added}


# ---------------------------------------------------------------- 启动

class Server(ThreadingHTTPServer):
    """浏览器关窗口时会强制断开 keep-alive 连接，这类异常不必打印堆栈"""
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def port_in_use(port, host="127.0.0.1"):
    """判断端口是否已被占用。

    不能用 bind 探测：Windows 上 SO_REUSEADDR 允许绑定已经在监听的口，
    bind 成功并不代表端口空闲。直接连一下最准。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.35)
    try:
        return s.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def is_our_instance(port, host="127.0.0.1"):
    """端口上跑的是不是本程序（区分别的软件占用了同一端口）"""
    try:
        req = urllib.request.Request(f"http://{host}:{port}/healthz")
        with urllib.request.urlopen(req, timeout=0.9) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data.get("app") == "anime-rss-reader"
    except Exception:
        return False


def remote_open_window(port, host="127.0.0.1"):
    """请已经在跑的那个实例把窗口接回去（它会继续监听关窗并弹框）"""
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/open-window", data=b"{}",
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return bool(data.get("ok") and data.get("opened"))
    except Exception:
        return False


def pick_port(want, tries=20):
    """端口被占用时自动往后找"""
    for i in range(tries):
        p = want + i
        if not port_in_use(p):
            return p
    return want


def main(argv=None):
    global STORE, REFRESHER, LAN_MODE, PORT, UI

    # Windows 控制台默认是 GBK，统一成 UTF-8，否则中文输出会乱码
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            pass

    try:                      # 输出重定向到文件/管道时也能实时看到日志
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace",
                                    line_buffering=True)
            except Exception:
                _stream.reconfigure(line_buffering=True)
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description=f"{APP_NAME} v{VERSION} — 动漫资源聚合 + RSS 阅读器")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8899")))
    ap.add_argument("--host", default=os.environ.get("HOST", ""))
    ap.add_argument("--lan", action="store_true", help="开放到局域网，手机/平板可访问")
    ap.add_argument("--no-browser", action="store_true", help="不要自动打开界面")
    ap.add_argument("--browser", action="store_true",
                    help="用系统默认浏览器开标签页（默认优先开无地址栏的独立窗口）")
    ap.add_argument("--window-size", default="1240,880", help="独立窗口大小，如 1240,880")
    ap.add_argument("--on-close", choices=("ask", "exit", "background"),
                    default=os.environ.get("ANIME_RSS_ON_CLOSE", "ask"),
                    help="关闭窗口后：ask 弹框询问（默认）/ exit 直接退出 / background 留在后台")
    ap.add_argument("--no-fetch", action="store_true", help="启动后不自动抓取")
    args = ap.parse_args(argv)

    if args.host:
        host = "0.0.0.0" if args.host.strip().lower() in (
            "lan", "all", "any", "*", "0.0.0.0", "::") else args.host.strip()
    else:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
    LAN_MODE = host == "0.0.0.0"

    # 已经有一个实例在跑（比如上次选了「留在后台」）就不要再开第二个，
    # 让老实例把窗口接回去 —— 这样关窗时它还会照常问你
    if not LAN_MODE and port_in_use(args.port) and is_our_instance(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        print(f"\n  已有实例在运行：{url}")
        if args.no_browser:
            return 0
        if not args.browser and remote_open_window(args.port):
            print("  已在原有实例中打开界面窗口\n")
            return 0
        if not args.browser and open_app_window(url, args.window_size) is not None:
            print("  已打开界面窗口\n")
        else:
            try:
                webbrowser.open(url)
                print("  已在浏览器中打开界面\n")
            except Exception:
                pass
        return 0

    if not os.path.isdir(DATA_DIR):
        os.makedirs(DATA_DIR, exist_ok=True)
    STORE = ST.Store(os.path.join(DATA_DIR, "reader.db"))
    REFRESHER = F.Refresher(STORE)
    F.STATE_PATH = os.path.join(DATA_DIR, "hosts.json")   # 记住各镜像入口的可用状态

    PORT = pick_port(args.port)
    try:
        srv = Server((host, PORT), Handler)
    except OSError as e:
        print(f"启动失败：{host}:{PORT} 无法监听（{e}）")
        return 1

    url = f"http://{'127.0.0.1' if LAN_MODE else host}:{PORT}/"
    print()
    print(f"  {APP_NAME}  v{VERSION}")
    print(f"  {'─' * 46}")
    print(f"  本地访问   {url}")
    if LAN_MODE:
        print(f"  局域网     http://{local_ip()}:{PORT}/   （手机 / 平板同一 WiFi 可用）")
    print(f"  订阅导出   {url}feed.xml")
    print(f"  数据文件   {os.path.join(DATA_DIR, 'reader.db')}")
    if args.no_browser or os.environ.get("ANIME_RSS_NO_BROWSER") == "1":
        print("  界面        已关闭自动打开，请手动访问上面的地址")
    elif args.browser:
        print("  界面        将在默认浏览器中打开（--browser 模式）")
    elif find_chromium():
        print("  界面        将打开独立窗口（无地址栏，--browser 可改用普通标签页）")
    else:
        print("  界面        未找到 Chrome/Edge，将在默认浏览器中打开")
    print(f"  {'─' * 46}")
    print("  关闭窗口后可选择退出，或留在后台继续运行\n")

    # 首次运行或数据过期时自动抓一轮
    stale = (time.time() - (STORE.newest_ts() or 0)) > 1800
    if not args.no_fetch and stale:
        def boot_fetch():
            time.sleep(0.6)
            REFRESHER.start()
        threading.Thread(target=boot_fetch, daemon=True).start()

    if not args.no_browser and os.environ.get("ANIME_RSS_NO_BROWSER") != "1":
        if args.browser:
            # 普通标签页没法感知"关掉了"，只负责打开一次
            def open_tab():
                time.sleep(0.8)
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            threading.Thread(target=open_tab, daemon=True).start()
        else:
            mode = "background" if os.environ.get("ANIME_RSS_KEEP_ALIVE") == "1" \
                else args.on_close
            UI = WindowSupervisor(url, args.window_size, mode, srv)
            UI.start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        try:
            srv.server_close()
        except Exception:
            pass
        STORE.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
