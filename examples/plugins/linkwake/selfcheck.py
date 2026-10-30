# -*- coding: utf-8 -*-
"""只读自检：这个东西在当前这台机器上到底能不能跑。

装完插件第一件事不是点开看，而是先问三个问题：

1. 文件齐不齐（少一个 view.js 就是一片空白）
2. 能不能 import（**顶层 import 缺模块 = 整趟插件加载失败**，一个插件
   报错会带走所有插件，这是踩过的真实事故）
3. 规则还认不认得主流网盘（域名表会过时，写了新规则没生效是常事）

跑到主程序本体上时，再加第四问：接口通不通，用的是
``python selfcheck.py --base http://127.0.0.1:8899``。这样连 exe 也能黑盒
验一遍（exe 里没有解释器，不可能在里面跑这个脚本）。

**这里不启动任何程序、不写任何文件、不发唤醒请求。** 只读。
"""
import argparse
import importlib
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = HERE

# 几个必须存在的文件：缺任何一个都不只是「不好看」
REQUIRED = [
    ("module.py", "模块主体"),
    ("rules.py", "规则层"),
    ("wake.py", "唤醒层"),
    ("clients.py", "下载器客户端"),
    ("__init__.py", "包入口"),
    (os.path.join("static", "view.js"), "界面脚本"),
    (os.path.join("static", "view.css"), "界面样式"),
]

# 顶层 import 一律走标准库的白名单：这些模块在 PyInstaller 单文件包里
# 一定存在（实测过）。``platform`` / ``html.parser`` 之类不在名单里的，
# 一旦被顶层 import，exe 里会 ModuleNotFoundError。
STDLIB_OK = ("os", "sys", "json", "re", "time", "threading", "subprocess",
             "webbrowser", "ctypes", "glob", "mimetypes", "sqlite3",
             "hashlib", "base64", "socket", "shutil", "urllib", "tempfile",
             "collections", "itertools", "typing", "traceback", "uuid")

# 认得出这些才算「主流网盘都覆盖了」；少一个就提示
SAMPLE_LINKS = {
    "magnet:?xt=urn:btih:0123456789abcdef": "magnet",
    "ed2k://|file|a.mkv|123|HASH|/": "ed2k",
    "https://pan.baidu.com/s/1abcdef": "baidu",
    "https://www.aliyundrive.com/s/abc": "alipan",
    "https://pan.quark.cn/s/abcdef": "quark",
    "https://www.123pan.com/s/abc-def": "pan123",
    "https://cloud.189.cn/t/abc": "tianyi",
    "https://115.com/s/abc": "pan115",
    "https://pan.xunlei.com/s/abc": "xunlei",
    "https://caiyun.139.com/m/i?abc": "cmcc",
    "https://share.weiyun.com/abc": "weiyun",
    "https://drive.uc.cn/s/abc": "uc",
    "https://wwa.lanzouq.com/iabc": "lanzou",
    "https://mypikpak.com/s/abc": "pikpak",
    "https://www.terabox.com/s/abc": "terabox",
    "https://cowtransfer.com/s/abc": "cowtransfer",
    "https://wenshushu.cn/f/abc": "wenshushu",
    "https://drive.google.com/file/d/abc/view": "gdrive",
    "https://1drv.ms/u/s!abc": "onedrive",
    "https://mega.nz/file/abc": "mega",
    "https://www.dropbox.com/s/abc/a.mkv": "dropbox",
}


class Report(object):
    def __init__(self):
        self.rows = []
        self.failed = 0

    def add(self, ok, title, detail=""):
        self.rows.append((bool(ok), title, detail))
        if not ok:
            self.failed += 1
        return ok

    def dump(self, as_json=False):
        if as_json:
            print(json.dumps({
                "ok": self.failed == 0,
                "failed": self.failed,
                "checks": [{"ok": o, "title": t, "detail": d}
                           for o, t, d in self.rows],
            }, ensure_ascii=False, indent=2))
            return
        group = ""
        for ok, title, detail in self.rows:
            g = title.split("/")[0]
            if g != group:
                group = g
                print("\n[%s]" % g)
            print("  %s %-34s %s" % ("OK " if ok else "!! ", title, detail))
        print("")
        if self.failed:
            print("有 %d 项没过。" % self.failed)
        else:
            print("全部通过（%d 项）。" % len(self.rows))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _bootstrap_repo():
    """往上找带 ``anime_rss/`` 的目录，加到 sys.path。

    装在 ``<repo>/plugins/linkwake`` 时这样就够；开发目录（插件在别处）找不到
    会返回空串，此时 ``module.py`` 导不进来是正常的 —— 但 ``rules`` / ``wake``
    这些仍然可用，不影响其余检查。
    """
    env = os.environ.get("ARR_REPO")
    start = os.path.abspath(env) if env else os.path.dirname(PLUGIN)
    cur = start
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, "anime_rss", "module.py")):
            if cur not in sys.path:
                sys.path.insert(0, cur)
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def check_env(r):
    r.add(os.name in ("nt", "posix"), "环境/os", os.name)
    ver = "%d.%d.%d" % sys.version_info[:3]
    r.add(sys.version_info >= (3, 8), "环境/python", ver)
    frozen = bool(getattr(sys, "frozen", False))
    r.add(True, "环境/运行方式", "打包 exe" if frozen else "解释器")


