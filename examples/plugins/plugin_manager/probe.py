# -*- coding: utf-8 -*-
"""静态探测：不执行插件代码，先把「它是什么」读出来。

插件管理器最尴尬的时刻是：插件加载失败，主程序里一个模块都没有，
此时想告诉用户「哪个插件坏了、坏在哪一行」—— 但恰恰不能再去 import 它。
所以这里的全部工作只做两件事：

1. ``ast.parse`` 入口文件，从语法树里读 ``id / name / version /
   api_version / depends / views / assets``；
2. 顺着 ``from .module import XxxModule`` 这类导入，跟到包内其它 ``.py``
   里找 ``Module`` 子类 —— 因为常见写法是入口只做转发（``arr_search``
   就用 PEP 562 的 ``__getattr__`` 延迟导入）。

整个过程**零副作用**：不 import、不执行、不建表、不联网。
解析失败也只是记一句 ``error``，仍然返回一条可展示的档案。
"""
import ast
import os
import re

__all__ = ["Probe", "probe_path", "probe_root", "BUCKETS", "BUCKET_DISABLED",
           "BUCKET_REMOVED", "BUCKET_ACTIVE", "STAGING_PREFIX"]

BUCKET_ACTIVE = "plugins"          # plugins/<名字>            —— 会加载
BUCKET_DISABLED = ".disabled"      # plugins/.disabled/<名字>  —— 被停用
BUCKET_REMOVED = ".removed"        # plugins/.removed/<名>@<ts> —— 回收站
BUCKETS = (BUCKET_ACTIVE, BUCKET_DISABLED, BUCKET_REMOVED)

STAGING_PREFIX = ".staging-"       # 安装时的临时目录，探测时跳过

_READ_MAX = 512 * 1024             # 入口文件最大读这么多字节（插件入口不该更大）
_SCAN_LIMIT = 4000                 # 统计体积时最多走这么多文件，防呆目录
_SIBLING_LIMIT = 60                # 跟包内导入时最多解析这么多文件
_MODULE_BASE = "Module"            # 契约基类名
_ENTRY_HINT = ("MODULE", "PLUGIN", "module", "plugin")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Probe:
    """磁盘上一个插件目录/文件的静态档案。"""

    __slots__ = ("bucket", "dirname", "path", "kind", "entry", "id", "title",
                 "version", "api_version", "depends", "views", "assets",
                 "size", "files", "mtime", "readme", "error", "notes",
                 "module_class", "class_file")

    def __init__(self, bucket, dirname, path):
        self.bucket = bucket
        self.dirname = dirname        # 目录名（或单文件名去掉 .py）
        self.path = path              # 绝对路径
        self.kind = ""                # "package" / "module" / ""
        self.entry = ""               # 入口文件绝对路径
        self.id = ""                  # 插件自己声明的 id（探测不到就空）
        self.title = ""
        self.version = ""
        self.api_version = ""
        self.depends = ()
        self.views = ()
        self.assets = ""
        self.size = 0
        self.files = 0
        self.mtime = 0
        self.readme = ""
        self.error = ""
        self.notes = []               # 给人看的补充说明
        self.module_class = ""        # 找到的 Module 子类名
        self.class_file = ""          # 该类定义在哪个文件

    # ------------------------------------------------------------ 对外
    @property
    def token(self):
        """回收站条目的唯一标识：目录名@时间戳。"""
        return self.dirname

    @property
    def name(self):
        """显示名：优先自己声明的，其次目录名。"""
        return self.title or self.id or self.dirname

    def as_dict(self):
        return {
            "bucket": self.bucket,
            "dirname": self.dirname,
            "token": self.token,
            "path": self.path,
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "version": self.version,
            "apiVersion": self.api_version,
            "depends": list(self.depends),
            "views": [dict(v) for v in self.views if isinstance(v, dict)],
            "assets": self.assets,
            "size": self.size,
            "files": self.files,
            "mtime": self.mtime,
            "readme": self.readme,
            "error": self.error,
            "notes": list(self.notes),
            "moduleClass": self.module_class,
            "classFile": self.class_file,
        }

    def __repr__(self):
        return "<Probe %s/%s id=%r %s>" % (self.bucket, self.dirname,
                                           self.id, self.error or "ok")


