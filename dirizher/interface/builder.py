"""Конструктор профиля чипа: форма → проверенный YAML → черновик → профиль.

Человек заполняет «паспорт» чипа по даташиту. Конструктор проверяет поля той же
проверкой, что и загрузчик профилей, показывает готовый YAML и подсказки.

  * Черновик сохраняется в data/drafts/. Существующие файлы не перезаписываются.
  * Профилем (profiles/chips/) он становится только по явному решению человека,
    подтвердившего, что напряжение и JEDEC ID сверены с даташитом (принцип 6).
  * Подсказки (производитель, ёмкость по JEDEC ID) — только подсказки: «обычно так»,
    а не знание о конкретном чипе. Решает даташит.
"""
from __future__ import annotations

import copy
import re
from datetime import date
from pathlib import Path

import yaml

from ..core.checks import DEFAULT_CHECKS
from ..core.learning import DEFAULT_RECIPES
from ..core.profiles import Profile

TARGET_CLASS = "memory.spi_nor"
ID_RE = re.compile(r"[a-z][a-z0-9_]{2,39}")
COMMON_VOLTAGES = (1.8, 2.5, 3.0, 3.3, 5.0)

# Первый байт JEDEC ID — код производителя. Таблица неполная, только подсказка.
MANUFACTURERS = {
    "EF": "Winbond", "C2": "Macronix", "C8": "GigaDevice", "1C": "EON", "BF": "SST / Microchip",
    "9D": "ISSI", "20": "Micron (ST, Numonyx) или XMC", "68": "Boya", "85": "Puya", "1F": "Adesto / Atmel",
    "5E": "Zbit", "0B": "XTX", "01": "Spansion / Cypress / Infineon", "A1": "Fudan",
}

FIELDS = [
    {"name": "id", "label": "Короткое имя (латиницей)", "required": True, "example": "gigadevice_gd25q64",
     "help": "маленькие латинские буквы, цифры и «_» — производитель и модель. Станет именем файла."},
    {"name": "name", "label": "Название для людей", "required": True,
     "example": "GigaDevice GD25Q64 (8 МБ, SPI NOR, 3.3 В)",
     "help": "производитель, модель, объём, напряжение — так профиль будет виден в списках"},
    {"name": "jedec_id", "label": "JEDEC ID", "required": True, "example": "C84017",
     "help": "6 шестнадцатеричных знаков — Дирижёр показывает его при опознании. "
             "Первый байт — производитель, последний — ёмкость."},
    {"name": "voltage", "label": "Напряжение питания, В", "required": True, "example": "3.3",
     "help": "строго по даташиту. Чип на 1.8 В можно испортить адаптером на 3.3 В."},
    {"name": "size", "label": "Объём", "example": "8 МБ",
     "help": "можно «8 МБ», «64 Мбит», 8388608 или 0x800000"},
    {"name": "allow_write", "label": "Разрешить запись", "type": "bool", "default": True,
     "help": "выключите, если чип можно только читать и проверять"},
    {"name": "notes", "label": "Заметки", "type": "text", "example": "роутеры, ТВ-приставки; даташит: …",
     "help": "где встречается, особенности подключения, ссылка на даташит"},
]

DRAFT_HEADER = """\
# ЧЕРНОВИК ПРОФИЛЯ из конструктора, {today}.
# Сверьте напряжение и JEDEC ID с даташитом, затем включите профиль в конструкторе
# (или перенесите файл в profiles/chips/).
"""
ENABLED_HEADER = """\
# Профиль создан конструктором {today}.
# Человек подтвердил: напряжение и JEDEC ID сверены с даташитом.
"""


class BuilderError(ValueError):
    pass


def _fmt_bytes(n: int) -> str:
    for unit, size in (("ГБ", 1 << 30), ("МБ", 1 << 20), ("КБ", 1 << 10)):
        if n >= size and n % size == 0:
            return f"{n // size} {unit}"
    return f"{n} байт"


