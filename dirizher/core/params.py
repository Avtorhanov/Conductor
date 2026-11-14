"""Карта параметров: именованные поля внутри образа памяти.

Один механизм для любого устройства: настройки в EEPROM, блок конфигурации во флеше,
регистры прибора по Modbus. Профиль описывает поля (`params`), ядро только кодирует и
декодирует их — о конкретных приборах ничего не знает.

    params:
      - id: range_high                 # латиницей
        title: Верхняя граница диапазона
        offset: 0x22                   # смещение в образе, байт  (или register: — см. ниже)
        type: i16be                    # u8 i8 u16be u16le i16be i16le u32be u32le i32be i32le f32be f32le
        scale: 0.1                     # значение = сырое число × scale
        unit: "°C"
        min: -50
        max: 600
        writable: true                 # false — только показывать
        choices: {0: "°C", 1: "°F"}    # для перечислений (необязательно)
        help: Что это и где написано в паспорте прибора

Для приборов с регистрами (Modbus) удобнее писать `register: 17` вместо offset:
offset = (register − bus.start) × 2, где bus.start — первый регистр окна чтения из профиля.

Запись параметров идёт тем же путём, что и любая запись: копия «до», сверка, слово «ДА»,
копия «после» (Conductor.tune → Conductor.apply).
"""
from __future__ import annotations

import math
import re
import struct

