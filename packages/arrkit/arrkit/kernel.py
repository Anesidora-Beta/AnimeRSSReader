"""内核组装：一处拼装，三种运行姿态。

    k = Kernel(modules=[Clock, Notes])        # 组装（不联网、不起服务）
    k.handle("GET", "/api/clock")             # ① 宿主直接调，拿 Response
    k.wsgi                                    # ② 交给任意 WSGI 容器
    k.serve("127.0.0.1", 8899)                # ③ 自己起服务

端口/适配器分开的好处在这里兑现：``transport`` 和 ``asset_port`` 都是可替换的，
换掉它们不影响 Router / Module / handler。
"""
import json
import os
import time
import urllib.parse

from . import discovery, module
from .adapters import FilesystemAssets, StdlibTransport
from .admin import AdminApi
from .bus import Bus
from .context import Context
from .dispatcher import Dispatcher, Mount
from .http import Response, Router
from .jobs import Jobs
from .settings import Settings
from .version import __version__
from .views import ViewRegistry

__all__ = ["Kernel", "KernelConfig", "create_kernel", "DEFAULT_ADMIN_PREFIX",
           "DEFAULT_ASSETS_PREFIX", "DEFAULT_PLUGIN_PREFIX"]

DEFAULT_ADMIN_PREFIX = "/_kit"
DEFAULT_ASSETS_PREFIX = "/assets/"
DEFAULT_PLUGIN_PREFIX = "/plug/"

_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
    429: "Too Many Requests", 500: "Internal Server Error",
    503: "Service Unavailable",
}


class KernelConfig:
    """一次运行的全部参数。全是可选，默认值就是「本机跑起来」。"""

    __slots__ = ("data_dir", "static_dir", "host", "port", "app_name", "version",
                 "log", "modules", "plugin_dirs", "plugin_group", "no_plugins",
                 "admin_prefix", "assets_prefix", "plugin_prefix", "transport",
                 "asset_port", "clock", "settings", "services", "auto_start",
                 "settings_file", "home_page")

    def __init__(self, data_dir=None, static_dir=None, host="127.0.0.1", port=0,
                 app_name="arrkit", version=__version__, log=None, modules=(),
                 plugin_dirs=None, plugin_group=discovery.DEFAULT_GROUP,
                 no_plugins=False, admin_prefix=DEFAULT_ADMIN_PREFIX,
                 assets_prefix=DEFAULT_ASSETS_PREFIX,
                 plugin_prefix=DEFAULT_PLUGIN_PREFIX, transport=None,
                 asset_port=None, clock=None, settings=None, services=None,
                 auto_start=True, settings_file=None, home_page="index.html"):
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.host = host
        self.port = int(port)
        self.app_name = app_name
        self.version = version
        self.log = log
        self.modules = list(modules or ())
        self.plugin_dirs = list(plugin_dirs) if plugin_dirs is not None else None
        self.plugin_group = plugin_group
        self.no_plugins = bool(no_plugins)
        self.admin_prefix = admin_prefix
        self.assets_prefix = assets_prefix
        self.plugin_prefix = plugin_prefix if plugin_prefix.endswith("/") \
            else plugin_prefix + "/"
        self.transport = transport
        self.asset_port = asset_port
        self.clock = clock
        self.settings = settings
        self.services = dict(services or {})
        self.auto_start = bool(auto_start)
        self.settings_file = settings_file
        self.home_page = home_page or ""

    # ------------------------------------------------------------ 路径
    def resolve_data_dir(self):
        if self.data_dir:
            return os.path.abspath(self.data_dir)
        env = os.environ.get("ARRKIT_DATA")
        if env:
            return os.path.abspath(env)
        return os.path.join(os.getcwd(), "data")

    def resolve_static_dir(self):
        # 刻意不做 abspath：内存资源端口用「虚拟路径」当键，abspath 会把它改写
        # 成平台路径（Windows 上 /pages → F:\pages），反而找不到。
        return str(self.static_dir) if self.static_dir else ""

    def resolve_plugin_dirs(self):
        if self.plugin_dirs is not None:
            return list(self.plugin_dirs)
        return [os.path.join(self.resolve_data_dir(), "modules")]

    def __repr__(self):
        return "<KernelConfig app=%r port=%r>" % (self.app_name, self.port)


