"""Схема настроек лезвия: какие ключи бывают в plugins: <имя>, какого они типа и что значат.

Лезвие описывает свои поля в `settings_schema` (список словарей, удобно — через `field()`), общие поля
(`enabled`, `programmer_voltage`, `extra_args`, таймауты) описаны здесь один раз; лезвие выбирает нужные
в `common_settings` и может поменять их значения по умолчанию в `settings_defaults`.

Поле:
    key       — ключ в config.yaml / config.tools.yaml (по-английски)
    title     — название для человека
    type      — str, int, float, bool, choice, list, port, path
    required  — без него лезвие не работает («недоступен: укажите …»)
    default   — значение, если ключ не задан (для показа; само лезвие берёт своё в коде)
    example   — пример значения
    help      — подсказка простыми словами
    choices   — варианты (для type: choice)
    safety    — "voltage": поле влияет на напряжение — в форме выделено, меняется только со словом «ДА»
    hex       — показывать целое шестнадцатерично (адреса)

Проверка (`validate`) только читает: неизвестный ключ — предупреждение (с догадкой, если похоже на опечатку),
нет обязательного — «укажите …», неверный тип — понятное сообщение. `coerce` превращает текст из формы
или командной строки в значение нужного типа.
"""
from __future__ import annotations

import difflib

TYPES = ("str", "int", "float", "bool", "choice", "list", "port", "path")
TYPE_RU = {"str": "текст", "int": "целое число", "float": "число", "bool": "да или нет",
           "choice": "один из вариантов", "list": "список", "port": "порт (например, /dev/ttyUSB0)",
           "path": "путь к файлу"}


def field(key: str, title: str, type: str = "str", required: bool = False, default=None, example=None,
          help: str = "", choices=None, safety=None, hex: bool = False) -> dict:
    return {"key": key, "title": title, "type": type, "required": required, "default": default,
            "example": example, "help": help, "choices": list(choices) if choices else None,
            "safety": safety, "hex": hex}


COMMON = {
    "enabled": field("enabled", "Включено", "bool", default=False,
                     help="Включите, когда инструмент установлен и переходник подключён."),
    "programmer_voltage": field(
        "programmer_voltage", "Напряжение адаптера, В", "float", example=3.3, safety="voltage",
        help="Своё напряжение логики этого адаптера. Пусто — общее safety.programmer_voltage. "
             "Ошибка здесь может сжечь чип: ставьте то, что на самом деле выдаёт переходник."),
    "extra_args": field("extra_args", "Доп. параметры программы", "list", example=["--verbose"],
                        help="Через пробел. Опасные (снятие защиты, фьюзы, стирание всего чипа, "
                             "отключение проверок) отклоняются."),
    "probe_timeout": field("probe_timeout", "Таймаут опознания, с", "int", default=60),
    "read_timeout": field("read_timeout", "Таймаут чтения, с", "int", default=600),
    "write_timeout": field("write_timeout", "Таймаут записи, с", "int", default=900),
}
COMMON_KEYS = tuple(COMMON)


def build_schema(own, common=COMMON_KEYS, defaults=None) -> list:
    """Общие поля (в порядке COMMON) + свои; своё поле с тем же ключом заменяет общее."""
    own = [dict(f) for f in (own or [])]
    own_keys = {f["key"] for f in own}
    out = []
    for key in COMMON_KEYS:
        if key in common and key not in own_keys:
            f = dict(COMMON[key])
            if defaults and key in defaults:
                f["default"] = defaults[key]
            out.append(f)
    for f in own:
        if defaults and f["key"] in defaults:
            f["default"] = defaults[f["key"]]
        out.append(f)
    return out


def _empty(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "") or value == []


def _int(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return int(value.strip().replace("_", ""), 0)
    raise ValueError


def _float(value):
    if isinstance(value, bool):
        raise ValueError
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        return float(value.strip().replace(",", "."))
    raise ValueError


def _choice(f: dict, value):
    for c in f.get("choices") or []:
        if str(c).strip().lower() == str(value).strip().lower():
            return c
    raise ValueError


def type_error(f: dict, value) -> str | None:
    """Текст ошибки, если значение из YAML не подходит полю; None — подходит."""
    t = f.get("type", "str")
    try:
        if t == "int":
            _int(value)
        elif t == "float":
            _float(value)
        elif t == "bool":
            if not isinstance(value, bool):
                raise ValueError
        elif t == "choice":
            _choice(f, value)
        elif t == "list":
            if not isinstance(value, (list, str)):
                raise ValueError
        elif isinstance(value, (dict, list)):
            raise ValueError
    except (ValueError, TypeError):
        need = TYPE_RU.get(t, t)
        if t == "choice":
            need += ": " + ", ".join(map(str, f.get("choices") or []))
        return f"«{f['title']}» ({f['key']}): нужно {need}, а указано «{value}»"
    return None


def coerce(f: dict, raw):
    """Текст из формы / командной строки → значение нужного типа. Пусто → None (ключ не задавать).

    Значения не из текста (bool, число, список из JSON) проверяются как есть. ValueError — с понятным текстом.
    """
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return None
    t = f.get("type", "str")
    if not isinstance(raw, str):
        err = type_error(f, raw)
        if err:
            raise ValueError(err)
        if t == "int":
            return _int(raw)
        if t == "float":
            return _float(raw)
        if t == "choice":
            return _choice(f, raw)
        if t == "list" and isinstance(raw, list):
            return [str(x) for x in raw]
        return raw
    s = raw.strip()
    try:
        if t == "int":
            return _int(s)
        if t == "float":
            return _float(s)
        if t == "bool":
            low = s.lower()
            if low in ("1", "true", "yes", "on", "да", "вкл", "включено"):
                return True
            if low in ("0", "false", "no", "off", "нет", "выкл", "выключено"):
                return False
            raise ValueError
        if t == "choice":
            return _choice(f, s)
        if t == "list":
            items = s.strip("[]").replace(",", " ").split()
            return [x.strip("'\"") for x in items if x.strip("'\"")]
    except (ValueError, TypeError):
        raise ValueError(type_error(f, s) or f"«{f['title']}»: не понял «{s}»") from None
    return s


def show(f: dict, value) -> str:
    """Значение для показа человеку."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "да" if value else "нет"
    if f.get("hex") and isinstance(value, int):
        return f"0x{value:08X}" if value > 0xFFFF else f"0x{value:02X}"
    if isinstance(value, list):
        return " ".join(map(str, value))
    return str(value)


def validate(schema: list, settings: dict, enabled_only: bool = False) -> dict:
    """Проверить настройки по схеме. Возвращает {"unknown": [...], "type": [...], "missing": [...]} —
    списки готовых строк. enabled_only — пропустить «нет обязательного», если лезвие выключено."""
    keys = {f["key"]: f for f in schema}
    out = {"unknown": [], "type": [], "missing": []}
    for key, value in (settings or {}).items():
        key = str(key)
        if key.startswith("_"):
            continue
        f = keys.get(key)
        if f is None:
            guess = difflib.get_close_matches(key, list(keys), n=1, cutoff=0.7)
            out["unknown"].append(f"неизвестный ключ «{key}»" + (f" — может быть, «{guess[0]}»?" if guess else ""))
            continue
        if value is None:
            continue
        err = type_error(f, value)
        if err:
            out["type"].append(err)
    if not (enabled_only and not (settings or {}).get("enabled")):
        for f in schema:
            if f.get("required") and _empty((settings or {}).get(f["key"])):
                out["missing"].append(f"укажите «{f['title']}» ({f['key']})")
    return out