_IMP_RE = re.compile(r"^(?:import|from)\s+([A-Za-z_][\w.]*)", re.M)


def check_toplevel_imports(r):
    """静态扫一遍：顶层 import 有没有碰到 exe 里不存在的模块。

    这一项比「本机能不能 import」重要得多。exe 是单文件 PyInstaller 包，
    只带主程序真正 import 过的标准库；插件顶层 import 一个没被打进去的
    模块，会 ModuleNotFoundError，而**插件发现是整趟 try 的** —— 一个插件
    炸掉，所有插件一起消失（踩过：一个 ``import uuid`` 让插件管理器与搜索
    同时不见了）。

    所以只认白名单，不认「我这台机器上有」。
    """
    bad = []
    for rel in ("__init__.py", "module.py", "rules.py", "wake.py",
                "clients.py"):
        path = os.path.join(PLUGIN, rel)
        if not os.path.isfile(path):
            continue
        try:
            src = _read(path)
        except Exception as e:                        # noqa: BLE001
            bad.append("%s 读不了：%s" % (rel, e))
            continue
        for name in _IMP_RE.findall(src):
            root = name.split(".")[0]
            if root in ("anime_rss", "rules", "wake", "clients", "module"):
                continue                    # 包内 / 主程序，主程序保证有
            if root in STDLIB_OK:
                continue
            bad.append("%s: import %s" % (rel, root))
    r.add(not bad, "环境/顶层 import 都在白名单",
          "OK" if not bad else "；".join(sorted(set(bad))[:5]))


def check_files(r):
    for rel, what in REQUIRED:
        p = os.path.join(PLUGIN, rel)
        ok = os.path.isfile(p)
        size = os.path.getsize(p) if ok else 0
        r.add(ok and size > 0, "文件/%s" % rel,
              ("%s，%d 字节" % (what, size)) if ok else "缺这个文件：%s" % what)
    for rel in ("README.md", os.path.join("tests", "test_rules.py")):
        p = os.path.join(PLUGIN, rel)
        r.add(os.path.isfile(p), "文件/%s" % rel, "可选，但建议有")


def check_imports(r):
    """逐个 import。少了依赖就在这儿显形，而不是在用户点开界面的时候。"""
    if PLUGIN not in sys.path:
        sys.path.insert(0, PLUGIN)
    repo = _bootstrap_repo()
    r.add(bool(repo), "导入/找到主程序",
          repo or "没找到（只在 plugins/ 下才有，rules 仍可单独用）")
    names = []
    for mod in ("rules", "wake", "clients"):
        try:
            __import__(mod)
            names.append(mod)
            r.add(True, "导入/%s" % mod, "OK")
        except Exception as e:                        # noqa: BLE001
            r.add(False, "导入/%s" % mod, "%s: %s" % (type(e).__name__, e))

    # module.py 走相对导入，只有「当包导入」才成立 —— 这正是插件发现的加载
    # 方式（目录名当包名）。按顶层模块 import 会得到
    # attempted relative import with no known parent package，那是假故障。
    parent, pkg = os.path.dirname(PLUGIN), os.path.basename(PLUGIN)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    try:
        p = importlib.import_module(pkg)
        klass = getattr(p, "MODULE")        # PEP 562 延迟导入，到这一步才真加载
        r.add(bool(klass), "导入/%s.MODULE" % pkg,
              "OK（%s v%s）" % (getattr(klass, "name", "?"),
                               getattr(klass, "version", "?")))
    except ImportError as e:
        r.add(False, "导入/%s.MODULE" % pkg,
              "缺主程序：%s（把插件放进主程序的 plugins/ 下再跑）" % e)
    except Exception as e:                        # noqa: BLE001
        r.add(False, "导入/%s.MODULE" % pkg, "%s: %s" % (type(e).__name__, e))
    return names


