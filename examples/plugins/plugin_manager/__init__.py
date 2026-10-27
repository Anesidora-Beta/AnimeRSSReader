# -*- coding: utf-8 -*-
"""plugin_manager —— AnimeRSSReader 的插件管理器（可独立、可组合）。

放到程序的 ``plugins/`` 目录即生效，不改主程序任何文件：

    <程序目录>/plugins/plugin_manager/
        __init__.py          ← 你现在看的这个（对外只暴露 MODULE）
        module.py            ← 模块本体：路由 / 视图声明 / 设置分组
        registry.py          ← 磁盘探测结果与内存实况的对齐（状态机）
        control.py           ← 停用 / 启用 / 重载 / 卸载 / 装 zip 的真动作
        probe.py             ← 纯 AST 探测，不 import 插件也能读出它是什么
        static/              ← view.js / view.css，前端视图
        tests/               ← 离线单测 + 真起服务的集成测试
        preview/             ← 假数据离线预览（双击就能看界面）

装好后：侧栏多一个「插件管理」，``/api/pluginmgr`` 可用，
设置页多一个「插件管理」分组（只有一个开关：自动摘除软停用插件）。

对外只暴露 ``MODULE``（延迟导入）
--------------------------------
``module.py`` 依赖宿主包 ``anime_rss``，而 ``probe.py`` / ``registry.py`` 的
核心逻辑是**纯标准库**的。如果在 ``__init__`` 里直接 import 模块本体，
单测就再也没法脱离主程序跑（``import plugin_manager.probe`` 会连带失败）。
所以这里用 PEP 562 的模块级 ``__getattr__``：只有插件发现机制来要
``MODULE`` 时才真正加载 ``module.py``。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 主程序按「文件路径」加载插件（discovery._load_file），这种情况下包对象的
# __path__ 是空的，相对导入会失败。补一次，插件就能保持「一个包多个文件」。
_pkg = sys.modules.get(__name__)
if _pkg is not None and not getattr(_pkg, "__path__", None):
    _pkg.__path__ = [_HERE]

__all__ = ["MODULE", "PluginManagerModule"]


def __getattr__(name):
    if name in ("MODULE", "PluginManagerModule"):
        from .module import PluginManagerModule as _Base

        # 每次都用一个「绑定了自己目录」的子类，而不是直接返回基类。
        #
        # 原因是内核的插件发现机制：它每加载一次就把
        # ``sys.modules["anime_rss_plugin_plugin_manager"]`` 换成一个新模块对象，
        # 但 ``from .module import X`` 会**复用已存在的子模块**，于是
        # ``module.py`` 里的 ``__file__`` 还停在第一次加载的位置。
        # 一个进程里起两个应用（嵌入场景、集成测试）时，第二个实例就会去操作
        # 第一个实例的插件目录 —— 界面看着正常，动作全落在别处。
        # 子类带一份自己的 HOME，两个实例就各管各的了。
        bound = type("PluginManagerModule", (_Base,), {"HOME": _HERE})
        globals()["PluginManagerModule"] = bound
        globals()["MODULE"] = bound
        return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + __all__)
