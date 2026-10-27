"""可嵌入的应用组装：一处拼装，三种运行姿态。

    app = create_app(data_dir="...")     # 组装（不联网、不起服务）
    app.handle("GET", "/api/items")      # ① 宿主直接调，拿 Response
    wsgi = app.wsgi                      # ② 交给任意 WSGI 框架
    app.serve("127.0.0.1", 8899)         # ③ 自己起服务（桌面版走这条）

桌面入口 ``app.py`` 现在也只是这份组装 + 窗口监督，不再自己拼部件 ——
否则「自己跑」和「被别人加载」迟早会长成两套逻辑。

同样的路由会额外挂一份 ``/api/v1/...`` 别名：老路径继续可用（用户的脚本、
手机上的阅读器都还指着它），新接入方用版本化前缀，将来换版本不必再动老路径。
"""
import json
import os
import time
import urllib.parse

import feeds as F
import settings as CFG
import store as ST

from . import __version__
from .api.core import CoreApi
from .discovery import discover, merge_unique
from .kernel.assets import Assets, Mount
from .kernel.bus import Bus
from .kernel.context import Context
from .kernel.http import Response, Router
from .kernel.jobs import Jobs
from .kernel.server import Dispatcher, make_server
from .kernel.views import ViewRegistry
from .module import ModuleError, load_modules, unload_modules
from .modules import BUILTIN_MODULES

__all__ = ["App", "AppConfig", "create_app", "DEFAULT_APP_NAME", "API_PREFIX"]

DEFAULT_APP_NAME = "动漫资源 · RSS 阅读器"
API_PREFIX = "/api/v1"

_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 409: "Conflict", 413: "Payload Too Large",
    429: "Too Many Requests", 500: "Internal Server Error",
    503: "Service Unavailable",
}


class AppConfig:
    """一次运行的全部参数。全是可选，默认值就是「本机跑起来」。"""

    __slots__ = ("data_dir", "static_dir", "base_dir", "port", "host", "lan_mode",
                 "app_name", "version", "log", "no_third_party", "fetch_on_start",
                 "auto_refresh", "modules", "plugin_dirs", "api_v1")

    def __init__(self, data_dir=None, static_dir=None, base_dir="", port=8899,
                 host="127.0.0.1", lan_mode=False, app_name=DEFAULT_APP_NAME,
                 version=__version__, log=None, no_third_party=False,
                 fetch_on_start=False, auto_refresh=True, modules=None,
                 plugin_dirs=None, api_v1=True):
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.base_dir = base_dir or ""
        self.port = int(port)
        self.host = host
        self.lan_mode = bool(lan_mode)
        self.app_name = app_name
        self.version = version
        self.log = log
        self.no_third_party = bool(no_third_party)
        # 嵌入场景默认不主动抓取：宿主没准备好时不该先打一轮网络
        self.fetch_on_start = bool(fetch_on_start)
        self.auto_refresh = bool(auto_refresh)
        self.modules = list(modules) if modules else None
        self.plugin_dirs = list(plugin_dirs) if plugin_dirs is not None else None
        self.api_v1 = bool(api_v1)

    def resolve_data_dir(self):
        if self.data_dir:
            return os.path.abspath(self.data_dir)
        env = os.environ.get("ANIME_RSS_DATA")
        if env:
            return os.path.abspath(env)
        return os.path.join(os.path.abspath(self.base_dir or os.getcwd()), "data")

    def resolve_static_dir(self):
        if self.static_dir:
            return os.path.abspath(self.static_dir)
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")

    def resolve_plugin_dirs(self):
        if self.plugin_dirs is not None:
            return list(self.plugin_dirs)
        return [os.path.join(os.path.abspath(self.base_dir or os.getcwd()), "plugins")]

    def __repr__(self):
        return "<AppConfig data_dir=%r port=%r>" % (self.data_dir, self.port)


