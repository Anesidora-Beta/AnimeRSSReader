# -*- coding: utf-8 -*-
"""arr_search —— AnimeRSSReader 的深度搜索插件（可独立、可组合）。

放到程序的 ``plugins/`` 目录即生效，不需要改主程序任何文件：

    <程序目录>/plugins/arr_search/
        __init__.py          ← 你现在看的这个（对外只暴露 MODULE）
        module.py            ← 模块本体：路由 / 设置 / 事件 / 视图声明
        query.py             ← 组合查询：语法解析 + 下推 + 兜底过滤
        model.py             ← 统一条目结构、去重、排序（纯函数）
        net.py               ← 抓取原语（UA / 重试 / TTL 缓存）
        storage.py           ← 组合收藏与历史（mod_search_* 表）
        providers/           ← 每个源站一个文件，加源 = 加文件
        static/              ← view.js / view.css，前端视图
        tests/               ← 离线单测（不联网）

装好后：侧栏多一个「深度搜索」，``/api/search`` 可用，
设置页多一个「深度搜索」分组。

对外只暴露 ``MODULE``（延迟导入）
--------------------------------
``module.py`` 依赖宿主包 ``anime_rss``，而 ``query.py`` / ``model.py`` /
``providers/*`` 是**纯标准库**的。如果在 ``__init__`` 里直接 import 模块本体，
单测就再也无法脱离主程序跑（``import arr_search.query`` 会连带失败）。
所以这里改成 PEP 562 的模块级 ``__getattr__``：只有插件发现机制来要
``MODULE`` 时才真正加载 ``module.py``，纯逻辑层保持零依赖。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 主程序按「文件路径」加载插件（discovery._load_file），这种情况下包对象的
# __path__ 是空的，相对导入会失败。这里补一次，插件就能保持「一个包多个文件」
# 的写法，而不用退化成单文件插件。
_pkg = sys.modules.get(__name__)
if _pkg is not None and not getattr(_pkg, "__path__", None):
    _pkg.__path__ = [_HERE]

__all__ = ["MODULE", "SearchModule"]


def __getattr__(name):
    """按需加载模块本体（discovery 会依次 getattr ``MODULE`` / ``PLUGIN``）。"""
    if name in ("MODULE", "SearchModule"):
        from .module import SearchModule
        globals()["SearchModule"] = SearchModule
        globals()["MODULE"] = SearchModule
        return globals()[name]
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def __dir__():
    return sorted(list(globals()) + __all__)
