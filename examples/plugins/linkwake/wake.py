"""真正「叫醒」的一层：把链接交给系统关联的程序，或交给用户指定的 exe。

这一层会**启动进程**，所以护栏写在这里，而不是指望上层记得校验：

* 只放行 ``http / https / magnet / ed2k / thunder`` 与用户显式配置的 scheme。
  ``file:`` ``javascript:`` ``data:`` ``smb:`` 一律拒绝 —— 一个被投毒的 RSS
  条目就能让你点出「打开本地文件」，这类洞不值得为方便留。
* 只在本机请求时动手（由 module 层看 Host 判断）。局域网里别人打开网页，
  不该能让你机器上弹出程序。
* ``app`` 模式必须是**真实存在的文件**，参数整条作为一个 argv 传给
  ``subprocess``，永远不经 shell —— 否则标题里一个 ``&`` 就能拼接命令。
* 所有启动失败都转成 ``WakeError``，带上人话原因；绝不静默失败。

``opener`` / ``spawner`` 可注入，测试时换成记录调用的假函数，
这样单测不会真的在你机器上弹一堆窗口。
"""
import os
import re
import subprocess
import sys

__all__ = ["WakeError", "SAFE_SCHEMES", "check_url", "is_local_host",
           "split_cmd", "Waker"]

# 常见到可以无脑放行的
SAFE_SCHEMES = ("http", "https", "magnet", "ed2k", "thunder")

# 明确有害 / 会碰到本地文件系统的
DENY_SCHEMES = ("file", "filesystem", "javascript", "data", "vbscript", "about",
                "blob", "jar", "ms-msdt", "search-ms", "smb", "nfs", "ftp",
                "chrome", "res", "shell")

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]{1,24}):")
_CUSTOM_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]{0,24}$")
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

# 本机判定用的主机名。局域网 IP 不算 —— 那正是要挡住的情况。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}


class WakeError(RuntimeError):
    """唤醒失败（链接不安全、程序不存在、系统拒绝等）。"""


def _scheme_of(url):
    m = _SCHEME_RE.match(url or "")
    return m.group(1).lower() if m else ""


def check_url(url, extra_schemes=()):
    """校验并规范化一个待唤醒的链接；不安全就抛 WakeError。

    返回去掉首尾空白的 url。空串直接拒绝 —— 空链接唤醒没有意义，
    却可能让 ``os.startfile("")`` 弹出一个资源管理器。
    """
    u = (url or "").strip()
    if not u:
        raise WakeError("链接是空的")
    if _CTRL_RE.search(u):
        raise WakeError("链接里有控制字符，拒绝执行")
    if u.startswith("\\\\") or u.startswith("//"):
        raise WakeError("不接受网络路径（\\\\ 开头）")
    sch = _scheme_of(u)
    if not sch:
        raise WakeError("认不出协议头：%s" % u[:40])
    if sch in DENY_SCHEMES:
        raise WakeError("出于安全考虑，不唤醒 %s: 链接" % sch)
    allowed = set(SAFE_SCHEMES)
    for s in extra_schemes or ():
        if _CUSTOM_SCHEME_RE.match(str(s or "").lower()):
            allowed.add(str(s).lower())
    if sch not in allowed:
        raise WakeError("不支持的协议：%s（可在客户端设置里显式允许）" % sch)
    return u


def is_local_host(host_header):
    """请求是不是来自本机。用于「别让局域网设备在你机器上开程序」。"""
    host = (host_header or "").strip().lower()
    if not host:
        return True                      # 没有 Host 头（很老的客户端）按本机处理
    if host.startswith("["):             # [::1]:8899
        return host.split("]")[0].lstrip("[") in ("::1", "")
    name = host.rsplit(":", 1)[0] if ":" in host else host
    return name in _LOCAL_HOSTS


def split_cmd(template):
    """把 ``"{exe} --flag {url}"`` 这样的模板切成 argv。

    自己实现而不是用 shlex：exe 里 ``shlex`` 未必打进去（PyInstaller 只带
    主程序 import 过的模块），而这段逻辑简单到不值得为它冒加载失败的风险。
    规则与 Windows 命令行一致：双引号包裹的片段当一个参数，反斜杠不转义。
    """
    out, buf, quoted, has = [], [], False, False
    for ch in str(template or ""):
        if ch == '"':
            quoted = not quoted
            has = True
        elif ch.isspace() and not quoted:
            if buf or has:
                out.append("".join(buf))
                buf, has = [], False
        else:
            buf.append(ch)
    if buf or has:
        out.append("".join(buf))
    return out


