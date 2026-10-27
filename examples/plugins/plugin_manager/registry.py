# -*- coding: utf-8 -*-
"""把「磁盘上有什么」和「进程里加载了什么」对齐成一份插件档案。

这两份信息经常不一致，而**不一致本身就是最有用的信息**：

============================  ==========================================
磁盘                          进程                       结论
============================  ==========================================
plugins/foo                   已加载                     normal（正常）
plugins/foo                   没加载                    刚放进去没重启 / 加载失败
plugins/foo                   没加载 + 探测报错           broken（代码有问题）
.disabled/foo                 没加载                    disabled（被停用）
.disabled/foo                 仍在跑                    停用只做了一半
.removed/foo@1700             没加载                    removed（可恢复）
（没有）                       已加载                    external（entry_points 装的）
============================  ==========================================

所以 ``state`` 不是「我们记了什么」，而是**两边对完之后的事实**。
用户最怕的是「界面说已停用，其实还在跑」，这里专门把这种半吊子状态
标成 ``partial`` 并写清原因。
"""
import os
import sys

from . import probe

__all__ = ["runtime", "merge", "Snapshot", "owns_pattern", "STATE_TEXT"]

STATE_TEXT = {
    "core": "内置",
    "normal": "运行中",
    "external": "外部安装",
    "stale": "待加载",
    "broken": "加载失败",
    "disabled": "已停用",
    "soft": "本次停用",
    "partial": "停用未完成",
    "removed": "已卸载",
    "unmanaged": "不是插件",
}

_ORDER = {"broken": 0, "partial": 1, "soft": 2, "stale": 3, "normal": 4,
          "disabled": 5, "removed": 6, "unmanaged": 7, "external": 8,
          "core": 9}


# ---------------------------------------------------------------- 运行态

def owns_pattern(module_id, segments):
    """这条路由是不是这个模块的。

    只认契约里写的前缀，够用且不误伤：
    ``/api/<id>/...``、``/api/v1/<id>/...``、``/<id>/...``。
    """
    if not module_id or not segments:
        return False
    if segments[0] == module_id:
        return True
    if segments[0] != "api":
        return False
    if len(segments) > 1 and segments[1] == module_id:
        return True
    if len(segments) > 2 and segments[1] == "v1" and segments[2] == module_id:
        return True
    return False


def runtime(ctx, plugins_root=""):
    """采集已加载模块的运行态：``{id: {...}}``。"""
    out = {}
    if ctx is None or getattr(ctx, "modules", None) is None:
        return out

    router = getattr(ctx, "router", None)
    routes = list(getattr(router, "routes", ()) or ())
    views = getattr(ctx, "views", None)
    schema = {}
    try:
        settings = getattr(ctx, "settings", None)
        for group in (settings.schema() if settings is not None else ()):
            schema[group.get("group")] = [it.get("k") for it in group.get("items") or ()]
    except Exception:                          # noqa: BLE001 - 设置表坏了不影响插件列表
        schema = {}

    for mod in ctx.modules:
        mid = getattr(mod, "id", "") or ""
        if not mid:
            continue
        owned = getattr(mod, "_pm_routes", None)     # 热加载时精确记录过
        if owned:
            mine = [r for r in routes if _route_key(r) in owned]
        else:
            mine = owned_routes(mod, mid, routes)

        out[mid] = {
            "id": mid,
            "name": getattr(mod, "name", "") or mid,
            "version": getattr(mod, "version", "") or "",
            "apiVersion": str(getattr(mod, "api_version", "") or ""),
            "builtin": is_builtin(mod, plugins_root),
            "depends": [str(x) for x in (getattr(mod, "depends", ()) or ())],
            "routes": sorted(_route_key(r) for r in mine),
            "views": ([v.id for v in views.all() if v.module == mid]
                      if views is not None else []),
            "assets": bool(views.static_dir(mid)) if views is not None else False,
            "settings": list(schema.get(mid) or ()),
            "file": _module_file(mod),
            "hasTeardown": callable(getattr(mod, "teardown", None)),
        }
    return out


