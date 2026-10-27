#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无控制台（打包成窗口程序）时的输出兜底测试。

打包后的程序没有 cmd 黑框，``sys.stdout`` / ``sys.stderr`` 是 ``None``。
这里验证：不会因为日志把请求搞崩，日志还能落到文件里。

跑法： ``python -m unittest discover -s tests -v``
"""
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anime_rss import runtime as RT                       # noqa: E402
from anime_rss.kernel.server import Handler               # noqa: E402


class _Silent:
    """临时把 stdout/stderr 换成 None，退出时一定还原。"""

    def __enter__(self):
        self._real = (sys.stdout, sys.stderr)
        self._attached = RT._attached
        RT._attached = None
        sys.stdout = None
        sys.stderr = None

    def __exit__(self, *exc):
        for stream in (sys.stdout, sys.stderr):
            if isinstance(stream, RT._FileStream):
                stream.flush()
                stream.close()
        sys.stdout, sys.stderr = self._real
        RT._attached = self._attached
        return False


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class NullStreamTest(unittest.TestCase):
    def test_write_returns_length(self):
        s = RT.NullStream()
        self.assertEqual(s.write("abc"), 3)
        self.assertEqual(s.write(""), 0)

    def test_stream_protocol(self):
        s = RT.NullStream()
        s.writelines(["a", "b"])
        s.flush()
        s.reconfigure(encoding="utf-8", line_buffering=True)
        self.assertFalse(s.isatty())
        self.assertEqual(s.encoding, "utf-8")
        with self.assertRaises(io.UnsupportedOperation):
            s.fileno()

    def test_usable_as_context_manager(self):
        with RT.NullStream() as s:
            self.assertIsNotNone(s)


class AttachTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-runtime-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_noop_when_streams_exist(self):
        """有控制台时不能抢走开发者的实时输出。"""
        self.assertIsNone(RT.attach_streams(self.tmp))
        self.assertFalse(os.path.isdir(os.path.join(self.tmp, "logs")))

    def test_logs_to_file_when_streams_missing(self):
        with _Silent():
            path = RT.attach_streams(self.tmp)
            self.assertTrue(path and path.endswith("app.log"))
            print("hello-print")
            sys.stderr.write("hello-stderr\n")
            sys.stdout.flush()
            self.assertTrue(os.path.isfile(path))
            text = _read(path)
        self.assertIn("hello-print", text)
        self.assertIn("hello-stderr", text)

    def test_falls_back_to_null_without_dir(self):
        """给不了可写目录也不能抛，退化成什么都丢的流。"""
        with _Silent():
            self.assertIsNone(RT.attach_streams(None))
            self.assertIsInstance(sys.stdout, RT.NullStream)
            self.assertEqual(sys.stdout.write("x"), 1)

    def test_attach_is_idempotent(self):
        with _Silent():
            first = RT.attach_streams(self.tmp)
            second = RT.attach_streams(os.path.join(self.tmp, "other"))
        self.assertEqual(first, second)

    def test_oversized_log_is_rotated(self):
        path = os.path.join(self.tmp, "logs", "app.log")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (RT._MAX_LOG + 10))
        with _Silent():
            RT.attach_streams(self.tmp)
            print("after-rotate")
        text = _read(path)
        self.assertNotIn("x" * 100, text)
        self.assertIn("after-rotate", text)


class AccessLogTest(unittest.TestCase):
    """这条最要紧：log_message 在无控制台时曾会抛 AttributeError，整请求挂掉。"""

    def _handler(self):
        h = Handler.__new__(Handler)          # 不启动 socket，只要方法
        h.quiet = False
        return h

    def test_log_message_survives_missing_stderr(self):
        with _Silent():
            self._handler().log_message('"%s" %s', "GET /x", 200)

    def test_quiet_still_skips(self):
        h = self._handler()
        h.quiet = True
        with _Silent():
            h.log_message("%s", "GET /x")     # 不应抛

    def test_log_message_writes_when_redirected(self):
        with _Silent():
            path = RT.attach_streams(tempfile.mkdtemp(prefix="arr-runtime-"))
            self._handler().log_message('"%s" %s', "GET /logged", 200)
            sys.stderr.flush()
            self.assertIn("GET /logged", _read(path))


class ConsoleDetectTest(unittest.TestCase):
    def test_detects_streams(self):
        self.assertTrue(RT.has_console())     # 测试进程本身有输出流

    def test_detects_missing(self):
        with _Silent():
            if os.name == "nt":
                return                        # Windows 上再看有没有真实控制台句柄
            self.assertFalse(RT.has_console())


if __name__ == "__main__":
    unittest.main()
