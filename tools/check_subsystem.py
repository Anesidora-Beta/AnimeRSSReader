#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 exe 的 PE 子系统：2 = 窗口程序（不带黑框），3 = 控制台程序。

用法： ``python tools/check_subsystem.py dist/AnimeRSSReader.exe``
"""
import struct
import sys

SUBSYSTEM = {
    1: "NATIVE（驱动，异常）",
    2: "WINDOWS_GUI（窗口程序，无控制台）",
    3: "WINDOWS_CUI（控制台程序，带黑框）",
    5: "OS2_CUI",
    7: "POSIX_CUI",
    9: "WINDOWS_CE_GUI",
    10: "EFI_APPLICATION",
}


def read_subsystem(path):
    with open(path, "rb") as f:
        head = f.read(0x40)
        if head[:2] != b"MZ":
            raise SystemExit("不是有效的 PE 文件：%s" % path)
        pe_off = struct.unpack_from("<I", head, 0x3C)[0]
        f.seek(pe_off)
        sig = f.read(4)
        if sig != b"PE\0\0":
            raise SystemExit("找不到 PE 头：%s" % path)
        f.seek(pe_off + 4 + 20 + 68)          # 跳过 COFF 头，定位 OptionalHeader.Subsystem
        return struct.unpack("<H", f.read(2))[0]


def main(argv):
    if not argv:
        return 2
    path = argv[0]
    sub = read_subsystem(path)
    print("%s  Subsystem = %d  %s" % (path, sub, SUBSYSTEM.get(sub, "(未知)")))
    return 0 if sub == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