# ---------------------------------------------------------------- 入口

def probe_root(root):
    """扫描一个 ``plugins`` 目录，返回 ``{bucket: [Probe, ...]}``。

    只认两种东西：带 ``__init__.py`` 的目录、顶层 ``.py`` 文件。
    以 ``.`` 或 ``_`` 开头的条目本来就不会被 ``discovery`` 加载，这里也跳过
    （``.disabled`` / ``.removed`` 自己例外，它们是我们的桶）。
    """
    out = {b: [] for b in BUCKETS}
    if not root or not os.path.isdir(root):
        return out

    for bucket in BUCKETS:
        folder = root if bucket == BUCKET_ACTIVE else os.path.join(root, bucket)
        if not os.path.isdir(folder):
            continue
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            if name.startswith((".", "_")) and bucket == BUCKET_ACTIVE:
                continue                       # .staging- / __pycache__ / .git
            path = os.path.join(folder, name)
            if os.path.isdir(path):
                if not os.path.isfile(os.path.join(path, "__init__.py")):
                    continue                   # 不是包，discovery 也不会理它
                out[bucket].append(probe_path(path, bucket))
            elif bucket != BUCKET_ACTIVE:
                continue                       # 停用/回收站里只收目录
            elif name.endswith(".py"):
                out[bucket].append(probe_path(path, bucket))
    return out


def probe_path(path, bucket=BUCKET_ACTIVE):
    """探测一个插件目录或单文件插件。"""
    path = os.path.abspath(path)
    base = os.path.basename(path.rstrip(os.sep))
    if os.path.isdir(path):
        dirname = base
    else:
        dirname = base[:-3] if base.endswith(".py") else base
    p = Probe(bucket, dirname, path)

    entry = _entry_of(path)
    if not entry:
        p.error = "不是插件：目录里没有 __init__.py，也不是 .py 文件"
        return p
    p.entry = entry
    p.kind = "package" if os.path.isdir(path) else "module"

    _measure(p, path)
    p.readme = _readme_of(path if os.path.isdir(path) else os.path.dirname(path))

    src = _read(entry)
    if src is None:
        p.error = "入口文件读不出来：%s" % os.path.basename(entry)
        return p
    try:
        tree = ast.parse(src, filename=entry)
    except SyntaxError as e:
        p.error = "语法错误（第 %s 行）：%s" % (e.lineno or "?", e.msg)
        return p
    except Exception as e:                     # noqa: BLE001 - 解析器也可能抛出别的
        p.error = "解析失败：%s: %s" % (type(e).__name__, e)
        return p

    _fill_from_tree(p, tree, src, entry)
    if not p.error and not p.module_class:
        p.error = "没找到 Module 子类（插件得有一个继承 Module 的类）"
    return p


# ---------------------------------------------------------------- 填充

