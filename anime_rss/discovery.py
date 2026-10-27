"""插件发现：让第三方模块不用改我们的代码就能装进来。

两个来源：

* **entry_points**：``pip install`` 过的包，在 ``pyproject.toml`` 里声明
  ``[project.entry-points."anime_rss.plugins"]`` 即可，适合正式分发的插件；
* **本地 plugins/ 目录**：丢一个 ``.py`` 或包进去就生效，适合自己随手写的。

两条原则：

1. **内置优先**：插件不能顶掉内置模块的 id，冲突时跳过并提示；
2. **插件出错不拖垮启动**：加载失败的插件只记录原因，主程序照常跑
   —— 用户装了个坏插件不应该连界面都打不开。
"""
import importlib.util
import os
import sys
import traceback

from .module import Module

GROUP = "anime_rss.plugins"


def discover(dirs=(), use_entry_points=True, log=None):
    """返回 (模块列表, 问题列表)。问题只用于展示，不影响启动。"""
    say = log or (lambda msg: None)
    found, problems = [], []

    if use_entry_points:
        for mod, err in _from_entry_points():
            if err:
                problems.append(err)
                say("  插件跳过：%s" % err)
            else:
                found.append(mod)

    for directory in dirs:
        for mod, err in _from_directory(directory):
            if err:
                problems.append(err)
                say("  插件跳过：%s" % err)
            else:
                found.append(mod)

    return found, problems


# ---------------------------------------------------------------- 入口点

def _from_entry_points():
    try:
        from importlib.metadata import entry_points
    except ImportError:                      # Python < 3.8 不会走到，保险起见
        return []

    try:
        eps = entry_points()
        group = (eps.select(group=GROUP) if hasattr(eps, "select")
                 else eps.get(GROUP, []))
    except Exception as e:                   # 元数据坏了也不该影响启动
        yield None, "读取 entry_points 失败：%s: %s" % (type(e).__name__, e)
        return

    for ep in group:
        try:
            obj = ep.load()
        except Exception as e:
            yield None, "入口点 %s 导入失败：%s: %s" % (ep.name, type(e).__name__, e)
            continue
        mod = _as_module(obj)
        if mod is None:
            yield None, "入口点 %s 没有提供 Module 子类" % ep.name
        else:
            yield mod, None


# ---------------------------------------------------------------- 本地目录

def _from_directory(directory):
    if not directory or not os.path.isdir(directory):
        return
    for name in sorted(os.listdir(directory)):
        if name.startswith((".", "_")):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path) and name.endswith(".py"):
            yield _load_file(path, name[:-3])
        elif os.path.isdir(path) and os.path.isfile(os.path.join(path, "__init__.py")):
            yield _load_file(os.path.join(path, "__init__.py"), name)


def _load_file(path, name):
    mod_name = "anime_rss_plugin_" + name.replace("-", "_")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            return None, "%s 无法作为 Python 模块加载" % path
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module          # 让插件内部的相对导入能工作
        spec.loader.exec_module(module)
    except Exception as e:
        sys.modules.pop(mod_name, None)
        detail = "%s 加载失败：%s: %s" % (os.path.basename(path),
                                          type(e).__name__, e)
        if os.environ.get("ANIME_RSS_PLUGIN_DEBUG") == "1":
            detail += "\n" + traceback.format_exc()
        return None, detail

    plugin = _as_module(module)
    if plugin is None:
        return None, "%s 里没有 Module 子类" % os.path.basename(path)
    return plugin, None


def _as_module(obj):
    """从「类 / 实例 / 模块对象」里取出一个可用的 Module 实例。"""
    if isinstance(obj, Module):
        return obj
    if isinstance(obj, type) and issubclass(obj, Module) and obj is not Module:
        return obj()

    # 模块对象：优先用约定名 MODULE / PLUGIN，否则找唯一的 Module 子类
    if hasattr(obj, "__dict__"):
        for attr in ("MODULE", "PLUGIN", "module"):
            cand = getattr(obj, attr, None)
            if isinstance(cand, type) and issubclass(cand, Module) and cand is not Module:
                return cand()
            if isinstance(cand, Module):
                return cand
        own = [v for v in vars(obj).values()
               if isinstance(v, type) and issubclass(v, Module)
               and v is not Module and getattr(v, "id", "")]
        if len(own) == 1:
            return own[0]()
    return None


def merge_unique(builtin, extra, log=None):
    """内置优先：同 id 的插件丢掉，避免一个坏插件顶掉核心功能。"""
    say = log or (lambda msg: None)
    seen = {getattr(m, "id", "") for m in builtin}
    out = list(builtin)
    for mod in extra:
        mid = getattr(mod, "id", "")
        if not mid:
            say("  插件跳过：%s 没有 id" % type(mod).__name__)
            continue
        if mid in seen:
            say("  插件跳过：id「%s」与已安装模块重名" % mid)
            continue
        seen.add(mid)
        out.append(mod)
    return out
