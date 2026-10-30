# -*- coding: utf-8 -*-
"""测试共用：找主程序、起一个临时应用。

插件可能装在两个位置，测试都要能跑：

    <repo>/plugins/linkwake/tests              ← 正式安装
    <任意开发目录>/plugins/linkwake/tests        ← 改代码的地方

所以不写死 ``../..``，而是一路往上找带 ``anime_rss/`` 的目录；也认
``ARR_REPO`` 环境变量与同级的 ``AnimeRSSReader``。
"""
import os
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PLUGINS_DIR = os.path.dirname(PLUGIN)


def _looks_like_repo(path):
    return (path and os.path.isdir(os.path.join(path, "anime_rss"))
            and os.path.isfile(os.path.join(path, "anime_rss", "module.py")))


def find_repo():
    for start in (PLUGIN, PLUGINS_DIR):
        cur = os.path.abspath(start)
        for _ in range(5):
            if _looks_like_repo(cur):
                return cur
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent

    env = os.environ.get("ARR_REPO")
    if env and _looks_like_repo(env):
        return os.path.abspath(env)

    for base in (os.path.dirname(PLUGINS_DIR),
                 os.path.dirname(os.path.dirname(PLUGINS_DIR))):
        for name in ("AnimeRSSReader", "anime_rss_reader"):
            cand = os.path.join(base, name)
            if _looks_like_repo(cand):
                return cand
    return ""


REPO = find_repo()
if REPO and REPO not in sys.path:
    sys.path.insert(0, REPO)

LOCAL_HOST = {"Host": "127.0.0.1:8899"}
REMOTE_HOST = {"Host": "192.168.1.20:8899"}


def have_host():
    return bool(REPO)


def make_app(tmpdir, extra_plugins=None):
    """在临时数据目录里起一个真应用，插件目录指向本插件所在的那一层。

    只用 ``create_app`` 的公开参数，不碰主程序内部 —— 这样主程序改实现时
    测试仍然有效（改契约才会红）。
    """
    from anime_rss.app import create_app
    data = os.path.join(tmpdir, "data")
    os.makedirs(data, exist_ok=True)
    return create_app(data_dir=data, base_dir=REPO,
                      plugin_dirs=[PLUGINS_DIR] + list(extra_plugins or ()),
                      log=lambda m: None)


def seed_items(tmpdir, items):
    """往临时库里塞条目，返回 data 目录。

    字段与 ``store._row`` 一致；``links`` 传 list 会自动转 JSON。
    """
    import json
    data = os.path.join(tmpdir, "data")
    os.makedirs(data, exist_ok=True)
    db = os.path.join(data, "reader.db")

    # 复用主程序自己的建表脚本，别手写 schema —— 手写会随主程序演进而失效
    from store import Store
    st = Store(db)
    try:
        feed = st.add_feed("t-feed", "测试源", "custom")
        st.upsert_items(feed if isinstance(feed, str) else "t-feed", items)
    finally:
        st.close()
    return data


def call(app, method, path, body=None, headers=None):
    """发一个请求并返回 (status, json)。body 给 dict 会自动序列化。"""
    import json
    raw = b""
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
    resp = app.handle(method, path, raw, headers or dict(LOCAL_HOST))
    try:
        data = json.loads(resp.body) if resp.body else None
    except ValueError:
        data = None
    return resp.status, data


def rm(path):
    shutil.rmtree(path, ignore_errors=True)


def query(db_path, sql, args=()):
    con = sqlite3.connect(db_path)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()