def parse_size(text) -> int | None:
    """«8 МБ», «64 Мбит», «512К», 8388608, 0x800000 → байты. Пусто → None."""
    if text is None or str(text).strip() == "":
        return None
    if isinstance(text, int):
        value = text
    else:
        s = str(text).strip().lower().replace(" ", "")
        if s.startswith("0x"):
            try:
                value = int(s, 16)
            except ValueError:
                raise BuilderError(f"не понял объём «{text}»") from None
        else:
            m = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)([a-zа-яё]*)", s)
            if not m:
                raise BuilderError(f"не понял объём «{text}» — напишите, например, 8 МБ")
            num = float(m.group(1).replace(",", "."))
            unit = m.group(2)
            units = {
                "": 1, "b": 1, "байт": 1, "б": 1,
                "k": 1 << 10, "kb": 1 << 10, "к": 1 << 10, "кб": 1 << 10,
                "m": 1 << 20, "mb": 1 << 20, "м": 1 << 20, "мб": 1 << 20,
                "g": 1 << 30, "gb": 1 << 30, "г": 1 << 30, "гб": 1 << 30,
                "kbit": 1 << 7, "кбит": 1 << 7,
                "mbit": 1 << 17, "мбит": 1 << 17,
                "gbit": 1 << 27, "гбит": 1 << 27,
            }
            if unit not in units:
                raise BuilderError(f"не понял единицу «{m.group(2)}» — используйте КБ, МБ или Мбит")
            value = num * units[unit]
            if value != int(value):
                raise BuilderError(f"объём «{text}» получается дробным числом байт")
            value = int(value)
    if value <= 0:
        raise BuilderError("объём должен быть больше нуля")
    return value


def _as_bool(value, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "да", "yes", "y", "д", "on"}


def build(fields: dict, profiles=None) -> dict:
    """Собрать профиль из полей формы. Возвращает profile, yaml, errors, warnings, hints."""
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in (fields or {}).items()}
    errors, warnings, hints = [], [], []

    pid = str(f.get("id") or "").lower()
    if not ID_RE.fullmatch(pid):
        errors.append("Короткое имя: латинская буква в начале, затем буквы, цифры и «_», "
                      "от 3 до 40 знаков (например, gigadevice_gd25q64)")
    name = str(f.get("name") or "")
    if not name:
        errors.append("Название для людей не заполнено")

    jedec = re.sub(r"[\s:]", "", str(f.get("jedec_id") or "")).upper()
    if jedec.startswith("0X"):
        jedec = jedec[2:]
    capacity = None
    if not re.fullmatch(r"[0-9A-F]{6}", jedec):
        errors.append("JEDEC ID — ровно 6 шестнадцатеричных знаков, например EF4017")
    else:
        maker = MANUFACTURERS.get(jedec[:2])
        hints.append(f"первый байт {jedec[:2]} — обычно {maker} (сверьте с даташитом)" if maker
                     else f"первый байт {jedec[:2]} — нет в моей подсказке, это нормально; решает даташит")
        cap = int(jedec[4:], 16)
        if 0x10 <= cap <= 0x22:
            capacity = 1 << cap
            hints.append(f"последний байт {jedec[4:]} — у SPI NOR обычно означает 2^{cap} байт = "
                         f"{_fmt_bytes(capacity)}")

    voltage = None
    raw_v = f.get("voltage")
    if raw_v is None or str(raw_v).strip() == "":
        warnings.append("напряжение не указано — Дирижёр будет каждый раз спрашивать вас; "
                        "включить профиль без напряжения нельзя")
    else:
        try:
            voltage = float(str(raw_v).replace(",", ".").rstrip("вВvV ").strip())
        except ValueError:
            errors.append(f"напряжение «{raw_v}» — нужно число, например 3.3")
        else:
            if not 1.0 <= voltage <= 5.5:
                errors.append(f"напряжение {voltage} В вне разумных пределов 1.0–5.5 В")
            elif voltage not in COMMON_VOLTAGES:
                warnings.append(f"{voltage} В — необычное значение; у SPI-памяти чаще 3.3 или 1.8 В")

    size = None
    try:
        size = parse_size(f.get("size"))
    except BuilderError as e:
        errors.append(str(e))
    if size is None and capacity:
        hints.append(f"объём не указан; по JEDEC ID, скорее всего, {_fmt_bytes(capacity)}")
    elif size and capacity and size != capacity:
        warnings.append(f"объём {_fmt_bytes(size)} не совпадает с подсказкой по JEDEC ID "
                        f"({_fmt_bytes(capacity)}) — проверьте по даташиту")

    if profiles is not None and jedec:
        for p in profiles:
            options = p.match.get("jedec_id") if p.match else None
            options = options if isinstance(options, list) else [options] if options else []
            if jedec in {str(o).upper() for o in options} and not p.content_match:
                warnings.append(f"такой JEDEC ID уже описан в профиле «{p.id}» — новый будет с ним спорить")
            if p.id == pid:
                errors.append(f"профиль с именем «{pid}» уже есть — выберите другое")

    notes = str(f.get("notes") or "").strip()
    profile = {
        "id": pid, "name": name, "class": TARGET_CLASS,
        "match": {"jedec_id": jedec},
        "priority": 10, "voltage": voltage, "size": size,
        "allow_write": _as_bool(f.get("allow_write"), True),
        "checks": list(DEFAULT_CHECKS) + ["binwalk"],
        "recipes": copy.deepcopy(DEFAULT_RECIPES),
        "notes": notes or "Создан конструктором профиля.",
    }
    if not errors:
        try:
            Profile.from_dict(copy.deepcopy(profile), Path("<конструктор>"))
        except ValueError as e:
            errors.append(f"проверка профиля: {e}")
    text = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
    return {"profile": profile, "yaml": text, "errors": errors, "warnings": warnings, "hints": hints}


