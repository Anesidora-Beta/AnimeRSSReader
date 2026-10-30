# -*- coding: utf-8 -*-
"""链接唤醒：把条目里的磁力 / 种子 / 网盘 / 直链，一键交给对应的客户端。

解决的是这么一件小事
--------------------
条目的「资源」大多不是一个能直接下的链接，而是三四条堆在一起：磁力一条、
``.torrent`` 一条、阿里云盘与百度网盘各一条，提取码还可能写在摘要里。
想下的时候得先点开详情、一条条复制、再去客户端里粘贴，中间还要人肉对提取码。
这个模块把这段路缩短成「点一下」。

三层，各管一件事
----------------
* ``rules``  —— 纯规则：这是哪个网盘？条目里一共有哪些能下的？提取码是多少？
* ``wake``   —— 真动手：把链接交给系统关联程序 / 指定 exe（带安全护栏）。
* ``clients``—— 真回执：qBittorrent 与 aria2 的接口，能确认「确实加进去了」。

``module.py`` 只负责把这三层接到 ``ctx`` 上：读库、拼响应、校验来源。

两条刻意的取舍
--------------
1. **网盘默认不直接拉客户端**。各家客户端协议（``baiduyun://`` 之类）时有时无，
   猜一个不可靠的协议名，用户看到的是「点了没反应」。所以网盘默认走「打开
   网页 + 自动带提取码」，想要客户端就在设置里指定 exe —— 那才是可靠的。
   BT 类相反：磁力一定有人认领，默认直接交给系统关联程序。
2. **唤醒只认本机**。这个动作会在你机器上启动进程。局域网里别人打开页面，
   不该能让你弹出一堆窗口。默认只接受 Host 是 127.0.0.1 / localhost 的请求，
   要放开得自己去设置里开（并且知道自己在做什么）。

路由都在 ``/api/linkwake/`` 下，前端走 ``/plug/linkwake/``，不占公共路径。
"""
import json
import os
import threading
import time

from anime_rss.kernel.http import Response
from anime_rss.module import Module

from . import clients
from . import rules
from . import wake

_HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_NAME = "linkwake.json"
CONFIG_VERSION = 2

# 一大批链接一次唤醒时别把机器拖死：超过这个数就要求分批
MAX_BATCH = 200

MODE_TEXT = {
    "open": "系统默认程序",
    "web": "浏览器打开",
    "copy": "只复制不启动",
    "scheme": "客户端协议",
    "app": "指定程序",
}

# 「优先响应」的可选项。这是给用户看的一层抽象：用户不该被迫理解
# open / web / scheme / app 这几个 mode 的区别，他只想说「磁力交给 qB」。
#
#   auto  —— 沿用下面「每种链接交给谁」的逐网盘配置（老行为，默认）
#   qb    —— 直接进 qBittorrent 队列（能确认真的加进去了）
#   aria2 —— 同上
#   open  —— 系统关联程序（磁力会弹注册过的 BT 客户端）
#   web   —— 浏览器（网盘链接会带上提取码）
#   copy  —— 只复制，不启动任何程序
#   ask   —— 点的时候弹选择，由用户当场决定
TARGETS = (
    {"id": "auto", "text": "自动", "note": "按「每种链接交给谁」里的设置",
     "kinds": ("bt", "pan", "file", "web")},
    {"id": "qb", "text": "qBittorrent", "note": "直接加进下载队列",
     "kinds": ("bt", "file")},
    {"id": "aria2", "text": "aria2", "note": "直接加进下载队列",
     "kinds": ("bt", "file")},
    {"id": "open", "text": "系统默认程序", "note": "交给系统关联的客户端",
     "kinds": ("bt", "pan", "file", "web")},
    {"id": "web", "text": "浏览器打开", "note": "网盘链接会带上提取码",
     "kinds": ("bt", "pan", "file", "web")},
    {"id": "copy", "text": "只复制链接", "note": "不启动任何程序",
     "kinds": ("bt", "pan", "file", "web")},
    {"id": "ask", "text": "每次都问我", "note": "点链接时弹出选择",
     "kinds": ("bt", "pan", "file", "web")},
)
_TARGET_BY_ID = {t["id"]: t for t in TARGETS}
KINDS = ("bt", "pan", "file", "web")