def _fill_from_tree(p, tree, src, entry):
    here = _scan_tree(tree)
    # 1) 入口文件里就有 Module 子类：最常见，直接取
    pick = _pick_class(here, src)
    if pick is not None:
        _apply(p, pick, entry)
        return

    # 2) 入口只是转发（from .module import Xxx / __getattr__ 里延迟导入）：
    #    去包内其它文件里找。只解析同级 .py，够覆盖「一个包多个文件」的写法。
    folder = os.path.dirname(entry)
    if not os.path.isdir(folder):
        return
    found = []
    for extra in _siblings(folder, entry):
        text = _read(extra)
        if not text:
            continue
        try:
            sub = ast.parse(text, filename=extra)
        except SyntaxError:
            continue
        info = _scan_tree(sub)
        for cls in info["classes"]:
            if cls["is_module"]:
                found.append((sub, text, cls, extra))
        if len(found) > 6:                     # 多个子类通常意味着有歧义，够了
            break

    if not found:
        return
    if len(found) == 1:
        sub, text, cls, extra = found[0]
        _apply(p, cls, extra, tree=sub)
        p.notes.append("模块类定义在 %s" % os.path.basename(extra))
        return

    # 多个候选：用入口文件里 import 进来的名字投票
    wanted = here["imported"] | {"MODULE", "PLUGIN"}
    for sub, text, cls, extra in found:
        if cls["name"] in wanted:
            _apply(p, cls, extra, tree=sub)
            p.notes.append("按入口导入名选中 %s（%s）"
                           % (cls["name"], os.path.basename(extra)))
            return
    sub, text, cls, extra = found[0]
    _apply(p, cls, extra, tree=sub)
    p.notes.append("包内有 %d 个 Module 子类，按顺序取了 %s"
                   % (len(found), cls["name"]))


def _apply(p, cls, file, tree=None):
    a = cls["attrs"]
    p.module_class = cls["name"]
    p.class_file = file
    p.id = _str(a.get("id"))
    p.title = _str(a.get("name"))
    p.version = _str(a.get("version") or a.get("__version__"))
    p.api_version = _str(a.get("api_version"))
    depends = a.get("depends") or a.get("requires")
    if isinstance(depends, (list, tuple)):
        p.depends = tuple(str(x) for x in depends if str(x))
    if isinstance(a.get("views"), (list, tuple)):
        p.views = tuple(v for v in a["views"] if isinstance(v, dict))
    p.assets = _str(a.get("assets"))
    if not p.id:
        p.notes.append("类 %s 没有声明 id，只能用目录名" % cls["name"])


def _scan_tree(tree):
    """把一棵语法树里我们要的东西捞出来：类、模块级赋值、导入名。

    分两遍：先把模块级字面量收集成常量表，再解析类体 —— 这样
    ``_ID = "x"`` + ``id = _ID`` 这种写法也读得出来（不少插件这么写）。
    """
    assign, imported = {}, set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    val = _literal(node.value)
                    if val is not None:
                        assign[tgt.id] = val
                    if isinstance(node.value, ast.Name):
                        imported.add(node.value.id)
                    elif isinstance(node.value, ast.Call):
                        func = node.value.func
                        if isinstance(func, ast.Name):
                            imported.add(func.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                val = _literal(node.value)
                if val is not None:
                    assign[node.target.id] = val
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.asname or alias.name.split(".")[-1])
        elif isinstance(node, ast.FunctionDef) and node.name == "__getattr__":
            # 延迟导入的写法：函数体里的 import 也算「入口导入名」
            for sub in ast.walk(node):
                if isinstance(sub, ast.ImportFrom):
                    for alias in sub.names:
                        imported.add(alias.asname or alias.name)

    classes = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "is_module": _inherits_module(node),
                "attrs": _class_attrs(node, assign),
            })
    return {"classes": classes, "assign": assign, "imported": imported}


def _pick_class(info, src):
    mods = [c for c in info["classes"] if c["is_module"] and _has_id(c)]
    if not mods:
        mods = [c for c in info["classes"] if c["is_module"]]
    if not mods:
        return None
    if len(mods) == 1:
        return mods[0]
    for key in _ENTRY_HINT:
        name = info["assign"].get(key)
        if isinstance(name, str):
            for c in mods:
                if c["name"] == name:
                    return c
    return mods[0]


def _has_id(cls):
    return bool(_str(cls["attrs"].get("id")))


def _inherits_module(node):
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id == _MODULE_BASE:
            return True
        if isinstance(base, ast.Attribute) and base.attr == _MODULE_BASE:
            return True
    return False


