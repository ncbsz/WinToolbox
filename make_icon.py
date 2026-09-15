# -*- coding: utf-8 -*-
"""
生成 WinToolbox 的 .ico（纯标准库，不依赖 Pillow）。

画一个「蓝色圆角方块 + 白色 W」，输出多尺寸 ICO（PNG 压缩条目，Vista+ 支持）。
"""
import math
import os
import struct
import zlib

BLUE = (0x00, 0x67, 0xC0)
WHITE = (0xFF, 0xFF, 0xFF)
SIZES = [16, 24, 32, 48, 64, 128, 256]

# W 的折线（归一化坐标，相对图形框）
W_PTS = [(0.10, 0.16), (0.29, 0.84), (0.50, 0.38), (0.71, 0.84), (0.90, 0.16)]
W_HALF = 0.075          # 半线宽
RADIUS = 0.22           # 圆角比例


def png_encode(w, h, rgba):
    """rgba: list of rows, each a bytes of w*4."""
    raw = b"".join(b"\x00" + row for row in rgba)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8-bit RGBA
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
            chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def dist_to_seg(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def render(size):
    S = float(size)
    r = RADIUS * S
    half = S / 2.0
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            cx, cy = x + 0.5, y + 0.5
            # 圆角方块（超采样 2x2 抗锯齿）
            inside = 0
            for sx in (0.25, 0.75):
                for sy in (0.25, 0.75):
                    px, py = x + sx, y + sy
                    qx = min(max(px, r), S - r)
                    qy = min(max(py, r), S - r)
                    d = math.hypot(px - qx, py - qy)
                    if d <= r:
                        inside += 1
            a_bg = inside / 4.0
            if a_bg <= 0.0:
                row += bytes((0, 0, 0, 0))
                continue
            # W
            px, py = cx / S, cy / S
            best = 1e9
            for i in range(len(W_PTS) - 1):
                a, b = W_PTS[i], W_PTS[i + 1]
                d = dist_to_seg(px, py, a[0], a[1], b[0], b[1])
                if d < best:
                    best = d
            # 覆盖比例（用半线宽做柔化）
            cov = (W_HALF - best) / (0.022 + 1e-9)
            cov = max(0.0, min(1.0, cov))
            R = int(BLUE[0] + (WHITE[0] - BLUE[0]) * cov)
            G = int(BLUE[1] + (WHITE[1] - BLUE[1]) * cov)
            B = int(BLUE[2] + (WHITE[2] - BLUE[2]) * cov)
            row += bytes((R, G, B, int(round(a_bg * 255))))
        rows.append(bytes(row))
    return png_encode(size, size, rows)


def build_ico(path):
    images = [(s, render(s)) for s in SIZES]
    n = len(images)
    head = struct.pack("<HHH", 0, 1, n)
    entries = b""
    offset = 6 + 16 * n
    body = b""
    for s, data in images:
        w = 0 if s >= 256 else s
        h = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
        body += data
    with open(path, "wb") as f:
        f.write(head + entries + body)
    return path, sum(len(d) for _, d in images)


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    p, sz = build_ico(out)
    print("icon written:", p, sz, "bytes,", len(SIZES), "sizes")
