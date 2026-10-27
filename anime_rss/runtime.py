#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""冻结环境（PyInstaller 窗口模式）下的输出兜底。

打包成「无控制台」程序后，``sys.stdout`` / ``sys.stderr`` 是 ``None``。
这时候任何 ``print()`` 会静默丢弃，但 ``sys.stderr.write(...)`` 这类直接调用
会抛 ``AttributeError``——最典型的就是 HTTP 访问日志，一崩溃就是整个请求挂掉。

这里给它们换成「写到日志文件」的替代品，顺便让用户出问题时还有个地方能看：

    data/logs/app.log      最近一次运行的输出（超过 1 MB 自动重开）

没有控制台、也写不了文件时退化为丢弃一切的空流，绝不因为日志本身报错。
"""

import io
import os
import sys
import time

__all__ = ["NullStream", "attach_streams", "has_console"]

_MAX_LOG = 1024 * 1024          # 单个日志文件上限，超了就重开
_attached = None                # 已接上的日志路径，重复调用直接返回


class NullStream:
    """长得像文本流、但什么都不做的对象。

    需要 ``reconfigure`` / ``isatty`` / ``fileno`` 这些方法，
    是因为不少标准库代码（含本项目启动流程）会去摸它们。
    """

    encoding = "utf-8"
    errors = "replace"
    name = "<null>"
    mode = "w"
    closed = False

    def write(self, text):
        return len(text) if text else 0

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        return None

    def close(self):
        return None

    def isatty(self):
        return False

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def reconfigure(self, **kwargs):        # 启动流程会调它统一编码
        return None

    def fileno(self):
        raise io.UnsupportedOperation("null stream has no file descriptor")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def has_console():
    """当前进程有没有可用的控制台 / 输出流。"""
    if sys.stdout is not None or sys.stderr is not None:
        return True
    if os.name != "nt":
        return False
    try:
        import ctypes
        return bool(ctypes.windll.kernel32.GetConsoleWindow())
    except Exception:
        return False


def _open_log(path):
    """打开日志文件；顺手把上一次超过上限的内容清掉。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path) and os.path.getsize(path) > _MAX_LOG:
            try:
                os.remove(path)
            except OSError:
                pass
        return open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return None


class _FileStream:
    """把写操作转给日志文件；文件没了就静默丢弃，不回抛。"""

    def __init__(self, handle):
        self._handle = handle

    @property
    def encoding(self):
        return getattr(self._handle, "encoding", "utf-8")

    @property
    def errors(self):
        return "replace"

    @property
    def name(self):
        return getattr(self._handle, "name", "<log>")

    def write(self, text):
        if not text:
            return 0
        try:
            self._handle.write(text)
            return len(text)
        except Exception:
            return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        try:
            self._handle.flush()
        except Exception:
            pass

    def close(self):
        try:
            self._handle.close()
        except Exception:
            pass

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):
        return None

    def fileno(self):
        try:
            return self._handle.fileno()
        except Exception:
            raise io.UnsupportedOperation("log stream has no file descriptor")


def attach_streams(log_dir=None):
    """把丢掉的 stdout/stderr 接到日志文件上；返回日志路径（失败返回 None）。

    没有控制台时才有动作：有控制台就保持原样，别抢走开发时的实时输出。
    重复调用是幂等的。
    """
    global _attached
    if _attached is not None:
        return _attached
    if sys.stdout is not None and sys.stderr is not None:
        return None

    path = None
    stream = None
    if log_dir:
        path = os.path.join(log_dir, "logs", "app.log")
        handle = _open_log(path)
        if handle is not None:
            stream = _FileStream(handle)
            stream.write("\n===== %s 启动 =====\n"
                         % time.strftime("%Y-%m-%d %H:%M:%S"))

    if stream is None:
        stream = NullStream()
        path = None

    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream
    _attached = path or ""
    return path


def fatal(message):
    """启动就失败时至少让人看见：有控制台就打印，没有就弹个小窗。

    窗口模式下程序崩了会「双击没反应」，这一句是唯一的求救信号。
    """
    text = str(message)
    try:
        sys.stderr.write(text + "\n")
    except Exception:
        pass
    if has_console():
        return
    if os.name != "nt":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, text, "动漫资源 · RSS 阅读器", 0x10)     # MB_ICONERROR
    except Exception:
        pass
