"""模块契约：一个功能就是一个 Module，只靠 ctx 拿能力。

约定（写进 docs/ARCHITECTURE.md，这里是对应的最小实现）：

* 模块只在自己的 ``setup(ctx)`` 里声明要什么：路由、配置项、表、定时任务；
* 模块之间不互相 import，要说话走 ``ctx`` 上的服务或后续的事件总线；
* 每个模块的路由挂在自己的 ``/api/<id>/`` 命名空间下；
* 数据库表以 ``mod_<id>_`` 开头（这一条从 P2 起执行）。

``api_version`` 不匹配时**拒绝加载并报错**，不做静默降级 —— 静默降级的
后果是运行到一半才发现某个接口不存在，比启动失败难查得多。
"""
from . import API_VERSION

__all__ = ["Module", "ModuleRegistry", "load_modules", "ModuleError"]


class ModuleError(RuntimeError):
    """模块加载失败（版本不匹配、依赖缺失、setup 抛错）。"""


class Module:
    """功能模块基类。子类至少要有 id / name，并实现 setup()。"""

    id = ""                  # 唯一标识，同时是路由前缀与表前缀
    name = ""                # 界面上显示的名字
    version = "0.0.0"
    api_version = API_VERSION
    depends = ()             # 依赖的其它模块 id
    builtin = True

    # -------------------------------------------------- 生命周期
    def setup(self, ctx):
        """注册路由 / 定义配置 / 建表 / 起任务。由内核在启动时调用一次。"""

    def teardown(self):
        """进程退出前的收尾（停线程、关连接）。默认什么都不做。"""

    # -------------------------------------------------- 便于测试
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
    ordered = _resolve_order([_instantiate(m) for m in modules])
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
            mod.setup(ctx)
        except Exception as e:                        # noqa: BLE001 - 要原样带上原因
            raise ModuleError("模块 %s 加载失败：%s: %s"
                              % (mid, type(e).__name__, e)) from e
        loaded.append(mod)
        say("  模块 %-10s v%-7s %s" % (mid, mod.version, mod.name))

    registry = ModuleRegistry(loaded)
    ctx.modules = registry
    return registry


def _instantiate(item):
    """清单项可以是类也可以是实例，统一成实例。"""
    if isinstance(item, type) and issubclass(item, Module):
        return item()
    return item


def _resolve_order(modules):
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
