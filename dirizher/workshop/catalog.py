"""Справочник: движки (внешние программы с открытым кодом) и переходники (что купить или собрать).

Карточка — YAML-файл:
  catalog/engines/<id>.yaml    движок: кто автор, лицензия, что умеет, как работает сам и внутри Дирижёра;
  catalog/adapters/<id>.yaml   переходник: для чего, как подключить или собрать — с рисунками;
  skills/<навык>/catalog/*.yaml — карточки, которые приносит навык.
Файлы на «_» — шаблоны, не загружаются.

Рисунки описываются данными (drawings), а рисует их интерфейс — без картинок и сети:
  wiring  — провода между двумя разъёмами (a → b, цвет провода);
  package — корпус микросхемы сверху (SOIC/DIP), вывод 1 отмечен точкой;
  header  — гребёнка выводов (Raspberry Pi, Bus Pirate, ISP), задействованные выводы подсвечены;
  chain   — цепочка «что к чему подключается».

Справочник только описывает: он ничего не включает и не запускает. Карточки не влияют на работу с железом,
поэтому правятся в капоте без слова подтверждения, но с проверкой (validate).
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path, PurePosixPath

import yaml

from ..core.skills import SkillDB

ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
URL_RE = re.compile(r"^https://[^\s\"'<>]+$")
USB_RE = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}$")

KINDS = {"engine": "Движок", "adapter": "Переходник"}
FOLDERS = {"engine": "engines", "adapter": "adapters"}
GROUPS = {
    "engine": {"tool": "Инструмент", "base": "Основа"},
    "adapter": {"buy": "Купить", "diy": "Собрать", "mod": "Купить и доработать"},
}
# Виды микросхем — по порядку от простого к сложному (так их показывает «Как всё связано»).
TARGETS = {
    "memory.spi_nor": "SPI-флешка 25xx",
    "memory.i2c_eeprom": "EEPROM 24Cxx",
    "memory.parallel": "Память в панельке",
    "mcu.avr": "Микроконтроллер AVR",
    "mcu.esp": "ESP8266 / ESP32",
    "mcu.arm": "Микроконтроллер ARM",
    "device.modbus": "Прибор Modbus RTU",
    "signals": "Сигналы на плате",
    "dump": "Любой дамп",
}
# Ступени сложности: справочник ведёт от первой флешки к настройкам приборов и капоту.
# У карточки — поле level; без него ступень берётся по самому простому виду микросхем (TARGET_LEVELS).
LEVELS = {
    1: {"title": "Первая флешка", "short": "Флешка",
        "about": "SPI-флешка 25xx на 3,3 В: CH341A, прищепка или панелька — без пайки"},
    2: {"title": "Другие напряжения и шины", "short": "Шины",
        "about": "флешки на 1,8 В, EEPROM 24Cxx по I2C, память в панельке, SPI самого Pi"},
    3: {"title": "Микроконтроллеры", "short": "МК",
        "about": "AVR, ESP и ARM: свой программатор у каждого, сброс и защита чтения"},
    4: {"title": "Приборы и замеры", "short": "Приборы",
        "about": "настройки приборов по RS-485, сигналы на проводах, что внутри дампа"},
    5: {"title": "Под капотом", "short": "Капот",
        "about": "на чём построен Дирижёр — для тех, кто правит профили, навыки и код"},
}
TARGET_LEVELS = {"memory.spi_nor": 1, "memory.i2c_eeprom": 2, "memory.parallel": 2, "mcu.avr": 3, "mcu.esp": 3,
                 "mcu.arm": 3, "device.modbus": 4, "signals": 4, "dump": 4}
DEFAULT_LEVEL = 3
COLORS = {
    "red": "красный", "black": "чёрный", "orange": "оранжевый", "yellow": "жёлтый", "green": "зелёный",
    "blue": "синий", "violet": "фиолетовый", "white": "белый", "gray": "серый", "brown": "коричневый",
}
DRAWINGS = ("wiring", "package", "header", "chain")
ICONS = ("chip", "usb", "clip", "board", "probe", "cable", "socket", "scope", "code", "db", "wave", "plug")

LIST_FIELDS = ("strengths", "how", "in_dirizher", "caution", "verify", "steps", "targets", "adapters",
               "engines", "needs", "preflight", "groups")
GROUP_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
TEXT_FIELDS = ("tagline", "what", "license", "install", "check", "notes", "plugin", "program", "voltage",
               "usb", "lesson", "price", "since", "lang")
MAX_ITEMS = 40


class CatalogError(ValueError):
    pass


# ======================= где лежат карточки =======================
def root_of(cfg) -> Path:
    return Path(cfg.get("_path") or "config.yaml").resolve().parent


def catalog_dir(cfg) -> Path:
    value = cfg.get("catalog_dir") or "./catalog"
    p = Path(str(value)).expanduser()
    return p if p.is_absolute() else (root_of(cfg) / p).resolve()


def _rel(cfg, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root_of(cfg)).as_posix()
    except ValueError:
        return str(path)


def files(cfg) -> list:
    """(путь, источник) — все карточки: справочник и активные навыки."""
    out = []
    base = catalog_dir(cfg)
    if base.is_dir():
        for p in sorted(base.rglob("*.yaml")):
            if not p.name.startswith("_"):
                out.append((p, "справочник"))
    try:
        skills = SkillDB(cfg.get("skills_dir")).active
    except Exception:
        skills = []
    for skill in skills:
        folder = Path(skill.path) / "catalog"
        if folder.is_dir():
            for p in sorted(folder.glob("*.yaml")):
                if not p.name.startswith("_"):
                    out.append((p, f"навык {skill.id}"))
    return out


def is_card_path(rel: str) -> bool:
    pp = PurePosixPath(str(rel).replace("\\", "/"))
    if pp.suffix not in (".yaml", ".yml"):
        return False
    parts = pp.parts
    return (len(parts) >= 2 and parts[0] == "catalog") or (len(parts) == 4 and parts[0] == "skills"
                                                           and parts[2] == "catalog")


# ======================= проверка =======================
def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def _norm_step(item) -> dict:
    if isinstance(item, dict):
        return {"text": _text(item.get("text")), "code": _text(item.get("code")), "tip": _text(item.get("tip"))}
    return {"text": _text(item), "code": "", "tip": ""}


def _check_drawing(d, where: str, errors: list) -> None:
    if not isinstance(d, dict) or d.get("type") not in DRAWINGS:
        errors.append(f"{where}: type — одно из {', '.join(DRAWINGS)}")
        return
    t = d["type"]
    if t == "wiring":
        wires = d.get("wires")
        if not isinstance(wires, list) or not wires:
            errors.append(f"{where}: нужен список wires — провода «a → b»")
            return
        for i, w in enumerate(wires, 1):
            if isinstance(w, list):
                w = {"a": w[0] if w else None, "b": w[1] if len(w) > 1 else None, "color": w[2] if len(w) > 2 else None}
            if not isinstance(w, dict) or not _text(w.get("a")) or not _text(w.get("b")):
                errors.append(f"{where}, провод {i}: нужны a и b (откуда и куда)")
            elif w.get("color") and w["color"] not in COLORS:
                errors.append(f"{where}, провод {i}: цвет — одно из {', '.join(COLORS)}")
    elif t == "package":
        pins = d.get("pins")
        if not isinstance(pins, list) or len(pins) < 4 or len(pins) % 2 or len(pins) > 64:
            errors.append(f"{where}: pins — подписи выводов по порядку с 1-го, чётное число от 4 до 64")
        if d.get("style", "soic") not in ("soic", "dip"):
            errors.append(f"{where}: style — soic или dip")
        for label, color in (d.get("mark") or {}).items():
            if color not in COLORS:
                errors.append(f"{where}: mark «{label}» — цвет одно из {', '.join(COLORS)}")
    elif t == "header":
        count, rows = d.get("count"), d.get("rows", 2)
        if not isinstance(count, int) or not 2 <= count <= 80 or rows not in (1, 2) or count % rows:
            errors.append(f"{where}: count — число выводов (2…80), rows — 1 или 2, count делится на rows")
        pins = d.get("pins") or {}
        if not isinstance(pins, dict):
            errors.append(f"{where}: pins — словарь «номер: {{label, color}}»")
            return
        for num, spec in pins.items():
            if not isinstance(num, int) or (isinstance(count, int) and not 1 <= num <= count):
                errors.append(f"{where}: вывод «{num}» вне 1…{count}")
            color = spec.get("color") if isinstance(spec, dict) else None
            if color and color not in COLORS:
                errors.append(f"{where}: вывод {num} — цвет одно из {', '.join(COLORS)}")
    elif t == "chain":
        steps = d.get("steps")
        if not isinstance(steps, list) or len(steps) < 2:
            errors.append(f"{where}: steps — хотя бы два звена цепочки")


def validate(data, where: str = "карточка") -> list:
    """Ошибки, из-за которых карточка не загрузится."""
    errors = []
    if not isinstance(data, dict):
        return [f"{where}: ожидался словарь полей"]
    cid = data.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        errors.append("id — латиница, цифры и _, с буквы (например ch341a)")
    kind = data.get("kind")
    if kind not in KINDS:
        errors.append(f"kind — {' или '.join(KINDS)}")
        return errors
    if not _text(data.get("title")):
        errors.append("не заполнено название (title)")
    if not _text(data.get("tagline")):
        errors.append("не заполнена строка «для чего» (tagline)")
    group = data.get("group")
    if group is not None and group not in GROUPS[kind]:
        errors.append(f"group — одно из {', '.join(GROUPS[kind])}")
    if data.get("icon") is not None and data["icon"] not in ICONS:
        errors.append(f"icon — одно из {', '.join(ICONS)}")
    level = data.get("level")
    if level is not None and (isinstance(level, bool) or level not in LEVELS):
        errors.append(f"level — ступень сложности, число от 1 до {len(LEVELS)}")
    for key in LIST_FIELDS:
        value = data.get(key)
        if value is not None and not isinstance(value, (list, str)):
            errors.append(f"{key} — список строк")
        elif isinstance(value, list) and len(value) > MAX_ITEMS:
            errors.append(f"{key}: не больше {MAX_ITEMS} пунктов")
        elif isinstance(value, list):
            for i, item in enumerate(value, 1):
                step_ok = key in ("how", "steps") and isinstance(item, dict) and _text(item.get("text"))
                if not step_ok and not isinstance(item, (str, int, float)):
                    errors.append(f"{key}, пункт {i}: ожидалась строка — возьмите текст в кавычки "
                                  f"(двоеточие с пробелом внутри YAML читает как «ключ: значение»)")
    for key in TEXT_FIELDS:
        value = data.get(key)
        if value is not None and not isinstance(value, (str, int, float)):
            errors.append(f"{key} — строка")
    for g in _as_list(data.get("groups")):
        if not GROUP_RE.match(str(g)):
            errors.append(f"groups: «{g}» — имя группы Linux (plugdev, dialout, spi)")
    for t in _as_list(data.get("targets")):
        if str(t) not in TARGETS:
            errors.append(f"targets: «{t}» — одно из {', '.join(TARGETS)}")
    for a in _as_list(data.get("authors")):
        name = a.get("name") if isinstance(a, dict) else a
        if not _text(name):
            errors.append("authors: у автора нет имени")
    for link in _as_list(data.get("links")):
        url = link.get("url") if isinstance(link, dict) else link
        if not URL_RE.match(_text(url)):
            errors.append(f"links: «{url}» — адрес должен начинаться с https://")
    for ex in _as_list(data.get("examples")):
        if not isinstance(ex, dict) or not _text(ex.get("cmd")):
            errors.append("examples: у примера нужна команда (cmd)")
    usb = data.get("usb")
    if usb:
        for item in str(usb).replace(",", " ").split():
            if not USB_RE.match(item.lower()):
                errors.append(f"usb: «{item}» — вид 1a86:5512 (как в lsusb)")
    for i, d in enumerate(_as_list(data.get("drawings")), 1):
        _check_drawing(d, f"рисунок {i}", errors)
    return errors


def level_of(data: dict) -> int:
    """Ступень сложности: из поля level, иначе — по самому простому виду микросхем; основа — «под капотом»."""
    if data.get("level") in LEVELS and not isinstance(data.get("level"), bool):
        return int(data["level"])
    if data.get("group") == "base":
        return max(LEVELS)
    found = [TARGET_LEVELS[str(t)] for t in _as_list(data.get("targets")) if str(t) in TARGET_LEVELS]
    return min(found) if found else DEFAULT_LEVEL


def levels_public() -> list:
    """Ступени для интерфейсов — по порядку."""
    return [dict(n=n, **lv) for n, lv in sorted(LEVELS.items())]


def normalize(data: dict, path: Path | None = None, source: str = "", rel: str = "") -> dict:
    """Карточка в едином виде для интерфейсов: списки — списки, шаги — словари."""
    kind = data["kind"]
    card = {
        "id": data["id"], "kind": kind, "kind_title": KINDS[kind], "title": _text(data["title"]),
        "tagline": _text(data.get("tagline")),
        "group": data.get("group") or ("tool" if kind == "engine" else "buy"),
        "icon": data.get("icon") or ("code" if kind == "engine" else "usb"),
        "path": rel, "source": source,
    }
    card["group_title"] = GROUPS[kind][card["group"]]
    card["level"] = level_of(data)
    card["level_title"] = LEVELS[card["level"]]["title"]
    for key in TEXT_FIELDS:
        if key in ("tagline",):
            continue
        card[key] = _text(data.get(key))
    for key in LIST_FIELDS:
        value = data.get(key)
        card[key] = [_text(v) for v in _as_list(value) if _text(v)] if key not in ("how", "steps") else \
            [s for s in (_norm_step(v) for v in _as_list(value)) if s["text"]]
    card["authors"] = []
    for a in _as_list(data.get("authors")):
        if isinstance(a, dict):
            card["authors"].append({"name": _text(a.get("name")), "role": _text(a.get("role"))})
        else:
            card["authors"].append({"name": _text(a), "role": ""})
    card["links"] = []
    for link in _as_list(data.get("links")):
        if isinstance(link, dict):
            card["links"].append({"title": _text(link.get("title")) or _text(link.get("url")),
                                  "url": _text(link.get("url"))})
        else:
            card["links"].append({"title": _text(link), "url": _text(link)})
    card["examples"] = [{"title": _text(e.get("title")), "cmd": _text(e.get("cmd")), "note": _text(e.get("note"))}
                        for e in _as_list(data.get("examples")) if isinstance(e, dict)]
    card["drawings"] = []
    for d in _as_list(data.get("drawings")):
        d = dict(d)
        if d["type"] == "wiring":
            wires = []
            for w in d.get("wires") or []:
                if isinstance(w, list):
                    w = {"a": w[0], "b": w[1], "color": w[2] if len(w) > 2 else "", "note": w[3] if len(w) > 3 else ""}
                wires.append({"a": _text(w.get("a")), "b": _text(w.get("b")), "color": w.get("color") or "gray",
                              "note": _text(w.get("note"))})
            d["wires"] = wires
        if d["type"] == "header":
            pins = {}
            for num, spec in (d.get("pins") or {}).items():
                spec = spec if isinstance(spec, dict) else {"label": str(spec)}
                pins[str(num)] = {"label": _text(spec.get("label")), "color": spec.get("color") or "gray",
                                  "to": _text(spec.get("to"))}
            d["pins"] = pins
            d.setdefault("rows", 2)
        if d["type"] == "chain":
            d["steps"] = [s if isinstance(s, dict) else {"title": str(s)} for s in d["steps"]]
        d["title"] = _text(d.get("title"))
        d["caption"] = _text(d.get("caption"))
        card["drawings"].append(d)
    text = " ".join([card["title"], card["tagline"], card["what"], card["license"],
                     " ".join(a["name"] for a in card["authors"]), " ".join(card["strengths"]),
                     " ".join(s["text"] for s in card["how"] + card["steps"]), " ".join(card["in_dirizher"]),
                     card["plugin"], card["program"], " ".join(card["caution"]), card["notes"]])
    card["_text"] = text
    return card


def link_warnings(cards: list) -> dict:
    """{id: [предупреждения]} — ссылки на карточки, которых нет. Не ошибка: навык мог не загрузиться."""
    ids = {c["id"]: c["kind"] for c in cards}
    out = {}
    for c in cards:
        warns = []
        for key, kind in (("adapters", "adapter"), ("needs", "adapter"), ("engines", "engine")):
            for ref in c.get(key) or []:
                if ids.get(ref) != kind:
                    warns.append(f"{key}: нет карточки {KINDS[kind].lower()}а «{ref}»")
        if c["kind"] == "engine" and not c["authors"]:
            warns.append("не указаны авторы — справочник отмечает авторов каждого движка")
        if c["kind"] == "engine" and not c["license"]:
            warns.append("не указана лицензия")
        if c["kind"] == "adapter" and not c["steps"]:
            warns.append("нет шагов «как подключить или собрать» (steps)")
        if warns:
            out[c["id"]] = warns
    return out


# ======================= загрузка =======================
def load(cfg) -> tuple:
    """(карточки, ошибки [(путь, текст)]). Сломанная карточка не мешает остальным."""
    cards, errors, seen = [], [], {}
    for path, source in files(cfg):
        rel = _rel(cfg, path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            problems = validate(data)
            if problems:
                raise CatalogError("; ".join(problems))
            if data["id"] in seen:
                raise CatalogError(f"id «{data['id']}» уже занят файлом {seen[data['id']]}")
            seen[data["id"]] = rel
            cards.append(normalize(data, path, source, rel))
        except yaml.YAMLError as e:
            mark = getattr(e, "problem_mark", None)
            errors.append((rel, f"YAML: {getattr(e, 'problem', None) or e}" + (f" (строка {mark.line + 1})" if mark else "")))
        except Exception as e:
            errors.append((rel, str(e)))
    warns = link_warnings(cards)
    for c in cards:
        c["warnings"] = warns.get(c["id"], [])
    cards.sort(key=sort_key)
    return cards, errors


GROUP_ORDER = {"buy": 0, "mod": 1, "diy": 2, "tool": 3, "base": 4}
TARGET_RANK = {t: i for i, t in enumerate(TARGETS)}


def sort_key(c: dict) -> tuple:
    """От простого к сложному: ступень → купить, доработать, собрать, программы, основа →
    первый вид микросхем карточки (в порядке TARGETS) → название."""
    first = TARGET_RANK.get(c["targets"][0], 99) if c.get("targets") else 99
    return c["level"], GROUP_ORDER.get(c["group"], 9), first, c["title"].lower()


def with_status(cards: list, plugin_report: list | None = None, sim: bool = False) -> list:
    """Добавить «что сейчас»: лезвие включено? программа установлена?
    В учебном режиме лезвия железа отключены нарочно — так и пишется."""
    report = {r["name"]: r for r in plugin_report or []}
    for c in cards:
        status, level = "", "info"
        if c["kind"] == "engine":
            row = report.get(c["plugin"]) if c["plugin"] else None
            if row is not None:
                if row["active"]:
                    status, level = f"лезвие {c['plugin']} включено: {row['status']}", "ok"
                elif sim and row["status"] == "выключен":
                    status = f"учебный режим: лезвие {c['plugin']} не используется — переключите на «Железо»"
                elif row["status"] == "выключен":
                    status = f"лезвие {c['plugin']} выключено — включается в config.yaml"
                else:
                    status, level = f"лезвие {c['plugin']}: {row['status']}", "warn"
            elif c["program"]:
                found = shutil.which(c["program"])
                status, level = (f"программа {c['program']} установлена", "ok") if found else \
                    (f"программа {c['program']} не установлена", "warn")
            elif c["group"] == "base":
                status, level = "встроено: работает уже сейчас", "ok"
        c["status"], c["status_level"] = status, level
    return cards


def public(card: dict) -> dict:
    return {k: v for k, v in card.items() if not k.startswith("_")}


def get(cards: list, cid: str) -> dict | None:
    return next((c for c in cards if c["id"] == cid), None)


def search(cards: list, q: str = "", kind: str = "") -> list:
    words = [w for w in re.split(r"\s+", (q or "").lower()) if w]
    out = []
    for c in cards:
        if kind and c["kind"] != kind and not (kind == "diy" and c["group"] in ("diy", "mod")):
            continue
        hay = (c["_text"] + " " + c["id"]).lower()
        if all(w in hay for w in words):
            out.append(c)
    return out


def related(cards: list, card: dict) -> dict:
    """Что связано с карточкой: для движка — переходники, для переходника — движки (в обе стороны)."""
    ids = {c["id"]: c for c in cards}
    if card["kind"] == "engine":
        names = list(card["adapters"]) + [c["id"] for c in cards
                                          if c["kind"] == "adapter" and card["id"] in c["engines"]]
        kind = "adapter"
    else:
        names = list(card["engines"]) + [c["id"] for c in cards
                                         if c["kind"] == "engine" and card["id"] in c["adapters"]]
        kind = "engine"
    seen, out = set(), []
    for n in names:
        if n in ids and n not in seen and ids[n]["kind"] == kind:
            seen.add(n)
            out.append({"id": n, "title": ids[n]["title"], "tagline": ids[n]["tagline"], "kind": kind})
    needs = [{"id": n, "title": ids[n]["title"], "tagline": ids[n]["tagline"], "kind": "adapter"}
             for n in card.get("needs") or [] if n in ids]
    return {"items": out, "needs": needs}


def overview(cards: list) -> list:
    """«Как всё связано»: для каждого вида целей — переходники → движок → лезвие → ядро Дирижёра.
    Виды — по ступеням (как в TARGETS), переходники в строке — тоже от простого к сложному."""
    ids = {c["id"]: c for c in cards}
    rows = []
    for target, title in TARGETS.items():
        engines = sorted((c for c in cards if c["kind"] == "engine" and target in c["targets"]), key=sort_key)
        for e in engines:
            adapters = sorted(related(cards, e)["items"], key=lambda a: sort_key(ids[a["id"]]))
            rows.append({"target": target, "target_title": title, "level": TARGET_LEVELS.get(target, DEFAULT_LEVEL),
                         "engine": e["id"], "engine_title": e["title"], "plugin": e["plugin"],
                         "adapters": [{"id": a["id"], "title": a["title"]} for a in adapters]})
    return rows


# ======================= текст для терминала =======================
def text(card: dict, cards: list | None = None) -> list:
    """Карточка строками — для мастера в терминале и команды catalog."""
    out = [card["tagline"], f"ступень {card['level']} из {len(LEVELS)} · {card['level_title']}", ""]

    def block(title, lines):
        if lines:
            out.append(f"■ {title}")
            out.extend(lines)
            out.append("")

    block("Что это", [card["what"]] if card["what"] else [])
    block("Авторы", [f"  • {a['name']}" + (f" — {a['role']}" if a["role"] else "") for a in card["authors"]]
          + ([f"  лицензия: {card['license']}"] if card["license"] else []))
    block("Сильные стороны", [f"  ✓ {s}" for s in card["strengths"]])
    block("Как работает", [f"  {i}. {s['text']}" for i, s in enumerate(card["how"], 1)])
    block("В Дирижёре", [f"  • {s}" for s in card["in_dirizher"]])
    for d in card["drawings"]:
        if d["type"] == "wiring":
            block(d["title"] or "Подключение",
                  [f"  {w['a']:<18} → {w['b']:<18} {COLORS.get(w['color'], '')}" + (f"  ({w['note']})" if w["note"] else "")
                   for w in d["wires"]])
        elif d["type"] == "package":
            pins, n = d["pins"], len(d["pins"])
            lines = []
            for i in range(n // 2):
                left, right = f"{i + 1} {pins[i]}", f"{pins[n - 1 - i]} {n - i}"
                lines.append(f"  {left:>12} ─┤{'●' if i == 0 else ' '}      ├─ {right}")
            block((d["title"] or "Корпус сверху") + " · ● — вывод 1", lines)
        elif d["type"] == "header":
            block(d["title"] or "Выводы", [f"  вывод {num:>2}: {p['label']}" + (f" → {p['to']}" if p["to"] else "")
                                           for num, p in sorted(d["pins"].items(), key=lambda x: int(x[0]))])
        elif d["type"] == "chain":
            block(d["title"] or "Цепочка", ["  " + " → ".join(s["title"] for s in d["steps"])])
    block("Как подключить" if card["kind"] == "adapter" else "Установка",
          [f"  {i}. {s['text']}" + ("".join(f"\n     $ {ln}" if j == 0 else f"\n       {ln}"
                                             for j, ln in enumerate(s["code"].splitlines())))
           for i, s in enumerate(card["steps"], 1)]
          + ([f"  $ {card['install']}"] if card["install"] else []))
    block("Пример", [f"  $ {e['cmd']}" + (f"   # {e['note'] or e['title']}" if (e["note"] or e["title"]) else "")
                     for e in card["examples"]])
    block("Как проверить", [f"  {card['check']}"] if card["check"] else [])
    block("Осторожно", [f"  ⚠ {s}" for s in card["caution"]])
    block("ПРОВЕРИТЬ НА ЖЕЛЕЗЕ", [f"  ☐ {s}" for s in card["verify"]])
    if cards:
        rel = related(cards, card)
        block("Связано", [f"  → {r['title']} [{r['id']}]" for r in rel["items"] + rel["needs"]])
    block("Ссылки", [f"  {link['title']}: {link['url']}" for link in card["links"]])
    return out


# ======================= пополнение =======================
TEMPLATES = {"engine": "catalog/_template_engine.yaml", "adapter": "catalog/_template_adapter.yaml"}


def _clean(value, limit: int = 300) -> str:
    return re.sub(r"[\r\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", str(value or "")).strip()[:limit]


def _lines(value, limit: int = MAX_ITEMS) -> list:
    if isinstance(value, list):
        items = value
    else:
        items = str(value or "").splitlines()
    return [_clean(x, 400) for x in items if _clean(x, 400)][:limit]


def from_fields(f: dict) -> dict:
    """Поля формы «Новая карточка» → словарь карточки (порядок полей — как в шаблоне)."""
    if not isinstance(f, dict):
        raise CatalogError("ожидались поля карточки")
    kind = f.get("kind")
    if kind not in KINDS:
        raise CatalogError(f"вид карточки — {' или '.join(KINDS)}")
    d = {"id": _clean(f.get("id"), 40), "kind": kind, "title": _clean(f.get("title"), 80),
         "tagline": _clean(f.get("tagline"), 160)}
    group = f.get("group")
    if group:
        d["group"] = group
    if str(f.get("level") or "").isdigit():
        d["level"] = int(f["level"])     # пусто — «авто»: ступень по видам микросхем
    if f.get("icon"):
        d["icon"] = f["icon"]
    if kind == "engine":
        authors = []
        for line in _lines(f.get("authors")):
            name, _, role = line.partition("—")
            if not role:
                name, _, role = line.partition(" - ")
            authors.append({"name": name.strip(), "role": role.strip()} if role.strip() else name.strip())
        if authors:
            d["authors"] = authors
        for key in ("license", "what", "plugin", "program", "install", "lesson"):
            if _clean(f.get(key)):
                d[key] = _clean(f.get(key), 600)
        for key in ("strengths", "how", "in_dirizher", "adapters", "targets", "caution", "verify"):
            if _lines(f.get(key)):
                d[key] = _lines(f.get(key))
        examples = []
        for line in _lines(f.get("examples")):
            cmd, _, note = line.partition("#")
            examples.append({"cmd": cmd.strip(), "note": note.strip()} if note.strip() else {"cmd": cmd.strip()})
        if examples:
            d["examples"] = examples
        url = _clean(f.get("link"))
        if url:
            d["links"] = [{"title": "Сайт проекта", "url": url}]
    else:
        for key in ("what", "voltage", "usb", "check", "lesson"):
            if _clean(f.get(key)):
                d[key] = _clean(f.get(key), 600)
        for key in ("engines", "needs", "targets", "steps", "caution", "verify"):
            if _lines(f.get(key)):
                d[key] = _lines(f.get(key))
        wires = []
        for w in f.get("wires") or []:
            if isinstance(w, dict) and (_clean(w.get("a")) or _clean(w.get("b"))):
                wire = {"a": _clean(w.get("a"), 40), "b": _clean(w.get("b"), 40), "color": w.get("color") or "gray"}
                if _clean(w.get("note")):
                    wire["note"] = _clean(w.get("note"), 80)
                wires.append(wire)
        if wires:
            d["drawings"] = [{"type": "wiring", "title": "Подключение",
                              "a_title": _clean(f.get("a_title"), 40) or "Переходник",
                              "b_title": _clean(f.get("b_title"), 40) or "Устройство", "wires": wires[:MAX_ITEMS]}]
    if not d.get("verify"):
        d["verify"] = ["подключение и вывод программы — сверить на своём экземпляре"]
    return d


HEADER = """\
# Карточка справочника «{title}» ({kind}). Создана из конструктора — правьте здесь же, в капоте.
# Поля и рисунки — шаблон catalog/_template_{kind_en}.yaml и справка «Справочник».
"""


def dump(d: dict) -> str:
    return HEADER.format(title=d.get("title", ""), kind=KINDS.get(d.get("kind"), ""),
                         kind_en=d.get("kind", "adapter")) + \
        yaml.safe_dump(d, allow_unicode=True, sort_keys=False, width=110)


def preview(cfg, fields: dict) -> dict:
    """Проверить поля формы без записи: карточка как её покажет интерфейс, YAML, ошибки."""
    d = from_fields(fields)
    errors = validate(d)
    cards, _ = load(cfg)
    if not errors and get(cards, d["id"]) is not None:
        errors.append(f"id «{d['id']}» уже занят")
    card = None
    if not errors:
        card = normalize(d, rel=f"catalog/{FOLDERS[d['kind']]}/{d['id']}.yaml", source="новая")
        card["warnings"] = link_warnings(cards + [card]).get(card["id"], [])
        card = public(card)
    return {"card": card, "yaml": dump(d), "errors": errors}


def create(hood, cfg, fields: dict, actor: str = "") -> str:
    """Сохранить новую карточку через капот (проверка, история; существующее не перезаписывается)."""
    res = preview(cfg, fields)
    if res["errors"]:
        from .hood import HoodError
        raise HoodError("; ".join(res["errors"]))
    d = from_fields(fields)
    rel = f"catalog/{FOLDERS[d['kind']]}/{d['id']}.yaml"
    hood.create(rel, res["yaml"], actor)
    return rel


def create_from_template(hood, kind: str, cid: str, title: str, actor: str = "") -> str:
    """Пустая карточка из шаблона — для тех, кто привык к YAML."""
    from .hood import HoodError
    if kind not in KINDS:
        raise HoodError(f"вид карточки — {' или '.join(KINDS)}")
    if not ID_RE.match(cid or ""):
        raise HoodError("имя карточки — латиница, цифры и _, с буквы (например my_adapter)")
    title = _clean(title, 80).replace('"', "'")
    if not title:
        raise HoodError("напишите название")
    tpl = hood.root / TEMPLATES[kind]
    text_ = tpl.read_text(encoding="utf-8") if tpl.is_file() else f"id: x\nkind: {kind}\ntitle: x\ntagline: x\n"
    text_ = re.sub(r"(?m)^id: .*$", f"id: {cid}", text_, count=1)
    text_ = re.sub(r"(?m)^title: .*$", f"title: \"{title}\"", text_, count=1)
    rel = f"catalog/{FOLDERS[kind]}/{cid}.yaml"
    hood.create(rel, text_, actor)
    return rel


def check_text(cfg, rel: str, text_: str) -> dict:
    """Для капота: ошибки и предупреждения правки карточки (как её загрузит справочник)."""
    res = {"errors": [], "warnings": [], "hints": []}
    try:
        data = yaml.safe_load(text_)
    except yaml.YAMLError as e:
        res["errors"].append(f"YAML: {e}")
        return res
    res["errors"] += validate(data)
    if res["errors"]:
        return res
    cards, _ = load(cfg)
    other = next((c for c in cards if c["id"] == data["id"] and c["path"] != rel), None)
    if other is not None:
        res["errors"].append(f"id «{data['id']}» уже занят файлом {other['path']}")
        return res
    card = normalize(data, rel=rel)
    res["warnings"] += link_warnings([c for c in cards if c["path"] != rel] + [card]).get(card["id"], [])
    res["hints"].append(f"{KINDS[card['kind']].lower()}: рисунков {len(card['drawings'])}, "
                        f"шагов {len(card['steps']) or len(card['how'])}")
    return res
