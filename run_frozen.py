#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包入口：PyInstaller 用这个文件做入口，逻辑与 app.py 完全一致。

单独放在这里是因为 PyInstaller 需要一个「顶层脚本」，
而 app.py 本身没有副作用，直接调用它的 main() 就够了。
"""

import app

if __name__ == "__main__":
    raise SystemExit(app.main())
