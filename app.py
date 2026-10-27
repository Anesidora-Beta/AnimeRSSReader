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
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

from anime_rss.app import create_app
from anime_rss.kernel.context import local_ip
from anime_rss.kernel.server import make_server
from anime_rss.module import ModuleError
from anime_rss import runtime as RUNTIME

APP_NAME = "动漫资源 · RSS 阅读器"
VERSION = "2.0.1"

HERE = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):          # PyInstaller 单文件模式
    HERE = getattr(sys, "_MEIPASS", HERE) or HERE
    _BASE = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE = HERE
DATA_DIR = os.environ.get("ANIME_RSS_DATA") or os.path.join(_BASE, "data")
STATIC_DIR = os.path.join(HERE, "static")

# 打包成无控制台程序后 stdout/stderr 是 None：先接上日志文件，
# 后面所有 print / 访问日志才有地方去（也避免直接 write 时抛异常）。
if getattr(sys, "frozen", False):
    LOG_PATH = RUNTIME.attach_streams(DATA_DIR)
else:
    LOG_PATH = None

CTX = None               # 应用上下文，由 main() 组装后注入
UI = None                # 全局窗口监督器，/api/open-window 用它把界面接回来


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


_OWNER_CLASS = "AnimeRSSCloseOwner"
_OWNER_PROC = None          # 必须留住窗口过程回调，否则被回收后窗口会崩


def _pid_windows(pid):
    """列出某进程的可见主窗口：[{"rect": (x, y, w, h), "min": 是否最小化}]"""
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            owner_pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner_pid))
            if owner_pid.value != pid or not user32.IsWindowVisible(hwnd):
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w >= 240 and h >= 160:        # 过滤掉工具条之类的小窗
                # 最小化后位置是 -32000，不能当坐标用，但「窗口还在」
                found.append({"rect": (rect.left, rect.top, w, h),
                              "min": bool(user32.IsIconic(hwnd))})
            return True

        user32.EnumWindows(proc(_cb), 0)
        return found
    except Exception:
        return []


def _pid_window_rect(pid):
    """主窗口矩形（面积最大的那个，跳过最小化的），拿不到就返回 None"""
    normal = [w["rect"] for w in _pid_windows(pid) if not w["min"]]
    if not normal:
        return None
    return max(normal, key=lambda r: r[2] * r[3])


def _make_msg_owner(x, y):
    """建一个 1x1 的置顶隐形窗口，作为 MessageBox 的父窗。

    关键点：MessageBox 只有「挂在置顶父窗上」并且自己也带 MB_TOPMOST 时，
    才会稳定压在所有窗口上面。之前的写法直接 MessageBoxW(None, ...)，
    第一次没弹出来就会退化成普通对话框，于是被别的程序盖住。
    """
    global _OWNER_PROC
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.CreateWindowExW.restype = wintypes.HWND

    if _OWNER_PROC is None:
        wndproc_t = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                       ctypes.c_uint, ctypes.c_size_t,
                                       ctypes.c_size_t)

        # 不声明 argtypes 的话，ctypes 会把 lparam 当 C int 传，大数值直接溢出
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [wintypes.HWND, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_size_t]

        def _wndproc(hwnd, msg, wparam, lparam):
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        _OWNER_PROC = wndproc_t(_wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", ctypes.c_uint),
                        ("lpfnWndProc", wndproc_t),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        wc = WNDCLASSW()
        wc.lpfnWndProc = _OWNER_PROC
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = _OWNER_CLASS
        user32.RegisterClassW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(
        0x00000008 | 0x00000080 | 0x00080000,   # TOPMOST | TOOLWINDOW | LAYERED
        _OWNER_CLASS, APP_NAME,
        0x80000000 | 0x10000000,                # WS_POPUP | WS_VISIBLE
        int(x), int(y), 1, 1,
        None, None, kernel32.GetModuleHandleW(None), None)
    if hwnd:
        # 全透明：看不见，但 MessageBox 会认这个「可见父窗」的位置，
        # 把对话框摆到主窗口原来的地方，而不是屏幕正中
        user32.SetLayeredWindowAttributes(hwnd, 0, 0, 0x2)   # LWA_ALPHA
    return hwnd


def _bring_to_front(hwnd):
    """把自己进程的窗口强行提到前台，绕过 Windows 的前台锁"""
    if os.name != "nt" or not hwnd:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        tid_me = ctypes.windll.kernel32.GetCurrentThreadId()
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)
    except Exception:
        pass


