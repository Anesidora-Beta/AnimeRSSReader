"""装完自检：确认主程序真的看见了插件管理器，并且它看见了别的插件。

用法（在程序目录下跑，插件已经放进 ``plugins/``）::

    python plugins/plugin_manager/selfcheck.py            # 只看加载与状态
    python plugins/plugin_manager/selfcheck.py --json     # 给脚本用
    python plugins/plugin_manager/selfcheck.py --base D:\\AnimeRSSReader

它**只读**：用临时目录当 data_dir，不碰你现有的订阅、收藏和插件状态；
也不会移动任何插件目录。
"""
import argparse
import json
import os
import sys
import tempfile


def find_base(start):
    """往上找带 anime_rss/ 的目录（源码版与 exe 版都适用）。"""
    cur = os.path.abspath(start)
    for _ in range(6):
        if os.path.isdir(os.path.join(cur, "anime_rss")) and \
                os.path.isfile(os.path.join(cur, "anime_rss", "module.py")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    env = os.environ.get("ARR_REPO")
    if env and os.path.isdir(os.path.join(env, "anime_rss")):
        return os.path.abspath(env)
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="程序目录（含 anime_rss/ 的那一层）")
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    plugins = os.path.dirname(here)
    base = os.path.abspath(args.base) if args.base else (
        find_base(here) or find_base(os.getcwd()))

    report = {
        "pluginDir": here,
        "pluginsDir": plugins,
        "base": base,
        "ok": False,
        "problems": [],
    }

    if not base:
        report["problems"].append(
            "找不到主程序目录（应该有 anime_rss/ 子目录）；用 --base 指定")
        return _out(report, args.json, 2)

    sys.path.insert(0, base)
    try:
        from anime_rss import API_VERSION, __version__
        from anime_rss.app import create_app
    except Exception as e:                      # noqa: BLE001
        report["problems"].append("导入主程序失败：%s: %s" % (type(e).__name__, e))
        return _out(report, args.json, 2)

    report["coreVersion"] = __version__
    report["apiVersion"] = str(API_VERSION)

    data = tempfile.mkdtemp(prefix="pm-selfcheck-")
    app = None
    try:
        app = create_app(data_dir=data, base_dir=base,
                         plugin_dirs=[plugins], log=None)
        modules = list(app.ctx.modules.ids())
        report["modules"] = modules
        report["loaded"] = "pluginmgr" in modules
        if not report["loaded"]:
            report["problems"].append(
                "pluginmgr 没有加载：检查 id 是否与别的模块重名，"
                "或临时设 ANIME_RSS_PLUGIN_DEBUG=1 重启看详细报错")

        views = [v.id for v in app.views] if app.views else []
        report["views"] = views
        if "pluginmgr" not in views:
            report["problems"].append("视图没注册上：static/view.js 可能读不到")

        resp = app.handle("GET", "/api/pluginmgr")
        report["apiStatus"] = resp.status
        if resp.status != 200:
            report["problems"].append("GET /api/pluginmgr 返回 %d" % resp.status)
        else:
            data_json = json.loads(resp.body)
            report["env"] = data_json.get("env")
            report["counts"] = data_json.get("counts")
            report["plugins"] = [
                {"id": e["id"], "name": e["name"], "state": e["state"],
                 "builtin": e["builtin"], "routes": len(e["routes"])}
                for e in data_json.get("entries") or []
            ]
            report["issues"] = [
                {"name": i["name"], "level": i["level"], "text": i["text"]}
                for i in data_json.get("issues") or []
            ]

        # 前端资源能不能取到（装的如果是打包版，静态目录最容易漏）
        asset = app.handle("GET", "/plug/pluginmgr/view.js")
        report["assetStatus"] = asset.status
        if asset.status != 200:
            report["problems"].append(
                "静态资源 /plug/pluginmgr/view.js 取不到（%d），界面会打不开"
                % asset.status)

        report["ok"] = not report["problems"]
        return _out(report, args.json, 0 if report["ok"] else 1)
    except Exception as e:                      # noqa: BLE001
        report["problems"].append("启动主程序失败：%s: %s" % (type(e).__name__, e))
        return _out(report, args.json, 2)
    finally:
        if app is not None:
            try:
                app.stop()
            except Exception:                   # noqa: BLE001
                pass
        import shutil
        shutil.rmtree(data, ignore_errors=True)


def _out(report, as_json, code):
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return code

    ok = report.get("ok")
    print("")
    print("  插件管理器自检")
    print("  " + "-" * 56)
    print("  插件目录   %s" % report.get("pluginDir", "?"))
    print("  主程序     %s%s" % (
        report.get("base") or "（没找到）",
        "  v%s / 契约 v%s" % (report.get("coreVersion"), report.get("apiVersion"))
        if report.get("coreVersion") else ""))
    print("  已加载     %s" % ("是" if report.get("loaded") else "否"))
    print("  视图       %s" % ("、".join(report.get("views") or []) or "（无）"))
    print("  API        /api/pluginmgr → %s" % report.get("apiStatus", "?"))
    print("  静态资源   /plug/pluginmgr/view.js → %s" % report.get("assetStatus", "?"))

    counts = report.get("counts") or {}
    if counts:
        print("  发现插件   %d 个（运行中 %d / 停用 %d / 待处理 %d / 内置 %d）" % (
            counts.get("total", 0), counts.get("on", 0), counts.get("off", 0),
            counts.get("bad", 0), counts.get("core", 0)))

    plugins = report.get("plugins") or []
    if plugins:
        print("")
        for p in plugins:
            if p["builtin"]:
                continue
            print("    · %-16s %-8s 路由 %d" % (p["id"], p["state"], p["routes"]))

    issues = report.get("issues") or []
    if issues:
        print("")
        print("  体检：")
        for i in issues:
            print("    [%s] %s：%s" % (
                "!" if i["level"] == "error" else "-", i["name"], i["text"]))

    problems = report.get("problems") or []
    print("")
    if ok:
        print("  结果：一切正常。打开主程序，侧栏会多一个「插件管理」。")
    else:
        print("  结果：有 %d 个问题" % len(problems))
        for p in problems:
            print("    - %s" % p)
    print("")
    return code


if __name__ == "__main__":
    sys.exit(main())