def _class_attrs(node, consts=None):
    """认两种写法：``id = "x"`` 与 ``id = _SOME_CONST``（常量在模块级定义）。"""
    consts = consts or {}
    out = {}
    for stmt in node.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if isinstance(tgt, ast.Name):
                    val = _resolve(stmt.value, consts)
                    if val is not None:
                        out[tgt.id] = val
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, ast.Name):
                val = _resolve(stmt.value, consts)
                if val is not None:
                    out[stmt.target.id] = val
    return out


def _resolve(node, consts):
    val = _literal(node)
    if val is not None:
        return val
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    return None


def _literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:                          # noqa: BLE001 - 变量/调用一律当没有
        return None


def _str(v):
    return v.strip() if isinstance(v, str) else ""


# ---------------------------------------------------------------- 文件小工具

def _entry_of(path):
    if os.path.isdir(path):
        init = os.path.join(path, "__init__.py")
        return init if os.path.isfile(init) else ""
    if os.path.isfile(path) and path.endswith(".py"):
        return path
    return ""


def _siblings(folder, entry):
    """入口同级的其它 .py（入口自己排最后，避免重复解析）。"""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".py") or name.startswith("."):
            continue
        full = os.path.join(folder, name)
        if os.path.abspath(full) == os.path.abspath(entry):
            continue
        out.append(full)
        if len(out) >= _SIBLING_LIMIT:
            break
    return out


def _read(path):
    try:
        if os.path.getsize(path) > _READ_MAX:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _measure(p, path):
    """体积 / 文件数 / 最后修改时间（跳过缓存与隐藏目录）。"""
    total = files = 0
    newest = 0.0
    if os.path.isfile(path):
        try:
            st = os.stat(path)
            p.size, p.files, p.mtime = st.st_size, 1, int(st.st_mtime)
        except OSError:
            pass
        return
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames
                       if d != "__pycache__" and not d.startswith(".")]
        for name in filenames:
            if name.endswith((".pyc", ".pyo")):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            total += st.st_size
            files += 1
            newest = max(newest, st.st_mtime)
            if files >= _SCAN_LIMIT:
                break
        if files >= _SCAN_LIMIT:
            break
    p.size, p.files, p.mtime = total, files, int(newest)


def _readme_of(folder):
    """读 README 的第一段，用于卡片上的一句话说明。"""
    for name in ("README.md", "README.MD", "readme.md", "README.txt", "README"):
        full = os.path.join(folder, name)
        if not os.path.isfile(full):
            continue
        text = _read(full)[:4000]
        if not text:
            return ""
        return _first_paragraph(text)
    return ""


def _first_paragraph(text):
    """取一句「这个插件是干什么的」。

    卡片上只放得下两行，所以取到第一个句号就停，最多 160 字 ——
    整段塞进去会让每张卡片都变成一堵字墙，反而看不清状态。
    """
    lines, total = [], 0
    for raw in text.splitlines():
        line = raw.strip().lstrip(">").strip()      # 引用块也当正文，别整段丢掉
        if not line:
            if lines:
                break
            continue
        if line.startswith(("#", "!", "[![", "<", "---", "===", "```", "|")):
            continue
        if line.startswith("*") and line.endswith("*") and len(line) < 80:
            continue
        lines.append(line)
        total += len(line)
        if total >= 160:
            break

    text = _join_wrapped(lines).replace("**", "").replace("`", "")
    for i, ch in enumerate(text):
        if ch in "。！？" and i >= 12:
            return text[:i + 1]
    return text[:160]


def _join_wrapped(lines):
    """把被换行折断的一行行接回一句话。

    不能直接用空格拼：中文在任意字符处都能断行，硬拼会得到「停用、 重载」
    这种多出来的空格，直接显示在卡片上。只有两边都是英文/数字时才需要空格。
    """
    out = ""
    for line in lines:
        if not out:
            out = line
            continue
        a, b = out[-1], line[0]
        gap = a.isascii() and b.isascii()
        out += (" " if gap else "") + line
    return out
