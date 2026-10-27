# -*- coding: utf-8 -*-
"""插件管理器：把「装了哪些插件、它们现在什么状态、怎么关掉」变成看得见的事。

为什么需要它
------------
主程序对插件的态度是「装了就跑、出错就跳过」，这对启动稳定性很好，
但把两类麻烦留给了用户：

* **看不见**：装了哪几个、版本多少、注册了哪些路由、为什么没生效，
  只能去翻控制台日志；
* **关不掉**：一个插件天天报错或者抢路由，除了手动去 ``plugins/`` 里
  删目录，没有别的办法 —— 而 ``plugins/`` 在哪，第一次用的人根本找不到
  （``.gitignore`` 里就有 ``/plugins/``）。

这个模块不改变主程序的任何行为，它只是把 ``discovery`` 已经在做的事
（扫描目录、跳过坏插件）**摊开给人看**，再加两个动作：停用、装上。

设计取舍
--------
1. **停用要真的停**。仅写一个 ``enabled=false`` 是骗人的 —— 插件照样
   import、照样挂路由。这里把目录移进 ``plugins/.disabled/``（``discovery``
   天然跳过 ``.`` 开头的条目），同时在**当前进程**里把路由、视图、静态目录、
   定时任务、模块注册表一起摘掉。两件事都做，界面才敢说「已停用」。
2. **不改主程序**。全部能力都用已有契约：目录约定 + ``ctx`` 上的公开对象。
   唯一碰私有字段的是 ``ViewRegistry`` 没有 ``unregister``（只有 ``clear``），
   所以对 ``_views`` 做了一次删除，并整段包在 try 里 —— 万一以后改名，
   最坏结果是「视图没摘干净，重启后消失」，不会让管理器崩掉。
3. **静态探测代替 import**。插件加载失败时恰恰不能再 import 它，所以清单
   由 ``probe`` 用 ``ast`` 读出来，``registry`` 再和内存里的实况对齐。

路由（都在 ``/api/pluginmgr/`` 下，不占公共路径）
------------------------------------------------
======================  ======  ==================================
路径                    方法    干什么
======================  ======  ==================================
``/api/pluginmgr``       GET     全量快照：插件档案 + 统计 + 体检
``/api/pluginmgr/detail`` GET    单个插件的路由/视图/文件/源码
``/api/pluginmgr/action`` POST   停用 / 启用 / 重载 / 卸载 / 恢复 / 彻底删除
``/api/pluginmgr/install`` POST  上传 zip 安装（body 就是 zip 字节）
``/api/pluginmgr/export`` GET    把某个插件打回 zip
``/api/pluginmgr/file``   GET     读插件里的一个源码文件
``/api/pluginmgr/note``   POST   给插件写备注（存在本地 json）
``/api/pluginmgr/open``   POST   在系统文件管理器里打开插件目录
``/api/pluginmgr/journal`` GET    最近操作记录
======================  ======  ==================================
"""
import os
import sys
import time

from anime_rss.kernel.http import Response
from anime_rss.module import Module

from . import control as C
from . import registry

__all__ = ["PluginManagerModule", "MODULE"]

_MODULE_ID = "pluginmgr"
_HERE = os.path.dirname(os.path.abspath(__file__))

_ACTIONS = ("disable", "enable", "reload", "remove", "restore", "purge")


