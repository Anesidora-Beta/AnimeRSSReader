"""装完自检：确认动画 RSS 阅读器真的看见了这个插件。

用法（在程序目录下跑，插件已经放进 ``plugins/``）::

    python plugins/arr_search/selfcheck.py              # 只看加载，不联网
    python plugins/arr_search/selfcheck.py --live       # 顺便真搜一次
    python plugins/arr_search/selfcheck.py --base D:\\AnimeRSSReader

它只读：用临时目录当 data_dir，不会碰你现有的订阅和收藏。
"""
import argparse
import json
import os
import sys
import tempfile


def find_base(start):
    """往上找带 app.py 的目录；找不到就用当前目录。"""
    cur = os.path.abspath(start)
    for _ in range(6):
        if os.path.isfile(os.path.join(cur, "app.py")) and \
                os.path.isdir(os.path.join(cur, "anime_rss")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return os.path.abspath(os.getcwd())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None, help="程序目录（含 app.py 或 exe 的那层）")
    ap.add_argument("--live", action="store_true", help="额外跑一次真实检索")
    ap.add_argument("--kw", default="鬼灭")
    args = ap.parse_args()

    base = os.path.abspath(args.base) if args.base else find_base(__file__)
    plug_dir = os.path.join(base, "plugins")
    mine = os.path.join(plug_dir, "arr_search")

    print("程序目录：", base)
    print("插件目录：", plug_dir, "->", "存在" if os.path.isdir(plug_dir) else "不存在")
    print("本插件：  ", mine, "->", "存在" if os.path.isdir(mine) else "不存在")
    if not os.path.isdir(mine):
        print("\n[没装对] 把整个 arr_search/ 目录放到 %s 下面再跑一次。" % plug_dir)
        return 2

    sys.path.insert(0, base)                 # 让 import anime_rss 成立
    try:
        from anime_rss.app import create_app
    except Exception as e:                   # noqa: BLE001
        print("\n[导入失败] %s: %s" % (type(e).__name__, e))
        print("请把 --base 指到含 app.py（源码运行）或 AnimeRSSReader.exe（打包版）的目录。")
        return 2

    tmp = tempfile.mkdtemp(prefix="arr_selfcheck_")
    notes = []
    app = create_app(data_dir=tmp, runtime_dir=tmp,
                     static_dir=os.path.join(base, "static"),
                     base_dir=base, plugin_dirs=[plug_dir],
                     fetch_on_start=False, auto_refresh=False,
                     log=lambda m: notes.append(str(m)))
    try:
        boot = json.loads(app.handle("GET", "/api/bootstrap").body)
        views = [v for v in boot.get("views", []) if v.get("id") == "search"]
        cfg = {k: v for k, v in boot.get("settings", {}).items() if k.startswith("search")}

        print("\n侧栏视图：", "深度搜索 ✅" if views else "未出现 ❌")
        r = app.handle("GET", "/api/search/providers")
        prov = json.loads(r.body).get("providers", []) if r.status == 200 else []
        print("可用源：  %d 个 %s" % (len(prov), [p["id"] for p in prov]))
        print("设置项：  %s" % ("、".join(sorted(cfg)) or "无"))

        bad = [n for n in notes if "跳过" in n or "失败" in n]
        if bad:
            print("\n加载问题：")
            for b in bad:
                print("  -", b)

        ok = bool(views) and bool(prov)
        if args.live and ok:
            import urllib.parse
            path = "/api/search?q=%s&pages=2&limit=5" % urllib.parse.quote(args.kw)
            d = json.loads(app.handle("GET", path).body)
            print("\n实测检索「%s」：%sms，命中 %s 条（原始 %s）"
                  % (args.kw, d["took"], d["count"], d["raw"]))
            for s in d["sources"]:
                print("  %-12s ok=%-5s %5dms n=%s %s"
                      % (s["id"], s["ok"], s["ms"], s["count"], (s.get("error") or "")[:40]))
            for it in d["items"][:3]:
                print("  · %s | %s | %s" % (it["sourceName"], it["size"], it["title"][:50]))
            ok = ok and d["count"] > 0

        print("\n结论：", "安装成功，重启程序后侧栏就有「深度搜索」" if ok else "有问题，看上面的提示")
        return 0 if ok else 1
    finally:
        app.stop()


if __name__ == "__main__":
    sys.exit(main())