def is_builtin(mod, plugins_root=""):
    """这个模块是不是主程序自带的。

    ``Module.builtin`` 的默认值是 ``True``，第三方插件常常不记得改 ——
    照它判断会把所有插件都标成内置。所以先看模块名（插件发现机制有固定
    前缀 ``anime_rss_plugin_``，内置则是 ``anime_rss.modules.*``），
    最后才回落到文件位置与 ``builtin`` 属性。
    """
    name = ""
    try:
        name = type(mod).__module__ or ""
    except Exception:                          # noqa: BLE001
        name = ""
    if name.startswith("anime_rss_plugin_"):
        return False
    if name.startswith("anime_rss.modules"):
        return True
    path = _module_file(mod)
    if path and plugins_root:
        try:
            root = os.path.realpath(plugins_root)
            if os.path.realpath(path).startswith(root + os.sep):
                return False
        except OSError:
            pass
    return bool(getattr(mod, "builtin", True))


def owned_routes(mod, mid, routes):
    """挑出属于这个模块的路由。

    三层判据，从严到宽，宁可少认也不误伤别人：

    1. handler 是绑定在本模块实例上的方法（``fn.__self__ is mod``）—— 最准；
    2. handler 定义在同一插件包里（``anime_rss_plugin_x.*``）—— 插件内部
       再分包时也能认出来；
    3. 路径前缀符合 ``/api/<id>/`` 契约 —— 兜底。

    第 2 条只对插件生效：内置模块都挤在 ``anime_rss`` 这一个包里，
    用包名判断会把所有内置路由算到第一个模块头上。
    """
    modname = ""
    try:
        modname = type(mod).__module__ or ""
    except Exception:                          # noqa: BLE001
        modname = ""
    pkg = modname.split(".")[0]
    use_pkg = pkg.startswith("anime_rss_plugin_")

    out = []
    for route in routes:
        fn = route[2] if len(route) > 2 else None
        owner = getattr(fn, "__self__", None)
        if owner is not None and (owner is mod or getattr(owner, "id", None) == mid):
            out.append(route)
            continue
        if use_pkg:
            fn_mod = getattr(fn, "__module__", "") or ""
            if fn_mod and (fn_mod == pkg or fn_mod.startswith(pkg + ".")):
                out.append(route)
                continue
        if owns_pattern(mid, route[1]):
            out.append(route)
    return out


def _route_key(route):
    return "%s /%s" % (route[0], "/".join(route[1]))


def _module_file(mod):
    try:
        module = sys.modules.get(type(mod).__module__)
        path = getattr(module, "__file__", "") or ""
        return os.path.abspath(path) if path else ""
    except Exception:                          # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 合并

def merge(runtime_info, buckets, soft_disabled=(), api_version="1",
          plugins_root="", self_id=""):
    """把运行态与三个磁盘桶合成插件档案列表（已排序）。"""
    soft = {str(x) for x in (soft_disabled or ())}
    known = set(runtime_info or ())
    for items in (buckets or {}).values():
        for p in items:
            known.add(p.id or p.dirname)
            known.add(p.dirname)

    by_id = {}
    for mid, info in (runtime_info or {}).items():
        by_id[mid] = dict(info, disk=None, dirname="", bucket="")

    # 磁盘条目先按 id 对齐；探测不出 id 的按目录名兜底
    loose = []
    for bucket in probe.BUCKETS:
        for p in (buckets or {}).get(bucket, ()):
            key = p.id or p.dirname
            hit = by_id.get(key)
            if hit is None and p.id:
                hit = by_id.get(p.dirname)
            if hit is not None and not hit.get("disk"):
                hit["disk"] = p
                hit["bucket"] = bucket
                hit["dirname"] = p.dirname
                if not hit.get("id"):
                    hit["id"] = key
                continue
            loose.append((bucket, p))

    entries = []
    for mid, info in by_id.items():
        entries.append(_entry(info, plugins_root, soft, api_version, known, self_id))
    for bucket, p in loose:
        entries.append(_entry({"id": p.id or p.dirname, "name": p.name,
                               "version": p.version, "apiVersion": p.api_version,
                               "builtin": False, "depends": list(p.depends),
                               "routes": [], "views": [], "assets": False,
                               "settings": [], "file": "", "hasTeardown": False,
                               "disk": p, "bucket": bucket, "dirname": p.dirname},
                              plugins_root, soft, api_version, known, self_id))

    entries.sort(key=lambda e: (_ORDER.get(e["state"], 50),
                                e["name"].lower(), e["id"]))
    return entries