def check_rules(r):
    try:
        import rules
    except Exception:                                 # noqa: BLE001
        r.add(False, "规则/加载", "rules 没导进来，跳过")
        return
    n = len(rules.PROVIDERS)
    r.add(n >= 20, "规则/提供方数量", "%d 个" % n)
    ids = set(p["id"] for p in rules.PROVIDERS)
    r.add(len(ids) == n, "规则/id 不重复", "去重后 %d 个" % len(ids))

    bad = []
    for url, want in SAMPLE_LINKS.items():
        got = rules.classify(url)[0]
        if got != want:
            bad.append("%s→%s（期望 %s）" % (url[:38], got, want))
    r.add(not bad, "规则/主流网盘都认得",
          "全部认对" if not bad else "；".join(bad[:4]))

    # 认不出来的必须老实回落 web，不能瞎猜成某个网盘
    pid, _ = rules.classify("https://example.com/s/abc")
    r.add(pid == "web", "规则/陌生域名回落网页", pid)

    # 抽取顺序：磁力要在最前（前端第一眼看到的就是它）
    got = rules.extract({"magnet": "magnet:?xt=urn:btih:abc",
                         "link": "https://bgm.tv/subject/1",
                         "summary": "https://pan.baidu.com/s/1x 提取码: abcd"})
    order = [x["kind"] for x in got]
    r.add(bool(got) and order[0] == "bt", "规则/磁力排最前", "→".join(order))
    pwd = [x["pwd"] for x in got if x["pid"] == "baidu"]
    r.add(pwd == ["abcd"], "规则/自动补提取码", pwd[0] if pwd else "没补上")


def check_wake(r):
    try:
        import wake
    except Exception:                                 # noqa: BLE001
        r.add(False, "唤醒/加载", "wake 没导进来，跳过")
        return
    ok_urls = ["magnet:?xt=urn:btih:abc", "https://pan.baidu.com/s/1x",
               "ed2k://|file|a|1|H|/", "thunder://QUFodHRwOi8v"]
    bad = [u for u in ok_urls if not _passes(wake, u)]
    r.add(not bad, "唤醒/放行正常链接", "%d 条全过" % len(ok_urls) if not bad
          else "被误拒：%s" % "、".join(bad))

    deny = ["javascript:alert(1)", "file:///C:/Windows/win.ini",
            "data:text/html,x", r"\\evil\share\a.exe", "smb://x/y", ""]
    leak = [u for u in deny if _passes(wake, u)]
    r.add(not leak, "唤醒/挡住危险链接",
          "全部拒绝" if not leak else "漏了：%s" % "、".join(leak[:3]))

    r.add(wake.is_local_host("127.0.0.1:8899"), "唤醒/认本机 Host", "127.0.0.1")
    r.add(not wake.is_local_host("192.168.1.9:8899"),
          "唤醒/拦住局域网 Host", "192.168.1.9")

    # 只验证模板切分，不真启动
    argv = wake.split_cmd('--x "{url}" --flag')
    r.add(argv == ["--x", "{url}", "--flag"], "唤醒/参数模板切分",
          " ".join(argv))


def _passes(wake, url):
    try:
        wake.check_url(url)
        return True
    except wake.WakeError:
        return False
    except Exception:                                 # noqa: BLE001
        return False


def check_clients(r):
    try:
        import clients
    except Exception:                                 # noqa: BLE001
        r.add(False, "客户端/加载", "clients 没导进来，跳过")
        return
    for bad in ("", "ftp://x/y", "javascript:alert(1)"):
        try:
            clients.check_endpoint(bad, "qb")
            r.add(False, "客户端/拒绝非法地址", repr(bad))
        except clients.ClientError:
            r.add(True, "客户端/拒绝非法地址", repr(bad) if bad else "空地址")
    # 私有地址必须绕过系统代理：代理没启动时会报 502 而不是「端口没人监听」
    r.add(hasattr(clients, "_NO_PROXY"), "客户端/本机地址绕过代理", "已内置")


