"""stdlib HTTP 服务：把 Router + Assets 接到 ThreadingHTTPServer 上。

这一层是「适配器」——换成宿主的 Web 框架时，只需要另写一个同等角色的模块，
Router / Assets / handler 都不用动。
"""
import gzip
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .http import Request, Response, TEXT


class Dispatcher:
    """先查静态挂载，再查路由表，最后 404。"""

    def __init__(self, router=None, assets=None, mounts=None, verbose=True):
        self.router = router
        self.assets = assets
        self.mounts = mounts or []
        self.verbose = verbose

    def handle(self, method, raw_path, headers, body=b""):
        u = urllib.parse.urlsplit(raw_path)
        path = u.path
        for mount in self.mounts:
            name = mount.match(path)
            if name is None:
                continue
            data = self.assets.read(mount.directory, name)
            if data is None:
                return Response.text("Not Found", 404)
            if mount.transform is not None:
                data = mount.transform(name, data)
            return Response(data, self.assets.content_type(name), cache=mount.cache)
        if self.router is not None:
            query = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            req = Request(method, path.rstrip("/") or "/", query, headers, body)
            resp = self.router.dispatch(req)
            if resp is not None:
                return resp
        return Response.text("Not Found", 404)


class Handler(BaseHTTPRequestHandler):
    server_version = "AnimeRSSReader"
    protocol_version = "HTTP/1.1"

    # 由 Server 在启动时注入
    dispatcher = None
    quiet = False

    def log_message(self, fmt, *args):
        if self.quiet:
            return
        err = sys.stderr
        if err is None:                 # 无控制台（窗口模式）时别去碰 None
            return
        try:
            err.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))
        except Exception:
            pass

    # ------------------------------------------------------------ 写响应
    def _write(self, resp):
        body = resp.body
        headers = {
            "Content-Type": resp.ctype,
            "Cache-Control": resp.cache,
            "X-Content-Type-Options": "nosniff",
        }
        headers.update(resp.headers)
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
            resp = self.dispatcher.handle(self.command, self.path, self.headers, body)
        except Exception as e:                                # 兜底，绝不让线程炸掉
            resp = Response.err("%s: %s" % (type(e).__name__, e), 500)
        try:
            self._write(resp)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()


class Server(ThreadingHTTPServer):
    """浏览器关窗口时会强制断开 keep-alive 连接，这类异常不必打印堆栈。"""

    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(host, port, dispatcher, quiet=False):
    """构造并返回一个可 serve_forever() 的服务器实例。"""
    handler = type("BoundHandler", (Handler,),
                   {"dispatcher": dispatcher, "quiet": quiet})
    return Server((host, port), handler)
