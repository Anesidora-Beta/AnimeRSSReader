"""设置模块：设置的读写、恢复默认、数据库清理与压缩。

schema 本身由 ``ctx.settings`` 提供，各模块在 setup() 里往这块表里登记自己的
分组；这个模块只负责「读出来给前端」和「写回去并通知各模块」。
"""
import time

from ..common import apply_settings, settings_payload
from ..kernel.http import Response
from ..module import Module


class SettingsModule(Module):
    id = "settings"
    name = "设置"
    version = "1.0.0"

    def setup(self, ctx):
        r = ctx.router
        r.get("/api/settings", self.api_get)
        r.post("/api/settings", self.api_save)
        r.post("/api/settings/reset", self.api_reset)
        r.post("/api/maintenance", self.api_maintenance)

    def bootstrap(self, ctx):
        return {"settings": ctx.settings.get() if ctx.settings else {}}

    # ------------------------------------------------------------ 读写
    def api_get(self, req):
        if self.ctx.settings is None:
            return Response.err("设置未初始化", 503)
        return Response.json(settings_payload(self.ctx))

    def api_save(self, req):
        ctx = self.ctx
        if ctx.settings is None:
            return Response.err("设置未初始化", 503)
        body = req.json_body()
        patch = body.get("values") if isinstance(body.get("values"), dict) else body
        vals = ctx.settings.save(patch)
        apply_settings(ctx, vals)
        if ctx.auto is not None:
            ctx.auto.last_try = time.time()          # 改了间隔就重新计时
        return Response.json({"ok": True, "values": vals,
                              "info": settings_payload(ctx)["info"]})

    def api_reset(self, req):
        ctx = self.ctx
        if ctx.settings is None:
            return Response.err("设置未初始化", 503)
        vals = ctx.settings.reset()
        apply_settings(ctx, vals)
        if ctx.auto is not None:
            ctx.auto.last_try = time.time()
        return Response.json({"ok": True, "values": vals,
                              "info": settings_payload(ctx)["info"]})

    # ------------------------------------------------------------ 维护
    def api_maintenance(self, req):
        ctx = self.ctx
        body = req.json_body()
        action = (body.get("action") or "clean").lower()
        if action == "vacuum":
            ctx.store.vacuum()
            return Response.json({"ok": True, "action": action,
                                  "stats": ctx.store.stats()})
        vals = ctx.settings.get() if ctx.settings else {}
        removed, expired = ctx.store.prune(
            days=int(vals.get("keepDays") or 30),
            cap=int(vals.get("keepMax") or 30000))
        if body.get("vacuum", True):
            ctx.store.vacuum()
        return Response.json({"ok": True, "action": "clean",
                              "removed": removed, "expiredWatches": expired,
                              "stats": ctx.store.stats(), "counts": ctx.store.counts()})