def _entry(info, plugins_root, soft, api_version, known=(), self_id=""):
    disk = info.get("disk")
    loaded = bool(info.get("file"))
    builtin = bool(info.get("builtin"))
    bucket = info.get("bucket") or ""
    warnings = []

    # ---- 状态判定：先看两边是否自相矛盾，再看各自的正常态
    if builtin:
        state = "core"
    elif bucket == probe.BUCKET_ACTIVE and loaded:
        state = "normal"
    elif bucket == probe.BUCKET_ACTIVE and not loaded:
        state = "broken" if (disk is not None and disk.error) else "stale"
    elif bucket == probe.BUCKET_DISABLED:
        state = "partial" if loaded else "disabled"
    elif bucket == probe.BUCKET_REMOVED:
        state = "partial" if loaded else "removed"
    elif loaded:
        state = "external"
    else:
        state = "unmanaged"

    enabled = state in ("core", "normal", "external")

    # ---- 体检建议
    err = disk.error if disk is not None else ""
    if err:
        warnings.append(err)
    if disk is not None and disk.api_version and api_version \
            and disk.api_version != api_version and not builtin:
        warnings.append("契约版本不一致：插件要 v%s，内核是 v%s"
                        % (disk.api_version, api_version))
    if disk is not None and not disk.api_version and not builtin and loaded is False:
        warnings.append("没声明 api_version，按契约加载会被拒绝")
    if state == "partial":
        warnings.append("目录已在 %s，但模块还在进程里跑着；重启后彻底消失"
                        % bucket)
    if state == "stale":
        warnings.append("目录看起来没问题，但本次启动没加载它；重启程序试试")
    if state == "broken" and not err:
        warnings.append("目录正常但没加载成功，可能 id 与内置模块重名"
                        "（内置优先，插件会被跳过）")
    if state == "removed":
        warnings.append("还在回收站里，可以恢复")

    # ---- 依赖检查
    missing = [d for d in (info.get("depends") or ()) if d not in known]
    if missing:
        warnings.append("缺少依赖模块：%s" % "、".join(missing))

    if disk is not None and disk.dirname and info.get("id") \
            and disk.dirname != info["id"] and disk.id != info["id"]:
        warnings.append("目录名 %s 与模块 id %s 不一致（不影响加载，但容易找错）"
                        % (disk.dirname, info["id"]))
    if disk is not None and disk.size > 5 * 1024 * 1024:
        warnings.append("体积偏大（%s），插件一般不超过 5 MB" % _size(disk.size))

    # 目录移不动时只能「本次运行停用」：状态要跟「目录已移走」分开，
    # 否则重启后它又跑起来，用户会觉得管理器在骗人。
    soft_off = (info.get("id") in soft) and state in (
        "normal", "external", "stale", "partial", "broken")
    if soft_off:
        state = "soft"
        if not any("不可写" in w for w in warnings):
            warnings.append("插件目录不可写，停用只作用到本次运行"
                            "（启动时会自动再摘一次）")

    entry = {
        "id": info.get("id") or (disk.dirname if disk is not None else ""),
        "name": info.get("name") or (disk.name if disk is not None else ""),
        "version": info.get("version") or (disk.version if disk is not None else ""),
        "apiVersion": info.get("apiVersion") or
                      (disk.api_version if disk is not None else ""),
        "builtin": builtin,
        "loaded": loaded,
        "enabled": state in ("core", "normal", "external"),
        "state": state,
        "stateText": STATE_TEXT.get(state, state),
        "dirname": info.get("dirname") or (disk.dirname if disk is not None else ""),
        "bucket": bucket,
        "path": disk.path if disk is not None else "",
        "relPath": _rel(disk.path, plugins_root) if disk is not None else "",
        "kind": disk.kind if disk is not None else ("builtin" if builtin else ""),
        "size": disk.size if disk is not None else 0,
        "files": disk.files if disk is not None else 0,
        "mtime": disk.mtime if disk is not None else 0,
        "sizeText": _size(disk.size) if disk is not None else "",
        "summary": disk.readme if disk is not None else "",
        "moduleClass": disk.module_class if disk is not None else "",
        "depends": list(info.get("depends") or
                        (disk.depends if disk is not None else ())),
        "routes": list(info.get("routes") or ()),
        "views": list(info.get("views") or ()),
        "assets": bool(info.get("assets")),
        "settings": list(info.get("settings") or ()),
        "file": info.get("file") or (disk.class_file if disk is not None else ""),
        "isSelf": bool(self_id) and info.get("id") == self_id,
        "canDisable": (not builtin) and state in ("normal", "external", "stale",
                                                  "broken", "partial"),
        "canEnable": state in ("disabled", "removed", "partial", "broken",
                               "stale", "soft"),
        "canReload": (not builtin) and (loaded or state in ("disabled", "removed")),
        "canRemove": (not builtin) and bucket == probe.BUCKET_ACTIVE,
        "canRestore": state == "removed",
        "warnings": warnings,
        "soft": bool(soft_off),
    }
    return entry


