# -*- coding: utf-8 -*-
"""联网冒烟：真的去打各个源，确认「能不能翻到旧帖」。

    python live_smoke.py                 # 默认查「鬼灭」，看各源能回溯到哪
    python live_smoke.py 摇曳露营 2018-01-01
    python live_smoke.py --list          # 只看源能力表

它不依赖主程序（只用插件自己的 provider 层），所以在任何目录都能跑。
判断标准不是「有没有结果」，而是**结果里最旧的那条有多旧** —— 那才是
「能不能找回三年前的帖子」的答案。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arr_search.providers import build                     # noqa: E402
from arr_search.query import Query                          # noqa: E402


def human_date(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else "?"


def main(argv):
    if "--list" in argv:
        for p in build().values():
            print("%-12s %-16s caps=%-38s 每页%-4s 最深%s页" % (
                p.id, p.name, ",".join(sorted(p.caps)), p.page_size or "-", p.max_pages))
        return 0

    args = [a for a in argv if not a.startswith("--")]
    kw = args[0] if args else "鬼灭"
    after = args[1] if len(args) > 1 else "2018-01-01"
    q = Query.parse(kw, after=after, pages=2, limit=40)

    print("查询：%r  after=%s  pages=%d\n" % (kw, after, q.pages))
    table = build()
    worst = None
    for pid, p in table.items():
        if p.kind == "local":
            continue
        began = time.time()
        items, err, pages = [], "", 0
        page = 1
        try:
            while page <= min(q.pages, p.max_pages):
                pg = p.search(q, page)
                items.extend(pg.items)
                pages += 1
                if not pg.has_more:
                    break
                page += 1
        except Exception as e:                               # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
        ms = int((time.time() - began) * 1000)
        kept = [i for i in items if q.match(i)]
        stamps = [i["ts"] for i in kept if i.get("ts")]
        oldest = min(stamps) if stamps else 0
        if oldest and (worst is None or oldest < worst):
            worst = oldest
        print("%-12s %5dms  取回%-4d 命中%-4d 页%d  最旧 %s  %s" % (
            pid, ms, len(items), len(kept), pages, human_date(oldest),
            ("× " + err[:70]) if err else ""))
        for it in kept[:2]:
            print("      · %s %s %s" % (human_date(it["ts"]), it["size"] or "-",
                                        it["title"][:62]))

    print("\n结论：最旧能翻到 %s" % human_date(worst or 0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
