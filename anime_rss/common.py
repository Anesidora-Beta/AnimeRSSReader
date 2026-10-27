"""模块之间共用的应用层小工具。

这些函数不依赖任何具体模块，只依赖 ctx 上的通用部件（store / settings /
refresher），放在这里是为了避免模块互相 import —— 契约里模块之间只能通过
ctx 和公共设施打交道，不允许 A 模块直接 import B 模块。
"""
import schedule as SCH
import settings as CFG
import sources as S


def source_names(ctx):
    """source id -> 显示名（内置站点 + 自定义订阅源标题）。"""
    names = {s["id"]: s["name"] for s in S.SOURCES}
    try:
        for f in ctx.store.feeds():
            names.setdefault(f["id"], f["title"])
    except Exception:
        pass
    return names


def apply_settings(ctx, values=None):
    """把设置同步到各个组件：各模块自己的配置 + 关窗行为。

    不点名任何模块，谁实现了 ``apply_settings(values)`` 就通知谁，
    所以新增模块不用回来改这个函数。
    """
    vals = values or (ctx.settings.get() if ctx.settings else {})
    if ctx.modules is not None:
        for mod in ctx.modules:
            hook = getattr(mod, "apply_settings", None)
            if callable(hook):
                try:
                    hook(vals)
                except Exception:
                    pass
    if ctx.ui is not None:
        ctx.ui.on_close = vals.get("closeAction") or "ask"
    return vals


def settings_payload(ctx):
    """设置页要的全部内容：当前值 + schema + 运行信息。"""
    vals = ctx.settings.get() if ctx.settings else CFG.normalize({})
    info = {
        "version": ctx.version,
        "port": ctx.port,
        "lan": ctx.lan_mode,
        "dataDir": ctx.data_dir,
        "dbPath": ctx.db_path,
        "uptime": ctx.uptime,
        "stats": ctx.store.stats() if ctx.store else {},
        "nextRefresh": ctx.auto.next_run_at() if ctx.auto else 0,
        "refreshing": bool(ctx.refresher.status()["running"]) if ctx.refresher else False,
        "platforms": [{"id": p["id"], "name": p["name"]} for p in SCH.PLATFORMS],
    }
    if ctx.lan_mode:
        info["lanUrl"] = ctx.lan_url()
    return {"ok": True, "values": vals,
            "schema": ctx.settings.schema() if ctx.settings else CFG.SCHEMA,
            "info": info}
