"""内核自带的管理接口：健康检查、模块表、视图表、任务表、配置、首屏聚合。

全部挂在可配置的 ``/_kit/`` 前缀下，**故意不占** ``/api/``：
``/api/`` 是业务模块的地盘，内核不该跟它们抢路径。
"""
import time

from .http import Response

__all__ = ["AdminApi"]


class AdminApi:
    def __init__(self, kernel, prefix="/_kit"):
        self.kernel = kernel
        self.prefix = (prefix or "/_kit").rstrip("/")

    @property
    def ctx(self):
        return self.kernel.ctx

    # ------------------------------------------------------------ 路由
    def register(self, router):
        p = self.prefix
        router.get(p + "/health", self.health)
        router.get(p + "/status", self.status)
        router.get(p + "/modules", self.modules)
        router.get(p + "/views", self.views)
        router.get(p + "/jobs", self.jobs)
        router.post(p + "/jobs/{name}/run", self.run_job)
        router.get(p + "/bootstrap", self.bootstrap)
        router.get(p + "/settings", self.get_settings)
        router.post(p + "/settings", self.save_settings)
        router.post(p + "/settings/reset", self.reset_settings)
        return self

    # ------------------------------------------------------------ 处理器
    def health(self, req):
        ctx = self.ctx
        return Response.json({
            "ok": True,
            "app": ctx.app_name,
            "version": ctx.version,
            "uptime": ctx.uptime,
            "modules": len(ctx.modules) if ctx.modules else 0,
        })

    def status(self, req):
        return Response.json(self.kernel.status())

    def modules(self, req):
        reg = self.ctx.modules
        return Response.json({"ok": True,
                              "modules": reg.status() if reg else []})

    def views(self, req):
        reg = self.ctx.views
        return Response.json({"ok": True,
                              "views": reg.manifest() if reg else []})

    def jobs(self, req):
        jobs = self.ctx.jobs
        return Response.json(jobs.status() if jobs else {"ok": True, "jobs": []})

    def run_job(self, req):
        jobs = self.ctx.jobs
        if jobs is None:
            return Response.err("没有任务表", 404)
        name = req.param("name")
        if not jobs.trigger(name):
            return Response.err("任务不存在或正在运行：%s" % name, 409)
        return Response.json({"ok": True, "triggered": name})

    def bootstrap(self, req):
        """首屏聚合：内核字段 + 每个模块自愿贡献的一段。

        模块的 ``bootstrap(ctx)`` 抛异常只跳过它自己 —— 一个插件写坏了，
        不该让整个界面白屏。
        """
        ctx = self.ctx
        data = {
            "ok": True,
            "app": ctx.app_name,
            "version": ctx.version,
            "baseUrl": ctx.base_url(req),
            "serverTime": int(time.time()),
            "modules": ctx.modules.status() if ctx.modules else [],
            "views": ctx.views.manifest() if ctx.views else [],
            "settings": ctx.settings.snapshot() if ctx.settings else {"values": {},
                                                                      "groups": []},
        }
        for mod in (ctx.modules or []):
            hook = getattr(mod, "bootstrap", None)
            if not callable(hook):
                continue
            try:
                patch = hook(ctx)
            except Exception as e:                # noqa: BLE001 - 模块自己的问题
                ctx.say("模块 %s 的 bootstrap 出错：%s: %s"
                        % (getattr(mod, "id", "?"), type(e).__name__, e))
                continue
            if not isinstance(patch, dict):
                continue
            for key, value in patch.items():
                data.setdefault(key, value)      # 内核字段不被覆盖
        return Response.json(data)

    # ------------------------------------------------------------ 配置
    def get_settings(self, req):
        s = self.ctx.settings
        if s is None:
            return Response.json({"ok": True, "values": {}, "groups": []})
        return Response.json(dict(s.snapshot(), ok=True))

    def save_settings(self, req):
        s = self.ctx.settings
        if s is None:
            return Response.err("没有配置表", 404)
        values, changed = s.update(req.json_body())
        if changed:
            self._apply(changed)
        return Response.json({"ok": True, "changed": changed, "values": values})

    def reset_settings(self, req):
        s = self.ctx.settings
        if s is None:
            return Response.err("没有配置表", 404)
        body = req.json_body()
        keys = body.get("keys")
        changed = s.reset(keys if isinstance(keys, list) else None)
        if changed:
            self._apply(changed)
        return Response.json({"ok": True, "changed": changed, "values": s.get()})

    def _apply(self, changed):
        """把生效通知广播出去：模块的回调 + 一条事件。"""
        ctx = self.ctx
        values = ctx.settings.get()
        for mod in (ctx.modules or []):
            hook = getattr(mod, "apply_settings", None)
            if not callable(hook):
                continue
            try:
                hook(values)
            except Exception as e:                # noqa: BLE001 - 一个模块回调失败不拖累别人
                ctx.say("模块 %s 的 apply_settings 出错：%s: %s"
                        % (getattr(mod, "id", "?"), type(e).__name__, e))
        ctx.emit("settings.changed", {"keys": list(changed), "values": values})