def _find_msgbox_hwnd():
    """在自己进程里找刚弹出来的系统对话框（类名固定是 #32770）"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hit = []
        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _):
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value != os.getpid() or not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls, 64)
            if cls.value == "#32770":
                hit.append(hwnd)
            return True

        user32.EnumWindows(proc(_cb), 0)
        return hit[0] if hit else None
    except Exception:
        return None


def _place_dialog_soon(anchor=None):
    """对话框一出来就摆到 anchor（主窗口原来的中心点）上，并抢一次前台。

    MessageBox 不按父窗位置摆（系统有自己的规则，实测会跑到屏幕别处），
    只能等它建好再摆正。所以这里 10ms 一轮地等它出现，
    趁第一帧还没画出来就挪好 —— 用户看到的是「窗口一关，询问框就在原地」，
    看不到「先跳到角落再滑回来」。
    """
    def _work():
        if not anchor:
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
        except Exception:
            return
        for _ in range(200):              # 最多等 2 秒
            hwnd = _find_msgbox_hwnd()
            if hwnd:
                try:
                    rect = wintypes.RECT()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    nx = int(anchor[0] - w // 2)
                    ny = int(anchor[1] - h // 2)
                    if abs(nx - rect.left) > 2 or abs(ny - rect.top) > 2:
                        user32.SetWindowPos(hwnd, 0, nx, ny, 0, 0,
                                            0x0001 | 0x0004)   # NOSIZE | NOZORDER
                except Exception:
                    pass
                _bring_to_front(hwnd)
                return
            time.sleep(0.01)
    threading.Thread(target=_work, daemon=True, name="dialog-place").start()


def ask_close_action(rect=None):
    """关窗后的选择框：返回 True 表示退出，False 表示留在后台。

    rect 是主窗口最后的矩形，用来把弹窗摆在「主窗口原来的位置」，
    看起来就是弹窗从主窗口前面弹出来，而不是蹦到屏幕角落。
    """
    if os.name != "nt":
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.MessageBoxW.restype = ctypes.c_int
        MB_YESNO = 0x4
        MB_ICONQUESTION = 0x20
        MB_DEFBUTTON2 = 0x100        # 默认落在「否」，避免回车误退出
        MB_TOPMOST = 0x40000
        MB_SETFOREGROUND = 0x10000
        text = ("是否退出程序？\n\n"
                "　【是】退出，服务停止\n"
                "　【否】不退出，留在后台继续运行\n"
                "　　　　（手机或其它浏览器仍可访问）")
        flags = (MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2
                 | MB_TOPMOST | MB_SETFOREGROUND)

        if rect:
            x = rect[0] + rect[2] // 2
            y = rect[1] + rect[3] // 2
            owner = _make_msg_owner(x, y)
        else:
            # 拿不到原窗口位置：不加父窗，交给系统按当前屏幕居中，
            # 别把父窗摆到 (0, 0) 把弹窗顶到屏幕角上
            owner = None

        started = time.time()
        if owner:
            _bring_to_front(owner)
        _place_dialog_soon((x, y) if rect else None)    # 一出现就摆正 + 抢焦点
        try:
            ret = user32.MessageBoxW(owner, text, APP_NAME, flags)
        finally:
            if owner:
                user32.DestroyWindow(owner)
        _debug(f"MessageBox ret={ret} elapsed={time.time() - started:.2f}s "
               f"rect={rect}")
        if ret == 0:                 # 压根没弹出来：不带父窗再试一次
            ret = user32.MessageBoxW(None, text, APP_NAME, flags)
            _debug(f"MessageBox fallback ret={ret}")
        return ret == 6              # IDYES 才退出；IDNO / ESC / 其它都留在后台
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
        proc = None
        try:
            proc = open_app_window(self.url, self.size) if self.on_close != "browser" else None
            if proc is None:
                try:
                    webbrowser.open(self.url)
                except Exception:
                    pass
                return "browser"          # 普通标签页感知不到关闭，只能靠 stop
            started = time.time()
            rect, why = self._watch_window(proc)
            if why == "stop":
                return "stop"
            if rect is None and time.time() - started < 5:
                return "abort"            # 窗口没起来（启动失败），不当作关窗
            action = self.on_close
            if action == "ask":
                action = "exit" if ask_close_action(rect) else "background"
            if action == "exit":
                return "exit"
            self._stays_in_background()
            return "background"
        finally:
            # 关窗到进程真正退出之间还有几百毫秒，这里顺手收干净，
            # 免得「留在后台」之后还挂着一个没有窗口的浏览器进程
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
            with self._lock:
                self._have_window = False

    # ---------------- 盯着主窗口
    def _watch_window(self, proc):
        """等主窗口消失，返回 (最后的窗口矩形, 原因)。

        不等进程退出：Chromium 从窗口关掉到进程结束要几百毫秒，
        这段时间屏幕上什么都没有。窗口一消失就往下走，
        询问框能立刻在原地弹出来，不会先空一下再冒出来。
        连续两次采样都看不到窗口才算关了，避免重绘时的瞬间抖动。
        最小化不算关闭（窗口还在，只是位置是 -32000）。
        """
        rect = None
        gone = 0
        while proc.poll() is None:
            if self._stopping:
                return rect, "stop"
            wins = _pid_windows(proc.pid)
            if wins:
                gone = 0
                normal = [w["rect"] for w in wins if not w["min"]]
                if normal:
                    rect = max(normal, key=lambda r: r[2] * r[3])
            elif rect:
                gone += 1
                if gone >= 2:
                    return rect, "window-gone"
            time.sleep(0.06)
        return rect, "exited"

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


# ---------------------------------------------------------------- HTTP

# ---------------------------------------------------------------- 启动

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
    global CTX, UI

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
    ap.add_argument("--no-third-party", action="store_true",
                    help="不加载第三方插件（plugins/ 目录与 entry_points），排障时用")
    args = ap.parse_args(argv)

    if args.host:
        host = "0.0.0.0" if args.host.strip().lower() in (
            "lan", "all", "any", "*", "0.0.0.0", "::") else args.host.strip()
    else:
        host = "0.0.0.0" if args.lan else "127.0.0.1"
    lan_mode = host == "0.0.0.0"

    # 已经有一个实例在跑（比如上次选了「留在后台」）就不要再开第二个，
    # 让老实例把窗口接回去 —— 这样关窗时它还会照常问你
    if not lan_mode and port_in_use(args.port) and is_our_instance(args.port):
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

    # ---------------- 组装：交给可嵌入的 App（与宿主嵌入用的是同一份代码）
    quiet = os.environ.get("ANIME_RSS_QUIET") == "1"
    say = None if quiet else print
    port = pick_port(args.port)

    try:
        app = create_app(
            data_dir=DATA_DIR, static_dir=STATIC_DIR, base_dir=_BASE,
            port=port, lan_mode=lan_mode, app_name=APP_NAME, version=VERSION,
            log=say, no_third_party=args.no_third_party,
            fetch_on_start=not args.no_fetch,
            plugin_dirs=[os.path.join(_BASE, "plugins")],
        )
    except ModuleError as e:
        RUNTIME.fatal(f"模块加载失败：{e}")
        return 1

    ctx = app.ctx
    cfg = app.settings.get()
    CTX = ctx

    try:
        srv = make_server(host, port, app.dispatcher,
                          quiet=os.environ.get("ANIME_RSS_QUIET") == "1")
    except OSError as e:
        RUNTIME.fatal(f"启动失败：{host}:{port} 无法监听（{e}）")
        app.stop()
        return 1

    url = f"http://{'127.0.0.1' if lan_mode else host}:{port}/"
    print()
    print(f"  {APP_NAME}  v{VERSION}")
    print(f"  {'─' * 46}")
    print(f"  本地访问   {url}")
    if lan_mode:
        print(f"  局域网     http://{local_ip()}:{port}/   （手机 / 平板同一 WiFi 可用）")
    print(f"  订阅导出   {url}feed.xml")
    print(f"  数据文件   {ctx.db_path}")
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

    # 启动任务表：首次抓取、放送表预热、后台定时刷新都在 app.start() 里
    app.start()

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
            # 命令行 / 环境变量没强制指定时，用设置里的「关闭窗口时」
            forced = ("--on-close" in sys.argv
                      or os.environ.get("ANIME_RSS_ON_CLOSE") is not None)
            if not forced and os.environ.get("ANIME_RSS_KEEP_ALIVE") != "1":
                mode = cfg.get("closeAction") or mode
            UI = WindowSupervisor(url, args.window_size, mode, srv)
            ctx.ui = UI
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
        app.stop()                        # 停任务、收尾模块、关库
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