def _default_open(url):
    """交给系统关联程序。Windows 用 startfile，其余平台退化成浏览器。"""
    if os.name == "nt":
        os.startfile(url)                # noqa: S606 - 这是本插件的目的
    else:
        import webbrowser
        if not webbrowser.open(url):
            raise WakeError("系统没有可用的关联程序")


def _default_spawn(argv):
    """启动外部程序。argv 是列表，不经 shell。"""
    kwargs = {"close_fds": True, "stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # 不弹黑框，也不抢焦点
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(argv, **kwargs)
    except FileNotFoundError as e:
        raise WakeError("找不到程序：%s" % argv[0]) from e
    except OSError as e:
        raise WakeError("启动失败：%s" % e) from e


def _rewrite_scheme(url, scheme):
    """把 http(s):// 换成客户端协议，如 ``baiduyun://pan.baidu.com/s/xxx``。

    各家客户端的协议格式并不统一，所以这只是「app 模式」的一种便捷写法，
    失败也不奇怪 —— 所以界面上默认不选它。
    """
    sch = str(scheme or "").strip().lower()
    if not _CUSTOM_SCHEME_RE.match(sch):
        raise WakeError("客户端协议名不合法：%r" % scheme)
    rest = re.sub(r"^https?://", "", url, flags=re.I)
    return "%s://%s" % (sch, rest)


class Waker:
    """按 target 描述执行唤醒动作。

    target 是一次唤醒的完整描述::

        {"mode": "open" | "web" | "copy" | "scheme" | "app",
         "scheme": "baiduyun",      # mode=scheme 时用
         "exe": r"C:\\...\\qbittorrent.exe",   # mode=app 时用
         "args": "{url}"}           # mode=app 的参数模板
    """

    MODES = ("open", "web", "copy", "scheme", "app")

    def __init__(self, opener=None, spawner=None):
        self._open = opener or _default_open
        self._spawn = spawner or _default_spawn      # 启动失败要报错，见 register

    def _spawn_quiet(self, argv):
        """app 模式：先看文件在不在，再启动。"""
        exe = (argv[0] if argv else "")
        if not exe:
            raise WakeError("没有配置程序路径")
        if not os.path.isfile(exe):
            raise WakeError("程序不存在：%s" % exe)
        self._spawn(argv)

    def dispatch(self, url, target=None):
        """执行一次唤醒，返回 ``{"action": ..., "detail": ..., "url": ...}``。

        不返回进程对象 —— 进程活着与否跟「有没有唤醒成功」没关系。
        """
        t = target or {}
        mode = str(t.get("mode") or "open").strip().lower()
        if mode not in self.MODES:
            raise WakeError("未知的唤醒方式：%s" % mode)

        if mode == "copy":
            # 不启动任何东西，交给前端写剪贴板
            return {"action": "copy", "detail": "已复制到剪贴板", "url": url}

        u = check_url(url, extra_schemes=(t.get("scheme"),) if mode == "scheme" else ())

        if mode == "app":
            args = t.get("args") or "{url}"
            argv = [t.get("exe") or ""]
            argv += [a.replace("{url}", u) for a in split_cmd(args)]
            try:
                self._spawn_quiet(argv)
            except WakeError:
                raise
            except OSError as e:
                raise WakeError("启动失败：%s" % e) from e
            return {"action": "app", "detail": os.path.basename(argv[0]), "url": u}

        if mode == "scheme":
            u = _rewrite_scheme(u, t.get("scheme"))
            try:
                self._open(u)
            except Exception as e:                    # noqa: BLE001
                raise WakeError("系统没有接手这个协议：%s" % e) from e
            return {"action": "scheme", "detail": u.split(":", 1)[0] + "://", "url": u}

        try:
            self._open(u)
        except WakeError:
            raise
        except Exception as e:                        # noqa: BLE001
            raise WakeError("打不开：%s" % e) from e
        return {"action": mode,
                "detail": "已交给系统默认程序" if mode == "open" else "已在浏览器打开",
                "url": u}
