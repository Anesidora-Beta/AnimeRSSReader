# -*- coding: utf-8 -*-
"""用户数据：单独一个文件夹装「换台电脑也要带走」的东西。

目录约定（桌面版）::

    <程序目录>/userdata/        用户数据 —— 整个文件夹拷走就等于迁移
        reader.db               订阅源 / 条目 / 已读 / 收藏 / 关注 / 设置
        hosts.json              记下来的镜像可用状态（丢了会重新探测）
    <程序目录>/data/            本机运行残留，不用迁移
        .window/                浏览器配置（窗口模式用）
        logs/                   运行日志
        backups/                每次导入前的自动备份
        exports/                --export-userdata 的默认落点

导出的备份是一个 zip。里面那份 ``reader.db`` **不是直接拷文件**，而是走
SQLite 的在线备份接口取的一致性快照 —— 库是 WAL 模式，运行中直接拷
会漏掉还在 ``-wal`` 里没落盘的那部分，恢复后可能少掉最近的已读状态。

导入先备份当前数据，再整体换库；校验不过就一个字都不动。
"""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile

DB_NAME = "reader.db"
HOSTS_NAME = "hosts.json"
MANIFEST_NAME = "userdata.json"
STAMP_FMT = "%Y%m%d-%H%M%S"

#: 导入时必须存在的表：缺一个就说明不是本程序的数据
REQUIRED_TABLES = ("feeds", "items", "item_feeds", "watches",
                   "watch_items", "schedule", "meta")

SQLITE_MAGIC = b"SQLite format 3\x00"
ZIP_MAGIC = b"PK\x03\x04"


class UserDataError(Exception):
    """数据包本身有问题（不是 zip、不是本程序的库、版本太新……）"""


# ---------------------------------------------------------------- 小工具

def db_path(user_dir):
    """用户数据目录 → 库文件路径"""
    return os.path.join(user_dir, DB_NAME)


def human_size(n):
    """字节数转成人看的大小，界面上直接用"""
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _rm(path):
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def snapshot(db_file, dest=None):
    """取库的一致性快照（包含 WAL 里还没落盘的部分），返回快照路径"""
    if not os.path.isfile(db_file):
        raise UserDataError("找不到数据库：%s" % db_file)
    dest = dest or os.path.join(tempfile.mkdtemp(prefix="arr-snap-"), DB_NAME)
    try:
        src = sqlite3.connect(db_file, timeout=15)
        try:
            dst = sqlite3.connect(dest)
            try:
                with dst:
                    src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as e:
        _rm(os.path.dirname(dest))
        raise UserDataError("这个库打不开（%s），文件可能已经损坏" % e)
    return dest


# ---------------------------------------------------------------- 读统计

def stats_of(db_file):
    """从库文件里读几个数字，用来在界面上显示「备份里有什么」"""
    con = sqlite3.connect(db_file, timeout=15)
    try:
        def one(sql):
            try:
                row = con.execute(sql).fetchone()
                return int(row[0] or 0)
            except sqlite3.Error:
                return 0
        return {
            "items": one("SELECT COUNT(*) FROM items"),
            "feeds": one("SELECT COUNT(*) FROM feeds"),
            "watches": one("SELECT COUNT(*) FROM watches"),
            "starred": one("SELECT COUNT(*) FROM items WHERE starred=1"),
            "unread": one("SELECT COUNT(*) FROM items WHERE read=0"),
            "dbBytes": os.path.getsize(db_file),
        }
    finally:
        con.close()


def make_manifest(db_file, app="", version=""):
    """备份包的身份证：谁导的、什么时候、里面有多少东西"""
    return {
        "app": app or "anime-rss-reader",
        "version": version,
        "exported": int(time.time()),
        "exportedText": time.strftime("%Y-%m-%d %H:%M:%S"),
        "db": DB_NAME,
        "stats": stats_of(db_file),
    }


# ---------------------------------------------------------------- 导出

