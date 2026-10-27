"""配置：schema 由模块自己声明，内核只负责归一化与持久化。

    settings.define("clock", [
        {"key": "format", "type": "select", "default": "24h",
         "options": [{"value": "24h", "label": "24 小时"}, ...]},
        {"key": "refresh", "type": "int", "default": 30, "min": 5, "max": 600},
    ])

存储用 JSON（一个文件，人可直接改），不绑数据库 —— 换后端只需换 ``path``。
"""
import json
import os
import threading

__all__ = ["Settings", "SettingError"]

_TYPES = ("bool", "int", "float", "str", "text", "select")


class SettingError(ValueError):
    """配置项声明不合法。"""


class Settings:
    """线程安全的设置表。``define`` 必须在模块 setup 阶段调用。"""

    def __init__(self, path=None, autosave=True, values=None):
        self.path = path
        self.autosave = autosave
        self._groups = []                 # [{"id","title","items":[...]}]
        self._lock = threading.RLock()
        self._values = {}
        self._dirty = False
        if values:
            self._values.update(values)
        if path:
            self.load()

    # ------------------------------------------------------------ 声明
    def define(self, group, items=(), title=None, after=None):
        """声明一组配置项。同名分组会被合并（便于模块分批声明）。

        ``after`` 指定插在哪个分组之后；不指定就追加到末尾。
        """
        gid = str(group or "").strip()
        if not gid:
            raise SettingError("配置分组 id 不能为空")
        norm = [_normalize_item(gid, it) for it in items]

        with self._lock:
            existing = self._group(gid)
            if existing is None:
                existing = {"id": gid, "title": title or gid, "items": []}
                self._insert_group(existing, after)
            elif title:
                existing["title"] = title

            known = {it["key"] for it in existing["items"]}
            for it in norm:
                if it["key"] in known:
                    existing["items"] = [it if x["key"] == it["key"] else x
                                         for x in existing["items"]]
                else:
                    existing["items"].append(it)
                    known.add(it["key"])
                # 没存过的配置补上默认值
                if it["key"] not in self._values:
                    self._values[it["key"]] = it["default"]
            return existing

    def _group(self, gid):
        for g in self._groups:
            if g["id"] == gid:
                return g
        return None

    def _insert_group(self, group, after):
        if after:
            for i, g in enumerate(self._groups):
                if g["id"] == after:
                    self._groups.insert(i + 1, group)
                    return
        self._groups.append(group)

    # ------------------------------------------------------------ 读
    def get(self, key=None):
        """不给 key 返回全部值的副本；给 key 返回值（未知 key 返回 None）。"""
        with self._lock:
            if key is None:
                return dict(self._values)
            return self._values.get(key)

    def default(self, key):
        for g in self._groups:
            for it in g["items"]:
                if it["key"] == key:
                    return it["default"]
        return None

    def items(self):
        """扁平化的全部配置项声明（含当前值）。"""
        with self._lock:
            out = []
            for g in self._groups:
                for it in g["items"]:
                    d = dict(it)
                    d["group"] = g["id"]
                    d["value"] = self._values.get(it["key"], it["default"])
                    out.append(d)
            return out

    def schema(self):
        """给前端渲染用的分组结构。"""
        with self._lock:
            return [{
                "id": g["id"],
                "title": g["title"],
                "items": [dict(it, value=self._values.get(it["key"], it["default"]))
                          for it in g["items"]],
            } for g in self._groups]

    def snapshot(self):
        return {"values": self.get(), "groups": self.schema()}

    # ------------------------------------------------------------ 写
    def update(self, values):
        """归一化并保存；返回 ``(新值字典, 实际变化的 key 列表)``。

        未知 key 直接忽略（不让模块写脏数据），类型不合法也忽略。
        """
        if not isinstance(values, dict):
            raise SettingError("update() 需要一个 dict")
        changed = []
        with self._lock:
            for key, raw in values.items():
                item = self._item(key)
                if item is None:
                    continue
                val = _coerce(item, raw)
                if val != self._values.get(key):
                    self._values[key] = val
                    changed.append(key)
            if changed:
                self._dirty = True
                if self.autosave:
                    self._save_locked()
            return self.get(), changed

    def reset(self, keys=None):
        """把指定 key（或全部）恢复默认值，返回变化的 key 列表。"""
        changed = []
        with self._lock:
            for g in self._groups:
                for it in g["items"]:
                    if keys is not None and it["key"] not in keys:
                        continue
                    if self._values.get(it["key"]) != it["default"]:
                        self._values[it["key"]] = it["default"]
                        changed.append(it["key"])
            if changed:
                self._dirty = True
                if self.autosave:
                    self._save_locked()
        return changed

    # ------------------------------------------------------------ 持久化
    def load(self):
        if not self.path or not os.path.isfile(self.path):
            return 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return 0
        if isinstance(data, dict):
            with self._lock:
                self._values.update(data)
                return len(data)
        return 0

    def save(self):
        with self._lock:
            self._save_locked()
        return self.path

    def _save_locked(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._values, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
            self._dirty = False
        except OSError:
            # 盘坏了不该把服务打挂：内存里的值仍然有效
            pass

    @property
    def dirty(self):
        return self._dirty

    def _item(self, key):
        for g in self._groups:
            for it in g["items"]:
                if it["key"] == key:
                    return it
        return None

    def __len__(self):
        return sum(len(g["items"]) for g in self._groups)

    def __repr__(self):
        return "<Settings %d 项 %s>" % (len(self), self.path or "(内存)")


# ---------------------------------------------------------------- 归一化

def _normalize_item(group, raw):
    if not isinstance(raw, dict):
        raise SettingError("配置项必须是 dict，收到 %r" % type(raw).__name__)
    key = str(raw.get("key") or "").strip()
    if not key:
        raise SettingError("%s 组里有一项缺少 key" % group)
    kind = str(raw.get("type") or "str")
    if kind not in _TYPES:
        raise SettingError("配置项 %s 的 type 不认识：%s（可选 %s）"
                           % (key, kind, "/".join(_TYPES)))
    item = {
        "key": key,
        "type": kind,
        "label": str(raw.get("label") or key),
        "default": raw.get("default"),
        "help": str(raw.get("help") or ""),
    }
    for k in ("min", "max", "step", "unit", "placeholder"):
        if raw.get(k) is not None:
            item[k] = raw[k]
    if kind == "select":
        options = raw.get("options") or ()
        item["options"] = [_option(o) for o in options]
        if not item["options"]:
            raise SettingError("配置项 %s 是 select，但没有 options" % key)
        if item["default"] is None:
            item["default"] = item["options"][0]["value"]
    item["default"] = _coerce(item, item["default"])
    return item


def _option(raw):
    if isinstance(raw, dict):
        return {"value": raw.get("value"), "label": str(raw.get("label") or raw.get("value"))}
    return {"value": raw, "label": str(raw)}


def _coerce(item, raw):
    kind = item["type"]
    default = item.get("default")
    try:
        if kind == "bool":
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "on", "yes", "y")
            return bool(raw)
        if kind == "int":
            val = int(float(raw))
            return _clamp(item, val)
        if kind == "float":
            val = float(raw)
            return _clamp(item, val)
        if kind == "select":
            allowed = [o["value"] for o in item.get("options") or []]
            return raw if raw in allowed else (default if default in allowed
                                               else allowed[0])
        return "" if raw is None else str(raw)
    except (TypeError, ValueError):
        return default if default is not None else (0 if kind in ("int", "float") else "")


def _clamp(item, val):
    lo, hi = item.get("min"), item.get("max")
    if lo is not None and val < lo:
        val = type(val)(lo)
    if hi is not None and val > hi:
        val = type(val)(hi)
    return val
