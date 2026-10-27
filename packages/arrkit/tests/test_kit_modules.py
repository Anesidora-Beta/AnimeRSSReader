import unittest

from arrkit.context import Context
from arrkit.module import (API_VERSION, Module, ModuleError, ModuleRegistry,
                           load_modules, resolve_order, unload_modules)


def make_module(mid, depends=(), order=None, fail=False):
    class M(Module):
        id = mid
        name = mid.upper()
        version = "1.0.0"

    M.depends = tuple(depends)
    if fail:
        def setup(self, ctx):
            raise RuntimeError("setup 失败")
        M.setup = setup
    if order is not None:
        def setup(self, ctx):
            order.append(mid)
        M.setup = setup
    return M


class ModuleOrderTest(unittest.TestCase):
    def test_dependency_order(self):
        order = []
        mods = [make_module("c", ["b"], order), make_module("b", ["a"], order),
                make_module("a", (), order)]
        resolved = resolve_order([m() for m in mods])
        self.assertEqual([m.id for m in resolved], ["a", "b", "c"])

    def test_missing_dependency(self):
        with self.assertRaises(ModuleError):
            resolve_order([make_module("x", ["ghost"])()])

    def test_cycle_detected(self):
        with self.assertRaises(ModuleError):
            resolve_order([make_module("a", ["b"])(), make_module("b", ["a"])()])

    def test_duplicate_id(self):
        with self.assertRaises(ModuleError):
            resolve_order([make_module("a")(), make_module("a")()])


class LoadTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(bus=None, jobs=None, views=None, log=None)

    def test_load_reverse_teardown(self):
        done = []

        class A(Module):
            id, name = "a", "A"

            def teardown(self):
                done.append("a")

        class B(Module):
            id, name, depends = "b", "B", ("a",)

            def teardown(self):
                done.append("b")

        reg = load_modules(self.ctx, [B, A])
        self.assertEqual(reg.ids(), ["a", "b"])
        self.assertIs(self.ctx.modules, reg)
        self.assertIs(reg.get("a").ctx, self.ctx)      # 实例上被注入了 ctx
        unload_modules(reg)
        self.assertEqual(done, ["b", "a"])

    def test_teardown_error_isolated(self):
        class Bad(Module):
            id, name = "bad", "bad"

            def teardown(self):
                raise RuntimeError("x")

        class Good(Module):
            id, name = "good", "good"

            def teardown(self):
                self.__class__.done = True

        reg = load_modules(self.ctx, [Bad, Good])
        self.assertEqual(unload_modules(reg), 1)   # 坏的那个不计入

    def test_missing_id(self):
        class NoId(Module):
            name = "无名"

        with self.assertRaises(ModuleError):
            load_modules(self.ctx, [NoId])

    def test_api_version_mismatch(self):
        class Old(Module):
            id, name = "old", "old"
            api_version = "0"

        with self.assertRaises(ModuleError) as cm:
            load_modules(self.ctx, [Old])
        self.assertIn(str(API_VERSION), str(cm.exception))

    def test_setup_error_wrapped(self):
        with self.assertRaises(ModuleError) as cm:
            load_modules(self.ctx, [make_module("boom", fail=True)])
        self.assertIn("boom", str(cm.exception))

    def test_class_and_instance_both_ok(self):
        reg = load_modules(self.ctx, [make_module("a"), make_module("b")()])
        self.assertEqual(reg.ids(), ["a", "b"])
        self.assertTrue(reg.has("a"))
        self.assertEqual(reg.get("nope"), None)
        self.assertEqual(reg.get("nope", 1), 1)

    def test_bootstrap_and_settings_hooks_are_optional(self):
        class Plain(Module):
            id, name = "plain", "plain"

        reg = load_modules(self.ctx, [Plain])
        mod = reg.get("plain")
        self.assertIsNone(mod.bootstrap(self.ctx))
        self.assertIsNone(mod.apply_settings({}))


if __name__ == "__main__":
    unittest.main()
