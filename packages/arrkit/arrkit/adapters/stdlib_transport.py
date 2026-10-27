"""传输端口的 stdlib 实现：ThreadingHTTPServer。

零依赖、够用、能跑在任意有 Python 的机器上 —— 这是默认实现，
不是唯一实现。换 uvicorn / 宿主网络栈只需另写一个 TransportPort。
"""
import gzip
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..ports.transport import BoundServer, TransportPort

__all__ = ["StdlibTransport"]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 由 _Bound 在构造时注入
    dispatch = None
    quiet = False
    server_name = "arrkit"

    def log_message(self, fmt, *args):
        if self.quiet:
            return
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------ 写响应
    def _write(self, resp):
        body = resp.body
        headers = {
            "Content-Type": resp.ctype,
            "Cache-Control": resp.cache,
            "X-Content-Type-Options": "nosniff",
        }
        headers.update(resp.headers or {})
        use_gzip = (
            "gzip" in (self.headers.get("Accept-Encoding") or "")
            and len(body) > 1400
            and not resp.ctype.startswith(("image/", "font/"))
        )
        if use_gzip:
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(body))

        self.send_response(resp.status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    # ------------------------------------------------------------ 派发
    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        if not n:
            return b""
        try:
            return self.rfile.read(n)
        except Exception:
            return b""

    def _handle(self):
        body = self._body() if self.command in ("POST", "PATCH", "PUT", "DELETE") else b""
        try:
            resp = self.dispatch(self.command, self.path, self.headers, body)
        except Exception as e:                      # 兜底，绝不让线程安静地死掉
            from ..http import Response
            resp = Response.err("%s: %s" % (type(e).__name__, e), 500)
        try:
            self._write(resp)
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = do_HEAD = do_POST = do_PATCH = do_DELETE = _handle


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class _Bound(BoundServer):
    def __init__(self, server):
        self._server = server

    @property
    def address(self):
        return self._server.server_address[0], self._server.server_address[1]

    def serve_forever(self):
        self._server.serve_forever()

    def shutdown(self):
        try:
            self._server.shutdown()
        except Exception:
            pass

    def close(self):
        try:
            self._server.server_close()
        except Exception:
            pass


class StdlibTransport(TransportPort):
    """默认传输：一个线程一个请求，足够撑住本地与局域网使用。"""

    def __init__(self, quiet=False, server_name="arrkit"):
        self.quiet = quiet
        self.server_name = server_name

    def bind(self, dispatch, host, port):
        handler = type("BoundHandler", (_Handler,), {
            "dispatch": staticmethod(dispatch),
            "quiet": self.quiet,
            "server_name": self.server_name,
        })
        return _Bound(_Server((host, port), handler))
