"""护栏：插件顶层 import 只能用「exe 里真的有」的标准库。

为什么单列一个文件
------------------
exe 版是 PyInstaller 单文件包，只会带上主程序（``anime_rss`` 与几个入口脚本）
真正 import 过的标准库。插件是运行时从磁盘加载的源码，**不在打包分析范围内**，
所以插件一旦在模块顶层 ``import`` 了主程序没用过的库，frozen 环境里就会
``ModuleNotFoundError``。

更麻烦的是插件发现是整趟 ``try`` 的：一个插件抛错，**所有**插件都装不上。
真实事故：本插件在顶层 ``import uuid``，exe 里没打进去，结果插件管理器与
arr_search 同时消失，界面上什么都看不到，日志只有一行「插件发现失败」。
"""
import ast
import os
import sys
import unittest

import _env


def _module_level_imports(path):
    """只取模块顶层的 import（函数里的懒加载不会拖垮发现流程）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return set()

    found = set()

    def walk(body):
        for node in body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    found.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:      # 相对 import 是自家代码
                    found.add(node.module.split(".")[0])
            elif isinstance(node, (ast.If, ast.Try)):
                walk(node.body)
                walk(getattr(node, "orelse", []))
                walk(getattr(node, "finalbody", []))
                for h in getattr(node, "handlers", []):
                    walk(h.body)

    walk(tree.body)
    return found


def _host_imports(repo):
    """主程序 import 过的东西 —— 打包时一定会被带上。"""
    mods = set()
    for base, dirs, files in os.walk(os.path.join(repo, "anime_rss")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                mods |= _module_level_imports(os.path.join(base, f))
    for name in os.listdir(repo):
        if name.endswith(".py"):
            mods |= _module_level_imports(os.path.join(repo, name))
    return mods


# 主程序没 import、但已实测在发布版 exe 里可用（plugins/_modprobe 探过）。
# 加新条目务必先在 exe 里验一遍，别凭「应该是标准库」想当然。
EXTRA_OK = {
    "ast",            # 本插件的静态探测层，PyInstaller 会顺带带上
    "calendar",
    "secrets",
    "dataclasses",
    "typing",
    "textwrap",
    "inspect",
    "pathlib",
    "binascii",
    "queue",
    "collections",
    "functools",
    "itertools",
    "string",
    "weakref",
    "contextlib",
}


class FrozenCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = _env.find_repo()
        cls.here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_no_module_needs_lib_missing_from_frozen_exe(self):
        if not self.repo:
            self.skipTest("需要主程序在场才能算出它带了哪些模块")
        allowed = _host_imports(self.repo)
        stdlib = set(sys.stdlib_module_names)

        offenders = {}
        for name in sorted(os.listdir(self.here)):
            if not name.endswith(".py"):
                continue
            got = _module_level_imports(os.path.join(self.here, name))
            bad = sorted(m for m in got
                         if m in stdlib and m not in allowed and m not in EXTRA_OK)
            if bad:
                offenders[name] = bad

        self.assertEqual(offenders, {}, (
            "这些顶层 import 在发布版 exe 里不存在，会让**所有**插件一起加载失败："
            "%r。改用 EXTRA_OK 里已实测可用的模块，或改成函数内懒加载。"
            % offenders))

    def test_uuid_is_not_used(self):
        """点名守一下那次事故：uuid 在 exe 里确实缺失。"""
        bad = _module_level_imports(os.path.join(self.here, "control.py"))
        self.assertNotIn("uuid", bad,
                         "exe 里没有 uuid；用 control._rand() 代替")

    def test_rand_token_shape(self):
        """_rand 是 uuid 的替代品，长度与字符集要对。"""
        if not self.repo:
            self.skipTest("control.py 依赖 anime_rss.module")
        parent = os.path.dirname(self.here)          # plugins/ 这一层
        added = [parent, self.repo]
        for p in added:
            sys.path.insert(0, p)
        try:
            from plugin_manager.control import _rand
        finally:
            for p in added:
                sys.path.remove(p)
            for mod in [m for m in sys.modules if m.startswith("plugin_manager")]:
                sys.modules.pop(mod, None)
        got = _rand(10)
        self.assertRegex(got, r"^[0-9a-f]{10}$")
        self.assertNotEqual(_rand(10), _rand(10))
        self.assertEqual(len(_rand(3)), 3)


if __name__ == "__main__":
    unittest.main()