class LinkWakeModule(Module):
    id = "linkwake"
    name = "链接唤醒"
    version = "1.2.0"

    # 由包入口按实际加载位置注入，多实例时各管各的（见 __init__.py）
    HOME = ""

    views = ({
        "id": "linkwake",
        "title": "链接唤醒",
        "icon": "⇱",
        "order": 20,
        "description": "把条目里的磁力、种子、网盘链接一键交给下载器",
        "script": "view.js",
        "style": "view.css",
    },)
    assets = "static"

    def __init__(self):
        self._cfg = {}
        self._lock = threading.RLock()
        self._waker = wake.Waker()
        self._opt = {"allowRemote": False, "autoPwd": True,
                     "onlyDownloadable": True, "pageSize": 40}
        self._booted = False

    # ------------------------------------------------------------ 生命周期
    def setup(self, ctx):
        home = self.HOME or _HERE
        # 一个进程里起两个应用时，内核按 ``module.__file__`` 找静态目录，可能
        # 停在第一次加载的位置。这里按自己的 HOME 再挂一次，以本次为准。
        try:
            if (self.HOME and ctx.views is not None
                    and os.path.realpath(home) != os.path.realpath(_HERE)):
                ctx.views.mount(self.id, os.path.join(home, "static"))
        except Exception as e:                        # noqa: BLE001
            ctx.say("linkwake 静态目录重挂失败：%s" % e)

        ctx.settings.define({
            "group": "linkwake",
            "title": "链接唤醒",
            "note": "唤醒会在本机启动程序；客户端的具体路径在插件页面里配，"
                    "这里只放全局偏好。",
            "items": [
                {"k": "lwAllowRemote", "type": "switch",
                 "label": "允许局域网设备触发唤醒", "default": False,
                 "desc": "关着时只有本机（127.0.0.1）能点「唤醒」；"
                         "开着等于允许同一网络里的人在你这台机器上开程序。"},
                {"k": "lwAutoPwd", "type": "switch",
                 "label": "自动补提取码", "default": True,
                 "desc": "百度网盘链接没带 pwd 时，从摘要里找「提取码: xxxx」补上。"},
                {"k": "lwOnlyDownloadable", "type": "switch",
                 "label": "默认只看可下载的", "default": True,
                 "desc": "过滤掉 bgm.tv、telegram 群这类只是网页的链接。"},
                {"k": "lwPageSize", "type": "number", "label": "每次列出条数",
                 "default": 40, "min": 10, "max": 200},
            ],
        }, after="search")

        r = ctx.router
        r.get("/api/linkwake", self.api_index)
        r.get("/api/linkwake/item/{iid}", self.api_item)
        r.post("/api/linkwake/prepare", self.api_prepare)
        r.post("/api/linkwake/wake", self.api_wake)
        r.post("/api/linkwake/batch", self.api_batch)
        r.get("/api/linkwake/apps", self.api_apps)
        r.post("/api/linkwake/apps", self.api_apps_save)
        r.post("/api/linkwake/probe", self.api_probe)

        self.apply_settings(ctx.settings.get() if ctx.settings else {})
        self._load()
        self._booted = True
        ctx.say("  链接唤醒   %d 个提供方，客户端配置 %s"
                % (len(rules.PROVIDERS), "已载入" if self._cfg else "用默认"))

    def teardown(self):
        self._booted = False

    def apply_settings(self, values):
        v = values or {}
        self._opt = {
            "allowRemote": bool(v.get("lwAllowRemote", False)),
            "autoPwd": bool(v.get("lwAutoPwd", True)),
            "onlyDownloadable": bool(v.get("lwOnlyDownloadable", True)),
            "pageSize": _int(v.get("lwPageSize"), 40, 10, 200),
        }

    # ------------------------------------------------------------ 配置读写
    def config_path(self):
        base = getattr(self.ctx, "data_dir", "") or (self.HOME or _HERE)
        return os.path.join(base, CONFIG_NAME)

    def _default_cfg(self):
        return {
            "version": CONFIG_VERSION,
            "apps": {p["id"]: rules.target_of(p["id"]) for p in rules.PROVIDERS},
            # 优先响应：按链接类型给一个默认动作，默认全走「自动」，
            # 也就是原来的逐网盘配置 —— 升级不该悄悄改变已有手感。
            "prefs": {k: "auto" for k in KINDS},
            "qb": {"url": "", "user": "admin", "password": "", "savepath": ""},
            "aria2": {"url": "", "token": "", "dir": ""},
        }

    def _load(self):
        """读配置文件；坏了就用默认值，绝不因为配置损坏让插件起不来。"""
        cfg = self._default_cfg()
        path = self.config_path()
        try:
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                cfg = _merge_cfg(cfg, raw)
        except Exception as e:                        # noqa: BLE001
            if getattr(self, "ctx", None) is not None:
                self.ctx.say("linkwake 配置读取失败，改用默认：%s" % e)
        with self._lock:
            self._cfg = cfg
        return cfg

    def _save(self, cfg):
        path = self.config_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        with self._lock:
            self._cfg = cfg

    def _public_cfg(self):
        """给前端的配置：密码与 token 只回「有没有设」，不回明文。

        这个页面可能被同网段的人打开（``--lan`` 模式），把口令原样发下去等于
        把它贴在墙上。要改密码就重新填一遍。
        """
        with self._lock:
            cfg = json.loads(json.dumps(self._cfg or self._default_cfg()))
        qb = cfg.get("qb") or {}
        ar = cfg.get("aria2") or {}
        qb_set = bool(qb.pop("password", ""))
        ar_set = bool(ar.pop("token", ""))
        cfg["qb"] = qb
        cfg["aria2"] = ar
        cfg["qb"]["passwordSet"] = qb_set
        cfg["aria2"]["tokenSet"] = ar_set
        return cfg

    # ------------------------------------------------------------ 取数据
    def _store(self):
        return getattr(self.ctx, "store", None)

    def _keep(self, ln, kind):
        if kind in ("", "all"):
            return True
        if kind == "dl":
            return ln["kind"] in ("bt", "pan", "file")
        return ln["kind"] == kind

    def _entry(self, row, kind, include_page=False):        # 「全部」和「只看网页」这两档才带上详情页与纯网页链接；
        # 其余档位（默认的那档）主动筛掉，否则列表会被 bgm.tv、群链接灌满。
        want_page = include_page or kind in ("all", "web")
        links = rules.extract(row, allow_pwd=self._opt["autoPwd"],
                              include_page=want_page)
        links = [x for x in links if self._keep(x, kind)]
        if not links:
            return None
        return {
            "id": row.get("id"),
            "title": row.get("title") or "",
            "source": row.get("source") or "",
            "size": row.get("size") or "",
            "pubdate": row.get("pubdate") or "",
            "ts": row.get("ts") or 0,
            "page": row.get("link") or "",
            "starred": bool(row.get("starred")),
            "read": bool(row.get("read")),
            "links": [{"url": x["url"], "pid": x["pid"], "name": x["name"],
                       "badge": x["badge"], "kind": x["kind"],
                       "color": x["color"], "pwd": x["pwd"],
                       "from": x["from"]} for x in links],
            "groups": rules.group(links),
            "count": len(links),
        }

    def _collect(self, q="", kind="dl", limit=None):
        store = self._store()
        if store is None:
            return [], {"items": 0, "links": 0, "bt": 0, "pan": 0, "file": 0,
                        "web": 0}, 0
        want = _int(limit, self._opt["pageSize"], 1, 200)
        # 多扫一些候选：抽完链接可能有大量条目一个能下的都没有（纯资料站帖），
        # 只看前 want 条会显得「没什么可下」。选「只看网盘」时尤其明显 ——
        # 网盘链接在时间线上本来就稀，窗口给窄了会一条都找不到。
        span = min(max(want * 6, 300), 800)
        rows = store.query_items(feed="all", filt="all", q=q,
                                 limit=span, offset=0)
        entries, counts = [], {"items": 0, "links": 0, "bt": 0, "pan": 0,
                               "file": 0, "web": 0}
        scanned = 0
        for row in rows:
            scanned += 1
            e = self._entry(row, kind)
            if not e:
                continue
            entries.append(e)
            counts["items"] += 1
            counts["links"] += e["count"]
            for ln in e["links"]:
                counts[ln["kind"]] = counts.get(ln["kind"], 0) + 1
            if len(entries) >= want:
                break
        return entries, counts, scanned

    # ------------------------------------------------------------ 决策
    def _cfg_snapshot(self):
        with self._lock:
            return json.loads(json.dumps(self._cfg or self._default_cfg()))

    def _pref(self, kind, cfg):
        """这个类型当前「优先用什么」。坏值一律退回 auto，不让配置把人卡死。"""
        prefs = (cfg or {}).get("prefs") or {}
        v = str(prefs.get(kind) or "auto").strip().lower()
        t = _TARGET_BY_ID.get(v)
        if not t or kind not in t["kinds"]:
            return "auto"
        return v

    def _target_list(self, kind, pid, cfg):
        """这一类型能选的目标，附「能不能用」——没配 qB 地址就别假装能发。"""
        out = []
        for t in TARGETS:
            if kind not in t["kinds"]:
                continue
            d = {"id": t["id"], "text": t["text"], "note": t["note"],
                 "ready": True}
            if t["id"] == "qb":
                d["ready"] = bool((cfg.get("qb") or {}).get("url"))
                if not d["ready"]:
                    d["note"] = "还没填 qB 地址"
            elif t["id"] == "aria2":
                d["ready"] = bool((cfg.get("aria2") or {}).get("url"))
                if not d["ready"]:
                    d["note"] = "还没填 aria2 地址"
            elif t["id"] == "auto":
                tgt = ((cfg.get("apps") or {}).get(pid)
                       or rules.target_of(pid))
                d["note"] = "当前：%s" % MODE_TEXT.get(tgt.get("mode"),
                                                       tgt.get("mode"))
            out.append(d)
        return out

    def _run_target(self, url, tid, pid, cfg):
        """按目标执行一次。异常一律抛出去，由调用方转成人话。"""
        tid = str(tid or "auto").strip().lower()
        if tid not in _TARGET_BY_ID:
            tid = "auto"
        kind = rules.provider(pid)["kind"]
        # 网盘不认 qB / aria2。绕开界面直接调接口时也得挡住，否则用户会收到
        # 一句「qB 拒绝了这条链接」，而真正的原因是这搭配本来就不成立。
        if tid != "ask" and kind not in _TARGET_BY_ID[tid]["kinds"]:
            tid = "auto"
        if tid == "ask":
            tid = "auto"                    # 后端不该看到 ask，兜个底
        if tid == "auto":
            target = dict((cfg.get("apps") or {}).get(pid)
                          or rules.target_of(pid))
            return self._waker.dispatch(url, target)
        if tid in ("qb", "aria2"):
            sec = cfg.get(tid) or {}
            if tid == "qb":
                n = clients.qb_add(sec.get("url", ""), [url],
                                   savepath=sec.get("savepath", ""),
                                   user=sec.get("user", ""),
                                   password=sec.get("password", ""))
                return {"action": "qb", "detail": "qBittorrent",
                        "count": n, "url": url}
            n = clients.aria2_add(sec.get("url", ""), [url],
                                  token=sec.get("token", ""),
                                  save_dir=sec.get("dir", ""))
            return {"action": "aria2", "detail": "aria2",
                    "count": n, "url": url}
        mode = {"open": "open", "web": "web", "copy": "copy"}[tid]
        return self._waker.dispatch(url, {"mode": mode})

    # ------------------------------------------------------------ 接口
    def api_index(self, req):
        kind = req.q("kind", "dl" if self._opt["onlyDownloadable"] else "all")
        q = req.q("q")
        entries, counts, scanned = self._collect(q, kind)
        cfg = self._public_cfg()
        return Response.json({
            "ok": True,
            "entries": entries,
            "counts": counts,
            "scanned": scanned,
            "kind": kind,
            "q": q,
            "providers": [dict(p, default=rules.target_of(p["id"]))
                          for p in rules.PROVIDERS],
            "modes": [{"id": k, "text": v} for k, v in MODE_TEXT.items()],
            "targets": [dict(t) for t in TARGETS],
            "prefs": cfg.get("prefs") or {},
            "cfg": cfg,
            "opt": dict(self._opt),
            "env": {
                "dataDir": getattr(self.ctx, "data_dir", ""),
                "home": self.HOME or _HERE,
                "os": os.name,
                "canWake": self._can_wake(req),
            },
        })

    def api_item(self, req):
        store = self._store()
        if store is None:
            return Response.err("主库不可用", 503)
        iid = (req.params or {}).get("iid") or ""
        row = store.get_item(iid)
        if not row:
            return Response.err("条目不存在", 404)
        e = self._entry(row, "all", include_page=True)
        if not e:
            return Response.json({"ok": True, "entry": None,
                                  "message": "这个条目里没有链接"})
        return Response.json({"ok": True, "entry": e})

    def api_prepare(self, req):
        """点下去之前先问一句：这是什么链接、默认交给谁、要不要弹选择。

        别的插件（比如深度搜索）只拿得到一个 URL，它不该为了「唤醒」去抄一份
        网盘域名表。所以归类留在这里，前端只负责把结果画出来。
        """
        body = req.json_body()
        url = str(body.get("url") or "").strip()
        if not url:
            return Response.err("链接是空的")
        pid, u = rules.classify(url)
        want = str(body.get("pid") or "").strip()
        if want and want in {p["id"] for p in rules.PROVIDERS}:
            pid = want
        cfg = self._cfg_snapshot()
        p = rules.provider(pid)
        kind = p["kind"]
        pref = self._pref(kind, cfg)
        ask = pref == "ask" or bool(body.get("ask"))
        return Response.json({
            "ok": True,
            "url": u,
            "pid": pid,
            "kind": kind,
            "kindText": rules.KIND_TEXT.get(kind, kind),
            "name": p["name"],
            "badge": p["badge"],
            "color": p["color"],
            "title": str(body.get("title") or ""),
            "pref": pref,
            "default": "auto" if ask else pref,
            "ask": ask,
            "targets": self._target_list(kind, pid, cfg),
            "canWake": self._can_wake(req),
            "reason": "" if self._can_wake(req) else
                      "唤醒只在本机可用，这里只能复制链接",
        })

    def api_wake(self, req):
        body = req.json_body()
        url = str(body.get("url") or "")
        pid = str(body.get("pid") or "")
        mode = str(body.get("mode") or "").strip().lower()
        tid = str(body.get("target") or "").strip().lower()
        if not self._can_wake(req):
            return Response.err(
                "唤醒只接受本机请求。要用局域网地址操作，"
                "请到设置里打开「允许局域网设备触发唤醒」", 403)
        cfg = self._cfg_snapshot()
        try:
            if mode:
                # 老路径：逐网盘设置 + 强制方式（批量条与阅读区按钮在用）
                target = dict((cfg.get("apps") or {}).get(pid)
                              or rules.target_of(pid))
                if mode not in wake.Waker.MODES:
                    return Response.err("未知的唤醒方式：%s" % mode)
                target["mode"] = mode
                res = self._waker.dispatch(url, target)
            elif tid:
                # 用户在弹层里点了哪一个
                got_pid, u = rules.classify(url)
                if pid:
                    got_pid = pid
                res = self._run_target(u, tid, got_pid, cfg)
            elif pid:
                target = dict((cfg.get("apps") or {}).get(pid)
                              or rules.target_of(pid))
                res = self._waker.dispatch(url, target)
            else:
                # 只给了一个 URL：按类型查「优先响应」
                got_pid, u = rules.classify(url)
                res = self._run_target(u, self._pref(
                    rules.provider(got_pid)["kind"], cfg), got_pid, cfg)
        except wake.WakeError as e:
            return Response.err(str(e))
        except clients.ClientError as e:
            return Response.err(str(e))
        except Exception as e:                        # noqa: BLE001
            return Response.err("唤醒失败：%s: %s" % (type(e).__name__, e))
        out = {"ok": True}
        out.update(res)
        return Response.json(out)

    def api_batch(self, req):
        body = req.json_body()
        action = str(body.get("action") or "copy").strip().lower()
        raw = body.get("urls") or []
        urls = [str(u).strip() for u in raw if str(u).strip()]
        if not urls:
            return Response.err("没有要处理的链接")
        if len(urls) > MAX_BATCH:
            return Response.err("一次最多 %d 条，这次给了 %d 条"
                                % (MAX_BATCH, len(urls)))

        if action == "copy":
            # 复制不需要启动任何东西，远端也能用
            return Response.json({"ok": True, "action": "copy",
                                  "count": len(urls), "text": "\n".join(urls)})

        if action == "open":
            if not self._can_wake(req):
                return Response.err("唤醒只接受本机请求", 403)
            done, failed = 0, []
            for u in urls:
                try:
                    self._waker.dispatch(u, {"mode": "open"})
                    done += 1
                except wake.WakeError as e:
                    failed.append({"url": u, "error": str(e)})
            return Response.json({"ok": True, "action": "open", "count": done,
                                  "failed": failed})

        if action in ("qb", "aria2"):
            if not self._can_wake(req):
                return Response.err("唤醒只接受本机请求", 403)
            with self._lock:
                cfg = json.loads(json.dumps(self._cfg))
            try:
                if action == "qb":
                    qb = cfg.get("qb") or {}
                    n = clients.qb_add(qb.get("url", ""), urls,
                                       savepath=qb.get("savepath", ""),
                                       user=qb.get("user", ""),
                                       password=qb.get("password", ""))
                    return Response.json({"ok": True, "action": "qb",
                                          "count": n})
                ar = cfg.get("aria2") or {}
                n = clients.aria2_add(ar.get("url", ""), urls,
                                      token=ar.get("token", ""),
                                      save_dir=ar.get("dir", ""))
                return Response.json({"ok": True, "action": "aria2",
                                      "count": n})
            except clients.ClientError as e:
                return Response.err(str(e))

        return Response.err("未知的批量动作：%s" % action)

    def api_apps(self, req):
        cfg = self._public_cfg()
        return Response.json({"ok": True, "cfg": cfg,
                              "prefs": cfg.get("prefs") or {},
                              "targets": [dict(t) for t in TARGETS],
                              "providers": [dict(p, default=rules.target_of(p["id"]))
                                            for p in rules.PROVIDERS],
                              "modes": [{"id": k, "text": v}
                                        for k, v in MODE_TEXT.items()]})

    def api_apps_save(self, req):
        body = req.json_body()
        with self._lock:
            cur = json.loads(json.dumps(self._cfg or self._default_cfg()))
        incoming = _merge_cfg(cur, body)
        # 密码留空表示「不改」—— 前端拿不到明文，只能用这个约定
        for name, key in (("qb", "password"), ("aria2", "token")):
            if body.get(name) and not str((body.get(name) or {}).get(key) or "").strip():
                incoming[name][key] = cur.get(name, {}).get(key, "")
        try:
            self._save(incoming)
        except OSError as e:
            return Response.err("保存失败：%s" % e)
        return Response.json({"ok": True, "cfg": self._public_cfg()})

    def api_probe(self, req):
        body = req.json_body()
        which = str(body.get("which") or "").strip().lower()
        with self._lock:
            cfg = self._cfg or self._default_cfg()
        try:
            if which == "qb":
                qb = cfg.get("qb") or {}
                ver = clients.qb_test(qb.get("url", ""), qb.get("user", ""),
                                      qb.get("password", ""))
                return Response.json({"ok": True, "which": "qb",
                                      "version": ver,
                                      "detail": "qBittorrent %s 已连通" % ver})
            if which == "aria2":
                ar = cfg.get("aria2") or {}
                ver = clients.aria2_test(ar.get("url", ""), ar.get("token", ""))
                return Response.json({"ok": True, "which": "aria2",
                                      "version": ver,
                                      "detail": "aria2 %s 已连通" % ver})
        except clients.ClientError as e:
            return Response.err(str(e))
        return Response.err("要测哪个？which=qb 或 aria2")

    # ------------------------------------------------------------ 来源校验
    def _can_wake(self, req):
        if self._opt.get("allowRemote"):
            return True
        headers = getattr(req, "headers", None) or {}
        host = ""
        try:
            host = headers.get("Host") or headers.get("host") or ""
        except Exception:                             # noqa: BLE001
            host = ""
        return wake.is_local_host(host)