class Kernel:
    """组装好的内核实例。一个进程里可以存在多个（各有各的库与任务表）。"""

    def __init__(self, config=None, **kwargs):
        self.config = config if isinstance(config, KernelConfig) else KernelConfig(**kwargs)
        self.assets = None
        self.transport = None
        self.router = None
        self.bus = None
        self.jobs = None
        self.views = None
        self.settings = None
        self.ctx = None
        self.dispatcher = None
        self.server = None
        self.modules = []
        self._built = False
        self._started = False

    # ------------------------------------------------------------ 组装
    def build(self):
        """创建部件、加载模块、挂路由。幂等；失败时把已开的资源关掉再抛。"""
        if self._built:
            return self
        c = self.config
        say = c.log or (lambda msg: None)

        data_dir = c.resolve_data_dir()
        static_dir = c.resolve_static_dir()
        os.makedirs(data_dir, exist_ok=True)

        try:
            # ---- 端口（具体实现由 adapters 提供，可被宿主替换）
            self.assets = c.asset_port or FilesystemAssets()
            self.transport = c.transport or StdlibTransport()

            # ---- 内核能力
            self.router = Router()
            self.bus = Bus(log=say)
            self.jobs = Jobs(log=say, clock=c.clock)
            self.views = ViewRegistry(url_prefix=c.plugin_prefix,
                                      content_type=self.assets.content_type)
            self.settings = c.settings or Settings(
                path=c.settings_file or os.path.join(data_dir, "settings.json"))

            self.ctx = Context(
                router=self.router, assets=self.assets, bus=self.bus,
                jobs=self.jobs, views=self.views, settings=self.settings,
                config=c, services=c.services, data_dir=data_dir,
                static_dir=static_dir, app_name=c.app_name, version=c.version,
                port=c.port, host=c.host, log=c.log, clock=c.clock)

            # ---- 模块
            self.modules = self._collect_modules(say)
            module.load_modules(self.ctx, self.modules, log=say)

            # ---- 内核自带路由
            AdminApi(self, c.admin_prefix).register(self.router)
            self._register_plugin_assets()
            self._register_home()
            mounts = []
            if static_dir:
                # 不在这里 os.path.isdir 判断：资源可能来自内存端口，
                # 「目录」只是逻辑路径，读不到时由 Dispatcher 回 404。
                mounts.append(Mount(c.assets_prefix, static_dir))
            self.dispatcher = Dispatcher(self.router, self.assets, mounts)
        except Exception:
            self._close_quiet()
            raise

        self._built = True
        return self

    def _collect_modules(self, say):
        modules = list(self.config.modules)
        if self.config.no_plugins:
            return modules
        try:
            extra, _problems = discovery.discover(
                self.config.resolve_plugin_dirs(),
                group=self.config.plugin_group, log=say)
        except Exception as e:                 # noqa: BLE001 - 发现失败不该让内置模块起不来
            say("模块发现失败：%s: %s" % (type(e).__name__, e))
            return modules
        if extra:
            modules = discovery.merge_unique(modules, extra, log=say)
        return modules

    def _register_home(self):
        """把静态目录里的首页映射到 ``/``：单页应用的最短路径。"""
        page = self.config.home_page
        if not page or not self.config.static_dir:
            return

        def home(req):
            data = self.assets.read(self.config.static_dir, page)
            if data is None:
                return Response.text("Not Found", 404)
            return Response(data, self.assets.content_type(page), cache="no-store")

        self.router.get("/", home)
        self.router.get("/index.html", home)

    def _register_plugin_assets(self):
        """``/plug/<模块 id>/<文件>``：模块自带的前端资源。"""
        prefix = self.config.plugin_prefix.rstrip("/")

        def serve(req):
            got = self.views.read(req.param("mid"), req.param("path"))
            if got is None:
                return Response.text("Not Found", 404)
            data, ctype = got
            return Response(data, ctype, cache="public, max-age=300")

        self.router.get(prefix + "/{mid}/{path:path}", serve)

    # ------------------------------------------------------------ 启动 / 停止
    def start(self):
        """启动任务表并发一条启动事件。"""
        self.build()
        if self._started:
            return self
        self._started = True
        self.jobs.start()
        ids = list(self.ctx.modules.ids()) if self.ctx.modules else []
        self.bus.emit("app.started", {"version": self.config.version,
                                      "port": self.config.port, "modules": ids})
        return self

    def stop(self):
        """停任务、收尾模块、关资源。可以重复调用。"""
        if not self._built:
            return self
        try:
            self.bus.emit("app.stopping", {})
        except Exception:
            pass
        try:
            self.jobs.stop()
        except Exception:
            pass
        try:
            module.unload_modules(self.ctx.modules if self.ctx else None,
                                  log=self.config.log)
        except Exception:
            pass
        self._close_quiet()
        self._built = False
        self._started = False
        return self

    def _close_quiet(self):
        server, self.server = self.server, None
        if server is not None:
            try:
                server.close()
            except Exception:
                pass
        settings = self.settings
        if settings is not None and getattr(settings, "dirty", False):
            try:
                settings.save()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # ------------------------------------------------------------ 三种运行姿态
    def handle(self, method, path, body=b"", headers=None, query=None):
        """宿主直接调用：返回 Response，不碰任何服务器实现。"""
        self.build()
        raw = path or "/"
        if not raw.startswith("/"):
            raw = "/" + raw
        if query:
            raw += ("&" if "?" in raw else "?") + urllib.parse.urlencode(query, doseq=True)
        if isinstance(body, dict):
            body = json.dumps(body).encode("utf-8")
            headers = dict(headers or {}, **{"Content-Type": "application/json"})
        elif isinstance(body, str):
            body = body.encode("utf-8")
        return self.dispatcher.handle((method or "GET").upper(), raw,
                                      headers or {}, body or b"")

    def wsgi(self, environ, start_response):
        """WSGI 适配：Flask / Django / gunicorn 旁边挂一个前缀就能用。"""
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO") or "/"
        qs = environ.get("QUERY_STRING") or ""
        if qs:
            path += "?" + qs
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        body = environ["wsgi.input"].read(length) if length else b""
        headers = {}
        for key, value in environ.items():
            if key.startswith("HTTP_"):
                headers[key[5:].replace("_", "-").title()] = value

        resp = self.handle(method, path, body=body, headers=headers)
        payload = b"" if method == "HEAD" else resp.body
        out = [("Content-Type", resp.ctype),
               ("Cache-Control", resp.cache),
               ("Content-Length", str(len(payload)))]
        out += list((resp.headers or {}).items())
        start_response("%d %s" % (resp.status, _REASONS.get(resp.status, "OK")), out)
        return [payload]

    def serve(self, host=None, port=None, quiet=False):
        """自己起服务（阻塞）。返回退出后的 BoundServer，便于读实际端口。"""
        self.start()
        c = self.config
        srv = self.transport.bind(self.dispatcher.handle,
                                  host or c.host,
                                  c.port if port is None else port)
        self.server = srv
        real_host, real_port = srv.address
        c.port = real_port
        if self.ctx is not None:
            self.ctx.port = real_port
        if not quiet:
            print("  %s 已启动：http://%s:%d/" % (c.app_name, real_host, real_port))
        try:
            srv.serve_forever()
        finally:
            srv.close()
            self.server = None
            self.stop()
        return srv

    # ------------------------------------------------------------ 观察
    def status(self):
        c = self.config
        return {
            "ok": self._built,
            "app": c.app_name,
            "version": c.version,
            "dataDir": c.resolve_data_dir(),
            "port": self.ctx.port if self.ctx else c.port,
            "modules": self.ctx.modules.ids() if self.ctx and self.ctx.modules else [],
            "views": [v.id for v in (self.views or [])],
            "jobs": len(self.jobs) if self.jobs else 0,
            "adminPrefix": c.admin_prefix,
            "pluginPrefix": c.plugin_prefix,
        }

    def __repr__(self):
        return "<Kernel %s v%s built=%s>" % (self.config.app_name,
                                             self.config.version, self._built)


def create_kernel(config=None, **kwargs):
    """创建并组装内核（不启动任务表、不起服务）。"""
    k = Kernel(config, **kwargs)
    k.build()
    return k


def bootstrap_payload(kernel, req=None):
    """不经过 HTTP 也能拿一份首屏数据（宿主自己渲染时用）。"""
    admin = AdminApi(kernel, kernel.config.admin_prefix)
    from .http import Request
    return json.loads(admin.bootstrap(req or Request("GET", "/")).body)
