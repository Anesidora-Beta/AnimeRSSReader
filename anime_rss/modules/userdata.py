"""用户数据模块：导出备份、从备份恢复、打开数据文件夹。

「用户数据」指换了电脑也要带走的那一份：订阅源、条目、已读、收藏、
关注、设置。它们全在 ``userdata/reader.db`` 里，所以把整个文件夹拷到
新环境就等于迁移；不方便拷文件夹时，就走这里的 zip 备份 / 恢复。

导出用 SQLite 在线备份接口取快照，运行中导出也不会拿到半个库；
恢复前会先把当前数据整份备份到 ``<runtime>/backups/``。
"""
import os
import subprocess
import sys
import time

from store import SCHEMA_VERSION

from ..common import apply_settings
from ..kernel.http import Response
from ..module import Module
from .. import userdata as UD

ZIP = "application/zip"


class UserDataModule(Module):
    id = "userdata"
    name = "用户数据"
    version = "1.0.0"

    def setup(self, ctx):
        r = ctx.router
        r.get("/api/userdata", self.api_info)
        r.get("/api/userdata/export", self.api_export)
        r.post("/api/userdata/import", self.api_import)
        r.post("/api/userdata/reveal", self.api_reveal)

    # ------------------------------------------------------------ 位置
    def backup_dir(self):
        return os.path.join(self.ctx.runtime_dir or self.ctx.data_dir, "backups")

    def _backups(self):
        root = self.backup_dir()
        if not os.path.isdir(root):
            return []
        out = []
        for name in sorted(os.listdir(root), reverse=True):
            full = os.path.join(root, name)
            if not name.startswith("userdata-") or not os.path.isdir(full):
                continue
            db = os.path.join(full, UD.DB_NAME)
            out.append({"name": name, "path": full,
                        "bytes": os.path.getsize(db) if os.path.isfile(db) else 0})
        return out

    # ------------------------------------------------------------ 接口
    def api_info(self, req):
        ctx = self.ctx
        st = ctx.store.stats() if ctx.store else {}
        return Response.json({
            "ok": True,
            "dir": ctx.data_dir,
            "db": ctx.db_path,
            "runtimeDir": ctx.runtime_dir or ctx.data_dir,
            "backupDir": self.backup_dir(),
            "stats": st,
            "size": UD.human_size(st.get("dbBytes") or 0),
            "backups": self._backups()[:5],
            "schemaVersion": SCHEMA_VERSION,
        })

    def api_export(self, req):
        """打一个 zip 直接给浏览器下载（不落地，省得占空间）"""
        ctx = self.ctx
        try:
            name, blob = UD.export_zip(ctx.db_path, app=ctx.app_name,
                                       version=ctx.version)
        except UD.UserDataError as e:
            return Response.err(str(e))
        except OSError as e:
            return Response.err("导出失败：%s" % e, 500)
        return Response(blob, ZIP, 200, "no-store", {
            "Content-Disposition": 'attachment; filename="%s"' % name,
            "X-UserData-Bytes": str(len(blob)),
        })

    def api_import(self, req):
        """收一个 zip（请求体就是文件字节），校验 → 备份 → 整体换库"""
        ctx = self.ctx
        blob = req.body
        if not blob:
            return Response.err("没有收到备份文件")
        try:
            staged, manifest, stats = UD.unpack(blob, max_version=SCHEMA_VERSION)
        except UD.UserDataError as e:
            return Response.err(str(e))
        except Exception as e:                        # noqa: BLE001
            return Response.err("这个备份读不出来：%s" % e, 400)
        backup = ""
        try:
            backup = UD.backup(ctx.data_dir, self.backup_dir())
            ctx.store.reload_from(staged)
            vals = ctx.settings.reload()
            apply_settings(ctx, vals)                 # 间隔 / 关窗行为跟着换成新库的
            if ctx.auto is not None:
                ctx.auto.last_try = time.time()
            UD.write_manifest(ctx.data_dir, app=ctx.app_name, version=ctx.version)
            ctx.say("已从备份恢复用户数据（恢复前备份：%s）" % (backup or "无"))
            return Response.json({
                "ok": True,
                "backup": backup,
                "manifest": manifest,
                "stats": ctx.store.stats(),
                "counts": ctx.store.counts(),
            })
        except Exception as e:                        # noqa: BLE001
            return Response.err("恢复失败：%s" % e, 500)
        finally:
            UD.cleanup(staged)

    def api_reveal(self, req):
        """在文件管理器里打开用户数据文件夹（双击 exe 的用户不用自己找路径）"""
        path = self.ctx.data_dir
        try:
            if not os.path.isdir(path):
                return Response.err("文件夹还不存在：%s" % path)
            if os.name == "nt":
                os.startfile(path)                    # noqa: S606 - 打开自己的数据目录
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as e:                        # noqa: BLE001
            return Response.err("打不开文件夹：%s" % e, 500)
        return Response.json({"ok": True, "dir": path})