def export_zip(db_file, app="", version="", filename=""):
    """打一个备份 zip，返回 (文件名, zip 字节)"""
    snap = snapshot(db_file)
    try:
        manifest = make_manifest(snap, app=app, version=version)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(snap, DB_NAME)
            z.writestr(MANIFEST_NAME,
                       json.dumps(manifest, ensure_ascii=False, indent=1))
            hosts = os.path.join(os.path.dirname(db_file), HOSTS_NAME)
            if os.path.isfile(hosts):
                z.write(hosts, HOSTS_NAME)
    finally:
        _rm(os.path.dirname(snap))
    name = filename or "AnimeRSS-userdata-%s.zip" % time.strftime(STAMP_FMT)
    return name, buf.getvalue()


def export_to(path, db_file, app="", version=""):
    """导出到磁盘：``path`` 是目录就以默认文件名放进去，是 .zip 就直接写"""
    if os.path.isdir(path):
        name, blob = export_zip(db_file, app=app, version=version)
        dest = os.path.join(path, name)
    else:
        if os.path.exists(path):
            raise UserDataError("目标文件已存在：%s" % path)
        parent = os.path.dirname(os.path.abspath(path)) or "."
        if not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        name, blob = export_zip(db_file, app=app, version=version,
                                filename=os.path.basename(path))
        dest = path
    with open(dest, "wb") as f:
        f.write(blob)
    return dest, len(blob)


# ---------------------------------------------------------------- 校验 / 解包

def _check_db(db_file, max_version=0):
    """确认这确实是本程序的库，返回 (manifest 风格的信息, 统计)"""
    with open(db_file, "rb") as f:
        if f.read(16) != SQLITE_MAGIC:
            raise UserDataError("这个文件不是 SQLite 数据库，可能不是本程序的备份")
    con = sqlite3.connect(db_file, timeout=15)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        if str(ok).lower() != "ok":
            raise UserDataError("数据库校验没通过（%s），这个备份可能损坏了" % ok)
        version = int(con.execute("PRAGMA user_version").fetchone()[0] or 0)
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if max_version and version > max_version:
        raise UserDataError(
            "这份数据来自更新版本的程序（库版本 %d，本程序只认到 %d），"
            "请先升级程序再导入" % (version, max_version))
    missing = [t for t in REQUIRED_TABLES if t not in tables]
    if missing:
        raise UserDataError("备份里缺少必要的数据表：%s" % "、".join(missing))
    return version, stats_of(db_file)


def unpack(source, max_version=0):
    """把备份解成一份临时库，返回 (临时库路径, manifest, 统计)。

    ``source`` 可以是 zip 字节、裸库字节，或者磁盘上的 zip / db 路径。
    调用方负责删除临时库（连同所在目录）。
    """
    tmpdir = tempfile.mkdtemp(prefix="arr-import-")
    try:
        if isinstance(source, (bytes, bytearray)):
            blob = bytes(source)
            if blob[:4] == ZIP_MAGIC:
                return _unpack_zip(io.BytesIO(blob), tmpdir, max_version)
            if blob[:16] == SQLITE_MAGIC:
                db_file = os.path.join(tmpdir, DB_NAME)
                with open(db_file, "wb") as f:
                    f.write(blob)
                version, stats = _check_db(db_file, max_version)
                return db_file, {"version": version, "stats": stats}, stats
            raise UserDataError("看不懂这个文件：既不是 zip 备份也不是数据库")
        # 磁盘路径
        if os.path.isdir(source):
            cand = os.path.join(source, DB_NAME)
            if not os.path.isfile(cand):
                raise UserDataError("这个文件夹里没有 %s，不像是用户数据目录" % DB_NAME)
            db_file = snapshot(cand, os.path.join(tmpdir, DB_NAME))
            version, stats = _check_db(db_file, max_version)
            manifest = {}
            mpath = os.path.join(source, MANIFEST_NAME)
            if os.path.isfile(mpath):
                try:
                    with open(mpath, encoding="utf-8") as f:
                        manifest = json.load(f)
                except (OSError, ValueError):
                    manifest = {}
            manifest.setdefault("version", version)
            manifest.setdefault("stats", stats)
            return db_file, manifest, stats
        if not os.path.isfile(source):
            raise UserDataError("找不到备份文件：%s" % source)
        if source.lower().endswith(".zip"):
            with open(source, "rb") as f:
                return _unpack_zip(io.BytesIO(f.read()), tmpdir, max_version)
        db_file = snapshot(source, os.path.join(tmpdir, DB_NAME))
        version, stats = _check_db(db_file, max_version)
        return db_file, {"version": version, "stats": stats}, stats
    except Exception:
        _rm(tmpdir)
        raise


