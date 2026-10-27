"""arrkit 的最小完整示例：三个模块，把内核能力全用一遍。

    python examples/mini_app.py            # 起服务，浏览器打开

模块清单：

* ``clock``  每秒跳一次、发 ``clock.tick`` 事件、自带前端视图；
* ``notes``  依赖 clock，订阅它的事件，提供增删查接口；
* ``about``  只贡献首屏数据，证明「模块可以小到只有一个 bootstrap」。

这个文件不依赖仓库里任何业务代码 —— 复制到别处、``pip install arrkit`` 照样跑。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arrkit import Module, create_kernel          # noqa: E402
from arrkit.http import Response                  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


# ---------------------------------------------------------------- clock
class Clock(Module):
    id = "clock"
    name = "时钟"
    version = "0.1.0"
    assets = "static/clock"
    views = ({
        "id": "clock",
        "title": "时钟",
        "icon": "◷",
        "script": "view.js",
        "style": "view.css",
        "order": 10,
        "description": "每秒跳一次，事件总线演示",
    },)

    def setup(self, ctx):
        self.ticks = 0
        self.started = time.time()

        ctx.settings.define("clock", [
            {"key": "clock.format", "label": "时间格式", "type": "select",
             "default": "24h",
             "options": [{"value": "24h", "label": "24 小时"},
                         {"value": "12h", "label": "12 小时"}]},
            {"key": "clock.showSeconds", "label": "显示秒", "type": "bool",
             "default": True},
            {"key": "clock.interval", "label": "刷新间隔（秒）", "type": "int",
             "default": 1, "min": 1, "max": 60, "unit": "秒"},
        ], title="时钟")

        ctx.router.get("/api/clock", self.now)
        ctx.router.get("/api/clock/stats", self.stats)

        ctx.every(1, self.tick, name="clock.tick")
        ctx.on("clock.ping", self.on_ping)

    # ---- 任务：每秒一次
    def tick(self):
        self.ticks += 1
        self.ctx.emit("clock.tick", {"n": self.ticks})

    # ---- 事件：别人 ping 我，我回一条
    def on_ping(self, payload):
        self.ctx.say("clock 收到 ping：%r" % (payload,))

    # ---- 路由
    def now(self, req):
        fmt = (self.ctx.settings.get("clock.format") if self.ctx.settings else "24h") or "24h"
        return Response.json({
            "ok": True,
            "ticks": self.ticks,
            "time": time.strftime("%H:%M:%S" if fmt == "24h" else "%I:%M:%S %p"),
            "uptime": int(time.time() - self.started),
        })

    def stats(self, req):
        return Response.json({
            "ok": True,
            "ticks": self.ticks,
            "subscribers": self.ctx.bus.status() if self.ctx.bus else {},
        })

    # ---- 首屏数据
    def bootstrap(self, ctx):
        return {"clock": {"ticks": self.ticks}}

    # ---- 设置变了
    def apply_settings(self, values):
        self.ctx.say("clock 的格式改成了 %s" % values.get("clock.format"))


# ---------------------------------------------------------------- notes
class Notes(Module):
    id = "notes"
    name = "便签"
    version = "0.1.0"
    depends = ("clock",)                    # 依赖顺序由内核拓扑排序
    assets = "static/notes"
    views = ({
        "id": "notes",
        "title": "便签",
        "icon": "✎",
        "script": "view.js",
        "style": "view.css",
        "order": 20,
        "description": "订阅 clock.tick，记录跳了几次",
    },)

    def setup(self, ctx):
        self.items = []
        self.seen_ticks = 0
        ctx.router.get("/api/notes", self.list_items)
        ctx.router.post("/api/notes", self.add_item)
        ctx.router.delete("/api/notes/{nid}", self.del_item)
        ctx.on("clock.tick", self.on_tick)   # 跨模块只走事件

    def on_tick(self, payload):
        self.seen_ticks = (payload or {}).get("n", 0)

    def list_items(self, req):
        return Response.json({"ok": True, "items": self.items,
                              "seenTicks": self.seen_ticks})

    def add_item(self, req):
        text = str(req.json_body().get("text") or "").strip()
        if not text:
            return Response.err("内容不能为空")
        item = {"id": len(self.items) + 1, "text": text,
                "at": time.strftime("%H:%M:%S")}
        self.items.insert(0, item)
        return Response.json({"ok": True, "item": item}, status=201)

    def del_item(self, req):
        try:
            nid = int(req.param("nid"))
        except (TypeError, ValueError):
            return Response.err("id 不合法")
        before = len(self.items)
        self.items = [x for x in self.items if x["id"] != nid]
        return Response.json({"ok": True, "removed": before - len(self.items)})

    def bootstrap(self, ctx):
        return {"notes": {"count": len(self.items)}}


# ---------------------------------------------------------------- about
class About(Module):
    """只有一段首屏数据的模块 —— 证明模块可以非常小。"""

    id = "about"
    name = "关于"
    version = "0.1.0"

    def bootstrap(self, ctx):
        return {"about": {"kit": "arrkit", "modules": 3,
                          "note": "这段数据由 about 模块贡献"}}


def main():
    port = int(os.environ.get("PORT") or 8899)
    log = print if os.environ.get("QUIET") != "1" else None

    kernel = create_kernel(
        app_name="arrkit 示例",
        static_dir=STATIC,
        port=port,
        log=log,
        modules=[Clock, Notes, About],
        no_plugins=True,
    )
    print("  打开 http://127.0.0.1:%d/ 看看" % port)
    kernel.serve()


if __name__ == "__main__":
    main()
