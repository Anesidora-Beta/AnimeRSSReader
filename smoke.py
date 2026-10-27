#!/usr/bin/env python3
"""端到端冒烟测试 — 起一个临时实例，打一圈关键接口，断言行为，退出。

用途：任何重构（拆内核、拆模块）前后都跑一次，保证“功能没变”。

用法：
    python smoke.py                 # 默认随机端口 + 临时数据目录
    python smoke.py --verbose       # 打印每个请求
    python smoke.py --keep          # 保留临时数据目录，便于事后排查
    python smoke.py --port 8899     # 指定端口

退出码：0 全通过 / 1 有失败 / 2 起不来
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(BASE, "app.py")


def read_version():
    """从 app.py 源码里取 VERSION，避免测试里再写一份版本号。"""
    with open(APP, encoding="utf-8") as f:
        m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
    return m.group(1) if m else ""


class Checker:
    def __init__(self, verbose=False):
        self.rows = []
        self.verbose = verbose

    def check(self, name, ok, detail=""):
        self.rows.append((bool(ok), name, str(detail)))
        mark = "PASS" if ok else "FAIL"
        line = "  [%s] %s" % (mark, name)
        if detail and not ok:
            line += "  -> " + str(detail)[:160]
        print(line)
        return bool(ok)

    def eq(self, name, got, want):
        return self.check(name, got == want, "got %r, want %r" % (got, want))

    def summary(self):
        bad = [r for r in self.rows if not r[0]]
        print()
        print("  %d 项检查，%d 通过，%d 失败" % (len(self.rows), len(self.rows) - len(bad), len(bad)))
        for _, name, detail in bad:
            print("    - %s  (%s)" % (name, detail[:200]))
        return not bad


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def request(base, path, method="GET", body=None, timeout=10):
    """返回 (status, headers, bytes)。HTTP 错误码不抛异常。"""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()
    except Exception as e:                                    # 连接失败等
        return 0, {}, str(e).encode()


def jget(base, path, method="GET", body=None, timeout=10):
    status, _, raw = request(base, path, method, body, timeout)
    try:
        return status, json.loads(raw.decode("utf-8"))
    except Exception:
        return status, None


def wait_ready(base, proc, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        status, payload = jget(base, "/api/bootstrap", timeout=3)
        if status == 200 and isinstance(payload, dict) and payload.get("ok"):
            return True
        time.sleep(0.4)
    return False


def main():
    ap = argparse.ArgumentParser(description="AnimeRSSReader 冒烟测试")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--keep", action="store_true", help="保留临时数据目录")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    port = args.port or free_port()
    base = "http://127.0.0.1:%d" % port
    tmpdir = tempfile.mkdtemp(prefix="arr-smoke-")
    version = read_version()

    env = dict(os.environ)
    env["ANIME_RSS_DATA"] = tmpdir
    env["ANIME_RSS_QUIET"] = "1"
    env["PORT"] = str(port)

    print()
    print("  AnimeRSSReader 冒烟测试")
    print("  " + "-" * 46)
    print("  端口 %d   数据目录 %s" % (port, tmpdir))
    print("  期望版本 %s" % (version or "?"))
    print()

    proc = subprocess.Popen(
        [sys.executable, APP, "--port", str(port), "--no-browser", "--no-fetch"],
        cwd=BASE, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )

    ck = Checker(args.verbose)
    try:
        if not wait_ready(base, proc):
            err = b""
            if proc.poll() is not None and proc.stderr:
                err = proc.stderr.read() or b""
            print("  服务未能启动（退出码 %r）" % proc.poll())
            if err:
                print(err.decode("utf-8", "replace")[:800])
            return 2

        # ---------- 静态资源 ----------
        print("  静态资源")
        status, _, raw = request(base, "/")
        html = raw.decode("utf-8", "replace")
        ck.eq("首页 200", status, 200)
        ck.check("首页含设置弹窗容器", "modalSettings" in html)
        ck.check("首页含放送表入口", "新番放送表" in html)

        status, _, raw = request(base, "/assets/app.js?v=%s" % version)
        ck.eq("app.js 200", status, 200)
        ck.check("app.js 含放送表渲染", b"renderSchedule" in raw)
        ck.check("app.js 含设置逻辑", b"flushSettings" in raw)

        status, _, raw = request(base, "/assets/style.css")
        ck.eq("style.css 200", status, 200)
        ck.check("style.css 含放送表样式", b"sch-grid" in raw)

        status, _, _ = request(base, "/assets/icon.svg")
        ck.eq("icon.svg 200", status, 200)

        # 目录穿越不能读到源码
        status, _, raw = request(base, "/assets/../app.py")
        ck.check("拒绝目录穿越", status != 200 and b"VERSION" not in raw, "status=%s" % status)

        # ---------- 核心接口 ----------
        print()
        print("  核心接口")
        status, boot = jget(base, "/api/bootstrap")
        ck.eq("bootstrap 200", status, 200)
        ck.check("bootstrap.ok", isinstance(boot, dict) and boot.get("ok"))
        ck.eq("版本号一致", (boot or {}).get("version"), version)
        ck.check("有内置频道", len((boot or {}).get("feeds") or []) >= 3,
                 "feeds=%d" % len((boot or {}).get("feeds") or []))
        ck.check("有数据源清单", len((boot or {}).get("sources") or []) >= 3)
        ck.check("counts 结构完整", isinstance(((boot or {}).get("counts") or {}).get("total"), int))
        ck.check("settings 已注入 bootstrap", isinstance((boot or {}).get("settings"), dict))
        ck.check("schedule 摘要存在", "schedule" in (boot or {}))

        status, items = jget(base, "/api/items?limit=5")
        ck.eq("items 200", status, 200)
        ck.check("items 返回列表", isinstance((items or {}).get("items"), list))
        ck.check("items 带分页标记", "hasMore" in (items or {}))

        status, items = jget(base, "/api/items?filter=unread&limit=5")
        ck.eq("items?filter=unread 200", status, 200)

        status, _, raw = request(base, "/feed.xml")
        ck.eq("导出 RSS 200", status, 200)
        ck.check("导出 RSS 是 XML", raw.lstrip()[:5] in (b"<?xml", b"<rss>"))

        # ---------- 放送表 ----------
        print()
        print("  放送表")
        status, sch = jget(base, "/api/schedule", timeout=20)
        ck.eq("schedule 200", status, 200)
        days = (sch or {}).get("days") or []
        ck.eq("放送表 7 天", len(days), 7)
        ck.check("每天都有 items 字段", all(isinstance(d.get("items"), list) for d in days))
        ck.check("星期标签齐全", [d.get("label") for d in days][:2] == ["周一", "周二"])

        status, st = jget(base, "/api/schedule/status")
        ck.eq("schedule/status 200", status, 200)
        ck.check("status 含 total", isinstance((st or {}).get("total"), int))

        # ---------- 设置 ----------
        print()
        print("  设置")
        status, st = jget(base, "/api/settings")
        ck.eq("settings 200", status, 200)
        vals = (st or {}).get("values") or {}
        ck.check("设置项 >= 15", len(vals) >= 15, "实际 %d" % len(vals))
        ck.check("设置分组 >= 5", len((st or {}).get("schema") or []) >= 5)
        for key in ("refreshInterval", "theme", "fontSize", "keepDays", "closeAction"):
            ck.check("设置含 %s" % key, key in vals)

        status, saved = jget(base, "/api/settings", "POST", {"values": {"theme": "dark"}})
        ck.eq("保存设置 200", status, 200)
        ck.eq("保存后返回 dark", ((saved or {}).get("values") or {}).get("theme"), "dark")
        _, st = jget(base, "/api/settings")
        ck.eq("设置已持久化", ((st or {}).get("values") or {}).get("theme"), "dark")

        status, saved = jget(base, "/api/settings", "POST",
                             {"values": {"refreshInterval": 99999}})
        ck.eq("越界间隔被夹紧到 1440",
              ((saved or {}).get("values") or {}).get("refreshInterval"), 1440)

        status, saved = jget(base, "/api/settings", "POST", {"values": {"theme": "neon"}})
        ck.eq("非法主题回落 auto",
              ((saved or {}).get("values") or {}).get("theme"), "auto")

        status, reset = jget(base, "/api/settings/reset", "POST", {})
        ck.eq("恢复默认 200", status, 200)
        ck.eq("恢复默认后间隔 30",
              ((reset or {}).get("values") or {}).get("refreshInterval"), 30)

        # ---------- 维护与错误处理 ----------
        print()
        print("  维护 / 错误处理")
        status, m = jget(base, "/api/maintenance", "POST", {"action": "clean"})
        ck.eq("清理 200", status, 200)
        ck.check("清理返回 stats", isinstance((m or {}).get("stats"), dict))

        status, m = jget(base, "/api/maintenance", "POST", {"action": "vacuum"})
        ck.eq("压缩 200", status, 200)

        status, _ = jget(base, "/api/does-not-exist")
        ck.eq("未知接口 404", status, 404)

        status, _, _ = request(base, "/api/items", "POST", {})
        ck.check("GET-only 接口拒绝 POST", status in (404, 405), "status=%s" % status)

        # 重启后设置仍在（验证落库而不是内存）
        print()
        print("  持久化")
        jget(base, "/api/settings", "POST", {"values": {"theme": "dark"}})
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        proc = subprocess.Popen(
            [sys.executable, APP, "--port", str(port), "--no-browser", "--no-fetch"],
            cwd=BASE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if wait_ready(base, proc):
            _, st = jget(base, "/api/settings")
            ck.eq("重启后主题仍为 dark", ((st or {}).get("values") or {}).get("theme"), "dark")
        else:
            ck.check("重启后服务可用", False, "重启超时")

        ok = ck.summary()
        return 0 if ok else 1
    finally:
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass
        if args.keep:
            print("  临时目录保留：%s" % tmpdir)
        else:
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
