#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用户数据（单文件夹 + zip 备份/恢复）的单元测试。

要点：
  * 导出的是**一致性快照**，运行中导也不会拿到半个库；
  * 恢复前一定先备份旧数据；
  * 乱七八糟的文件不能把库冲掉（校验不过就一个字都不改）；
  * 老版本的 data/reader.db 会被搬进 userdata/。

跑法： ``python -m unittest discover -s tests -v``
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store as ST                                     # noqa: E402
from anime_rss import userdata as UD                   # noqa: E402
from anime_rss.app import create_app                    # noqa: E402


def _make_db(path, items=3, starred=1, watches=1, title="测试源"):
    """建一个有点内容的库，返回 (store, 真实条目 id 列表)。

    条目 id 是跨站指纹（哈希），不是调用方传的 id，所以这里回查一次。
    """
    st = ST.Store(path)
    st.add_feed("custom:1", title, "custom", {}, "https://example.com/feed.xml")
    rows = [{"id": "t%d" % i, "title": "条目 %d" % i, "ts": 1000 + i,
             "link": "https://example.com/%d" % i} for i in range(items)]
    st.upsert_items("custom:1", rows)
    ids = [it["id"] for it in st.query_items(feed="custom:1", limit=50)]
    for i in ids[:starred]:
        st.set_star(i, True)
    for _ in range(watches):
        st.add_watch("某部番", "关键词")
    st.meta_set("cfg.theme", json.dumps("dark"))
    return st, ids


class UserDataDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-ud-")
        self.user = os.path.join(self.tmp, "userdata")
        self.runtime = os.path.join(self.tmp, "data")
        os.makedirs(self.user)
        os.makedirs(self.runtime)
        self.store, self.ids = _make_db(UD.db_path(self.user))

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------- 导出
    def test_export_zip_contains_db_and_manifest(self):
        name, blob = UD.export_zip(UD.db_path(self.user), version="2.1.0")
        self.assertTrue(name.startswith("AnimeRSS-userdata-"))
        self.assertTrue(name.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            names = z.namelist()
            self.assertIn(UD.DB_NAME, names)
            self.assertIn(UD.MANIFEST_NAME, names)
            manifest = json.loads(z.read(UD.MANIFEST_NAME).decode("utf-8"))
        self.assertEqual(manifest["version"], "2.1.0")
        self.assertEqual(manifest["stats"]["items"], 3)
        self.assertEqual(manifest["stats"]["starred"], 1)

    def test_export_zip_is_a_snapshot_not_a_file_copy(self):
        """刚写进去还没 checkpoint 的内容也要在备份里（走备份接口而不是拷文件）"""
        self.store.set_star(self.ids[0], False)
        self.store.set_star(self.ids[2], True)
        _name, blob = UD.export_zip(UD.db_path(self.user))
        staged, _manifest, stats = UD.unpack(blob)
        try:
            con = sqlite3.connect(staged)
            got = con.execute("SELECT starred FROM items WHERE id=?",
                              (self.ids[2],)).fetchone()[0]
            con.close()
            self.assertEqual(got, 1)
            self.assertEqual(stats["items"], 3)
        finally:
            UD.cleanup(staged)

    def test_export_to_directory_and_file(self):
        out, size = UD.export_to(self.tmp, UD.db_path(self.user))
        self.assertTrue(out.endswith(".zip"))
        self.assertGreater(size, 0)
        target = os.path.join(self.tmp, "手动名字.zip")
        out2, _ = UD.export_to(target, UD.db_path(self.user))
        self.assertEqual(out2, target)
        with self.assertRaises(UD.UserDataError):
            UD.export_to(target, UD.db_path(self.user))   # 不覆盖已有文件

    # ---------------------------------------------------------- 校验
    def test_reject_garbage(self):
        with self.assertRaises(UD.UserDataError):
            UD.unpack(b"hello world")

    def test_reject_zip_without_db(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("readme.txt", "没有数据库")
        with self.assertRaises(UD.UserDataError) as ctx:
            UD.unpack(buf.getvalue())
        self.assertIn(UD.DB_NAME, str(ctx.exception))

    def test_reject_db_without_tables(self):
        bare = os.path.join(self.tmp, "bare.db")
        con = sqlite3.connect(bare)
        con.execute("CREATE TABLE whatever (x INTEGER)")
        con.commit()
        con.close()
        with self.assertRaises(UD.UserDataError) as ctx:
            UD.unpack(bare)
        self.assertIn("数据表", str(ctx.exception))

    def test_reject_newer_schema(self):
        newer = os.path.join(self.tmp, "newer.db")
        st, _ids = _make_db(newer)
        st._db.execute("PRAGMA user_version=%d" % (ST.SCHEMA_VERSION + 1))
        st._db.commit()
        st.close()
        with self.assertRaises(UD.UserDataError) as ctx:
            UD.unpack(newer, max_version=ST.SCHEMA_VERSION)
        self.assertIn("更新版本", str(ctx.exception))

    def test_reject_broken_sqlite(self):
        bad = os.path.join(self.tmp, "bad.db")
        with open(bad, "wb") as f:
            f.write(UD.SQLITE_MAGIC + b"\x00" * 2048)
        with self.assertRaises(UD.UserDataError):
            UD.unpack(bad)

    # ---------------------------------------------------------- 恢复
    def test_backup_then_restore_roundtrip(self):
        _name, blob = UD.export_zip(UD.db_path(self.user))
        # 把当前库改脏，再用备份覆盖回来
        self.store.set_star(self.ids[0], True)
        self.store.set_star(self.ids[2], True)
        self.assertEqual(self.store.counts()["starred"], 2)

        backup = UD.backup(self.user, os.path.join(self.runtime, "backups"))
        self.assertTrue(os.path.isfile(os.path.join(backup, UD.DB_NAME)))

        staged, _manifest, stats = UD.unpack(blob)
        try:
            self.store.reload_from(staged)
        finally:
            UD.cleanup(staged)

        self.assertEqual(stats["items"], 3)
        self.assertEqual(self.store.counts()["starred"], 1)   # 回到备份那一刻
        self.assertEqual(self.store.meta_get("cfg.theme"), '"dark"')

    def test_unpack_from_dir(self):
        staged, manifest, stats = UD.unpack(self.user)
        try:
            self.assertEqual(stats["items"], 3)
            self.assertEqual(manifest["stats"]["watches"], 1)
        finally:
            UD.cleanup(staged)

    def test_stats_of_missing_db_raises(self):
        with self.assertRaises(UD.UserDataError):
            UD.unpack(os.path.join(self.tmp, "nope.zip"))

    # ---------------------------------------------------------- 迁移
    def test_migrate_legacy_moves_db_and_logs_stay(self):
        legacy = os.path.join(self.tmp, "old", "data")
        fresh = os.path.join(self.tmp, "old", "userdata")
        os.makedirs(os.path.join(legacy, "logs"))
        st, _ids = _make_db(os.path.join(legacy, UD.DB_NAME))
        st.close()
        with open(os.path.join(legacy, "hosts.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(legacy, "logs", "app.log"), "w", encoding="utf-8") as f:
            f.write("old log")

        moved = UD.migrate_legacy(legacy, fresh)
        self.assertIn(UD.DB_NAME, moved)
        self.assertTrue(os.path.isfile(UD.db_path(fresh)))
        self.assertTrue(os.path.isfile(os.path.join(fresh, "hosts.json")))
        self.assertFalse(os.path.isfile(os.path.join(legacy, UD.DB_NAME)))
        self.assertTrue(os.path.isfile(os.path.join(legacy, "logs", "app.log")))

        # 再跑一次不会做任何事，也不会把已有的新库覆盖掉
        self.assertEqual(UD.migrate_legacy(legacy, fresh), [])

    def test_migrate_skips_when_target_exists(self):
        self.assertFalse(os.path.exists(UD.db_path(os.path.join(self.tmp, "x"))))
        other = os.path.join(self.tmp, "x")
        os.makedirs(other)
        st, _ids = _make_db(UD.db_path(other))
        st.close()
        self.assertEqual(UD.migrate_legacy(self.user, other), [])


class UserDataApiTest(unittest.TestCase):
    """接口层：导出给浏览器下载、导入整体换库、信息接口"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arr-udapi-")
        self.app = create_app(data_dir=os.path.join(self.tmp, "userdata"),
                              runtime_dir=os.path.join(self.tmp, "data"),
                              log=None)
        self.app.store.add_feed("custom:9", "接口源", "custom", {},
                                "https://example.com/f.xml")
        self.app.store.upsert_items("custom:9", [
            {"id": "a1", "title": "接口条目", "ts": 2000}])
        self.aid = self.app.store.query_items(feed="custom:9", limit=10)[0]["id"]
        self.app.store.set_star(self.aid, True)

    def tearDown(self):
        self.app.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_info(self):
        resp = self.app.handle("GET", "/api/userdata")
        data = json.loads(resp.body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["dir"], self.app.ctx.data_dir)
        self.assertEqual(data["stats"]["items"], 1)
        self.assertIn("backupDir", data)

    def test_export_sets_download_headers(self):
        resp = self.app.handle("GET", "/api/userdata/export")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.ctype, "application/zip")
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(resp.body)) as z:
            self.assertIn(UD.DB_NAME, z.namelist())

    def test_import_replaces_data_and_keeps_backup(self):
        blob = self.app.handle("GET", "/api/userdata/export").body
        # 导出之后再改一笔，导入后应该回到导出那一刻
        self.app.store.set_star(self.aid, False)
        self.app.store.upsert_items("custom:9", [
            {"id": "a2", "title": "导出之后才有的", "ts": 3000}])
        self.assertEqual(self.app.store.counts()["total"], 2)

        resp = self.app.handle("POST", "/api/userdata/import", body=blob)
        data = json.loads(resp.body)
        self.assertEqual(resp.status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["stats"]["items"], 1)
        self.assertTrue(os.path.isdir(data["backup"]))
        self.assertTrue(os.path.isfile(os.path.join(data["backup"], UD.DB_NAME)))
        # 库真的换了（换成新连接读出来也是旧内容）
        self.assertEqual(self.app.store.counts()["total"], 1)
        self.assertEqual(self.app.store.counts()["starred"], 1)

    def test_import_rejects_junk_without_touching_db(self):
        before = self.app.store.counts()
        resp = self.app.handle("POST", "/api/userdata/import", body=b"not a zip")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.app.store.counts(), before)

    def test_import_empty_body(self):
        resp = self.app.handle("POST", "/api/userdata/import", body=b"")
        self.assertEqual(resp.status, 400)

    def test_settings_reload_after_import(self):
        """设置也跟着换库走：导入后内存里那份不能还是旧的"""
        self.app.settings.save({"theme": "dark"})
        blob = self.app.handle("GET", "/api/userdata/export").body
        self.app.settings.save({"theme": "light"})
        self.assertEqual(self.app.settings.get()["theme"], "light")
        self.app.handle("POST", "/api/userdata/import", body=blob)
        self.assertEqual(self.app.settings.get()["theme"], "dark")


if __name__ == "__main__":
    unittest.main()
