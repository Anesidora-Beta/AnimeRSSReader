# -*- coding: utf-8 -*-
"""控制层：让「停用 / 启用 / 重载 / 卸载」真的发生，而不只是改个标志位。

一个插件在运行时有**四个落点**，少摘一个就会出现「界面说停了，其实还在跑」：

1. ``ctx.router`` 里的路由        —— 不摘，接口照旧能打；
2. ``ctx.views`` 里的视图与静态目录 —— 不摘，侧栏入口还在；
3. ``ctx.jobs`` 里的定时任务      —— 不摘，后台照样抓数据；
4. ``ctx.modules`` 里的注册表     —— 不摘，别的模块还能拿到它。

停用还要管**磁盘**：把目录移进 ``plugins/.disabled/``。这不是花招 ——
``discovery._from_directory`` 会跳过 ``.`` 开头的条目，所以移进去之后
下次启动连 import 都不会发生，是真正的「没装」。目录不可写时（Docker 只读挂载）
退化成「仅本次运行停用」，并且**启动时自动再摘一次**，功能上一样，只是会多一次 import。

顺序永远是「先摘运行态，再动目录」：先动目录的话，一旦摘运行态失败，
用户会以为卸载了其实还在跑，那种状态最难查。
"""
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile

from anime_rss.module import Module, ModuleRegistry

from . import probe, registry

__all__ = ["Controller", "ControlError", "STATE_FILE", "MAX_ZIP"]

MAX_ZIP = 24 * 1024 * 1024          # 单个插件包上限 24 MB，正常插件远小于此
MAX_MEMBERS = 3000                  # 包内文件数上限，防 zip 炸弹
_SAFE_NAME = re.compile(r"[^0-9A-Za-z_-]+")
STATE_FILE = "plugin_manager.json"


