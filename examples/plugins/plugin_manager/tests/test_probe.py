# -*- coding: utf-8 -*-
"""静态探测的离线测试：不联网、不起服务、不 import 插件。

重点覆盖「常见但容易漏」的写法：

* 类属性直接写字面量；
* 类属性引用模块级常量（``id = _ID``）；
* 入口只做转发（``from .module import XxxModule``）；
* 入口用 PEP 562 的 ``__getattr__`` 延迟导入（arr_search 就是这么写的）；
* 单文件插件；
* 写坏的插件（语法错误 / 没有 Module 子类）。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, os.path.dirname(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from plugin_manager import probe  # noqa: E402

import _env  # noqa: E402


class ProbeCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pmprobe-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, text):
        full = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)
        return full

    # -------------------------------------------------- 三种声明写法
    def test_literal_attrs(self):
        self._write("a/__init__.py", (
            "from anime_rss.module import Module\n"
            "class A(Module):\n"
            "    id = 'aa'\n"
            "    name = '甲'\n"
            "    version = '1.2.3'\n"
            "    api_version = '1'\n"
            "    depends = ('feeds', 'reader')\n"
            "    views = ({'id': 'aa', 'title': 't', 'script': 'v.js'},)\n"
            "    assets = 'static'\n"))
        p = probe.probe_path(os.path.join(self.tmp, "a"))
        self.assertEqual(p.id, "aa")
        self.assertEqual(p.title, "甲")
        self.assertEqual(p.version, "1.2.3")
        self.assertEqual(p.api_version, "1")
        self.assertEqual(p.depends, ("feeds", "reader"))
        self.assertEqual(p.views[0]["id"], "aa")
        self.assertEqual(p.assets, "static")
        self.assertEqual(p.error, "", p.error)

    def test_constant_attrs(self):
        """id = _ID 这种写法：常量在模块级定义，也得读出来。"""
        self._write("b/__init__.py", (
            "from anime_rss.module import Module\n"
            "_ID = 'bb'\n"
            "_NAME = '乙'\n"
            "class B(Module):\n"
            "    id = _ID\n"
            "    name = _NAME\n"
            "    version = '2.0.0'\n"))
        p = probe.probe_path(os.path.join(self.tmp, "b"))
        self.assertEqual(p.id, "bb")
        self.assertEqual(p.title, "乙")
        self.assertEqual(p.version, "2.0.0")

    def test_forwarding_entry(self):
        """入口只有一行 from .module import Xxx，类在 module.py 里。"""
        self._write("c/__init__.py", (
            "from .module import CModule\n"
            "MODULE = CModule\n"))
        self._write("c/module.py", (
            "from anime_rss.module import Module\n"
            "class CModule(Module):\n"
            "    id = 'cc'\n"
            "    name = '丙'\n"))
        p = probe.probe_path(os.path.join(self.tmp, "c"))
        self.assertEqual(p.id, "cc", p.error)
        self.assertEqual(p.module_class, "CModule")
        self.assertTrue(p.class_file.endswith("module.py"))

    def test_lazy_getattr_entry(self):
        """arr_search 的写法：PEP 562 __getattr__ 延迟导入。"""
        self._write("d/__init__.py", (
            "def __getattr__(name):\n"
            "    if name in ('MODULE', 'DModule'):\n"
            "        from .impl import DModule\n"
            "        return DModule\n"
            "    raise AttributeError(name)\n"))
        self._write("d/impl.py", (
            "from anime_rss.module import Module\n"
            "class DModule(Module):\n"
            "    id = 'dd'\n"
            "    name = '丁'\n"
            "    version = '0.3.0'\n"))
        p = probe.probe_path(os.path.join(self.tmp, "d"))
        self.assertEqual(p.id, "dd", p.error)
        self.assertEqual(p.version, "0.3.0")

    def test_single_file_plugin(self):
        self._write("one.py", (
            "from anime_rss.module import Module\n"
            "class OneModule(Module):\n"
            "    id = 'one'\n"
            "    name = '单文件'\n"))
        p = probe.probe_path(os.path.join(self.tmp, "one.py"))
        self.assertEqual(p.kind, "module")
        self.assertEqual(p.dirname, "one")
        self.assertEqual(p.id, "one")

    # -------------------------------------------------- 坏插件
    def test_syntax_error_is_reported_not_raised(self):
        self._write("bad/__init__.py", "class B(Module)\n    pass\n")
        p = probe.probe_path(os.path.join(self.tmp, "bad"))
        self.assertIn("语法错误", p.error)
        self.assertEqual(p.id, "")          # 读不出来，但档案还在

    def test_no_module_class(self):
        self._write("empty/__init__.py", "X = 1\n")
        p = probe.probe_path(os.path.join(self.tmp, "empty"))
        self.assertIn("Module 子类", p.error)

    def test_not_a_plugin(self):
        os.makedirs(os.path.join(self.tmp, "plain"))
        p = probe.probe_path(os.path.join(self.tmp, "plain"))
        self.assertIn("__init__.py", p.error)

    # -------------------------------------------------- 目录扫描
    def test_buckets(self):
        self._write("plugins/arr_search/__init__.py", (
            "from anime_rss.module import Module\n"
            "class S(Module):\n    id = 'search'\n"))
        self._write("plugins/.disabled/old/__init__.py", (
            "from anime_rss.module import Module\n"
            "class O(Module):\n    id = 'old'\n"))
        self._write("plugins/.removed/gone@20260101-0000/__init__.py", (
            "from anime_rss.module import Module\n"
            "class G(Module):\n    id = 'gone'\n"))
        # 这些都不该被当成插件
        os.makedirs(os.path.join(self.tmp, "plugins", "__pycache__"))
        os.makedirs(os.path.join(self.tmp, "plugins", ".staging-abc"))
        self._write("plugins/.staging-abc/__init__.py", "x = 1\n")
        self._write("plugins/README.md", "not a plugin")

        got = probe.probe_root(os.path.join(self.tmp, "plugins"))
        self.assertEqual([p.id for p in got[probe.BUCKET_ACTIVE]], ["search"])
        self.assertEqual([p.id for p in got[probe.BUCKET_DISABLED]], ["old"])
        self.assertEqual([p.dirname for p in got[probe.BUCKET_REMOVED]],
                         ["gone@20260101-0000"])

    def test_readme_summary(self):
        self._write("r/__init__.py", "X = 1\n")
        self._write("r/README.md", (
            "# 标题\n\n[![badge](x)](y)\n\n> 一句话说明这个插件干什么。\n"
            "> 第二行。\n\n后面的正文不要。\n"))
        p = probe.probe_path(os.path.join(self.tmp, "r"))
        self.assertIn("一句话说明", p.readme)
        self.assertNotIn("标题", p.readme)

    def test_readme_join_does_not_insert_spaces_in_chinese(self):
        """中文在任意字处折行，拼回去不能多出空格（卡片上会看得见）。"""
        self._write("j/__init__.py", "X = 1\n")
        self._write("j/README.md", (
            "> 顺手给出停用、\n> 重载、卸载四个动作，判断依据是磁盘而不是记忆。\n"))
        p = probe.probe_path(os.path.join(self.tmp, "j"))
        self.assertEqual(p.readme, "顺手给出停用、重载、卸载四个动作，判断依据是磁盘而不是记忆。")
        self.assertNotIn("、 ", p.readme)

    def test_readme_join_keeps_space_between_english_words(self):
        """英文换行是断在词边界上的，拼回去要补空格，否则单词会粘一起。"""
        self._write("e/__init__.py", "X = 1\n")
        self._write("e/README.md", "> A tiny plugin for reading\n> feeds faster.\n")
        p = probe.probe_path(os.path.join(self.tmp, "e"))
        self.assertEqual(p.readme, "A tiny plugin for reading feeds faster.")

    def test_size_and_mtime(self):
        self._write("s/__init__.py", "x = 1\n")
        self._write("s/data/blob.txt", "y" * 500)
        self._write("s/__pycache__/junk.pyc", "z" * 900)
        p = probe.probe_path(os.path.join(self.tmp, "s"))
        self.assertEqual(p.files, 2)                 # 缓存目录不算
        self.assertGreaterEqual(p.size, 500)
        self.assertGreater(p.mtime, 0)


@unittest.skipUnless(_env.have_host(), "需要主程序在场才能测真实插件")
class RealPluginProbe(unittest.TestCase):
    """拿真实的 arr_search 对一遍：它用的是延迟导入写法。"""

    def test_arr_search(self):
        path = os.path.join(_env.PLUGINS_DIR, "arr_search")
        if not os.path.isdir(path):
            self.skipTest("没有 arr_search")
        p = probe.probe_path(path)
        self.assertEqual(p.id, "search")
        self.assertEqual(p.title, "深度搜索")
        self.assertEqual(p.module_class, "SearchModule")
        self.assertEqual(p.error, "")
        self.assertTrue(p.views)
        self.assertEqual(p.assets, "static")


if __name__ == "__main__":
    unittest.main(verbosity=2)