TYPES = {
    "u8": ("B", 1), "i8": ("b", 1),
    "u16be": (">H", 2), "u16le": ("<H", 2), "i16be": (">h", 2), "i16le": ("<h", 2),
    "u32be": (">I", 4), "u32le": ("<I", 4), "i32be": (">i", 4), "i32le": ("<i", 4),
    "f32be": (">f", 4), "f32le": ("<f", 4),
}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def _int(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def normalize(params, bus: dict | None = None) -> list:
    """Проверить карту и привести к единому виду. Ошибка — ValueError с понятным текстом."""
    if params in (None, []):
        return []
    if not isinstance(params, list):
        raise ValueError("params должен быть списком")
    base = _int((bus or {}).get("start", 0))
    out, seen, spans = [], set(), []
    for i, raw in enumerate(params, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"параметр №{i}: ожидался словарь")
        pid = str(raw.get("id") or "")
        where = f"параметр «{pid or i}»"
        if not ID_RE.match(pid):
            raise ValueError(f"{where}: id — латиница, цифры и _, с буквы")
        if pid in seen:
            raise ValueError(f"{where}: id повторяется")
        seen.add(pid)
        if not raw.get("title"):
            raise ValueError(f"{where}: нет title")
        ptype = str(raw.get("type", "u8"))
        if ptype not in TYPES:
            raise ValueError(f"{where}: неизвестный type «{ptype}» (доступны: {', '.join(TYPES)})")
        if "offset" in raw:
            offset = _int(raw["offset"])
        elif "register" in raw:
            offset = (_int(raw["register"]) - base) * 2
        else:
            raise ValueError(f"{where}: нужен offset или register")
        if offset < 0:
            raise ValueError(f"{where}: смещение меньше нуля (register меньше bus.start?)")
        size = TYPES[ptype][1]
        for other_id, a, b in spans:
            if offset < b and a < offset + size:
                raise ValueError(f"{where}: пересекается с «{other_id}»")
        spans.append((pid, offset, offset + size))
        scale = float(raw.get("scale", 1))
        if scale == 0 or not math.isfinite(scale):
            raise ValueError(f"{where}: scale не может быть нулём")
        choices = raw.get("choices") or {}
        if not isinstance(choices, dict):
            raise ValueError(f"{where}: choices — словарь «число: название»")
        p = {
            "id": pid, "title": str(raw["title"]), "offset": offset, "type": ptype, "size": size,
            "scale": scale, "unit": str(raw.get("unit") or ""),
            "min": float(raw["min"]) if raw.get("min") is not None else None,
            "max": float(raw["max"]) if raw.get("max") is not None else None,
            "writable": bool(raw.get("writable", True)),
            "choices": {int(_int(k)): str(v) for k, v in choices.items()},
            "help": str(raw.get("help") or ""),
        }
        if p["min"] is not None and p["max"] is not None and p["min"] > p["max"]:
            raise ValueError(f"{where}: min больше max")
        if "register" in raw:
            p["register"] = _int(raw["register"])
        out.append(p)
    return out


def _decimals(scale: float) -> int:
    if scale >= 1 and float(scale).is_integer():
        return 0
    return max(0, min(6, -int(math.floor(math.log10(abs(scale)) + 1e-9))))


def decode(image: bytes, p: dict):
    """Значение параметра из образа (с учётом scale) или None, если образ короче."""
    fmt, size = TYPES[p["type"]]
    chunk = image[p["offset"]:p["offset"] + size]
    if len(chunk) != size:
        return None
    raw = struct.unpack(fmt, chunk)[0]
    if p["type"].startswith("f"):
        return raw * p["scale"]
    value = raw * p["scale"]
    return round(value, _decimals(p["scale"])) if not float(p["scale"]).is_integer() else int(value)


def fmt(p: dict, value) -> str:
    if value is None:
        return "—"
    if p["choices"] and isinstance(value, (int, float)) and int(value) in p["choices"]:
        return f"{p['choices'][int(value)]} ({int(value)})"
    if isinstance(value, float):
        text = f"{value:.{_decimals(p['scale']) if not p['type'].startswith('f') else 3}f}"
    else:
        text = str(value)
    return f"{text} {p['unit']}".strip()


def read_all(image: bytes, params: list) -> list:
    """Все параметры для показа человеку."""
    rows = []
    for p in params:
        v = decode(image, p)
        rows.append({k: p[k] for k in ("id", "title", "unit", "min", "max", "writable", "help")}
                    | {"value": v, "text": fmt(p, v), "choices": {str(k): v for k, v in p["choices"].items()},
                       "step": p["scale"] if not p["type"].startswith("f") else None})
    return rows


def _encode(p: dict, value) -> bytes:
    fmt_, size = TYPES[p["type"]]
    if p["type"].startswith("f"):
        return struct.pack(fmt_, float(value) / p["scale"])
    raw = round(float(value) / p["scale"])
    if abs(raw * p["scale"] - float(value)) > abs(p["scale"]) * 1e-6 + 1e-9:
        raise ValueError("не делится на шаг")
    try:
        return struct.pack(fmt_, raw)
    except struct.error:
        raise ValueError(f"не помещается в {p['type']}") from None


def apply_changes(image: bytes, params: list, changes: dict) -> tuple:
    """Новый образ с изменёнными параметрами.

    Возвращает (образ, строки «было → стало», ошибки). При любой ошибке образ не меняется.
    """
    by_id = {p["id"]: p for p in params}
    buf, lines, errors = bytearray(image), [], []
    for pid, raw_value in (changes or {}).items():
        p = by_id.get(pid)
        if p is None:
            errors.append(f"нет параметра «{pid}» в карте профиля")
            continue
        if not p["writable"]:
            errors.append(f"«{p['title']}» — только для чтения")
            continue
        try:
            value = float(str(raw_value).replace(",", ".").strip())
        except ValueError:
            errors.append(f"«{p['title']}»: «{raw_value}» — не число")
            continue
        if not math.isfinite(value):
            errors.append(f"«{p['title']}»: недопустимое число")
            continue
        if p["choices"] and int(value) not in p["choices"]:
            errors.append(f"«{p['title']}»: допустимо {', '.join(f'{k} ({v})' for k, v in p['choices'].items())}")
            continue
        if p["min"] is not None and value < p["min"] or p["max"] is not None and value > p["max"]:
            errors.append(f"«{p['title']}»: {raw_value} вне диапазона "
                          f"{fmt(p, p['min'])} … {fmt(p, p['max'])}")
            continue
        if p["offset"] + p["size"] > len(buf):
            errors.append(f"«{p['title']}»: смещение 0x{p['offset']:X} за пределами образа ({len(buf)} байт)")
            continue
        old = decode(bytes(buf), p)
        try:
            data = _encode(p, value)
        except ValueError as e:
            errors.append(f"«{p['title']}»: {raw_value} — {e} (шаг {p['scale']:g})")
            continue
        buf[p["offset"]:p["offset"] + p["size"]] = data
        new = decode(bytes(buf), p)
        if new != old:
            lines.append(f"{p['title']}: {fmt(p, old)} → {fmt(p, new)}")
    if errors:
        return bytes(image), [], errors
    return bytes(buf), lines, []