def _unpack_zip(fh, tmpdir, max_version):
    try:
        z = zipfile.ZipFile(fh)
    except zipfile.BadZipFile:
        raise UserDataError("这个 zip 打不开，可能下载/拷贝时损坏了")
    with z:
        names = z.namelist()
        member = ""
        for name in names:
            base = os.path.basename(name)
            if base == DB_NAME:
                member = name
                break
        if not member:
            raise UserDataError("备份里没有 %s，可能不是「导出用户数据」的包" % DB_NAME)
        # 只按名字取这一个成员，别的条目一律不解压（防止奇怪的路径）
        db_file = os.path.join(tmpdir, DB_NAME)
        with z.open(member) as src, open(db_file, "wb") as dst:
            shutil.copyfileobj(src, dst)
        manifest = {}
        if MANIFEST_NAME in names:
            try:
                manifest = json.loads(z.read(MANIFEST_NAME).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                manifest = {}
    version, stats = _check_db(db_file, max_version)
    manifest.setdefault("version", version)
    manifest.setdefault("stats", stats)
    return db_file, manifest, stats


# ---------------------------------------------------------------- 备份 / 落地

def cleanup(db_file):
    """扔掉 ``unpack()`` 产生的临时库（连它所在的临时目录一起删）"""
    if not db_file:
        return
    _rm(os.path.dirname(os.path.abspath(db_file)))


def backup(user_dir, backup_root, stamp=""):
    """把当前用户数据整份备份到 ``backup_root/userdata-<时间>/``，返回目录"""
    if not os.path.isfile(db_path(user_dir)):
        return ""
    stamp = stamp or time.strftime(STAMP_FMT)
    dest = os.path.join(backup_root, "userdata-" + stamp)
    os.makedirs(dest, exist_ok=True)
    snap = snapshot(db_path(user_dir))
    try:
        shutil.copyfile(snap, os.path.join(dest, DB_NAME))
    finally:
        _rm(os.path.dirname(snap))
    for name in (HOSTS_NAME, MANIFEST_NAME):
        src = os.path.join(user_dir, name)
        if os.path.isfile(src):
            try:
                shutil.copyfile(src, os.path.join(dest, name))
            except OSError:
                pass
    return dest


def write_manifest(user_dir, app="", version=""):
    """在用户数据目录里放一份「这是什么」的说明，方便人肉识别这个文件夹"""
    try:
        with open(os.path.join(user_dir, MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(make_manifest(db_path(user_dir), app=app, version=version),
                      f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def migrate_legacy(old_dir, new_dir):
    """老版本把用户数据放在 data/：挪到新的 userdata/ 去，只挪一次。

    返回被挪动的文件名列表（空列表表示没动）。
    """
    if not old_dir or not new_dir:
        return []
    if os.path.abspath(old_dir) == os.path.abspath(new_dir):
        return []
    src = db_path(old_dir)
    if not os.path.isfile(src) or os.path.isfile(db_path(new_dir)):
        return []
    os.makedirs(new_dir, exist_ok=True)
    moved = []
    for name in (DB_NAME, DB_NAME + "-wal", DB_NAME + "-shm", HOSTS_NAME):
        s = os.path.join(old_dir, name)
        if not os.path.isfile(s):
            continue
        try:
            shutil.move(s, os.path.join(new_dir, name))
            moved.append(name)
        except OSError:
            # 跨盘移动失败就退化成复制，源文件留着不影响使用
            try:
                shutil.copyfile(s, os.path.join(new_dir, name))
                moved.append(name)
            except OSError:
                pass
    return moved
