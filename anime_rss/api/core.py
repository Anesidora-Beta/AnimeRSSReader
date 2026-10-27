"""核心 API：外壳相关的一点点路由，以及 bootstrap 的聚合。

功能都搬进 ``anime_rss/modules/`` 了，这里只剩三件事：

* 静态页与图标（``/``、``/favicon.ico``）；
* 健康检查（``/healthz``）；
* ``/api/bootstrap``：把各模块的实现摘要拼成一份首屏数据。

聚合方式是约定而不是点名：谁的模块实现了 ``bootstrap(ctx) -> dict``，
返回值就并进结果里。新增模块不需要回来改这个文件。
"""
import time

from ..kernel.http import Response


class CoreApi:
    def __init__(self, ctx):
        self.ctx = ctx

    def register(self, router):
        r = router
        r.get("/", self.index)
        r.get("/index.html", self.index)
        r.get("/favicon.ico", self.favicon)
        r.get("/apple-touch-icon.png", self.favicon)
        r.get("/healthz", self.health)
        r.get("/api/health", self.health)
        r.get("/api/bootstrap", self.bootstrap)
        r.get("/api/views", self.views)
        r.get("/api/jobs", self.jobs)
        r.get("/plug/{mid}/{name:path}", self.plugin_asset)
        r.post("/api/open-window", self.open_window)
        return self

    # ------------------------------------------------------------ 外壳
    def index(self, req):
        page = self.ctx.assets.read(self.ctx.static_dir, "index.html")
        if page is None:
            page = "static/index.html 缺失".encode("utf-8")
        page = page.replace(b"__VERSION__", self.ctx.version.encode())
        return Response(page, "text/html; charset=utf-8", cache="no-cache")

    def favicon(self, req):
        data = self.ctx.assets.read(self.ctx.static_dir, "icon.svg") or b""
        return Response(data, "image/svg+xml", cache="max-age=604800")

    def health(self, req):
        return Response.json({"ok": True, "app": "anime-rss-reader",
                              "version": self.ctx.version,
                              "uptime": self.ctx.uptime})

    def open_window(self, req):
        if self.ctx.ui is None:
            return Response.json({"ok": True, "opened": False,
                                  "reason": "当前实例没有窗口监督器"})
        return Response.json({"ok": True, "opened": self.ctx.ui.request_open()})

    # ------------------------------------------------------------ 插件资源与运行状态
    def plugin_asset(self, req):
        """模块自带的前端资源：/plug/<模块 id>/<文件>。

        目录穿越在 ViewRegistry.read 里拦掉，这里只负责把结果包成响应。
        """
        reg = self.ctx.views
        if reg is None:
            return Response.err("当前实例没有视图注册表", 404)
        got = reg.read(req.params["mid"], req.params["name"])
        if got is None:
            return Response.err("资源不存在", 404)
        data, ctype = got
        return Response(data, ctype, cache="no-cache")

    def views(self, req):
        reg = self.ctx.views
        items = reg.manifest() if reg is not None else []
        return Response.json({"ok": True, "views": items})

    def jobs(self, req):
        if self.ctx.jobs is None:
            return Response.json({"ok": True, "running": False, "jobs": []})
        return Response.json(self.ctx.jobs.status())

    # ------------------------------------------------------------ 首屏聚合
    def bootstrap(self, req):
        ctx = self.ctx
        data = {
            "ok": True,
            "app": ctx.app_name,
            "version": ctx.version,
            "counts": ctx.store.counts(),
            "stats": ctx.store.stats(),
            "refresh": ctx.refresher.status(),
            "lan": {"enabled": ctx.lan_mode,
                    "urls": [ctx.lan_url()] if ctx.lan_mode else [],
                    "ip": ctx.lan_url().split("//")[-1].split(":")[0]},
            "baseUrl": ctx.base_url(req),
            "serverTime": int(time.time()),
            "views": ctx.views.manifest() if ctx.views is not None else [],
        }
        # 各模块的摘要：谁有 bootstrap(ctx) 就并进来，出错的模块不影响首屏
        if ctx.modules is not None:
            for mod in ctx.modules:
                hook = getattr(mod, "bootstrap", None)
                if not callable(hook):
                    continue
                try:
                    frag = hook(ctx)
                except Exception:
                    continue
                if isinstance(frag, dict):
                    data.update(frag)
        return Response.json(data)