def _rand(n):
    """随机十六进制串（n 个字符）。

    不要换回 ``uuid.uuid4().hex``：exe 版是 PyInstaller 打的包，只带上主程序
    真正 import 过的标准库，而 ``anime_rss`` 没用过 ``uuid``，所以 exe 里
    ``import uuid`` 会 ModuleNotFoundError —— 更糟的是插件发现是整趟 try 的，
    一个插件抛错会让**所有**插件都装不上（连好好的 arr_search 一起消失）。
    ``os.urandom`` 在任何环境都有。
    """
    return os.urandom((n + 1) // 2).hex()[:n]


class ControlError(RuntimeError):
    """操作被拒绝或失败（带能直接给用户看的原因）。"""


class Controller:
    """一台应用上的插件控制台。只由插件管理器模块自己实例化。"""

    def __init__(self, module):
        self.mod = module
        self.ctx = module.ctx

    # ------------------------------------------------------------ 基础信息
    @property
    def root(self):
        return self.mod.plugins_dir()

    def snapshot(self):
        return registry.Snapshot(self.ctx, self.root, self.state()["softDisabled"],
                                 self_id=self.mod.id)

    def find(self, ident):
        e = self.snapshot().get(ident)
        if e is None:
            raise ControlError("没有这个插件：%s" % ident)
        return e

    def say(self, msg):
        self.mod.say(msg)

    # ------------------------------------------------------------ 状态文件
    def state(self):
        raw = self._read_json()
        raw.setdefault("softDisabled", [])
        raw.setdefault("journal", [])
        raw.setdefault("notes", {})
        return raw

    def _read_json(self):
        path = self.mod.state_path()
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_json(self, data):
        path = self.mod.state_path()
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    def mark_soft(self, mid, on):
        """记下「这个插件只停用到本次运行」。目录不可写时的降级路径。"""
        data = self.state()
        soft = set(data.get("softDisabled") or ())
        if on:
            soft.add(mid)
        else:
            soft.discard(mid)
        data["softDisabled"] = sorted(soft)
        self._write_json(data)

    def journal(self, action, target, ok, message):
        data = self.state()
        rows = list(data.get("journal") or ())
        rows.insert(0, {"ts": int(time.time()), "action": action,
                        "target": target, "ok": bool(ok), "message": message})
        data["journal"] = rows[:120]
        try:
            self._write_json(data)
        except OSError:
            pass

    def note(self, mid, text):
        data = self.state()
        notes = dict(data.get("notes") or {})
        text = (text or "").strip()[:200]
        if text:
            notes[mid] = text
        else:
            notes.pop(mid, None)
        data["notes"] = notes
        self._write_json(data)

    # ------------------------------------------------------------ 停用
    def disable(self, ident, move=True):
        e = self.find(ident)
        self._guard_self(e)
        if e["builtin"]:
            raise ControlError("「%s」是内置模块，停用会让主程序缺功能，不允许" % e["name"])

        steps = []
        if e["loaded"]:
            self.unload(e["id"])
            steps.append("已摘掉路由 / 视图 / 定时任务")
            self.say("停用 %s：运行态已摘除" % e["id"])

        if e["bucket"] == probe.BUCKET_DISABLED:
            steps.append("目录本来就在 .disabled/ 里")
        elif move and e["path"] and e["bucket"] == probe.BUCKET_ACTIVE:
            dest = os.path.join(self.root, probe.BUCKET_DISABLED, e["dirname"])
            try:
                self._move(e["path"], dest)
                steps.append("目录已移入 plugins/.disabled/")
            except OSError as exc:
                self.mark_soft(e["id"], True)
                steps.append("目录移不动（%s），改为「仅本次运行停用」，重启后会自动再摘一次"
                             % exc)
        elif not move:
            self.mark_soft(e["id"], True)
            steps.append("只做了运行时停用（下次启动会重新加载）")

        self.journal("disable", e["id"], True, "；".join(steps))
        return {"ok": True, "id": e["id"], "steps": steps,
                "soft": e["id"] in self.state()["softDisabled"]}

    # ------------------------------------------------------------ 启用
    def enable(self, ident):
        e = self.find(ident)
        self._guard_self(e)

        steps = []
        target = e["path"]
        if e["bucket"] in (probe.BUCKET_DISABLED, probe.BUCKET_REMOVED):
            if not e["dirname"]:
                raise ControlError("找不到目录名，无法移回")
            dest = os.path.join(self.root, e["dirname"].split("@")[0])
            if os.path.exists(dest):
                raise ControlError("plugins/%s 已经存在，先处理同名目录再启用"
                                   % os.path.basename(dest))
            self._move(e["path"], dest)
            target = dest
            steps.append("目录已移回 plugins/")
        elif e["bucket"] == probe.BUCKET_ACTIVE and not e["loaded"]:
            target = e["path"]
            steps.append("目录就在这里，直接加载")
        elif not target:
            raise ControlError("「%s」不是从 plugins/ 目录装的（可能是 pip 入口点），"
                               "只能重启程序生效" % e["name"])

        self.mark_soft(e["id"], False)
        if e["loaded"]:
            self.unload(e["id"])
            steps.append("先摘掉旧实例")
        self.load(target, e["dirname"] or os.path.basename(target.rstrip(os.sep)))
        steps.append("已加载")
        self.journal("enable", e["id"], True, "；".join(steps))
        return {"ok": True, "id": e["id"], "steps": steps}

    # ------------------------------------------------------------ 重载
    def reload(self, ident):
        e = self.find(ident)
        self._guard_self(e)
        if e["builtin"]:
            raise ControlError("内置模块的重载请重启程序")
        if e["bucket"] in (probe.BUCKET_DISABLED, probe.BUCKET_REMOVED):
            return self.enable(ident)              # 重载一个停用的 = 启用它
        if not e["path"]:
            raise ControlError("「%s」没有可加载的目录，只能重启程序" % e["name"])

        index = self._index_of(e["id"])
        if e["loaded"]:
            self.unload(e["id"])
        self.load(e["path"], e["dirname"], insert_at=index)
        self.journal("reload", e["id"], True, "已重新加载")
        return {"ok": True, "id": e["id"], "steps": ["已重新加载"]}

    # ------------------------------------------------------------ 卸载 / 恢复
    def remove(self, ident):
        e = self.find(ident)
        self._guard_self(e)
        if e["builtin"]:
            raise ControlError("内置模块不能卸载")
        if e["bucket"] != probe.BUCKET_ACTIVE:
            raise ControlError("只有 plugins/ 根目录下的插件能卸载")

        steps = []
        if e["loaded"]:
            self.unload(e["id"])
            steps.append("已从运行态摘除")
        token = "%s@%s" % (e["dirname"], time.strftime("%Y%m%d-%H%M%S"))
        dest = os.path.join(self.root, probe.BUCKET_REMOVED, token)
        self._move(e["path"], dest)
        steps.append("已移入 plugins/.removed/%s（可恢复）" % token)
        self.journal("remove", e["id"], True, "；".join(steps))
        return {"ok": True, "id": e["id"], "token": token, "steps": steps}

    def restore(self, token):
        token = _safe_token(token)
        src = os.path.join(self.root, probe.BUCKET_REMOVED, token)
        if not os.path.isdir(src):
            raise ControlError("回收站里没有 %s" % token)
        dirname = token.rsplit("@", 1)[0]
        dest = os.path.join(self.root, dirname)
        if os.path.exists(dest):
            raise ControlError("plugins/%s 已经存在，先改个名或删掉它" % dirname)
        self._move(src, dest)
        self.load(dest, dirname)
        self.journal("restore", dirname, True, "已恢复并加载")
        return {"ok": True, "id": dirname, "steps": ["已恢复并加载"]}

    def purge(self, token):
        token = _safe_token(token)
        folder = os.path.realpath(os.path.join(self.root, probe.BUCKET_REMOVED))
        src = os.path.realpath(os.path.join(folder, token))
        if os.path.dirname(src) != folder or os.path.basename(src) != token:
            raise ControlError("路径不合法：%s" % token)
        if not os.path.isdir(src):
            raise ControlError("回收站里没有 %s" % token)
        shutil.rmtree(src)
        self.journal("purge", token, True, "已从回收站彻底删除")
        return {"ok": True, "token": token}

    # ------------------------------------------------------------ 安装 / 导出
    def install(self, data, filename="", overwrite=False):
        """从 zip 字节装一个插件：先解到 .staging-*，校验通过再原子落位。"""
        if not data:
            raise ControlError("上传内容为空")
        if len(data) > MAX_ZIP:
            raise ControlError("压缩包太大（%.1f MB，上限 %d MB）"
                               % (len(data) / 1048576.0, MAX_ZIP // 1048576))
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ControlError("这不是一个有效的 zip 文件")

        members = zf.infolist()
        if len(members) > MAX_MEMBERS:
            raise ControlError("压缩包里文件太多（%d 个，上限 %d）"
                               % (len(members), MAX_MEMBERS))

        plan = []                     # (zip 内路径, 落盘相对路径)
        tops = set()
        for info in members:
            raw = info.filename.replace("\\", "/")
            parts = [p for p in raw.split("/") if p]
            if not parts or info.is_dir():
                continue
            if raw.startswith("/") or ".." in parts:
                raise ControlError("压缩包里有越界路径：%s" % raw)
            if ":" in parts[0]:                      # Windows 盘符 / NTFS 流
                raise ControlError("压缩包里有非法路径：%s" % raw)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ControlError("压缩包里含符号链接，出于安全拒绝安装：%s" % raw)
            if any(p in ("__pycache__", ".git", ".svn") for p in parts):
                continue
            if any(p.startswith(".") for p in parts):
                continue
            tops.add(parts[0])
            plan.append((info, parts))

        if not plan:
            raise ControlError("压缩包里没有可安装的文件")

        # 只有一个顶层目录 → 剥掉它（GitHub 打包的 zip 都是这样）
        strip = len(tops) == 1 and all(len(parts) >= 2 for _, parts in plan)
        top_dir = next(iter(tops)) if strip else ""
        if strip:
            dirname = top_dir
        else:
            dirname = os.path.splitext(os.path.basename(filename or ""))[0] or "plugin"
        dirname = _safe_dirname(dirname)

        dest = os.path.join(self.root, dirname)
        # 覆盖掉正在运行的自己是最容易把自己弄没的操作：旧目录会被挪去回收站、
        # 新代码又加载不了（id 冲突）。落盘之前就拦住。
        own = os.path.realpath(getattr(self.mod, "HOME", "") or "")
        if own and os.path.realpath(dest) == own:
            raise ControlError("不能覆盖安装插件管理器自己。要升级它，"
                               "手动替换目录后重启程序")
        if os.path.exists(dest) and not overwrite:
            raise ControlError("plugins/%s 已存在；勾选「覆盖已有」再装一次" % dirname)

        staging = os.path.join(self.root, probe.STAGING_PREFIX + _rand(10))
        os.makedirs(staging, exist_ok=True)
        try:
            for info, parts in plan:
                rel = parts[1:] if strip else parts
                if not rel:
                    continue
                full = os.path.join(staging, *rel)
                real = os.path.realpath(full)
                if not (real == os.path.realpath(staging)
                        or real.startswith(os.path.realpath(staging) + os.sep)):
                    raise ControlError("压缩包里有越界路径：%s" % info.filename)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with zf.open(info) as src, open(full, "wb") as out:
                    shutil.copyfileobj(src, out)

            found = _validate_layout(staging)
            if not found:
                raise ControlError("包里没有可用的插件入口（要有 __init__.py，"
                                   "或根目录下一个 .py 文件）")

            # 落盘前先静态看一眼 id：跟自己重名的包直接拒绝，别等装完才发现加载不了
            peek = probe.probe_path(staging, "staging")
            if peek.id and peek.id == self.mod.id:
                raise ControlError("这个包的模块 id 是「%s」，与插件管理器自己冲突"
                                   % peek.id)

            backup = ""
            if os.path.exists(dest):
                backup = os.path.join(self.root, probe.BUCKET_REMOVED,
                                      "%s@%s" % (dirname,
                                                 time.strftime("%Y%m%d-%H%M%S")))
                self._move(dest, backup)

            self._move(staging, dest)
            staging = ""

            ident = ""
            steps = ["已解包到 plugins/%s" % dirname]
            if backup:
                steps.append("旧版本已备份到 .removed/")
            try:
                mod_id = self.load(dest, dirname)
                ident = mod_id or dirname
                steps.append("已加载 %s" % ident)
                self.mark_soft(ident, False)
            except ControlError as exc:
                steps.append("装好了但没加载成功：%s" % exc)
            self.journal("install", ident or dirname, True, "；".join(steps))
            return {"ok": True, "id": ident or dirname, "dirname": dirname,
                    "steps": steps, "backup": backup,
                    "entry": found.get("entry", "")}
        finally:
            if staging and os.path.isdir(staging):
                shutil.rmtree(staging, ignore_errors=True)

    def export(self, ident):
        """把一个插件打包回 zip（备份 / 迁移到另一台机器）。"""
        e = self.find(ident)
        if not e["path"]:
            raise ControlError("「%s」没有磁盘文件可导出" % e["name"])
        base = e["path"] if os.path.isdir(e["path"]) else os.path.dirname(e["path"])
        top = os.path.basename(base.rstrip(os.sep))
        buf = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames
                               if d != "__pycache__" and not d.startswith(".")]
                for name in filenames:
                    if name.endswith((".pyc", ".pyo")) or name.startswith("."):
                        continue
                    full = os.path.join(dirpath, name)
                    rel = os.path.relpath(full, os.path.dirname(base))
                    zf.write(full, rel.replace("\\", "/"))
                    count += 1
                    if count >= MAX_MEMBERS:
                        break
        return buf.getvalue(), "%s.zip" % top

    # ------------------------------------------------------------ 加载 / 摘除
    def load(self, path, dirname="", insert_at=None):
        """把一个插件目录加载进当前进程，返回它的模块 id。

        这是「重载」和「启用」共用的那一步，也是唯一会执行插件代码的地方。
        失败一律抛 ``ControlError``，并且保证模块不会半挂着留在注册表里。
        """
        entry = _entry_file(path)
        if not entry:
            raise ControlError("找不到入口文件：%s" % path)
        dirname = dirname or os.path.basename(path.rstrip(os.sep))
        mod_name = "anime_rss_plugin_" + _SAFE_NAME.sub("_", dirname)

        # 让子模块也重来一遍：不清理的话 from .module import X 拿到的还是旧类
        for key in [k for k in list(sys.modules)
                    if k == mod_name or k.startswith(mod_name + ".")]:
            sys.modules.pop(key, None)

        try:
            spec = importlib.util.spec_from_file_location(mod_name, entry)
            if spec is None or spec.loader is None:
                raise ControlError("%s 不是可加载的 Python 模块" % entry)
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
        except ControlError:
            sys.modules.pop(mod_name, None)
            raise
        except Exception as exc:                     # noqa: BLE001 - 插件代码什么都能抛
            sys.modules.pop(mod_name, None)
            raise ControlError("导入失败：%s: %s" % (type(exc).__name__, exc))

        inst = _as_module(module)
        if inst is None:
            raise ControlError("这个包里没有 Module 子类")

        mid = getattr(inst, "id", "") or ""
        if not mid:
            raise ControlError("模块没有声明 id")
        if mid == self.mod.id:
            raise ControlError("插件 id 与插件管理器自己冲突")

        api_v = str(getattr(inst, "api_version", "") or "")
        host_v = _host_api_version()
        if api_v != host_v:
            raise ControlError("契约版本不匹配：插件要 v%s，内核是 v%s"
                               % (api_v or "?", host_v))

        before_routes = {_route_key(r) for r in self.ctx.router.routes}
        before_jobs = _job_names(self.ctx)
        try:
            inst.ctx = self.ctx
            inst.setup(self.ctx)
        except Exception as exc:                     # noqa: BLE001
            _teardown_quiet(inst)
            self._rollback(before_routes, before_jobs, mid)
            raise ControlError("setup() 失败：%s: %s" % (type(exc).__name__, exc))

        # 记录「这个模块注册了哪些路由 / 任务」，以后摘除才摘得干净
        fresh = {_route_key(r) for r in self.ctx.router.routes} - before_routes
        fresh |= self._alias_v1(fresh)
        inst._pm_routes = fresh
        inst._pm_jobs = _job_names(self.ctx) - before_jobs
        self._mount_extra(inst, mid)

        mods = list(self.ctx.modules or ())
        if not any(getattr(m, "id", "") == mid for m in mods):
            if insert_at is not None and 0 <= insert_at <= len(mods):
                mods.insert(insert_at, inst)
            else:
                mods.append(inst)
        self.ctx.modules = ModuleRegistry(mods)
        self.say("加载插件 %s v%s（%s）" % (mid, getattr(inst, "version", "?"), dirname))
        try:
            self.ctx.emit("plugin.loaded", {"id": mid, "dir": dirname})
        except Exception:                            # noqa: BLE001 - 事件没人听不是错
            pass
        return mid

    def unload(self, mid):
        """把一个模块从运行态四件套里全部摘掉。返回是否真的摘了。"""
        mod = None
        for m in (self.ctx.modules or ()):
            if getattr(m, "id", "") == mid:
                mod = m
                break
        if mod is None:
            return False

        _teardown_quiet(mod)
        self._cancel_jobs(mid, mod)
        self._drop_routes(mid, mod)
        self._drop_views(mid)
        self.ctx.modules = ModuleRegistry(
            [m for m in (self.ctx.modules or ()) if getattr(m, "id", "") != mid])
        try:
            self.ctx.emit("plugin.unloaded", {"id": mid})
        except Exception:                            # noqa: BLE001
            pass
        return True

    def apply_soft(self):
        """启动时把「只停用到本次运行」的插件真的摘掉（目录只读的降级路径）。"""
        soft = self.state()["softDisabled"]
        done = []
        for mid in soft:
            try:
                if self.unload(mid):
                    done.append(mid)
            except Exception as exc:                 # noqa: BLE001
                self.say("自动停用 %s 失败：%s" % (mid, exc))
        return done

    # ------------------------------------------------------------ 内部：摘除
    def _cancel_jobs(self, mid, mod):
        jobs = getattr(self.ctx, "jobs", None)
        if jobs is None:
            return
        names = set(getattr(mod, "_pm_jobs", ()) or ())
        try:
            names |= {j["name"] for j in (jobs.status() or {}).get("jobs") or ()}
        except Exception:                            # noqa: BLE001
            pass
        for name in names:
            if not name:
                continue
            if name in (getattr(mod, "_pm_jobs", ()) or ()) \
                    or name == mid or name.startswith(mid + ".") \
                    or name.startswith(mid + ":"):
                try:
                    jobs.cancel(name)
                except Exception:                    # noqa: BLE001 - 取消失败不该中断卸载
                    pass

    def _drop_routes(self, mid, mod):
        router = getattr(self.ctx, "router", None)
        if router is None:
            return
        owned = getattr(mod, "_pm_routes", None)
        if owned:
            keys = set(owned)                    # 热加载时记录下来的精确集合
        else:
            keys = {_route_key(r) for r in registry.owned_routes(mod, mid,
                                                                 router.routes)}
        router.routes[:] = [r for r in router.routes if _route_key(r) not in keys]

    def _rollback(self, before_routes, before_jobs, mid):
        """setup() 中途抛错时，把它已经挂上的路由与任务摘掉。

        不摘的话会留下最难受的一种状态：接口能打，但模块不在注册表里 ——
        用户看到「启用失败」，功能却是半开的。
        """
        router = getattr(self.ctx, "router", None)
        if router is not None:
            router.routes[:] = [r for r in router.routes
                                if _route_key(r) in before_routes]
        jobs = getattr(self.ctx, "jobs", None)
        if jobs is not None:
            for name in _job_names(self.ctx) - set(before_jobs or ()):
                try:
                    jobs.cancel(name)
                except Exception:                    # noqa: BLE001
                    pass
        self._drop_views(mid)

    def _drop_views(self, mid):
        views = getattr(self.ctx, "views", None)
        if views is None:
            return
        try:
            for vid in [v.id for v in views.all() if v.module == mid]:
                views._views.pop(vid, None)          # noqa: SLF001 - 契约里没有 unregister
            views._dirs.pop(mid, None)               # noqa: SLF001
        except Exception as exc:                     # noqa: BLE001 - 视图摘不干净不致命
            self.say("摘视图失败：%s" % exc)

    def _alias_v1(self, fresh):
        """给**刚挂上的**路由补一份 ``/api/v1/...`` 别名，返回新增的键。

        宿主启动时会统一做一次别名（``app.alias_api_v1``），但那只覆盖**启动那一刻**
        已经在路由表里的路径。热加载的插件如果不管，就会出现「重启前有 v1、启用后
        没有」的怪事 —— 用户的脚本指着老地址，突然 404。

        只在宿主确实开着 v1（路由表里已有 v1 路径）时才补，避免给不想要 v1 的
        嵌入场景塞一堆重复路由。
        """
        router = getattr(self.ctx, "router", None)
        if router is None:
            return set()

        exists = set(router.paths())
        has_v1 = any(len(p) > 1 and p[0] == "api" and p[1] == "v1"
                     for _m, p, _f in router.routes)
        if not has_v1:
            return set()

        added = set()
        for method, pattern, fn in list(router.routes):
            if _route_key((method, pattern, fn)) not in fresh:
                continue                         # 只补自己刚注册的，别人的不碰
            if not pattern or pattern[0] != "api":
                continue
            if len(pattern) > 1 and pattern[1] == "v1":
                continue
            target = "/api/v1" + "".join("/" + seg for seg in pattern[1:])
            key = "%s %s" % (method, target)
            if key in exists:
                continue
            router.add(method, target, fn)
            exists.add(key)
            added.add("%s /%s" % (method, "/".join(["api", "v1"] + pattern[1:])))
        return added

    def _mount_extra(self, inst, mid):
        """复刻 kernel 的「声明式视图 / 静态目录」登记，热加载也要有。"""
        views = getattr(self.ctx, "views", None)
        if views is None:
            return
        folder = getattr(inst, "assets", "") or ""
        if folder:
            base = _module_dir(inst)
            if base and not views.mount(mid, os.path.join(base, folder)):
                self.say("静态目录不存在：%s" % folder)
        for spec in getattr(inst, "views", ()) or ():
            try:
                views.register(mid, spec)
            except Exception as exc:                 # noqa: BLE001 - 视图坏了少个入口而已
                self.say("视图 %r 跳过：%s" % (spec, exc))

    # ------------------------------------------------------------ 内部：杂项
    def _guard_self(self, entry):
        if entry["id"] == self.mod.id:
            raise ControlError("不能对自己的插件管理器动手（要停用它，请手动删目录）")

    def _index_of(self, mid):
        for i, m in enumerate(self.ctx.modules or ()):
            if getattr(m, "id", "") == mid:
                return i
        return None

    def _move(self, src, dest):
        """移动目录/文件，目标父目录自动建；Windows 偶发占用时重试两次。"""
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(dest):
            raise OSError("目标已存在：%s" % dest)
        last = None
        for attempt in range(3):
            try:
                shutil.move(src, dest)
                return True
            except OSError as exc:
                last = exc
                time.sleep(0.15 * (attempt + 1))
        raise last or OSError("移动失败")

    # ------------------------------------------------------------ 打开目录
    def open_folder(self, which=""):
        """在系统文件管理器里打开插件目录（用户找不到 plugins/ 时最有用）。"""
        target = self.root
        if which == "disabled":
            target = os.path.join(self.root, probe.BUCKET_DISABLED)
        elif which == "removed":
            target = os.path.join(self.root, probe.BUCKET_REMOVED)
        os.makedirs(target, exist_ok=True)
        try:
            if hasattr(os, "startfile"):
                os.startfile(target)                 # noqa: S606 - Windows 专用
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            return True, target
        except Exception as exc:                     # noqa: BLE001 - 无 GUI 环境很常见
            return False, "%s（%s）" % (target, exc)


# ---------------------------------------------------------------- 工具函数

def _entry_file(path):
    if os.path.isdir(path):
        init = os.path.join(path, "__init__.py")
        return init if os.path.isfile(init) else ""
    if os.path.isfile(path) and path.endswith(".py"):
        return path
    return ""


def _validate_layout(folder):
    """落位前检查：这真是一个能被 discovery 认出来的插件吗。"""
    init = os.path.join(folder, "__init__.py")
    if os.path.isfile(init):
        return {"entry": init, "dirname": ""}
    pys = [n for n in os.listdir(folder)
           if n.endswith(".py") and not n.startswith("_")]
    if len(pys) == 1:
        return {"entry": os.path.join(folder, pys[0]),
                "dirname": os.path.splitext(pys[0])[0]}
    return None


def _safe_dirname(name):
    name = _SAFE_NAME.sub("_", (name or "").strip()).strip("_")
    if not name:
        name = "plugin"
    if name[0].isdigit():
        name = "p_" + name
    return name[:48]


def _safe_token(token):
    token = (token or "").strip()
    if not token or "/" in token or "\\" in token or ".." in token:
        raise ControlError("回收站条目名不合法：%r" % token)
    return token


def _route_key(route):
    return "%s /%s" % (route[0], "/".join(route[1]))


def _host_api_version():
    try:
        from anime_rss import API_VERSION
        return str(API_VERSION)
    except Exception:                                # noqa: BLE001 - 脱离主程序时降级
        return "1"


def _job_names(ctx):
    jobs = getattr(ctx, "jobs", None)
    if jobs is None:
        return set()
    try:
        return {j["name"] for j in (jobs.status() or {}).get("jobs") or ()}
    except Exception:                                # noqa: BLE001
        return set()


def _teardown_quiet(mod):
    hook = getattr(mod, "teardown", None)
    if callable(hook):
        try:
            hook()
        except Exception:                            # noqa: BLE001 - 收尾失败也继续摘
            pass


def _module_dir(mod):
    """模块文件所在目录（插件从任意路径加载时也适用）。"""
    try:
        module = sys.modules.get(type(mod).__module__)
        path = getattr(module, "__file__", "") or ""
        return os.path.dirname(os.path.abspath(path)) if path else ""
    except Exception:                                # noqa: BLE001
        return ""


def _as_module(obj):
    """复刻 discovery 的取模逻辑：类 / 实例 / 模块对象都认。"""
    if isinstance(obj, Module):
        return obj
    if isinstance(obj, type) and issubclass(obj, Module) and obj is not Module:
        return obj()
    for attr in ("MODULE", "PLUGIN", "module"):
        try:
            cand = getattr(obj, attr, None)
        except Exception:                            # noqa: BLE001 - 插件 __getattr__ 可能抛
            continue
        if isinstance(cand, Module):
            return cand
        if isinstance(cand, type) and issubclass(cand, Module) and cand is not Module:
            return cand()
    own = [v for v in vars(obj).values()
           if isinstance(v, type) and issubclass(v, Module)
           and v is not Module and getattr(v, "id", "")] if hasattr(obj, "__dict__") else []
    if len(own) == 1:
        return own[0]()
    return None
