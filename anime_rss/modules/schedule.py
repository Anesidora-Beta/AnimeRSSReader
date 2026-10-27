"""新番放送表模块：聚合各平台放送时间，输出一周周历。

它是第一个按新契约搬迁的模块，用来验证「模块只靠 ctx 就能完整工作」：
服务实例由模块自己建、路由挂在 /api/schedule/、配置读写走 ctx.settings，
核心 API 与 app.py 都不再知道它的存在（除了 bootstrap 里一个可选摘要）。
"""
from ..kernel.http import Response
from ..module import Module

import schedule as SCH


class ScheduleModule(Module):
    id = "schedule"
    name = "新番放送表"
    version = "1.0.0"

    # 设置在设置页里显示成什么样（模块自带，不再写进中央 schema）
    SETTINGS_GROUP = {
        "group": "schedule",
        "title": "新番放送表",
        "note": "关掉的来源不再抓取；已经抓到的数据下次刷新后消失。",
        "items": [
            {"k": "scheduleSources", "type": "checks", "label": "数据来源",
             "default": [p["id"] for p in SCH.PLATFORMS],
             "options": [{"value": pid, "label": label} for pid, label in (
                 ("bilibili", "哔哩哔哩"), ("bangumi", "Bangumi"),
                 ("anilist", "AniList"), ("mal", "MAL / Jikan"))]},
            {"k": "scheduleTtl", "type": "number", "label": "缓存有效期",
             "desc": "超过这个时间就在后台重新抓放送表", "default": 6,
             "min": 1, "max": 48, "step": 1, "unit": "小时"},
            {"k": "airingNotify", "type": "switch", "label": "开播提醒",
             "desc": "有番即将开播时弹浏览器通知（需要授权）", "default": False},
        ],
    }

    def __init__(self):
        self.service = None

    # ------------------------------------------------------------ 生命周期
    def setup(self, ctx):
        # 先把自己的设置项登记进去，后面读配置才拿得到默认值
        if ctx.settings is not None:
            ctx.settings.define(self.SETTINGS_GROUP, after="read")

        cfg = ctx.settings.get() if ctx.settings is not None else {}
        self.service = SCH.Service(
            ctx.store,
            enabled=cfg.get("scheduleSources"),
            ttl=int(cfg.get("scheduleTtl") or 6) * 3600,
        )
        ctx.schedule = self.service          # 老代码路径仍然可用

        r = ctx.router
        r.get("/api/schedule", self.api_list)
        r.get("/api/schedule/status", self.api_status)
        r.get("/api/schedule/progress", self.api_progress)
        r.post("/api/schedule/refresh", self.api_refresh)

    def teardown(self):
        self.service = None

    # ------------------------------------------------------------ 设置联动
    def apply_settings(self, values):
        """设置里改了来源开关 / 缓存时长后调用，下一次抓取按新配置走。"""
        if self.service is None:
            return
        try:
            self.service.configure(
                enabled=values.get("scheduleSources"),
                ttl=int(values.get("scheduleTtl") or 6) * 3600,
            )
        except Exception:
            pass

    # ------------------------------------------------------------ 给别处用
    def bootstrap(self, ctx):
        return {"schedule": self.brief()}

    def brief(self):
        """侧栏「新番表」上那个数字：今天要播几部。"""
        sch = self.service
        if sch is None:
            return {"total": 0, "today": 0, "updated": 0, "running": False}
        d = sch.data()
        return {"total": len(d.get("entries") or []),
                "today": sch.today_count(),
                "updated": d.get("updated") or 0,
                "running": bool(sch.state["running"])}

    def warm_up(self):
        """启动时预热一次，打开放送表就不用干等。"""
        if self.service is not None and self.service.should_auto_refresh():
            self.service.start()

    # ------------------------------------------------------------ 路由
    def api_list(self, req):
        sch = self.service
        if sch is None:
            return Response.err("放送表未初始化", 503)
        flag = req.query.get("refresh")
        if flag in ("1", "true", "yes"):
            sch.start(force=True)
        elif sch.should_auto_refresh():
            sch.start()
        return Response.json(sch.payload())

    def api_status(self, req):
        sch = self.service
        if sch is None:
            return Response.err("放送表未初始化", 503)
        return Response.json({"ok": True, **sch.status()})

    def api_progress(self, req):
        return self.api_status(req)

    def api_refresh(self, req):
        sch = self.service
        if sch is None:
            return Response.err("放送表未初始化", 503)
        body = req.json_body()
        started = sch.start(force=bool(body.get("force", True)))
        return Response.json({"ok": True, "started": started, **sch.status()})
