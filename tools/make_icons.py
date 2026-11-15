"""Иконки приложения «Дирижёр» (PWA) — только стандартная библиотека Python, без Pillow.

Рисунок тот же, что у значка вкладки: золотая палочка дирижёра на тёмно-синем фоне темы «Нави».
Запуск (из корня проекта):  python3 tools/make_icons.py
Пишет в dirizher/interface/static/: icon-192.png, icon-512.png (скруглённый квадрат) и
icon-maskable-512.png (фон во весь квадрат, рисунок в безопасной зоне — Android сам обрезает форму).
"""
from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "dirizher" / "interface" / "static"
BG = (0x0A, 0x14, 0x30)      # тема «Нави», как theme-color страницы
GOLD = (0xE8, 0xB0, 0x4A)    # как у значка вкладки

# Рисунок в клетке 24×24 (как SVG-значок): палочка (5,19)→(16,8) толщиной 2.4 и кружок (17.5,6.5) r=2.2.
LINE = ((5.0, 19.0), (16.0, 8.0), 1.2)
DOT = ((17.5, 6.5), 2.2)
CENTER = (12.35, 11.65)      # середина рисунка


def _seg_dist(px, py, ax, ay, bx, by) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _in_rounded(x, y, size, r) -> bool:
    cx = min(max(x, r), size - r)
    cy = min(max(y, r), size - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def render(px: int, maskable: bool, ss: int = 3) -> bytes:
    """RGBA-строки PNG (с байтом фильтра 0 в начале каждой)."""
    # Сколько клеток рисунка на всю иконку: обычная — рисунок почти во весь квадрат,
    # maskable — рисунок в круге 80 % (безопасная зона по спецификации), берём с запасом.
    cells = 24.0 / 0.62 if maskable else 24.0 / 0.80
    scale = px / cells
    off = (cells / 2 - CENTER[0], cells / 2 - CENTER[1])
    (ax, ay), (bx, by), hw = LINE
    (dx0, dy0), dr = DOT
    rows = bytearray()
    n = ss * ss
    for j in range(px):
        rows.append(0)
        for i in range(px):
            bg = gold = 0
            for sj in range(ss):
                for si in range(ss):
                    x = i + (si + 0.5) / ss
                    y = j + (sj + 0.5) / ss
                    if not maskable and not _in_rounded(x, y, px, px * 5 / 24):
                        continue
                    bg += 1
                    u, v = x / scale - off[0], y / scale - off[1]
                    if _seg_dist(u, v, ax, ay, bx, by) <= hw or math.hypot(u - dx0, v - dy0) <= dr:
                        gold += 1
            if bg == 0:
                rows += b"\0\0\0\0"
                continue
            k = gold / bg
            rows += bytes(round(BG[c] * (1 - k) + GOLD[c] * k) for c in range(3))
            rows.append(round(255 * bg / n))
    return bytes(rows)


def png(px: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0)   # 8 бит, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def main() -> int:
    for name, px, maskable in (("icon-192.png", 192, False), ("icon-512.png", 512, False),
                               ("icon-maskable-512.png", 512, True)):
        (STATIC / name).write_bytes(png(px, render(px, maskable)))
        print("записано:", STATIC / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
