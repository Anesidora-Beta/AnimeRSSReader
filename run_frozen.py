#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：PyInstaller 用这个文件做入口，逻辑与 app.py 完全一致。

单独放在这里是因为 PyInstaller 需要一个「顶层脚本」，
而 app.py 本身没有副作用，直接调用它的 main() 就够了。

打包默认是窗口程序（没有 cmd 黑框），出了问题没地方看 traceback，
所以这里兜一层：异常写进 data/logs/app.log，并弹一个错误框。
"""

import traceback

import app
from anime_rss import runtime


def _run():
    return app.main()


if __name__ == "__main__":
    try:
        raise SystemExit(_run())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        raise SystemExit(0)
    except Exception:                       # noqa: BLE001 - 打包后必须自己兜住
        runtime.fatal("程序异常退出：\n\n" + traceback.format_exc())
        raise SystemExit(1)
