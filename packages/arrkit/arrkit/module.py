"""模块契约：一个功能就是一个 Module，只靠 ctx 拿能力。

约定：

* 模块只在自己的 ``setup(ctx)`` 里声明要什么：路由、配置项、任务、视图；
* 模块之间**不互相 import**，要说话走 ``ctx.emit`` / ``ctx.service``；
* 每个模块的路由挂在自己的命名空间下（惯例 ``/api/<id>/``）；
* 模块自带的表用 ``<id>_`` 前缀（如果你自己管数据库的话）。

``api_version`` 不匹配时**拒绝加载并报错**，不静默降级 —— 静默降级的后果
是运行到一半才发现某个接口不存在，比启动失败难查得多。
"""
import os
import sys

from .version import API_VERSION

__all__ = ["Module", "ModuleRegistry", "ModuleError",
           "load_modules", "unload_modules", "API_VERSION"]


class ModuleError(RuntimeError):
    """模块加载失败（版本不匹配、依赖缺失、setup 抛错）。"""


class Module:
    """功能模块基类。子类至少要有 id / name，并实现 setup()。"""

    id = ""                  # 唯一标识，同时作为路由前缀与表前缀
    name = ""                # 界面上显示的名字
    version = "0.0.0"
    api_version = API_VERSION
    depends = ()             # 依赖的其它模块 id
    builtin = True
    ctx = None               # 由 load_modules 在 setup 之前注入

    # 可选：前端视图声明与静态目录（目录名相对模块文件所在目录）
    #   views  = ({"id": "clock", "title": "时钟", "script": "view.js"},)
    #   assets = "static"       → /plug/clock/view.js
    views = ()
    assets = ""

    # -------------------------------------------------- 生命周期
    def setup(self, ctx):
        """注册路由 / 定义配置 / 起任务。由内核在启动时调用一次。"""

    def bootstrap(self, ctx):
        """往首屏聚合数据里加一段（返回值会并进 ``/_kit/bootstrap``）。

        抛异常会被跳过，不影响别的模块。
        """

    def apply_settings(self, values):
        """配置保存后的回调，让新配置立刻生效。"""

    def teardown(self):
        """进程退出前的收尾（停线程、关连接）。默认什么都不做。"""

    def __repr__(self):
        return "<Module %s v%s>" % (self.id or type(self).__name__, self.version)


class ModuleRegistry:
    """已加载模块的只读索引。handler 通过它拿别的模块暴露的能力。"""

    def __init__(self, modules=()):
        self._map = {}
        for m in modules:
            self._map[m.id] = m

    def get(self, module_id, default=None):
        return self._map.get(module_id, default)

    def has(self, module_id):
        return module_id in self._map

    def all(self):
        return list(self._map.values())

    def ids(self):
        return list(self._map)

    def status(self):
        return [{"id": m.id, "name": m.name, "version": m.version}
                for m in self._map.values()]

    def __contains__(self, module_id):
        return module_id in self._map

    def __len__(self):
        return len(self._map)

    def __iter__(self):
        return iter(self._map.values())


def load_modules(ctx, modules, log=None):
    """按依赖顺序加载模块，返回 ModuleRegistry。

    清单里可以给类也可以给实例；给类时由这里实例化，避免多个应用共用状态。
    任何一步失败都抛 ModuleError —— 启动阶段就炸，比跑起来后某个接口 500 好查。
    """
    say = log or (lambda msg: None)
    ordered = resolve_order([instantiate(m) for m in modules])
    loaded = []

    for mod in ordered:
        mid = getattr(mod, "id", "") or type(mod).__name__
        if not getattr(mod, "id", ""):
            raise ModuleError("模块 %s 缺少 id" % type(mod).__name__)
        if mod.api_version != API_VERSION:
            raise ModuleError(
                "模块 %s 要求 api_version=%r，当前内核是 %r"
                % (mid, mod.api_version, API_VERSION))
        try:
            mod.ctx = ctx                     # handler 里直接用 self.ctx
            mod.setup(ctx)
        except Exception as e:                # noqa: BLE001 - 要原样带上原因
            raise ModuleError("模块 %s 加载失败：%s: %s"
                              % (mid, type(e).__name__, e)) from e
        register_extras(mod, mid, ctx, say)
        loaded.append(mod)
        say("  模块 %-12s v%-7s %s" % (mid, mod.version, mod.name))

    registry = ModuleRegistry(loaded)
    ctx.modules = registry
    return registry


def unload_modules(registry, log=None):
    """按加载的逆序 teardown，单个模块出错不影响其它模块。"""
    say = log or (lambda msg: None)
    if registry is None:
        return 0
    done = 0
    for mod in reversed(registry.all()):
        hook = getattr(mod, "teardown", None)
        if not callable(hook):
            continue
        try:
            hook()
            done += 1
        except Exception as e:                # noqa: BLE001 - 退出阶段不能被一个模块卡住
            say("  模块 %s 收尾失败：%s: %s" % (getattr(mod, "id", "?"),
                                              type(e).__name__, e))
    return done


def register_extras(mod, mid, ctx, say):
    """把模块声明的前端视图 / 静态目录交给内核登记。

    这一步是**尽力而为**：视图坏了只少一个入口，不该让整个模块起不来。
    真正的硬性契约（id、api_version、依赖）在 load_modules 里已经校验过。
    """
    reg = getattr(ctx, "views", None)
    if reg is None:
        return

    folder = getattr(mod, "assets", "") or ""
    if folder:
        base = module_dir(mod)
        if not base or not reg.mount(mid, os.path.join(base, folder)):
            say("  视图       %s 的静态目录不存在：%s" % (mid, folder))

    for spec in getattr(mod, "views", ()) or ():
        try:
            reg.register(mid, spec)
        except Exception as e:                # noqa: BLE001 - 视图是可选的
            say("  视图       %s 跳过：%s" % (mid, e))


def module_dir(mod):
    """模块文件所在目录（插件从任意路径加载时也适用）。"""
    module = sys.modules.get(type(mod).__module__)
    path = getattr(module, "__file__", "") or ""
    return os.path.dirname(os.path.abspath(path)) if path else ""


def instantiate(item):
    """清单项可以是类也可以是实例，统一成实例。"""
    if isinstance(item, type) and issubclass(item, Module):
        return item()
    return item


def resolve_order(modules):
    """拓扑排序：被依赖的排在前面。环依赖直接报错。"""
    by_id = {}
    for m in modules:
        mid = getattr(m, "id", "") or type(m).__name__
        if mid in by_id:
            raise ModuleError("模块 id 重复：%s" % mid)
        by_id[mid] = m

    ordered, state = [], {}

    def visit(mid, chain):
        mark = state.get(mid)
        if mark == "done":
            return
        if mark == "visiting":
            raise ModuleError("模块依赖成环：%s" % " → ".join(chain + [mid]))
        mod = by_id.get(mid)
        if mod is None:
            raise ModuleError("缺少依赖模块 %s（被 %s 需要）"
                              % (mid, chain[-1] if chain else "?"))
        state[mid] = "visiting"
        for dep in getattr(mod, "depends", ()) or ():
            visit(dep, chain + [mid])
        state[mid] = "done"
        ordered.append(mod)

    for m in modules:
        visit(getattr(m, "id", "") or type(m).__name__, [])
    return ordered