def check_targets(r):
    """「优先响应」那张表要自洽 —— 它决定了点一下就交给谁。

    这几条都是**纯逻辑**：不启动程序、不碰网络。表要是配错了（比如网盘也能
    选 qB），用户点了只会得到一句看不懂的报错。
    """
    if not _bootstrap_repo():
        r.add(True, "偏好/跳过", "没有主程序可导入，跳过")
        return
    parent, pkg = os.path.dirname(PLUGIN), os.path.basename(PLUGIN)
    for p in (parent, PLUGIN):
        if p not in sys.path:
            sys.path.insert(0, p)
    try:
        mod = importlib.import_module(pkg + ".module")
    except Exception as e:                            # noqa: BLE001
        r.add(False, "偏好/加载", "缺主程序：%s" % e)
        return

    targets = {t["id"]: t for t in getattr(mod, "TARGETS", ())}
    if not targets:
        r.add(False, "偏好/目标表", "空的")
        return

    kinds = ("bt", "pan", "file", "web")
    missing = [k for k in kinds
               if not all(t in targets for t in ("auto", "copy", "ask"))
               or any(k not in targets[t]["kinds"] for t in ("auto", "copy", "ask"))]
    r.add(not missing, "偏好/兜底选项齐全",
          "四类都有 自动/只复制/每次都问" if not missing else missing)

    bad = [t["id"] for t in targets.values()
           if t["id"] in ("qb", "aria2")
           and set(t["kinds"]) - {"bt", "file"}]
    r.add(not bad, "偏好/下载器只挂 BT 与直链",
          "网盘不可能进 qB 队列" if not bad else bad)

    try:
        m = mod.LinkWakeModule()
        cfg = m._default_cfg()
        prefs = cfg.get("prefs") or {}
        r.add(set(prefs) == set(kinds) and all(v == "auto" for v in prefs.values()),
              "偏好/默认不改老手感", ", ".join("%s=%s" % (k, prefs.get(k))
                                               for k in kinds))
        r.add(m._pref("pan", {"prefs": {"pan": "qb"}}) == "auto",
              "偏好/拒绝不可能的搭配", "pan+qb → auto")
        r.add(m._pref("bt", {"prefs": {"bt": "ask"}}) == "ask",
              "偏好/记住「每次都问」", "bt=ask")
    except Exception as e:                            # noqa: BLE001
        r.add(False, "偏好/实例化", "%s: %s" % (type(e).__name__, e))


def check_config(r):
    """配置只在**读**的时候碰一下：坏了会回落默认值，不该让插件起不来。"""
    path = os.path.join(PLUGIN, "linkwake.json")
    if not os.path.isfile(path):
        r.add(True, "配置/当前目录无配置文件", "用默认值（正常）")
        return
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        r.add(isinstance(cfg, dict) and "apps" in cfg, "配置/可解析", path)
    except Exception as e:                        # noqa: BLE001
        r.add(False, "配置/可解析", "%s: %s" % (type(e).__name__, e))


def check_live(r, base):
    """对着一个正在跑的主程序做黑盒检查（exe 也能这么验）。"""
    base = base.rstrip("/")
    if "://" not in base:
        base = "http://" + base
    for path, want in (("/api/linkwake", "entries"),
                       ("/api/linkwake/apps", "cfg"),
                       ("/api/linkwake/apps", "targets")):
        url = base + path
        try:
            req = urllib.request.Request(url, headers={"Host": "127.0.0.1"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            ok = resp.status == 200 and body.get("ok") and want in body
            detail = "200"
            if ok and want == "entries":
                detail = "%d 个条目，%d 条链接" % (
                    len(body.get("entries") or []),
                    (body.get("counts") or {}).get("links", 0))
            r.add(ok, "在线/%s" % path.lstrip("/"), detail)
        except urllib.error.HTTPError as e:
            r.add(False, "在线/%s" % path.lstrip("/"), "HTTP %s" % e.code)
        except Exception as e:                        # noqa: BLE001
            r.add(False, "在线/%s" % path.lstrip("/"), "%s: %s" % (type(e).__name__, e))

    # 远端来源必须被挡：用 X-Forwarded-Host 冒充局域网来试探
    try:
        req = urllib.request.Request(
            base + "/api/linkwake/wake",
            data=json.dumps({"url": "magnet:?xt=urn:btih:abc"}).encode(),
            headers={"Content-Type": "application/json",
                     "Host": "192.168.1.9:8899"})
        urllib.request.urlopen(req, timeout=8)
        r.add(False, "在线/远端唤醒被拒", "居然放行了")
    except urllib.error.HTTPError as e:
        # 403 才是对的；400 说明先被链接校验拦下，也不算漏
        r.add(e.code in (400, 403), "在线/远端唤醒被拒", "HTTP %d" % e.code)
    except Exception as e:                        # noqa: BLE001
        r.add(False, "在线/远端唤醒被拒", "%s: %s" % (type(e).__name__, e))


def main(argv=None):
    ap = argparse.ArgumentParser(description="链接唤醒插件自检（只读）")
    ap.add_argument("--base", default="", metavar="URL",
                    help="主程序地址，如 http://127.0.0.1:8899；给了才做在线检查")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出结果行数")
    args = ap.parse_args(argv)

    r = Report()
    check_env(r)
    check_toplevel_imports(r)
    check_files(r)
    check_imports(r)
    check_rules(r)
    check_wake(r)
    check_clients(r)
    check_targets(r)
    check_config(r)
    if args.base:
        check_live(r, args.base)

    if args.quiet:
        print("selfcheck: %d 项，失败 %d" % (len(r.rows), r.failed))
    else:
        r.dump(as_json=args.json)
    return 0 if r.failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
