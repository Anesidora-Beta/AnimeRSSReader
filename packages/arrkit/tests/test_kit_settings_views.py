import os
import shutil
import tempfile
import unittest

from arrkit.settings import SettingError, Settings
from arrkit.views import ViewError, ViewRegistry


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arrkit-set-")
        self.path = os.path.join(self.tmp, "settings.json")
        self.s = Settings(path=self.path)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_define_defaults_and_order(self):
        self.s.define("a", [{"key": "a.x", "type": "int", "default": 3}])
        self.s.define("b", [{"key": "b.x", "type": "str", "default": "hi"}])
        self.s.define("c", [{"key": "c.x", "type": "bool", "default": True}], after="a")
        self.assertEqual([g["id"] for g in self.s.schema()], ["a", "c", "b"])
        self.assertEqual(self.s.get("a.x"), 3)
        self.assertEqual(self.s.get(), {"a.x": 3, "b.x": "hi", "c.x": True})

    def test_define_twice_merges(self):
        self.s.define("g", [{"key": "k1", "default": 1}])
        self.s.define("g", [{"key": "k2", "default": 2}])
        keys = [it["key"] for it in self.s.items()]
        self.assertEqual(keys, ["k1", "k2"])
        self.assertEqual(len(self.s.schema()), 1)

    def test_coerce_and_clamp(self):
        self.s.define("g", [
            {"key": "n", "type": "int", "default": 5, "min": 1, "max": 10},
            {"key": "f", "type": "float", "default": 1.5},
            {"key": "b", "type": "bool", "default": False},
            {"key": "t", "type": "text", "default": ""},
        ])
        values, changed = self.s.update({"n": 999, "f": "2.5", "b": "yes", "t": 7})
        self.assertEqual(values["n"], 10)
        self.assertEqual(values["f"], 2.5)
        self.assertTrue(values["b"])
        self.assertEqual(values["t"], "7")
        self.assertEqual(sorted(changed), ["b", "f", "n", "t"])

    def test_bad_value_keeps_default(self):
        self.s.define("g", [{"key": "n", "type": "int", "default": 5}])
        values, changed = self.s.update({"n": "abc"})
        self.assertEqual(values["n"], 5)
        self.assertEqual(changed, [])

    def test_select_rejects_unknown(self):
        self.s.define("g", [{"key": "m", "type": "select", "default": "a",
                             "options": ["a", "b"]}])
        self.assertEqual(self.s.update({"m": "a"})[0]["m"], "a")
        self.assertEqual(self.s.update({"m": "zzz"})[0]["m"], "a")

    def test_unknown_key_ignored(self):
        self.s.define("g", [{"key": "n", "type": "int", "default": 1}])
        values, changed = self.s.update({"nope": 1})
        self.assertNotIn("nope", values)
        self.assertEqual(changed, [])

    def test_persist_and_reload(self):
        self.s.define("g", [{"key": "n", "type": "int", "default": 1}])
        self.s.update({"n": 7})
        again = Settings(path=self.path)
        again.define("g", [{"key": "n", "type": "int", "default": 1}])
        self.assertEqual(again.get("n"), 7)

    def test_reset(self):
        self.s.define("g", [{"key": "n", "type": "int", "default": 1}])
        self.s.update({"n": 9})
        self.assertEqual(self.s.reset(["n"]), ["n"])
        self.assertEqual(self.s.get("n"), 1)
        self.assertEqual(self.s.reset(), [])

    def test_bad_declaration(self):
        for bad in ({"type": "int"}, {"key": "k", "type": "nope"},
                    {"key": "k", "type": "select"}):
            with self.assertRaises(SettingError):
                self.s.define("g", [bad])

    def test_in_memory_mode(self):
        s = Settings()
        s.define("g", [{"key": "k", "type": "str", "default": "v"}])
        s.update({"k": "w"})
        self.assertEqual(s.get("k"), "w")
        self.assertIsNone(s.save())


class ViewRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arrkit-view-")
        self.dir = os.path.join(self.tmp, "static")
        os.makedirs(os.path.join(self.dir, "js"))
        with open(os.path.join(self.dir, "view.js"), "wb") as f:
            f.write(b"window.x=1")
        with open(os.path.join(self.dir, "js", "deep.js"), "wb") as f:
            f.write(b"deep")
        self.reg = ViewRegistry()
        self.reg.mount("demo", self.dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mount_missing_dir(self):
        self.assertFalse(self.reg.mount("x", os.path.join(self.tmp, "nope")))
        self.assertFalse(self.reg.mount("", self.dir))

    def test_register_builds_urls(self):
        v = self.reg.register("demo", {"id": "demo", "title": "示例", "script": "view.js"})
        self.assertEqual(v.script, "/plug/demo/view.js")
        self.assertEqual(self.reg.manifest()[0]["id"], "demo")

    def test_bad_id_and_duplicate(self):
        with self.assertRaises(ViewError):
            self.reg.register("demo", {"id": "Bad"})
        self.reg.register("demo", {"id": "ok"})
        with self.assertRaises(ViewError):
            self.reg.register("demo", {"id": "ok"})
        with self.assertRaises(ViewError):
            self.reg.register("demo", "not a dict")

    def test_missing_script(self):
        with self.assertRaises(ViewError):
            self.reg.register("demo", {"id": "x", "script": "nope.js"})

    def test_read_and_traversal(self):
        self.assertIsNotNone(self.reg.read("demo", "view.js"))
        self.assertIsNotNone(self.reg.read("demo", "js/deep.js"))
        for bad in ("../demo/view.js", "../../etc/passwd", "/etc/passwd",
                    "js/../../x", "", "nope.js"):
            self.assertIsNone(self.reg.read("demo", bad), bad)
        self.assertIsNone(self.reg.read("unknown", "view.js"))

    def test_order_and_clear(self):
        self.reg.register("demo", {"id": "b", "order": 5})
        self.reg.register("demo", {"id": "a", "order": 5})
        self.assertEqual([v.id for v in self.reg.all()], ["a", "b"])
        self.assertIn("a", self.reg)
        self.reg.clear()
        self.assertEqual(len(self.reg), 0)
        self.assertIsNone(self.reg.static_dir("demo"))


if __name__ == "__main__":
    unittest.main()
