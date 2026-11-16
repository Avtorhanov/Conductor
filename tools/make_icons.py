"""Иконки приложения «Дирижёр» (PWA) — только стандартная библиотека Python, без Pillow.

Рисунок: микросхема SOIC-8 (вид сверху, выводы сверху и снизу) и золотая палочка дирижёра на тёмно-синем
фоне темы «Нави». Шарик на кончике палочки стоит на месте метки первого вывода: Дирижёр работает с микросхемами
и всегда начинает с того, где у чипа вывод 1. Корпус чуть объёмный (градиент, мягкая тень) — как вкладки «Нави».

Запуск (из корня проекта):  python3 tools/make_icons.py
Пишет в dirizher/interface/static/:
  icon-192.png, icon-512.png — скруглённый квадрат, углы прозрачные;
  icon-maskable-512.png      — фон во весь квадрат, рисунок в безопасной зоне (круг 80 %): Android сам обрезает форму;
  apple-touch-icon.png       — 180×180 без прозрачности: iPhone сам скругляет углы, а прозрачное красит чёрным;
  icon.svg                   — тот же рисунок векторно (без теней и блика).
И обновляет значок вкладки (<link rel="icon">) в static/index.html и static/playground.html — тот же icon.svg.

Все файлы строятся из одного списка фигур SHAPES (клетка 24×24, как у SVG). Сглаживание — по расстоянию
до края фигуры, поэтому значок чёткий и на 16 px, и на 512 px.
"""
from __future__ import annotations

import math
import re
import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "dirizher" / "interface" / "static"
PAGES = ("index.html", "playground.html")

BG = "#0a1430"              # тема «Нави», как theme-color страницы
GOLD = "#e8b04a"            # палочка, как раньше
GOLD_LIGHT = "#f6d38a"      # блик на шарике
PIN = "#b4c2e2"             # выводы — светлый металл
BODY = ("#2c4686", "#1d2f62", 6.5, 17.5)     # корпус: цвет сверху, снизу и где начинается / кончается переход
SHADOW = "#000000"

# Корпус и выводы (клетки 24×24): корпус 14×11, по 4 вывода сверху и снизу с шагом 3,5.
BODY_BOX = (5.0, 6.5, 19.0, 17.5)
BODY_R = 1.6
PIN_X = (6.75, 10.25, 13.75, 17.25)
PIN_HALF_W = 0.9
PIN_LEN = 3.0
# Палочка: от рукояти (слева снизу) к шарику на месте вывода 1 (справа сверху).
BATON = ((7.6, 15.2), (15.6, 9.2), 0.95)
BALL = ((16.4, 8.6), 1.75)

# Фигура: (вид, параметры, цвет, мягкость края в клетках — 0 значит чёткий край, непрозрачность).
# Мягкие фигуры (тени, блик) есть только в PNG; в SVG идут чёткие.
SHAPES = [
    ("rrect", (BODY_BOX[0], BODY_BOX[1] + 0.6, BODY_BOX[2], BODY_BOX[3] + 0.6, BODY_R), SHADOW, 1.6, 0.35),
] + [
    ("rrect", (x - PIN_HALF_W, BODY_BOX[1] - PIN_LEN, x + PIN_HALF_W, BODY_BOX[1] + 1, 0.45), PIN, 0, 1.0)
    for x in PIN_X
] + [
    ("rrect", (x - PIN_HALF_W, BODY_BOX[3] - 1, x + PIN_HALF_W, BODY_BOX[3] + PIN_LEN, 0.45), PIN, 0, 1.0)
    for x in PIN_X
] + [
    ("rrect", BODY_BOX + (BODY_R,), BODY, 0, 1.0),
    ("capsule", (BATON[0][0] + 0.2, BATON[0][1] + 0.6, BATON[1][0] + 0.2, BATON[1][1] + 0.6, BATON[2]),
     SHADOW, 1.2, 0.45),
    ("circle", (BALL[0][0] + 0.2, BALL[0][1] + 0.6, BALL[1]), SHADOW, 1.2, 0.45),
    ("capsule", BATON[0] + BATON[1] + (BATON[2],), GOLD, 0, 1.0),
    ("circle", BALL[0] + (BALL[1],), GOLD, 0, 1.0),
    ("circle", (BALL[0][0] - 0.45, BALL[0][1] - 0.5, 0.55), GOLD_LIGHT, 0.9, 0.9),
]
CENTER = (12.0, 12.0)       # середина рисунка (выводы 3,5…20,5 по высоте, корпус 5…19 по ширине)
CELLS_ANY = 24 / 0.86       # обычный значок: рисунок занимает ~61 % высоты
CELLS_MASKABLE = 30.0       # maskable: дальний угол выводов — 11,1 клетки от центра, это 37 % < 40 % (безопасная зона)
CORNER = 5 / 24             # радиус скругления фона — доля стороны


def _rgb(color: str) -> tuple:
    c = color.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _sd_circle(x, y, cx, cy, r) -> float:
    return math.hypot(x - cx, y - cy) - r


def _sd_capsule(x, y, ax, ay, bx, by, r) -> float:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(x - ax - t * dx, y - ay - t * dy) - r


def _sd_rrect(x, y, x0, y0, x1, y1, r) -> float:
    qx = abs(x - (x0 + x1) / 2) - ((x1 - x0) / 2 - r)
    qy = abs(y - (y0 + y1) / 2) - ((y1 - y0) / 2 - r)
    return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - r


