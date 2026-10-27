"""前端视图注册：模块声明一个视图，侧栏就会自动多一个入口。

后端这一侧只做三件事：

1. 收模块声明（``Module.views``），校验 id 与重名；
2. 把模块自带的静态目录挂到 ``/plug/<模块 id>/<文件>``；
3. 通过 ``/api/bootstrap`` 把视图清单发给前端，前端按清单加载脚本。

真正的界面由模块自己的 ``view.js`` 调 ``ARR.registerView({...})`` 画出来，
所以内核不需要知道「放送表长什么样」「示例插件画了什么」。
"""
import os
import re

from .assets import Assets

__all__ = ["View", "ViewRegistry", "ViewError"]

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class ViewError(ValueError):
    """视图声明不合法（id 格式、重名、脚本不可读）。"""


class View:
    __slots__ = ("id", "title", "icon", "module", "script", "style",
                 "order", "description", "badge")

    def __init__(self, id, title, module, icon="", script="", style="",
                 order=100, description="", badge=""):
        self.id = id
        self.title = title
        self.module = module
        self.icon = icon
        self.script = script
        self.style = style
        self.order = order
        self.description = description
        self.badge = badge

    def manifest(self):
        """发给前端的描述。字段只增不改 —— 前端契约的一部分。"""
        return {
            "id": self.id,
            "title": self.title,
            "icon": self.icon,
            "module": self.module,
            "description": self.description,
            "badge": self.badge,
            "order": self.order,
            "script": self.script,
            "style": self.style,
        }

    def __repr__(self):
        return "<View %s from %s>" % (self.id, self.module)


class ViewRegistry:
    """已注册视图 + 各模块的静态目录。"""

    URL_PREFIX = "/plug/"

    def __init__(self):
        self._views = {}
        self._dirs = {}                   # module_id -> 绝对目录

    # ------------------------------------------------------------ 静态目录
    def mount(self, module_id, directory):
        """把模块的静态目录挂到 ``/plug/<module_id>/``。目录不存在返回 False。"""
        if not module_id or not directory:
            return False
        path = os.path.realpath(directory)
        if not os.path.isdir(path):
            return False
        self._dirs[module_id] = path
        return True

    def static_dir(self, module_id):
        return self._dirs.get(module_id)

    def read(self, module_id, name):
        """安全读文件，返回 (bytes, content_type)；越界 / 不存在返回 None。

        允许子目录（``js/view.js``），但 ``..``、绝对路径、符号链接逃逸一律拒绝。
        """
        root = self._dirs.get(module_id)
        if not root:
            return None
        rel = (name or "").replace("\\", "/").strip("/")
        if not rel:
            return None
        parts = [p for p in rel.split("/") if p]
        if not parts or any(p in (".", "..") or "\x00" in p for p in parts):
            return None
        real = os.path.realpath(os.path.join(root, *parts))
        if not (real == root or real.startswith(root + os.sep)):
            return None
        if not os.path.isfile(real):
            return None
        try:
            with open(real, "rb") as f:
                data = f.read()
        except OSError:
            return None
        return data, Assets.content_type(real)

    def url(self, module_id, name):
        return "%s%s/%s" % (self.URL_PREFIX, module_id,
                            "/".join(p for p in str(name).split("/") if p))

    # ------------------------------------------------------------ 视图
    def register(self, module_id, spec):
        """登记一个视图。spec 是 dict：id / title / icon / script / style / order。"""
        if not isinstance(spec, dict):
            raise ViewError("视图声明必须是 dict，收到 %r" % type(spec).__name__)
        vid = str(spec.get("id") or module_id or "").strip()
        if not _ID_RE.match(vid):
            raise ViewError("视图 id 不合法：%r（小写字母开头，仅 a-z0-9_-）" % vid)
        if vid in self._views:
            raise ViewError("视图 id 重复：%s（已被 %s 占用）"
                            % (vid, self._views[vid].module))
        title = str(spec.get("title") or vid)
        script = str(spec.get("script") or "")
        style = str(spec.get("style") or "")
        if script:
            if not self.read(module_id, script):
                raise ViewError("视图 %s 的脚本读不到：%s" % (vid, script))
            script = self.url(module_id, script)
        if style:
            if not self.read(module_id, style):
                raise ViewError("视图 %s 的样式读不到：%s" % (vid, style))
            style = self.url(module_id, style)
        view = View(vid, title, module_id,
                    icon=str(spec.get("icon") or ""),
                    script=script, style=style,
                    order=_order(spec.get("order")),
                    description=str(spec.get("description") or ""),
                    badge=str(spec.get("badge") or ""))
        self._views[vid] = view
        return view

    def get(self, view_id, default=None):
        return self._views.get(view_id, default)

    def all(self):
        return sorted(self._views.values(), key=lambda v: (v.order, v.id))

    def manifest(self):
        return [v.manifest() for v in self.all()]

    def ids(self):
        return list(self._views)

    def clear(self):
        self._views.clear()
        self._dirs.clear()

    def __len__(self):
        return len(self._views)

    def __iter__(self):
        return iter(self.all())

    def __contains__(self, view_id):
        return view_id in self._views


def _order(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 100
