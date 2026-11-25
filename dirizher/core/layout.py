"""Карта разделов: из чего состоит образ памяти (загрузчик, ядро, файловая система, настройки…).

По образцу карты параметров (core/params.py): профиль описывает разделы, ядро только считает —
о конкретных платах ничего не знает.

    layout:
      - id: boot                     # латиницей
        title: Загрузчик
        offset: 0x0
        length: 0x40000              # или end: 0x40000 (не включая)
        kind: boot                   # boot kernel rootfs config calibration unique data free
        preserve: false              # уникальное для платы: при ремонте берётся с текущего чипа
        restore: true                # можно брать из эталона (рецепт restore_regions)
        help: Что это и чем грозит порча

По умолчанию: `preserve` — только у kind: unique; `restore` — у boot, kernel, rootfs, data, free
(и никогда — у preserve). Настройки, калибровки и уникальное из эталона не берутся, пока профиль
явно не скажет `restore: true`.

Разделы с `preserve: true` дополняют старое поле профиля `preserve` (оно работает как раньше).
Разделы не обязаны покрывать весь чип: место вне разделов — «вне карты».

Рецепт `restore_regions` (действие ядра `write_reference_regions`) собирает образ так:
текущий чип, а разделы с `restore: true`, которые отличаются от эталона, — из эталона.
Запись идёт тем же путём, что и любая: копия «до», план, слово «ДА», сверка всего чипа, копия «после».
"""
from __future__ import annotations

import re

SECTOR = 4096
KINDS = {
    "boot": "загрузчик", "kernel": "ядро", "rootfs": "файловая система", "config": "настройки",
    "calibration": "калибровки", "unique": "уникальное (MAC, серийник)", "data": "данные", "free": "свободно",
}
RESTORE_BY_DEFAULT = {"boot", "kernel", "rootfs", "data", "free"}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
FIELDS = {"id", "title", "offset", "length", "end", "kind", "preserve", "restore", "help"}


def _int(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def normalize(layout, size: int | None = None) -> list:
    """Проверить карту и привести к единому виду (по возрастанию смещения). Ошибка — ValueError."""
    if layout in (None, []):
        return []
    if not isinstance(layout, list):
        raise ValueError("layout должен быть списком разделов")
    out, seen = [], set()
    for i, raw in enumerate(layout, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"раздел №{i}: ожидался словарь")
        rid = str(raw.get("id") or "")
        where = f"раздел «{rid or i}»"
        if not ID_RE.match(rid):
            raise ValueError(f"{where}: id — латиница, цифры и _, с буквы")
        if rid in seen:
            raise ValueError(f"{where}: id повторяется")
        seen.add(rid)
        extra = set(raw) - FIELDS
        if extra:
            raise ValueError(f"{where}: лишние поля {', '.join(sorted(extra))}")
        if not raw.get("title"):
            raise ValueError(f"{where}: нет title")
        kind = str(raw.get("kind") or "data")
        if kind not in KINDS:
            raise ValueError(f"{where}: неизвестный kind «{kind}» (доступны: {', '.join(KINDS)})")
        if "offset" not in raw:
            raise ValueError(f"{where}: нет offset")
        offset = _int(raw["offset"])
        if ("length" in raw) == ("end" in raw):
            raise ValueError(f"{where}: нужен ровно один из length или end")
        end = _int(raw["end"]) if "end" in raw else offset + _int(raw["length"])
        if offset < 0 or end <= offset:
            raise ValueError(f"{where}: пустой или перевёрнутый раздел (0x{offset:X}…0x{end:X})")
        if size and end > size:
            raise ValueError(f"{where}: выходит за размер чипа {size} байт (конец 0x{end:X})")
        preserve = bool(raw.get("preserve", kind == "unique"))
        restore = bool(raw.get("restore", kind in RESTORE_BY_DEFAULT and not preserve))
        if preserve and restore:
            raise ValueError(f"{where}: preserve и restore вместе нельзя — уникальное из эталона не берётся")
        out.append({"id": rid, "title": str(raw["title"]), "offset": offset, "end": end, "length": end - offset,
                    "kind": kind, "preserve": preserve, "restore": restore, "help": str(raw.get("help") or "")})
    out.sort(key=lambda r: r["offset"])
    for a, b in zip(out, out[1:]):
        if b["offset"] < a["end"]:
            raise ValueError(f"раздел «{b['id']}» пересекается с «{a['id']}»")
    return out


def preserve_regions(layout: list) -> list:
    """Разделы preserve: true в формате старого поля профиля preserve."""
    return [{"offset": r["offset"], "length": r["length"], "why": r["title"]} for r in layout if r["preserve"]]


def span(r: dict) -> str:
    return f"0x{r['offset']:X}…0x{r['end']:X}"


def _sectors(a: bytes, b: bytes, start: int, end: int) -> int:
    n, off = 0, start - start % SECTOR
    while off < end:
        lo, hi = max(off, start), min(off + SECTOR, end)
        if a[lo:hi] != b[lo:hi]:
            n += 1
        off += SECTOR
    return n


def diff(current: bytes, reference: bytes, layout: list) -> list:
    """Разделы, отличающиеся от эталона: [{id, title, kind, sectors, restore, preserve, offset, end}].
    Последней строкой — «вне карты», если отличается место, не покрытое разделами."""
    rows, covered = [], []
    for r in layout:
        n = _sectors(current, reference, r["offset"], r["end"])
        covered.append((r["offset"], r["end"]))
        if n:
            rows.append({k: r[k] for k in ("id", "title", "kind", "restore", "preserve", "offset", "end")}
                        | {"sectors": n})
    size = max(len(current), len(reference))
    gaps, pos = [], 0
    for a, b in covered:
        if a > pos:
            gaps.append((pos, a))
        pos = max(pos, b)
    if pos < size:
        gaps.append((pos, size))
    outside = sum(_sectors(current, reference, a, b) for a, b in gaps)
    if outside:
        rows.append({"id": "", "title": "вне карты", "kind": "", "restore": False, "preserve": False,
                     "offset": None, "end": None, "sectors": outside})
    return rows


def sectors_text(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} сектор"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return f"{n} сектора"
    return f"{n} секторов"


def describe(rows: list) -> str:
    """«Прошивка» — 157 секторов, «Параметры» — 1 сектор"""
    return ", ".join(f"«{r['title']}» — {sectors_text(r['sectors'])}" for r in rows)


def blank_boot(data: bytes, layout: list) -> list:
    """Разделы загрузчика, которые целиком 0xFF или 0x00."""
    out = []
    for r in layout:
        if r["kind"] != "boot":
            continue
        chunk = data[r["offset"]:r["end"]]
        if chunk and (chunk.count(0xFF) == len(chunk) or chunk.count(0x00) == len(chunk)):
            out.append(r)
    return out


def build(current: bytes, reference: bytes, layout: list) -> tuple:
    """Образ для restore_regions: (образ, взятые разделы, не тронутые отличающиеся разделы).

    Берутся из эталона только разделы restore: true, которые отличаются. Всё остальное — как на чипе."""
    if len(current) != len(reference):
        raise ValueError(f"размер эталона ({len(reference)} байт) не равен размеру чипа ({len(current)} байт)")
    image = bytearray(current)
    taken, left = [], []
    for row in diff(current, reference, layout):
        if row["restore"]:
            image[row["offset"]:row["end"]] = reference[row["offset"]:row["end"]]
            taken.append(row)
        else:
            left.append(row)
    return bytes(image), taken, left