SDF = {"circle": _sd_circle, "capsule": _sd_capsule, "rrect": _sd_rrect}


def _color_at(color, y) -> tuple:
    if isinstance(color, str):
        return _rgb(color)
    top, bottom, y0, y1 = color
    t = min(1.0, max(0.0, (y - y0) / (y1 - y0)))
    a, b = _rgb(top), _rgb(bottom)
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def render(px: int, maskable: bool, opaque: bool = False) -> bytes:
    """RGBA-строки PNG (с байтом фильтра 0 в начале каждой).

    maskable — фон во весь квадрат и рисунок мельче (безопасная зона); opaque — фон во весь квадрат,
    рисунок как у обычного значка (для iPhone). Иначе — скруглённый квадрат с прозрачными углами.
    """
    cells = CELLS_MASKABLE if maskable else CELLS_ANY
    full = maskable or opaque
    scale = px / cells
    ox, oy = cells / 2 - CENTER[0], cells / 2 - CENTER[1]
    bg = _rgb(BG)
    shapes = [(SDF[kind], args, color, soft, op) for kind, args, color, soft, op in SHAPES]
    rows = bytearray()
    for j in range(px):
        rows.append(0)
        for i in range(px):
            x, y = i + 0.5, j + 0.5
            alpha = 1.0 if full else min(1.0, max(0.0, 0.5 - _sd_rrect(x, y, 0, 0, px, px, px * CORNER)))
            if alpha <= 0.0:
                rows += b"\0\0\0\0"
                continue
            r, g, b = bg
            u, v = x / scale - ox, y / scale - oy
            for fn, args, color, soft, op in shapes:
                d = fn(u, v, *args)
                k = 0.5 - (d / soft if soft else d * scale)
                if k <= 0.0:
                    continue
                k = min(k, 1.0) * op
                cr, cg, cb = _color_at(color, v)
                r, g, b = r + (cr - r) * k, g + (cg - g) * k, b + (cb - b) * k
            rows += bytes((round(r), round(g), round(b), round(255 * alpha)))
    return bytes(rows)


def png(px: int, raw: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", px, px, 8, 6, 0, 0, 0)   # 8 бит, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def svg() -> str:
    """Тот же рисунок векторно: фон и рамка как у icon-192.png, только чёткие фигуры."""
    off = (CELLS_ANY - 24) / 2
    box = f"{_n(-off)} {_n(-off)} {_n(CELLS_ANY)} {_n(CELLS_ANY)}"
    top, bottom, y0, y1 = BODY
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box}">',
        f'<defs><linearGradient id="b" gradientUnits="userSpaceOnUse" x1="0" y1="{_n(y0)}" x2="0" y2="{_n(y1)}">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bottom}"/></linearGradient></defs>',
        f'<rect x="{_n(-off)}" y="{_n(-off)}" width="{_n(CELLS_ANY)}" height="{_n(CELLS_ANY)}" '
        f'rx="{_n(CELLS_ANY * CORNER)}" fill="{BG}"/>',
    ]
    for kind, args, color, soft, _op in SHAPES:
        if soft:
            continue
        fill = color if isinstance(color, str) else "url(#b)"
        if kind == "rrect":
            x0, y0_, x1, y1_, r = args
            parts.append(f'<rect x="{_n(x0)}" y="{_n(y0_)}" width="{_n(x1 - x0)}" height="{_n(y1_ - y0_)}" '
                         f'rx="{_n(r)}" fill="{fill}"/>')
        elif kind == "capsule":
            ax, ay, bx, by, r = args
            parts.append(f'<path d="M{_n(ax)} {_n(ay)}L{_n(bx)} {_n(by)}" stroke="{fill}" '
                         f'stroke-width="{_n(2 * r)}" stroke-linecap="round"/>')
        else:
            cx, cy, r = args
            parts.append(f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" fill="{fill}"/>')
    parts.append("</svg>")
    return "".join(parts)


def favicon_href(text: str) -> str:
    """SVG для <link rel="icon" href="…">: кавычки — одинарные, спецсимволы — через %."""
    text = text.replace('"', "'")
    for ch, code in (("%", "%25"), ("#", "%23"), ("<", "%3C"), (">", "%3E")):
        text = text.replace(ch, code)
    return "data:image/svg+xml," + text


FAVICON_RE = re.compile(r'<link rel="icon" href="data:image/svg\+xml,[^"]*">')


def favicon_link() -> str:
    return f'<link rel="icon" href="{favicon_href(svg())}">'


def main() -> int:
    for name, px, maskable, opaque in (("icon-192.png", 192, False, False), ("icon-512.png", 512, False, False),
                                       ("icon-maskable-512.png", 512, True, False),
                                       ("apple-touch-icon.png", 180, False, True)):
        (STATIC / name).write_bytes(png(px, render(px, maskable, opaque)))
        print("записано:", STATIC / name)
    (STATIC / "icon.svg").write_text(svg() + "\n", encoding="utf-8")
    print("записано:", STATIC / "icon.svg")
    link = favicon_link()
    for page in PAGES:
        path = STATIC / page
        text = path.read_text(encoding="utf-8")
        if len(FAVICON_RE.findall(text)) != 1:
            raise SystemExit(f"в {page} не нашёл ровно одну строку <link rel=\"icon\" href=\"data:image/svg+xml,…\">")
        new = FAVICON_RE.sub(lambda _m: link, text, count=1)
        if new != text:
            path.write_text(new, encoding="utf-8")
            print("значок вкладки обновлён:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
