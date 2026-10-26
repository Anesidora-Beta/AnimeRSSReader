#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户设置：默认值、校验、持久化、后台定时刷新。

设置分两类：
  * 影响服务端的（刷新间隔、保留天数、放送表来源…）—— 存进 SQLite 的 meta 表，
    改完立即作用到刷新器 / 放送表服务上；
  * 影响界面的（主题、字号、密度…）—— 同样存服务端，前端启动时读一次再应用，
    这样换个浏览器打开也是同一套观感。

SCHEMA 同时提供「有哪些设置、什么类型、怎么校验」和给前端渲染用的元数据，
新增一个设置项只需要在这里加一行。
"""

import json
import threading
import time

PREFIX = "s."

# ---------------------------------------------------------------- 设置项定义


def _opt(v, label=""):
    return {"value": v, "label": label or v}


SCHEMA = [
    {
        "group": "fetch",
        "title": "抓取与刷新",
        "note": "后台按间隔自动抓取，只在服务端计时，关掉网页也会继续。",
        "items": [
            {"k": "autoRefresh", "type": "switch", "label": "自动刷新",
             "desc": "按下面的间隔自动抓取最新资源", "default": True},
            {"k": "refreshInterval", "type": "number", "label": "刷新间隔",
             "desc": "两次自动抓取之间隔多久", "default": 30,
             "min": 5, "max": 1440, "step": 5, "unit": "分钟",
             "depends": "autoRefresh"},
            {"k": "fetchOnStart", "type": "switch", "label": "启动时抓取",
             "desc": "程序启动后立刻抓一轮", "default": True},
            {"k": "fetchLimit", "type": "number", "label": "单源条数上限",
             "desc": "每个站点每次最多抓多少条，越小越快", "default": 150,
             "min": 50, "max": 500, "step": 10, "unit": "条"},
        ],
    },
    {
        "group": "read",
        "title": "阅读体验",
        "note": "只影响这个界面，改完立即生效。",
        "items": [
            {"k": "theme", "type": "select", "label": "主题", "default": "auto",
             "options": [_opt("auto", "跟随系统"), _opt("light", "浅色"), _opt("dark", "深色")]},
            {"k": "fontSize", "type": "select", "label": "正文字号", "default": "m",
             "options": [_opt("s", "小"), _opt("m", "标准"), _opt("l", "大"), _opt("xl", "特大")]},
            {"k": "density", "type": "select", "label": "列表密度", "default": "cozy",
             "options": [_opt("cozy", "宽松"), _opt("compact", "紧凑")]},
            {"k": "autoRead", "type": "switch", "label": "点开即已读",
             "desc": "打开条目就自动标记为已读", "default": True},
            {"k": "hotkeys", "type": "switch", "label": "键盘快捷键",
             "desc": "J/K 切换、S 收藏、T 新番表、R 刷新、/ 搜索", "default": True},
        ],
    },
    {
        "group": "schedule",
        "title": "新番放送表",
        "note": "关掉的来源不再抓取；已经抓到的数据下次刷新后消失。",
        "items": [
            {"k": "scheduleSources", "type": "checks", "label": "数据来源",
             "default": ["bilibili", "bangumi", "anilist", "mal"],
             "options": [_opt("bilibili", "哔哩哔哩"), _opt("bangumi", "Bangumi"),
                         _opt("anilist", "AniList"), _opt("mal", "MAL / Jikan")]},
            {"k": "scheduleTtl", "type": "number", "label": "缓存有效期",
             "desc": "超过这个时间就在后台重新抓放送表", "default": 6,
             "min": 1, "max": 48, "step": 1, "unit": "小时"},
            {"k": "airingNotify", "type": "switch", "label": "开播提醒",
             "desc": "有番即将开播时弹浏览器通知（需要授权）", "default": False},
        ],
    },
    {
        "group": "store",
        "title": "存储与清理",
        "note": "收藏的条目和关注命中的条目不会被清掉。",
        "items": [
            {"k": "keepDays", "type": "number", "label": "保留天数",
             "desc": "超过天数的未收藏条目会被删掉", "default": 30,
             "min": 3, "max": 365, "step": 1, "unit": "天"},
            {"k": "keepMax", "type": "number", "label": "条目上限",
             "desc": "总量超过后淘汰最旧的未收藏条目", "default": 30000,
             "min": 2000, "max": 200000, "step": 1000, "unit": "条"},
        ],
    },
    {
        "group": "app",
        "title": "程序行为",
        "note": "关窗行为改完立即生效，不用重启。",
        "items": [
            {"k": "closeAction", "type": "select", "label": "关闭窗口时", "default": "ask",
             "options": [_opt("ask", "先问我"), _opt("background", "留在后台"),
                         _opt("exit", "直接退出")]},
        ],
    },
]

ITEM_BY_KEY = {it["k"]: it for g in SCHEMA for it in g["items"]}


def defaults():
    return {k: (list(it["default"]) if isinstance(it["default"], list)
                else it["default"]) for k, it in ITEM_BY_KEY.items()}


# ---------------------------------------------------------------- 校验

def _clamp(v, it):
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return it["default"]
    return max(it.get("min", n), min(it.get("max", n), n))


def _pick(v, it):
    vals = [o["value"] for o in it.get("options") or []]
    return v if v in vals else it["default"]


def _checks(v, it):
    vals = [o["value"] for o in it.get("options") or []]
    if not isinstance(v, (list, tuple)):
        return list(it["default"])
    got = [x for x in v if x in vals]
    return got or list(it["default"])


def normalize(values):
    """把前端/老数据里乱七八糟的值收敛成合法值"""
    out = {}
    for k, it in ITEM_BY_KEY.items():
        v = values.get(k, it["default"])
        t = it["type"]
        if t == "switch":
            out[k] = bool(v)
        elif t == "number":
            out[k] = _clamp(v, it)
        elif t == "select":
            out[k] = _pick(v, it)
        elif t == "checks":
            out[k] = _checks(v, it)
        else:
            out[k] = v if isinstance(v, str) else str(v)
    return out


# ---------------------------------------------------------------- 服务

class Settings:
    """设置的读写。写入后立刻对内存里那份生效，同时落库。"""

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._cache = None
        self._load()

    # ---------------- 读
    def _load(self):
        raw = self.store.meta_prefix(PREFIX)
        values = defaults()
        for k, v in raw.items():
            key = k[len(PREFIX):]
            if key not in ITEM_BY_KEY:
                continue
            try:
                values[key] = json.loads(v)
            except (ValueError, TypeError):
                pass
        with self._lock:
            self._cache = normalize(values)
        return dict(self._cache)

    def get(self):
        with self._lock:
            if self._cache is None:
                return self._load()
            return dict(self._cache)

    def schema(self):
        return SCHEMA

    # ---------------- 写
    def save(self, patch):
        values = self.get()
        for k, v in (patch or {}).items():
            if k in ITEM_BY_KEY:
                values[k] = v
        values = normalize(values)
        self._persist(values)
        return self.get()

    def reset(self):
        values = normalize(defaults())
        self._persist(values)
        return self.get()

    def _persist(self, values):
        for k, v in values.items():
            self.store.meta_set(PREFIX + k, json.dumps(v, ensure_ascii=False))
        with self._lock:
            self._cache = dict(values)


# ---------------------------------------------------------------- 定时刷新

class AutoRefresh(threading.Thread):
    """后台计时器：到点就抓一轮订阅 + 该刷新放送表时也顺手刷新。

    只在服务端计时，所以关掉网页、手机端断开都不影响；
    每 15 秒醒一次，判断成本可以忽略。
    """

    TICK = 15

    def __init__(self, settings, refresher, schedule, store):
        super().__init__(daemon=True, name="auto-refresh")
        self.settings = settings
        self.refresher = refresher
        self.schedule = schedule
        self.store = store
        self._stop = threading.Event()
        self.last_try = time.time()
        self.last_run = 0

    def stop(self):
        self._stop.set()

    def next_run_at(self):
        s = self.settings.get()
        if not s.get("autoRefresh"):
            return 0
        return int(self.last_try + s["refreshInterval"] * 60)

    def run(self):
        while not self._stop.wait(self.TICK):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        now = time.time()
        s = self.settings.get()

        if s.get("autoRefresh"):
            gap = s["refreshInterval"] * 60
            if now - self.last_try >= gap:
                st = self.refresher.status()
                if not st.get("running"):
                    self.last_try = now
                    self.last_run = now
                    self.refresher.start()
                else:
                    self.last_try = now      # 上一轮还没跑完，别叠任务

        if self.schedule is not None:
            try:
                if self.schedule.should_auto_refresh():
                    self.schedule.start()
            except Exception:
                pass