# ---------------------------------------------------------------- 工具函数
def _int(value, default, lo=None, hi=None):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        n = int(default)
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _clamp_mode(mode):
    m = str(mode or "").strip().lower()
    return m if m in wake.Waker.MODES else "open"


def _merge_cfg(base, raw):
    """把外部读到的（可能是旧的、可能是坏的）配置合进默认配置。

    只认已知的 provider id 与已知字段，多余的一律丢掉 —— 配置文件是用户
    手改过的概率很高，不能因为多一个键就把整个插件搞挂。
    """
    out = json.loads(json.dumps(base))
    if not isinstance(raw, dict):
        return out

    apps = raw.get("apps")
    if isinstance(apps, dict):
        for pid, val in apps.items():
            if pid not in out["apps"] or not isinstance(val, dict):
                continue
            tgt = out["apps"][pid]
            for key in ("mode", "scheme", "exe", "args"):
                if key in val and isinstance(val[key], str):
                    tgt[key] = val[key]
            tgt["mode"] = _clamp_mode(tgt.get("mode"))

    for name, keys in (("qb", ("url", "user", "password", "savepath")),
                       ("aria2", ("url", "token", "dir"))):
        sec = raw.get(name)
        if isinstance(sec, dict):
            for key in keys:
                if key in sec and isinstance(sec[key], str):
                    out[name][key] = sec[key].strip()

    prefs = raw.get("prefs")
    if isinstance(prefs, dict):
        for k in KINDS:
            v = prefs.get(k)
            if isinstance(v, str) and v.strip().lower() in _TARGET_BY_ID:
                t = _TARGET_BY_ID[v.strip().lower()]
                if k in t["kinds"]:
                    out["prefs"][k] = t["id"]

    return out
