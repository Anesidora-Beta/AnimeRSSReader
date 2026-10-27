"""示例插件：最小但完整的第三方模块。

安装方式（任选其一）：

1. 把 ``hello.py`` 与 ``static/`` 一起复制到程序目录下的 ``plugins/`` 里
   （启动时自动发现，目录结构如下）：

       plugins/
         hello.py
         static/
           view.js
           view.css

2. 打成 Python 包，把静态目录一起带上，并在 ``pyproject.toml`` 里声明入口点：

   ```toml
   [project.entry-points."anime_rss.plugins"]
   hello = "my_hello_plugin:HelloModule"
   ```

生效后：

* ``GET /api/hello`` 返回一句问候，``/api/hello/stats`` 是它自己的统计；
* 设置页多一个「示例插件」分组；
* 侧栏多一个「示例插件」入口 —— 界面来自 ``static/view.js``，主程序没为它写过 HTML；
* 事件总线：订阅 ``refresh.finished``，每次抓完记一笔；
* 定时任务：每 60 秒 +1，进程退出时由内核统一停掉。

注意：插件与主程序同进程运行，拥有完整权限 —— 只装你信任的插件。
"""
from anime_rss.kernel.http import Response
from anime_rss.module import Module


class HelloModule(Module):
    id = "hello"
    name = "示例插件"
    version = "0.2.0"

    # 前端视图：脚本与样式都在本模块的 static/ 下，走 /plug/hello/ 对外提供
    views = ({
        "id": "hello",
        "title": "示例插件",
        "icon": "◆",
        "order": 50,
        "description": "插件自带界面的最小示例",
        "script": "view.js",
        "style": "view.css",
    },)
    assets = "static"

    def __init__(self):
        self.refreshes = 0
        self.ticks = 0
        self.last_refresh = None

    def setup(self, ctx):
        # 自己的设置项：登记完设置页自动出现，不用改主程序的任何文件
        ctx.settings.define({
            "group": "hello",
            "title": "示例插件",
            "note": "这是 plugins/hello.py 带来的设置分组。",
            "items": [
                {"k": "helloName", "type": "text", "label": "称呼",
                 "desc": "接口里回应谁", "default": "世界"},
                {"k": "helloShout", "type": "switch", "label": "大写回答",
                 "default": False},
            ],
        }, after="read")

        ctx.router.get("/api/hello", self.api_hello)
        ctx.router.get("/api/hello/stats", self.api_stats)

        # 跨模块通信只走事件，不 import 别的模块
        ctx.on("refresh.finished", self._on_refresh)

        # 定时任务交给内核统一管，退出时自动收尾
        ctx.every(60, self._tick, name="hello.tick", run_now=False)

    def teardown(self):
        self.last_refresh = None

    # ------------------------------------------------------------ 事件与任务
    def _on_refresh(self, payload):
        self.refreshes += 1
        self.last_refresh = payload or {}

    def _tick(self):
        self.ticks += 1

    # ------------------------------------------------------------ 钩子
    def bootstrap(self, ctx):                 # 可选：贡献首屏数据
        return {"hello": {"greeting": self._greeting(ctx), "views": len(self.views)}}

    def apply_settings(self, values):         # 可选：设置变了被回调
        self._cache = None

    def _greeting(self, ctx):
        vals = ctx.settings.get() if ctx.settings else {}
        text = "你好，%s" % (vals.get("helloName") or "世界")
        return text.upper() if vals.get("helloShout") else text

    # ------------------------------------------------------------ 路由
    def api_hello(self, req):
        return Response.json({"ok": True, "text": self._greeting(self.ctx),
                              "module": self.id, "version": self.version})

    def api_stats(self, req):
        status = self.ctx.jobs.status() if self.ctx.jobs is not None else {}
        mine = [j for j in status.get("jobs") or [] if j["name"] == "hello.tick"]
        return Response.json({
            "ok": True,
            "refreshes": self.refreshes,
            "ticks": self.ticks,
            "lastRefresh": self.last_refresh or {},
            "job": mine[0] if mine else None,
            "bus": self.ctx.bus.status() if self.ctx.bus is not None else {},
        })