def _rel(path, root):
    if not path or not root:
        return ""
    try:
        return os.path.relpath(path, root).replace("\\", "/")
    except ValueError:                         # Windows 跨盘
        return path


def _size(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return ""


# ---------------------------------------------------------------- 快照

class Snapshot:
    """一次「磁盘 + 内存」的完整快照，供 HTTP 层直接用。"""

    def __init__(self, ctx, plugins_dir, soft_disabled=(), self_id=""):
        self.ctx = ctx
        self.root = plugins_dir
        self.self_id = self_id
        self.soft = list(soft_disabled or ())
        self.buckets = probe.probe_root(plugins_dir)
        self.rt = runtime(ctx, plugins_dir)
        self.entries = merge(self.rt, self.buckets, self.soft,
                             api_version=_api_version(),
                             plugins_root=plugins_dir, self_id=self_id)

    # -------------------------------------------------- 查询
    def get(self, ident):
        for e in self.entries:
            if e["id"] == ident or e["dirname"] == ident:
                return e
        return None

    def by_bucket(self, bucket):
        return [e for e in self.entries if e["bucket"] == bucket]

    def counts(self):
        out = {"total": 0, "on": 0, "off": 0, "bad": 0, "core": 0, "trash": 0}
        for e in self.entries:
            if e["builtin"]:
                out["core"] += 1
                continue
            out["total"] += 1
            if e["state"] in ("normal", "external"):
                out["on"] += 1
            elif e["state"] in ("disabled", "removed"):
                out["off"] += 1
                if e["state"] == "removed":
                    out["trash"] += 1
            else:
                out["bad"] += 1
        return out

    def issues(self):
        """体检：把所有需要人管的事挑出来。"""
        out = []
        for e in self.entries:
            if e["builtin"]:
                continue
            for w in e["warnings"]:
                out.append({"id": e["id"], "name": e["name"],
                            "state": e["state"], "level": _level(e, w),
                            "text": w})
        return out

    def as_dict(self):
        return {
            "root": self.root,
            "entries": self.entries,
            "counts": self.counts(),
            "issues": self.issues(),
            "trash": [{"id": e["id"], "name": e["name"], "dirname": e["dirname"],
                       "token": e["dirname"], "sizeText": e["sizeText"],
                       "mtime": e["mtime"], "state": e["state"]}
                      for e in self.entries
                      if e["bucket"] == probe.BUCKET_REMOVED
                      or e["bucket"] == probe.BUCKET_DISABLED],
        }


def _level(entry, text):
    if entry["state"] in ("broken", "partial"):
        return "error"
    if "契约版本" in text or "缺少依赖" in text:
        return "error"
    return "warn"


def _api_version():
    try:
        from anime_rss import API_VERSION
        return str(API_VERSION)
    except Exception:                          # noqa: BLE001 - 脱离主程序时降级
        return "1"
