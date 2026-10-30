# -*- coding: utf-8 -*-
"""唤醒层测试：护栏与命令构造。

这里**不会**真的打开任何程序：``Waker`` 的 opener / spawner 全部换成
记录用的假函数。真正需要验证的是「什么时候该拒绝」——
把 file: 链接放行一次，代价远大于多写十个用例。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wake  # noqa: E402


class CheckUrl(unittest.TestCase):
    def test_allowed(self):
        for url in ("https://pan.baidu.com/s/1a", "http://x.com/a",
                    "magnet:?xt=urn:btih:abc", "ed2k://|file|a|1|h|/",
                    "thunder://QUFodHRw"):
            self.assertEqual(wake.check_url(url), url)

    def test_rejects_dangerous_schemes(self):
        bad = ["file:///C:/Windows/System32/calc.exe",
               "javascript:alert(1)",
               "data:text/html,<script>x</script>",
               "vbscript:msgbox(1)",
               "about:blank",
               "smb://server/share",
               "ms-msdt:/id", "search-ms:query=x"]
        for url in bad:
            with self.assertRaises(wake.WakeError, msg=url):
                wake.check_url(url)

    def test_rejects_unc_path(self):
        for url in ("\\\\server\\share\\a.exe", "//server/share"):
            with self.assertRaises(wake.WakeError):
                wake.check_url(url)

    def test_rejects_empty(self):
        with self.assertRaises(wake.WakeError):
            wake.check_url("   ")

    def test_rejects_control_chars(self):
        with self.assertRaises(wake.WakeError):
            wake.check_url("https://x.com/\x00a")
        with self.assertRaises(wake.WakeError):
            wake.check_url("https://x.com/\na")

    def test_rejects_no_scheme(self):
        with self.assertRaises(wake.WakeError):
            wake.check_url("pan.baidu.com/s/1a")

    def test_unknown_scheme_needs_explicit_allow(self):
        url = "baiduyun://pan.baidu.com/s/1a"
        with self.assertRaises(wake.WakeError):
            wake.check_url(url)                       # 默认不放行
        self.assertEqual(wake.check_url(url, extra_schemes=("baiduyun",)), url)

    def test_bad_custom_scheme_ignored(self):
        url = "weird://x"
        with self.assertRaises(wake.WakeError):
            wake.check_url(url, extra_schemes=("../evil", ""))


class SplitCmd(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(wake.split_cmd("{url}"), ["{url}"])
        self.assertEqual(wake.split_cmd("-a {url} -b"), ["-a", "{url}", "-b"])

    def test_quoted(self):
        self.assertEqual(
            wake.split_cmd('--flag "a b" {url}'), ["--flag", "a b", "{url}"])

    def test_empty(self):
        self.assertEqual(wake.split_cmd(""), [])
        self.assertEqual(wake.split_cmd("   "), [])

    def test_empty_quoted_argument(self):
        self.assertEqual(wake.split_cmd('"" x'), ["", "x"])


class LocalHost(unittest.TestCase):
    def test_loopback(self):
        for host in ("127.0.0.1:8899", "localhost:8899", "LOCALHOST",
                     "[::1]:8899", "127.0.0.1", ""):
            self.assertTrue(wake.is_local_host(host), host)

    def test_lan_and_public_are_not_local(self):
        for host in ("192.168.1.5:8899", "10.0.0.2:8899", "example.com",
                     "8.8.8.8:80"):
            self.assertFalse(wake.is_local_host(host), host)


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.opened = []
        self.spawned = []
        self.w = wake.Waker(opener=lambda u: self.opened.append(u),
                            spawner=lambda argv: self.spawned.append(argv))

    def test_copy_does_not_launch_anything(self):
        r = self.w.dispatch("https://pan.baidu.com/s/1a", {"mode": "copy"})
        self.assertEqual(r["action"], "copy")
        self.assertEqual(self.opened, [])
        self.assertEqual(self.spawned, [])

    def test_open_and_web(self):
        self.w.dispatch("magnet:?xt=urn:btih:a", {"mode": "open"})
        self.w.dispatch("https://pan.baidu.com/s/1a", {"mode": "web"})
        self.assertEqual(len(self.opened), 2)

    def test_app_mode_uses_args_template(self):
        exe = sys.executable                      # 一定存在的文件
        r = self.w.dispatch("magnet:?xt=urn:btih:a",
                            {"mode": "app", "exe": exe, "args": "-x {url}"})
        self.assertEqual(r["action"], "app")
        self.assertEqual(self.spawned[0], [exe, "-x", "magnet:?xt=urn:btih:a"])

    def test_app_mode_missing_exe(self):
        with self.assertRaises(wake.WakeError):
            self.w.dispatch("magnet:?xt=urn:btih:a",
                            {"mode": "app", "exe": r"C:\definitely\not\here.exe"})
        self.assertEqual(self.spawned, [])

    def test_app_mode_without_exe(self):
        with self.assertRaises(wake.WakeError):
            self.w.dispatch("magnet:?xt=urn:btih:a", {"mode": "app"})

    def test_scheme_rewrite(self):
        r = self.w.dispatch("https://pan.baidu.com/s/1abc",
                            {"mode": "scheme", "scheme": "baiduyun"})
        self.assertEqual(r["url"], "baiduyun://pan.baidu.com/s/1abc")
        self.assertEqual(self.opened[-1], "baiduyun://pan.baidu.com/s/1abc")

    def test_scheme_rewrite_rejects_bad_name(self):
        with self.assertRaises(wake.WakeError):
            self.w.dispatch("https://x.com/a",
                            {"mode": "scheme", "scheme": "ja vascript"})

    def test_scheme_mode_does_not_allow_arbitrary_scheme_url(self):
        """scheme 模式下放行的是「改写出来的」链接，原链接仍要过一遍白名单。"""
        with self.assertRaises(wake.WakeError):
            self.w.dispatch("file:///C:/Windows/calc.exe",
                            {"mode": "scheme", "scheme": "baiduyun"})

    def test_unknown_mode(self):
        with self.assertRaises(wake.WakeError):
            self.w.dispatch("magnet:?xt=urn:btih:a", {"mode": "rm -rf"})

    def test_default_mode_is_open(self):
        r = self.w.dispatch("magnet:?xt=urn:btih:a", {})
        self.assertEqual(r["action"], "open")

    def test_opener_failure_becomes_wake_error(self):
        def boom(url):
            raise OSError("没有关联程序")
        w = wake.Waker(opener=boom)
        with self.assertRaises(wake.WakeError):
            w.dispatch("magnet:?xt=urn:btih:a", {"mode": "open"})

    def test_existing_temp_file_can_be_used_as_exe(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            path = fh.name
        try:
            self.w.dispatch("magnet:?xt=urn:btih:a",
                            {"mode": "app", "exe": path, "args": "{url}"})
            self.assertTrue(self.spawned)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
