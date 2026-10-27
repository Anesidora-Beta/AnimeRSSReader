"""示例插件：最小可用的第三方模块。

安装方式（任选其一）：

1. 把本文件复制到程序目录下的 ``plugins/`` 里（启动时自动发现）；
2. 打成 Python 包，在 ``pyproject.toml`` 里声明入口点：

   ```toml
   [project.entry-points."anime_rss.plugins"]
   hello = "my_hello_plugin:HelloModule"
   ```

生效后：侧栏设置里会多一个「示例插件」分组，``GET /api/hello`` 返回一句问候。

注意：插件与主程序同进程运行，拥有完整权限 —— 只装你信任的插件。
"""
from anime_rss.kernel.http import Response
from anime_rss.module import Module


class HelloModule(Module):
    id = "hello"
    name = "示例插件"
    version = "0.1.0"

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

    def bootstrap(self, ctx):                 # 可选：贡献首屏数据
        return {"hello": {"greeting": self._greeting(ctx)}}

    def apply_settings(self, values):         # 可选：设置变了被回调
        self._cache = None

    def _greeting(self, ctx):
        vals = ctx.settings.get() if ctx.settings else {}
        text = "你好，%s" % (vals.get("helloName") or "世界")
        return text.upper() if vals.get("helloShout") else text

    def api_hello(self, req):
        return Response.json({"ok": True, "text": self._greeting(self.ctx),
                              "module": self.id, "version": self.version})
