# -*- coding: utf-8 -*-
"""下载器客户端测试：对着两个假服务跑真 HTTP。

为什么起假服务而不是打桩替换 ``_fetch``
--------------------------------------
真问题几乎都出在协议细节上：qB 的登录要带 Referer、SID 在 Set-Cookie 里、
aria2 的 token 在 params 第一个、``addUri`` 的数组是「同一任务的多镜像」。
把 ``_fetch`` 换掉就全测不到了，只剩「我猜它应该这样」。所以这里起真的
HTTP 服务，走真的 socket。
"""
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import clients  # noqa: E402


class _Server(object):
    """起一个本地 HTTP 服务，handler 由调用方给。"""

    def __init__(self, handler):
        self.handler = handler

    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.srv.shutdown()
        self.srv.server_close()

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port


class FakeQB(BaseHTTPRequestHandler):
    seen = []
    password = "secret"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode("utf-8", "replace")
        FakeQB.seen.append({"path": self.path, "body": body,
                            "referer": self.headers.get("Referer", ""),
                            "cookie": self.headers.get("Cookie", "")})
        if self.path == "/api/v2/auth/login":
            if ("password=" + FakeQB.password) in body:
                self._send(200, b"Ok.", {"Set-Cookie": "SID=abc; path=/"})
            else:
                self._send(200, b"Fails.")
            return
        if self.path == "/api/v2/torrents/add":
            self._send(200, b"Ok.")
            return
        self._send(404)

    def do_GET(self):
        if self.path == "/api/v2/app/version":
            self._send(200, b"v4.6.2")
            return
        self._send(404)


class FakeAria2(BaseHTTPRequestHandler):
    calls = []
    token = "tok"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode())
        except ValueError:
            req = {}
        FakeAria2.calls.append(req)
        params = req.get("params") or []
        if FakeAria2.token and (not params or params[0] != "token:" + FakeAria2.token):
            out = {"jsonrpc": "2.0", "id": req.get("id"),
                   "error": {"code": 1, "message": "Unauthorized"}}
        elif req.get("method") == "aria2.getVersion":
            out = {"jsonrpc": "2.0", "id": req.get("id"),
                   "result": {"version": "1.37.0"}}
        elif req.get("method") == "aria2.addUri":
            out = {"jsonrpc": "2.0", "id": req.get("id"), "result": "gid-1"}
        else:
            out = {"jsonrpc": "2.0", "id": req.get("id"),
                   "error": {"code": 2, "message": "No such method"}}
        body = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Endpoints(unittest.TestCase):
    def test_requires_http(self):
        for bad in ("", "   ", "127.0.0.1:8080", "ftp://x"):
            with self.assertRaises(clients.ClientError, msg=bad):
                clients.check_endpoint(bad, "qB")

    def test_strips_trailing_slash(self):
        self.assertEqual(clients.check_endpoint("http://x:1/"), "http://x:1")

    def test_private_hosts(self):
        for url in ("http://127.0.0.1:8080", "http://localhost:6800/jsonrpc",
                    "http://192.168.1.9:1", "http://10.1.2.3:1",
                    "http://172.20.0.1:1", "http://[::1]:6800"):
            self.assertTrue(clients._is_private_host(clients._host_of(url)), url)
        for url in ("http://8.8.8.8:1", "https://qb.example.com"):
            self.assertFalse(clients._is_private_host(clients._host_of(url)), url)

    def test_private_hosts_bypass_proxy(self):
        """本机地址必须用不走代理的 opener —— 否则代理回 502，看着像客户端没开。"""
        self.assertIs(clients._opener("http://127.0.0.1:8080"), clients._NO_PROXY)
        self.assertIsNot(clients._opener("http://example.com"), clients._NO_PROXY)


class QBittorrent(unittest.TestCase):
    def setUp(self):
        FakeQB.seen = []
        FakeQB.password = "secret"

    def test_version_and_referer(self):
        with _Server(FakeQB) as s:
            ver = clients.qb_test(s.base, "admin", "secret")
        self.assertEqual(ver, "v4.6.2")
        login = [c for c in FakeQB.seen if "login" in c["path"]][0]
        self.assertTrue(login["referer"].startswith("http://127.0.0.1:"),
                        "qB 的登录接口没有 Referer 会被拒")

    def test_wrong_password(self):
        with _Server(FakeQB) as s:
            with self.assertRaises(clients.ClientError) as cm:
                clients.qb_test(s.base, "admin", "nope")
        self.assertIn("密码", str(cm.exception))

    def test_add_joins_urls_and_sends_savepath(self):
        with _Server(FakeQB) as s:
            n = clients.qb_add(s.base, ["magnet:?xt=urn:btih:a",
                                        "magnet:?xt=urn:btih:b"],
                               savepath="/dl", user="admin", password="secret")
        self.assertEqual(n, 2)
        add = [c for c in FakeQB.seen if "torrents/add" in c["path"]][0]
        self.assertIn("urls=magnet", add["body"])
        self.assertIn("%0A", add["body"])          # 两个链接用换行分隔
        self.assertIn("savepath=", add["body"])

    def test_add_rejects_empty(self):
        with self.assertRaises(clients.ClientError):
            clients.qb_add("http://127.0.0.1:1", [])

    def test_connection_refused(self):
        with self.assertRaises(clients.ClientError) as cm:
            clients.qb_test("http://127.0.0.1:1", "a", "b", timeout=3)
        self.assertIn("连不上", str(cm.exception))


class Aria2(unittest.TestCase):
    def setUp(self):
        FakeAria2.calls = []
        FakeAria2.token = "tok"

    def test_version(self):
        with _Server(FakeAria2) as s:
            self.assertEqual(clients.aria2_test(s.base + "/jsonrpc", "tok"),
                             "1.37.0")

    def test_token_wrong(self):
        with _Server(FakeAria2) as s:
            with self.assertRaises(clients.ClientError) as cm:
                clients.aria2_test(s.base + "/jsonrpc", "wrong")
        self.assertIn("aria2", str(cm.exception))

    def test_add_one_call_per_url(self):
        """addUri 的数组是「同一任务的多镜像」，批量必须一条一次调用。"""
        with _Server(FakeAria2) as s:
            n = clients.aria2_add(s.base + "/jsonrpc",
                                  ["magnet:?xt=urn:btih:a",
                                   "magnet:?xt=urn:btih:b",
                                   "https://x.com/a.torrent"],
                                  token="tok", save_dir="/dl")
        self.assertEqual(n, 3)
        adds = [c for c in FakeAria2.calls if c.get("method") == "aria2.addUri"]
        self.assertEqual(len(adds), 3)
        self.assertEqual(adds[0]["params"][0], "token:tok")
        # params = ["token:tok", [u], opts] —— 每次装的镜像列表里只能有一个 url
        self.assertEqual(adds[0]["params"][1], ["magnet:?xt=urn:btih:a"])
        self.assertEqual(adds[2]["params"][1], ["https://x.com/a.torrent"])
        self.assertEqual(adds[0]["params"][2].get("dir"), "/dl")

    def test_add_rejects_empty(self):
        with self.assertRaises(clients.ClientError):
            clients.aria2_add("http://127.0.0.1:1/jsonrpc", [])


class BadResponses(unittest.TestCase):
    def test_non_json(self):
        class NotJson(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                body = b"<html>login page</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        with _Server(NotJson) as s:
            with self.assertRaises(clients.ClientError) as cm:
                clients.aria2_test(s.base + "/jsonrpc", "")
        self.assertIn("JSON", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