class PluginManagerModule(Module):
    id = _MODULE_ID
    name = "插件管理"
    version = "1.0.0"

    # 本插件所在的 plugins/ 目录。由包入口按实际加载位置注入一个子类属性，
    # 这样同一进程里两个应用实例不会互相指错目录（见 __init__.py 的说明）。
    HOME = ""

    views = ({
        "id": _MODULE_ID,
        "title": "插件管理",
        "icon": "⧉",
        "order": 95,
        "description": "看清楚装了哪些插件、它们的状态，停用/启用/重载/安装",
        "script": "view.js",
        "style": "view.css",
    },)
    assets = "static"

    def __init__(self):
        self.control = None
        self._applied = False

    # ------------------------------------------------------------ 生命周期
    def setup(self, ctx):
        self.control = C.Controller(self)

        ctx.settings.define({
            "group": _MODULE_ID,
            "title": "插件管理",
            "note": "只管第三方插件；内置模块为安全起见不允许停用。",
            "items": [
                {"k": "pmAutoSoft", "type": "switch", "label": "自动摘除软停用插件",
                 "default": True,
                 "desc": "插件目录不可写时（如 Docker 只读挂载），停用只能作用到"
                         "本次运行；打开这项后每次启动都会自动摘掉它们。"},
            ],
        }, after="read")

        r = ctx.router
        r.get("/api/pluginmgr", self.api_list)
        r.get("/api/pluginmgr/detail", self.api_detail)
        r.post("/api/pluginmgr/action", self.api_action)
        r.post("/api/pluginmgr/install", self.api_install)
        r.get("/api/pluginmgr/export", self.api_export)
        r.get("/api/pluginmgr/file", self.api_file)
        r.post("/api/pluginmgr/note", self.api_note)
        r.post("/api/pluginmgr/open", self.api_open)
        r.get("/api/pluginmgr/journal", self.api_journal)
        r.post("/api/pluginmgr/refresh", self.api_refresh)

        # 软停用要在所有模块都到位之后再摘一次（本模块不一定排在最后），
        # 所以 setup 里先摘一遍，任务表起来后再补一遍。
        self._apply_soft(force=True)
        try:
            ctx.later(0.8, self._apply_soft, name="pluginmgr.soft")
        except Exception:                          # noqa: BLE001 - 没有任务表就算了
            pass

    def teardown(self):
        self.control = None

    # ------------------------------------------------------------ 工具
    def plugins_dir(self):
        """插件目录就是本插件所在目录的上一级 —— 我们自己也住在里面。"""
        home = getattr(self, "HOME", "") or _HERE
        return os.path.dirname(home.rstrip(os.sep))

    def state_path(self):
        data_dir = getattr(self.ctx, "data_dir", "") or "."
        return os.path.join(data_dir, C.STATE_FILE)

    def say(self, msg):
        log = getattr(self.ctx, "log", None)
        if log:
            try:
                log("  [pluginmgr] %s" % msg)
            except Exception:                      # noqa: BLE001
                pass

    def _snapshot(self):
        return self.control.snapshot()

    def _apply_soft(self, force=False):
        if self._applied and not force:
            return []
        self._applied = True
        try:
            values = self.ctx.settings.get() if self.ctx.settings else {}
            if not values.get("pmAutoSoft", True):
                return []
            done = self.control.apply_soft()
            if done:
                self.say("自动摘除软停用插件：%s" % "、".join(done))
            return done
        except Exception as exc:                   # noqa: BLE001 - 自检失败不该影响启动
            self.say("应用软停用失败：%s: %s" % (type(exc).__name__, exc))
            return []

    def _env(self):
        root = self.plugins_dir()
        writable = True
        try:
            writable = os.access(root, os.W_OK) if os.path.isdir(root) else False
        except Exception:                          # noqa: BLE001
            writable = False
        return {
            "root": root,
            "rootExists": os.path.isdir(root),
            "writable": writable,
            "separator": os.sep,
            "platform": sys.platform,
            "coreVersion": getattr(self.ctx, "version", ""),
            "apiVersion": _api_version(),
            "dataDir": getattr(self.ctx, "data_dir", ""),
            "moduleId": _MODULE_ID,
            "canOpenFolder": bool(hasattr(os, "startfile") or sys.platform != "win32"),
            "readme": "",
        }

    # ------------------------------------------------------------ 路由
    def api_list(self, req):
        self._apply_soft()
        snap = self._snapshot()
        data = snap.as_dict()
        data["ok"] = True
        data["env"] = self._env()
        data["notes"] = self.control.state().get("notes") or {}
        data["journal"] = (self.control.state().get("journal") or [])[:12]
        data["counts"] = snap.counts()
        data["serverTime"] = int(time.time())
        return Response.json(data)

    def api_refresh(self, req):
        return self.api_list(req)

    def api_detail(self, req):
        ident = req.q("id")
        snap = self._snapshot()
        entry = snap.get(ident)
        if entry is None:
            return Response.err("没有这个插件：%s" % ident, 404)

        detail = dict(entry)
        # 注意别覆盖 entry 里的 files（那是文件**数量**），列表单开一个键
        detail["fileList"] = _file_list(entry.get("path"))
        detail["source"] = {}
        target = entry.get("file") or ""
        if target and os.path.isfile(target):
            detail["source"] = {
                "path": os.path.basename(target),
                "text": _read_text(target, 24000),
            }
        detail["notes"] = (self.control.state().get("notes") or {}).get(entry["id"], "")
        detail["ok"] = True
        return Response.json(detail)

    def api_action(self, req):
        body = req.json_body() or {}
        action = str(body.get("action") or "").lower()
        if action not in _ACTIONS:
            return Response.err("不认识的动作：%s（可用：%s）"
                                % (action, " / ".join(_ACTIONS)))
        ident = str(body.get("id") or body.get("token") or "")
        if not ident:
            return Response.err("缺少插件 id")

        try:
            if action == "disable":
                res = self.control.disable(ident, move=bool(body.get("move", True)))
            elif action == "enable":
                res = self.control.enable(ident)
            elif action == "reload":
                res = self.control.reload(ident)
            elif action == "remove":
                res = self.control.remove(ident)
            elif action == "restore":
                res = self.control.restore(ident)
            else:
                res = self.control.purge(ident)
        except C.ControlError as exc:
            self.control.journal(action, ident, False, str(exc))
            return Response.err(str(exc), 400)
        except Exception as exc:                   # noqa: BLE001 - 兜底，别让 500 无声无息
            self.say("%s %s 失败：%s: %s" % (action, ident, type(exc).__name__, exc))
            return Response.err("%s 失败：%s: %s" % (action, type(exc).__name__, exc), 500)

        res.update({"ok": True, "action": action, "entry": self._fresh(ident)})
        return Response.json(res)

    def _fresh(self, ident):
        try:
            return self._snapshot().get(ident)
        except Exception:                          # noqa: BLE001
            return None

    def api_install(self, req):
        name = req.q("name")
        overwrite = req.q("overwrite") in ("1", "true", "yes")
        data = req.body or b""
        if not data:
            return Response.err("没有收到文件内容（body 应为 zip 字节）")
        try:
            res = self.control.install(data, filename=name, overwrite=overwrite)
        except C.ControlError as exc:
            self.control.journal("install", name, False, str(exc))
            return Response.err(str(exc), 400)
        except Exception as exc:                   # noqa: BLE001
            self.say("安装失败：%s: %s" % (type(exc).__name__, exc))
            return Response.err("安装失败：%s: %s" % (type(exc).__name__, exc), 500)
        res["ok"] = True
        res["entry"] = self._fresh(res.get("id") or "")
        return Response.json(res)

    def api_export(self, req):
        ident = req.q("id")
        try:
            data, filename = self.control.export(ident)
        except C.ControlError as exc:
            return Response.err(str(exc), 400)
        except Exception as exc:                   # noqa: BLE001
            return Response.err("打包失败：%s" % exc, 500)
        return Response(data, "application/zip", headers={
            "Content-Disposition": 'attachment; filename="%s"' % filename,
        })

    def api_file(self, req):
        ident = req.q("id")
        rel = req.q("path")
        snap = self._snapshot()
        entry = snap.get(ident)
        if entry is None:
            return Response.err("没有这个插件：%s" % ident, 404)
        root = entry.get("path") or ""
        if not root:
            return Response.err("这个插件没有磁盘文件", 404)
        root = root if os.path.isdir(root) else os.path.dirname(root)
        target = _safe_join(root, rel)
        if not target:
            return Response.err("路径不合法", 400)
        if not os.path.isfile(target):
            return Response.err("文件不存在：%s" % rel, 404)
        text = _read_text(target, 120000)
        if text is None:
            return Response.err("这个文件不是文本，或太大读不了", 415)
        return Response.json({"ok": True, "path": rel, "text": text,
                              "size": os.path.getsize(target)})

    def api_note(self, req):
        body = req.json_body() or {}
        ident = str(body.get("id") or "")
        if not ident:
            return Response.err("缺少插件 id")
        try:
            self.control.note(ident, str(body.get("text") or ""))
        except Exception as exc:                   # noqa: BLE001
            return Response.err("备注没存上：%s" % exc, 500)
        return Response.json({"ok": True})

    def api_open(self, req):
        body = req.json_body() or {}
        which = str(body.get("which") or "")
        ok, msg = self.control.open_folder(which)
        if not ok:
            return Response.json({"ok": False, "path": self.plugins_dir(),
                                  "error": "系统没让打开：%s" % msg})
        return Response.json({"ok": True, "path": msg})

    def api_journal(self, req):
        st = self.control.state()
        return Response.json({"ok": True, "journal": st.get("journal") or [],
                              "softDisabled": st.get("softDisabled") or [],
                              "notes": st.get("notes") or {}})

    # ------------------------------------------------------------ 首屏
    def bootstrap(self, ctx):
        """给首屏一个小摘要，不做全量扫描（打开插件页时才扫）。"""
        try:
            snap = self._snapshot()
            return {"plugins": snap.counts()}
        except Exception:                          # noqa: BLE001 - 首屏不能因为插件页坏掉
            return {}


# ---------------------------------------------------------------- 文件小工具

def _api_version():
    try:
        from anime_rss import API_VERSION
        return str(API_VERSION)
    except Exception:                              # noqa: BLE001
        return "1"


def _file_list(path, limit=200):
    if not path:
        return []
    base = path if os.path.isdir(path) else os.path.dirname(path)
    if not os.path.isdir(base):
        return []
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames
                             if d != "__pycache__" and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, name)
            try:
                size = os.path.getsize(full)
            except OSError:
                continue
            out.append({"path": os.path.relpath(full, base).replace("\\", "/"),
                        "size": size})
            if len(out) >= limit:
                return out
    return out


def _safe_join(root, rel):
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel or ".." in rel.split("/") or rel.startswith("/"):
        return ""
    real_root = os.path.realpath(root)
    target = os.path.realpath(os.path.join(real_root, *rel.split("/")))
    if target != real_root and not target.startswith(real_root + os.sep):
        return ""
    return target


def _read_text(path, limit):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


MODULE = PluginManagerModule
