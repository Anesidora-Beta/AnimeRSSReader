#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把应用打成单文件可执行程序（Windows / macOS / Linux 各跑一次即可）。

用法:
    python build_exe.py            # 生成单文件启动器
    python build_exe.py --onedir   # 生成文件夹版（启动更快，便于排查问题）

产物在 dist/ 下：
    Windows  dist/AnimeRSSReader.exe
    macOS    dist/AnimeRSSReader
    Linux    dist/AnimeRSSReader

注意：PyInstaller 不支持交叉编译，要哪个平台的程序就在哪个平台上跑一次。
数据目录在可执行文件旁边自动创建（data/reader.db），复制走即可带走阅读状态。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "AnimeRSSReader"
VERSION = "1.5.0"
SEP = ";" if os.name == "nt" else ":"

VERSION_TEMPLATE = """# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=({v0}, {v1}, {v2}, 0),
    prodvers=({v0}, {v1}, {v2}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('080404B0', [
        StringStruct('CompanyName', 'Anime RSS Reader'),
        StringStruct('FileDescription', '动漫资源 RSS 阅读器'),
        StringStruct('FileVersion', '{version}'),
        StringStruct('InternalName', '{name}'),
        StringStruct('LegalCopyright', '仅供个人学习使用'),
        StringStruct('OriginalFilename', '{name}.exe'),
        StringStruct('ProductName', '动漫资源 RSS 阅读器'),
        StringStruct('ProductVersion', '{version}')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])
  ]
)
"""


def write_version_file():
    """Windows 资源里的版本信息，让 exe 属性面板显示正常"""
    parts = (VERSION.split(".") + ["0", "0", "0"])[:3]
    text = VERSION_TEMPLATE.format(
        v0=int(parts[0]), v1=int(parts[1]), v2=int(parts[2]),
        version=VERSION, name=NAME)
    path = os.path.join(HERE, "_version_info.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("需要先安装 PyInstaller：")
        print("    pip install pyinstaller")
        return 1

    onedir = "--onedir" in sys.argv

    # 图标：没有就现生成（build_icon.py 只用标准库）
    icon = os.path.join(HERE, "icon.ico" if os.name == "nt" else "icon.png")
    if not os.path.isfile(icon):
        gen = os.path.join(HERE, "build_icon.py")
        if os.path.isfile(gen):
            print("生成图标 ...")
            subprocess.call([sys.executable, gen], cwd=HERE)

    entry = os.path.join(HERE, "run_frozen.py")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir" if onedir else "--onefile",
        "--name", NAME,
        "--add-data", "static%sstatic" % SEP,
        "--hidden-import", "sources",
        "--hidden-import", "feeds",
        "--hidden-import", "store",
        "--hidden-import", "textutil",
        "--hidden-import", "schedule",
        "--hidden-import", "settings",
        # 内核与核心 API：静态列出而不靠目录扫描，打包时才不会漏
        "--hidden-import", "anime_rss",
        "--hidden-import", "anime_rss.kernel",
        "--hidden-import", "anime_rss.kernel.http",
        "--hidden-import", "anime_rss.kernel.assets",
        "--hidden-import", "anime_rss.kernel.server",
        "--hidden-import", "anime_rss.kernel.context",
        "--hidden-import", "anime_rss.kernel.bus",
        "--hidden-import", "anime_rss.kernel.jobs",
        "--hidden-import", "anime_rss.kernel.views",
        "--hidden-import", "anime_rss.api",
        "--hidden-import", "anime_rss.api.core",
        # 模块化：契约 + 内置模块清单（清单是静态元组，打包也不会漏）
        "--hidden-import", "anime_rss.module",
        "--hidden-import", "anime_rss.common",
        "--hidden-import", "anime_rss.discovery",
        "--hidden-import", "anime_rss.modules",
        "--hidden-import", "anime_rss.modules.feeds",
        "--hidden-import", "anime_rss.modules.reader",
        "--hidden-import", "anime_rss.modules.settings",
        "--hidden-import", "anime_rss.modules.schedule",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
    ]
    if os.path.isfile(icon):
        cmd += ["--icon", icon]
    if os.name == "nt":
        cmd += ["--version-file", write_version_file()]
    cmd.append(entry)

    print("执行:", " ".join(cmd))
    rc = subprocess.call(cmd, cwd=HERE)
    if rc != 0:
        print("\n打包失败，退出码 %d" % rc)
        return rc

    dist = os.path.join(HERE, "dist")
    exe = os.path.join(dist, NAME + (".exe" if os.name == "nt" else ""))
    if os.path.isfile(exe):
        print("\n打包完成：%s  (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576.0))
    else:
        print("\n打包完成，产物目录：%s" % dist)

    for junk in ("build", "%s.spec" % NAME, "_version_info.txt"):
        p = os.path.join(HERE, junk)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
