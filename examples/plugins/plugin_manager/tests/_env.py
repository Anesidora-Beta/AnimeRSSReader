# -*- coding: utf-8 -*-
"""测试共用的一点路径活儿。

插件可能装在两个位置，测试都要能跑：

    <repo>/plugins/plugin_manager/tests      ← 正式安装
    <任意开发目录>/plugins/plugin_manager/tests ← 改代码的地方

所以不写死 ``../..``，而是一路往上找带 ``anime_rss/`` 的目录；
也认 ``ARR_REPO`` 环境变量和同级的 ``AnimeRSSReader``。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PLUGINS_DIR = os.path.dirname(PLUGIN)


def _looks_like_repo(path):
    return (path and os.path.isdir(os.path.join(path, "anime_rss"))
            and os.path.isfile(os.path.join(path, "anime_rss", "module.py")))


def find_repo():
    seen = []
    for start in (PLUGIN, PLUGINS_DIR):
        cur = os.path.abspath(start)
        for _ in range(5):
            seen.append(cur)
            if _looks_like_repo(cur):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

    env = os.environ.get("ARR_REPO")
    if env and _looks_like_repo(env):
        return os.path.abspath(env)

    # 开发副本常见布局：仓库和开发目录是兄弟
    for base in (os.path.dirname(PLUGINS_DIR), os.path.dirname(os.path.dirname(PLUGINS_DIR))):
        for name in ("AnimeRSSReader", "anime_rss_reader"):
            cand = os.path.join(base, name)
            if _looks_like_repo(cand):
                return cand
    return ""


REPO = find_repo()

# 让 `import plugin_manager` 与 `import anime_rss` 都能找到
for path in (PLUGINS_DIR, REPO):
    if path and path not in sys.path:
        sys.path.insert(0, path)


def have_host():
    return bool(REPO)


def copy_plugin(dest_root, name="plugin_manager"):
    """把本插件复制到一个临时 plugins 目录（测试里要能随便移动/删除）。"""
    import shutil
    src = PLUGIN if name == os.path.basename(PLUGIN) else os.path.join(PLUGINS_DIR, name)
    dst = os.path.join(dest_root, name)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".pytest_cache"))
    return dst


_MINI_TMPL = '''# -*- coding: utf-8 -*-
"""测试用的最小插件：两条路由 + 一个定时任务 + 一个视图。"""
from anime_rss.kernel.http import Response
from anime_rss.module import Module


class MiniModule(Module):
    id = "%(id)s"
    name = "%(name)s"
    version = "0.1.0"
    builtin = False
    views = ({"id": "%(id)s", "title": "%(name)s", "script": "view.js"},)
    assets = "static"

    def setup(self, ctx):
        ctx.router.get("/api/%(id)s/ping", self.ping)
        ctx.router.get("/api/%(id)s/echo", self.echo)
        ctx.every(600, self.tick, name="%(id)s.tick")
        self._n = 0

    def tick(self):
        self._n = getattr(self, "_n", 0) + 1

    def ping(self, req):
        return Response.json({"ok": True, "pong": True})

    def echo(self, req):
        return Response.json({"ok": True, "q": req.q("q")})
'''

_MINI_VIEW = '''/* 最小视图 */
(function () {
  if (window.ARR) window.ARR.registerView({id: "%(id)s", title: "%(name)s",
    mount: function (el) { el.innerHTML = "<p>%(id)s</p>"; }});
})();
'''


def minimal_source(pid="mini", name="测试插件"):
    """生成一个最小插件的源码（id 可参数化，测试里要造好几个）。"""
    return _MINI_TMPL % {"id": pid, "name": name}


def minimal_view(pid="mini", name="测试插件"):
    return _MINI_VIEW % {"id": pid, "name": name}


MINIMAL_PLUGIN = minimal_source()
MINI_VIEW = minimal_view()


def write_minimal_plugin(root, dirname="mini_plug", body=None, view=True,
                         pid="mini", name="测试插件"):
    """在 root 下造一个能跑的最小插件目录。"""
    folder = os.path.join(root, dirname)
    os.makedirs(os.path.join(folder, "static"), exist_ok=True)
    with open(os.path.join(folder, "__init__.py"), "w", encoding="utf-8") as f:
        f.write(body or minimal_source(pid, name))
    if view:
        with open(os.path.join(folder, "static", "view.js"), "w",
                  encoding="utf-8") as f:
            f.write(minimal_view(pid, name))
    return folder