class App:
    """组装好的应用实例。一个进程里可以存在多个（各有各的库与任务表）。"""

    def __init__(self, config=None, **kwargs):
        self.config = config if isinstance(config, AppConfig) else AppConfig(**kwargs)
        self.store = None
        self.settings = None
        self.refresher = None
        self.bus = None
        self.jobs = None
        self.views = None
        self.router = None
        self.assets = None
        self.ctx = None
        self.auto = None
        self.dispatcher = None
        self.modules = []
        self._built = False
        self._started = False
        self._stopped = False

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

        self.store = ST.Store(os.path.join(data_dir, "reader.db"))
        try:
            self.settings = CFG.Settings(self.store)
            self.bus = Bus(log=say)
            self.jobs = Jobs(log=say)
            self.views = ViewRegistry()
            self.refresher = F.Refresher(self.store, self.settings, bus=self.bus)
            F.STATE_PATH = os.path.join(data_dir, "hosts.json")   # 记住镜像可用状态

            self.assets = Assets()
            self.router = Router()
            self.ctx = Context(
                store=self.store, settings=self.settings, refresher=self.refresher,
                assets=self.assets, router=self.router, port=c.port,
                lan_mode=c.lan_mode, data_dir=data_dir, static_dir=static_dir,
                app_name=c.app_name, version=c.version,
                bus=self.bus, jobs=self.jobs, views=self.views, log=c.log)

            self.modules = self._collect_modules(say)
            load_modules(self.ctx, self.modules, log=say)

            self.auto = CFG.AutoRefresh(self.settings, self.refresher,
                                        self.ctx.schedule, self.store)
            self.ctx.auto = self.auto
            CoreApi(self.ctx).register(self.router)
            if c.api_v1:
                alias_api_v1(self.router)

            mounts = [Mount("/assets/", static_dir)]
            self.dispatcher = Dispatcher(self.router, self.assets, mounts)
        except Exception:
            self._close_quiet()
            raise

        self._built = True
        return self

    def _collect_modules(self, say):
        modules = list(self.config.modules or BUILTIN_MODULES)
        if self.config.no_third_party:
            return modules
        try:
            extra, _problems = discover(self.config.resolve_plugin_dirs(), log=say)
        except Exception as e:                     # noqa: BLE001 - 发现失败不该让内置功能起不来
            say("插件发现失败：%s: %s" % (type(e).__name__, e))
            return modules
        if extra:
            modules = merge_unique(modules, extra, log=say)
        return modules

    # ------------------------------------------------------------ 启动 / 停止
    def start(self):
        """启动任务表，并按配置安排首次抓取与自动刷新。"""
        self.build()
        if self._started:
            return self
        self._started = True

        self.jobs.start()
        c = self.config
        if c.fetch_on_start:
            try:
                cfg = self.settings.get()
                stale = (time.time() - (self.store.newest_ts() or 0)) > 1800
                if stale and cfg.get("fetchOnStart", True):
                    self.jobs.later(0.6, self.refresher.start, name="boot-fetch")
                sch = self.ctx.module("schedule")
                if sch is not None:
                    self.jobs.later(1.2, sch.warm_up, name="boot-schedule")
            except Exception as e:                 # noqa: BLE001 - 预热失败不影响服务
                (c.log or (lambda m: None))("启动预热失败：%s: %s" % (type(e).__name__, e))

        if c.auto_refresh:
            self.jobs.every(self.auto.TICK, self.auto.tick, name="auto-refresh")

        ids = list(self.ctx.modules.ids()) if self.ctx.modules else []
        self.bus.emit("app.started", {"version": c.version, "port": c.port,
                                      "modules": ids})
        return self

    def stop(self):
        """停任务、收尾模块、关库。可以重复调用。"""
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
            unload_modules(self.ctx.modules if self.ctx else None,
                           log=self.config.log)
        except Exception:
            pass
        self._close_quiet()
        self._built = False
        self._started = False
        self._stopped = True
        return self

    def _close_quiet(self):
        try:
            if self.store is not None:
                self.store.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()
        return False

    # ------------------------------------------------------------ 三种运行姿态
    def handle(self, method, path, body=b"", headers=None, query=None):
        """宿主直接调用：返回内核的 Response，不碰任何服务器实现。"""
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
        return self.dispatcher.handle(method.upper(), raw, headers or {}, body or b"")

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
        out_headers = [("Content-Type", resp.ctype),
                       ("Cache-Control", resp.cache),
                       ("Content-Length", str(len(payload)))]
        out_headers += list((resp.headers or {}).items())
        start_response("%d %s" % (resp.status, _REASONS.get(resp.status, "OK")),
                       out_headers)
        return [payload]

    def serve(self, host=None, port=None, quiet=False):
        """自己起 stdlib 服务（阻塞）。桌面版在窗口监督之外用的就是它。"""
        self.start()
        c = self.config
        srv = make_server(host or c.host, port or c.port, self.dispatcher, quiet=quiet)
        try:
            srv.serve_forever()
        finally:
            try:
                srv.server_close()
            except Exception:
                pass
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
            "modules": list(self.ctx.modules.ids()) if self.ctx and self.ctx.modules else [],
            "views": [v.id for v in (self.views or [])],
            "jobs": len(self.jobs) if self.jobs else 0,
            "apiPrefix": API_PREFIX if c.api_v1 else "",
        }

    def __repr__(self):
        return "<App %s v%s built=%s>" % (self.config.app_name,
                                          self.config.version, self._built)


def create_app(config=None, **kwargs):
    """创建并组装应用（不启动任务表、不起服务）。"""
    app = App(config, **kwargs)
    app.build()
    return app


def alias_api_v1(router):
    """给现有 ``/api/...`` 路由挂一份 ``/api/v1/...`` 别名，老路径保持不动。"""
    existing = set(router.paths())
    added = 0
    for method, pattern, fn in list(router.routes):
        if not pattern or pattern[0] != "api":
            continue
        if len(pattern) > 1 and pattern[1] == "v1":
            continue
        target = API_PREFIX + "".join("/" + seg for seg in pattern[1:])
        if "%s %s" % (method, target) in existing:
            continue
        router.add(method, target, fn)
        existing.add("%s %s" % (method, target))
        added += 1
    return added