def _write_new(folder: Path, stem: str, text: str) -> Path:
    """Записать файл, не трогая существующие: имя.yaml, имя_2.yaml, …"""
    folder.mkdir(parents=True, exist_ok=True)
    for n in range(1, 1000):
        path = folder / (f"{stem}.yaml" if n == 1 else f"{stem}_{n}.yaml")
        if path.exists():
            if path.read_text(encoding="utf-8") == text:
                return path
            continue
        path.write_text(text, encoding="utf-8")
        return path
    raise BuilderError("слишком много черновиков с одним именем")


def save_draft(drafts_dir: Path, fields: dict, profiles=None) -> Path:
    res = build(fields, profiles)
    if res["errors"]:
        raise BuilderError("исправьте: " + "; ".join(res["errors"]))
    text = DRAFT_HEADER.format(today=date.today().isoformat()) + res["yaml"]
    return _write_new(Path(drafts_dir), res["profile"]["id"], text)


def enable(profiles_dir: Path, fields: dict, confirmed: bool, profiles=None) -> Path:
    """Включить профиль: только с подтверждением человека и только в новый файл."""
    res = build(fields, profiles)
    if res["errors"]:
        raise BuilderError("исправьте: " + "; ".join(res["errors"]))
    if not confirmed:
        raise BuilderError("подтвердите, что напряжение и JEDEC ID сверены с даташитом")
    if res["profile"]["voltage"] is None:
        raise BuilderError("без напряжения профиль включить нельзя — впишите его по даташиту")
    target = Path(profiles_dir) / "chips" / f"{res['profile']['id']}.yaml"
    if target.exists():
        raise BuilderError(f"файл {target.name} уже есть — ничего не перезаписываю, выберите другое имя")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ENABLED_HEADER.format(today=date.today().isoformat()) + res["yaml"], encoding="utf-8")
    return target


def fields_from_file(path: Path) -> dict:
    """Поля формы из черновика (в том числе созданного при опознании незнакомого чипа)."""
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    match = d.get("match") or {}
    pid = str(d.get("id") or "")
    if pid.startswith("draft_"):
        pid = pid[len("draft_"):]
    size = d.get("size")
    return {
        "id": pid.lower(),
        "name": d.get("name") or "",
        "jedec_id": str(match.get("jedec_id") or ""),
        "voltage": "" if d.get("voltage") is None else str(d.get("voltage")),
        "size": "" if size is None else (_fmt_bytes(int(size)) if str(size).isdigit() else str(size)),
        "allow_write": bool(d.get("allow_write", True)),
        "notes": d.get("notes") or "",
    }


def list_drafts(folders) -> list:
    out, seen = [], set()
    for folder in folders:
        folder = Path(folder)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            if path.name in seen:
                continue
            seen.add(path.name)
            try:
                out.append({"file": path.name, "fields": fields_from_file(path)})
            except Exception as e:                       # испорченный черновик не мешает остальным
                out.append({"file": path.name, "fields": {}, "error": str(e)})
    return out
