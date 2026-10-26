#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Windows 图标 icon.ico（纯标准库，不需要 Pillow）。

设计：圆角方块 + 品牌渐变（#2f6fed → #6f5cf5）+ 白色 RSS 电波符，
与网页 favicon、界面主色保持一致。

用法:
    python build_icon.py
产物:
    icon.ico    多尺寸（16/24/32/48/64/128/256）
    icon.png    256x256 预览图
"""

import math
import os
import struct
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))

SS = 1024                      # 超采样画布边长
RADIUS = 15.0 / 64.0           # 圆角半径（相对画布）
C_TOP = (0x2F, 0x6F, 0xED)     # 渐变起点
C_BOT = (0x6F, 0x5C, 0xF5)     # 渐变终点

SIZES = (16, 24, 32, 48, 64, 128, 256)

# RSS 电波符的几何参数（相对画布，原点在左下角的圆点）
DOT_X, DOT_Y, DOT_R = 0.295, 0.715, 0.072
ARC_HALF_W = 0.042
ARC_RADII = (0.215, 0.375)


def clamp01(v):
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


def sd_round_rect(u, v):
    """圆角方块的符号距离场（负值在内部）"""
    qx = abs(u - 0.5) - (0.5 - RADIUS)
    qy = abs(v - 0.5) - (0.5 - RADIUS)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - RADIUS


def sd_dot(u, v):
    return math.hypot(u - DOT_X, v - DOT_Y) - DOT_R


def sd_arc(u, v, radius):
    """四分之一圆弧（右上象限），两端加圆头"""
    dx, dy = u - DOT_X, v - DOT_Y
    dist = math.hypot(dx, dy)
    d = abs(dist - radius) - ARC_HALF_W
    # 只在右上象限生效
    if dx < 0.0 or dy > 0.0:
        d = 1.0
    # 两端圆头
    for cx, cy in ((DOT_X + radius, DOT_Y), (DOT_X, DOT_Y - radius)):
        cap = math.hypot(u - cx, v - cy) - ARC_HALF_W
        if cap < d:
            d = cap
    return d


def render_master():
    """渲染 1024x1024 的 RGBA 母版"""
    px = 1.0 / SS
    rows = []
    for y in range(SS):
        v = (y + 0.5) * px
        row = bytearray()
        for x in range(SS):
            u = (x + 0.5) * px

            bg = clamp01(0.5 - sd_round_rect(u, v) / px)
            if bg <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue

            t = clamp01((u * 0.55 + v * 0.45))
            t = t * t * (3 - 2 * t)
            r = C_TOP[0] + (C_BOT[0] - C_TOP[0]) * t
            g = C_TOP[1] + (C_BOT[1] - C_TOP[1]) * t
            b = C_TOP[2] + (C_BOT[2] - C_TOP[2]) * t

            glyph = clamp01(0.5 - sd_dot(u, v) / px)
            for rad in ARC_RADII:
                a = clamp01(0.5 - sd_arc(u, v, rad) / px)
                if a > glyph:
                    glyph = a

            if glyph > 0.0:
                r = r + (255.0 - r) * glyph
                g = g + (255.0 - g) * glyph
                b = b + (255.0 - b) * glyph

            row += bytes((int(r + 0.5), int(g + 0.5), int(b + 0.5),
                          int(bg * 255.0 + 0.5)))
        rows.append(bytes(row))
    return rows


def downsample(master, size):
    """区域平均缩放到目标尺寸"""
    step = SS / size
    out = []
    for j in range(size):
        y0 = int(j * step)
        y1 = max(y0 + 1, int((j + 1) * step))
        row = bytearray()
        for i in range(size):
            x0 = int(i * step)
            x1 = max(x0 + 1, int((i + 1) * step))
            sr = sg = sb = sa = n = 0
            for y in range(y0, y1):
                src = master[y]
                for x in range(x0, x1):
                    o = x * 4
                    a = src[o + 3]
                    if a:
                        sr += src[o] * a
                        sg += src[o + 1] * a
                        sb += src[o + 2] * a
                        sa += a
                        n += 1
            if n == 0:
                row += b"\x00\x00\x00\x00"
            else:
                row += bytes((min(255, sr // sa), min(255, sg // sa),
                              min(255, sb // sa), sa // n))
        out.append(bytes(row))
    return out


def encode_png(rows, size):
    raw = b"".join(b"\x00" + r for r in rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build_ico(images):
    """images: [(size, png_bytes)] -> ico bytes（PNG 压缩条目，Vista+ 支持）"""
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries = b""
    blobs = b""
    for size, png in images:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
        blobs += png
    return header + entries + blobs


def main():
    print("渲染母版 %dx%d ..." % (SS, SS))
    master = render_master()

    images = []
    for size in SIZES:
        rows = master if size == SS else downsample(master, size)
        png = encode_png(rows, size)
        images.append((size, png))
        print("  %3d px  %6d bytes" % (size, len(png)))

    ico = build_ico(images)
    ico_path = os.path.join(HERE, "icon.ico")
    with open(ico_path, "wb") as f:
        f.write(ico)

    png_path = os.path.join(HERE, "icon.png")
    for size, png in images:
        if size == 256:
            with open(png_path, "wb") as f:
                f.write(png)

    print("完成：%s（%.1f KB），%s" % (ico_path, len(ico) / 1024.0, png_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
