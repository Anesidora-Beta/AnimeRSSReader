# -*- coding: utf-8 -*-
"""linkwake —— 链接唤醒：把条目里的资源链接一键交给网盘 / 下载器。

放到程序的 ``plugins/`` 目录即生效，不改主程序任何文件：

    <程序目录>/plugins/linkwake/
        __init__.py      ← 你现在看的这个（对外只暴露 MODULE）
        module.py        ← 模块本体：路由 / 视图声明 / 设置分组
        rules.py         ← 纯规则：这是哪个网盘、条目里有哪些能下的
        wake.py          ← 真动手：交给系统关联程序或指定 exe（带护栏）
        clients.py       ← qBittorrent Web API / aria2 JSON-RPC
        static/          ← view.js / view.css
        tests/           ← 单测 + 假 qB 服务 + jsdom 前端断言
        preview/         ← 假数据离线预览（双击就能看界面）

装好后侧栏多一个「链接唤醒」，``/api/linkwake`` 可用。

为什么要延迟导入
----------------
``rules.py`` / ``wake.py`` / ``clients.py`` 只用标准库，单测可以直接
``import linkwake.rules``；只有 ``module.py`` 依赖宿主包 ``anime_rss``。
若在这里直接 import 模块本体，那些纯逻辑的测试就再也没法脱离主程序跑。
所以用 PEP 562 的模块级 ``__getattr__``：等插件发现机制来要 ``MODULE``
时再真正加载。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 主程序按「文件路径」加载插件（discovery._load_file），这种情况下包对象的
# __path__ 是空的，``from . import rules`` 会失败。补一次即可。
_pkg = sys.modules.get(__name__)
if _pkg is not None and not getattr(_pkg, "__path__", None):
    _pkg.__path__ = [_HERE]

__all__ = ["MODULE", "LinkWakeModule"]


def __getattr__(name):
    if name in ("MODULE", "LinkWakeModule"):
        from .module import LinkWakeModule as _Base

        # 每次给一个绑定自己目录的子类。原因与 plugin_manager 那边一样：
        # 内核每次加载都会换掉 sys.modules 里的外层模块，但 ``from .module
        # import X`` 会复用已存在的子模块，``module.py`` 的 ``__file__`` 就
        # 停在第一次的位置。一个进程里跑两个应用时，第二个会读到第一个的
        # 静态目录。带上自己的 HOME 就各管各的了。
        bound = type("LinkWakeModule", (_Base,), {"HOME": _HERE})
        globals()["LinkWakeModule"] = bound
        globals()["MODULE"] = bound
        return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + __all__)
