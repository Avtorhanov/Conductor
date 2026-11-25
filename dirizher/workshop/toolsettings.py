"""Настройки инструментов формой: «Прибор → Инструменты → лезвие → Настроить».

Поля берутся из схемы лезвия (Plugin.schema(), core/settings.py). Сохраняется только файл
config.tools.yaml рядом с config.yaml — через капот: проверка по схеме, прежняя версия в историю,
слово «ДА» (настройки влияют на железо). config.yaml программа не правит.

У каждого поля видно, откуда значение: config.tools.yaml (форма), config.yaml, навык (skill.yaml)
или «по умолчанию». В config.tools.yaml пишется только то, что отличается от унаследованного:
пустое поле в форме — ключ убирается из config.tools.yaml (значение вернётся из config.yaml или по умолчанию).

Те же функции зовут веб (/api/tool…), мастер в терминале и команда `python3 -m dirizher tool`.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ..core import settings as S
from ..core.config import TOOLS_FILE, read_tools, reload_tools
from ..core.registry import discover
from ..core.skills import SkillDB

HEADER = ("# Настройки инструментов из формы «Прибор → Инструменты» (python3 -m dirizher tool …).\n"
          "# Читаются после config.yaml и перекрывают его секцию plugins:. Файл ведёт программа —\n"
          "# правьте формой или в капоте; прежние версии — в истории капота (data/hood/).\n")
SOURCE_RU = {"tools": TOOLS_FILE, "config": "config.yaml", "skill": "навык (skill.yaml)", "default": "по умолчанию"}


class ToolSettingsError(ValueError):
    pass


def plugin_classes(cfg: dict, root: Path | None = None) -> dict:
    """{имя: (класс, навык или None)} — все лезвия, включая выключенные."""
    skills_dir = cfg.get("skills_dir") or ((root / "skills") if root else None)
    found, _ = discover(SkillDB(skills_dir).active)
    return found


def _lookup(cfg: dict, name: str, root: Path | None = None) -> tuple:
    classes = plugin_classes(cfg, root)
    if name not in classes:
        known = ", ".join(sorted(classes))
        raise ToolSettingsError(f"нет лезвия «{name}». Есть: {known}")
    return classes[name]


def _tools_path(hood) -> Path:
    return hood.root / TOOLS_FILE


def _file_sections(hood) -> dict:
    tools, err = read_tools(_tools_path(hood))
    if err:
        raise ToolSettingsError(err + " — исправьте его в капоте («Прибор» → капот) или самопроверкой")
    return tools


def _config_section(cfg: dict, name: str) -> dict:
    """Секция plugins: <имя> без наложенного config.tools.yaml (то, что дал config.yaml)."""
    section = dict((cfg.get("plugins") or {}).get(name) or {})
    base = (cfg.get("_plugins_base") or {}).get(name) or {}
    for key in ((cfg.get("_plugins_tools") or {}).get(name) or {}):
        if key in base:
            section[key] = base[key]
        else:
            section.pop(key, None)
    return {k: v for k, v in section.items() if v is not None}


def _inherited(cfg: dict, name: str, skill) -> dict:
    """Значения без config.tools.yaml: навык → config.yaml."""
    out = dict(skill.settings) if skill else {}
    out.update(_config_section(cfg, name))
    return out


def describe(cfg: dict, name: str, root: Path | None = None) -> dict:
    """Поля формы: схема + текущее значение + откуда оно."""
    cls, skill = _lookup(cfg, name, root)
    tools = (cfg.get("_plugins_tools") or {}).get(name) or {}
    effective = dict(skill.settings) if skill else {}
    effective.update({k: v for k, v in ((cfg.get("plugins") or {}).get(name) or {}).items() if v is not None})
    config_section = _config_section(cfg, name)
    schema = cls.schema()
    fields = []
    for f in schema:
        key = f["key"]
        if key in tools:
            source = "tools"
        elif key in config_section:
            source = "config"
        elif skill and key in skill.settings:
            source = "skill"
        else:
            source = "default"
        value = effective.get(key) if source != "default" else f.get("default")
        if key == "programmer_voltage" and f.get("default") is None:
            f = dict(f, default=(cfg.get("safety") or {}).get("programmer_voltage"))
            if source == "default":
                value = None                          # пусто = общее safety.programmer_voltage
        fields.append(dict(f, value=value, shown=S.show(f, value), source=source, source_title=SOURCE_RU[source],
                           default_shown=S.show(f, f.get("default")), example_shown=S.show(f, f.get("example")),
                           type_title=S.TYPE_RU.get(f["type"], f["type"])))
    issues = S.validate(schema, effective)
    if not cls.has_schema():
        issues["unknown"] = []
    return {"name": name, "title": cls.title, "skill": skill.id if skill else None, "hardware": cls.hardware,
            "voltage_check": cls.voltage_check, "fields": fields, "tools_file": TOOLS_FILE,
            "unknown": issues["unknown"], "type_errors": issues["type"], "missing": issues["missing"],
            "enabled": bool(effective.get("enabled", False))}


def render(sections: dict) -> str:
    clean = {k: v for k, v in sorted(sections.items()) if v}
    if not clean:
        return HEADER + "plugins: {}\n"
    return HEADER + yaml.safe_dump({"plugins": clean}, allow_unicode=True, sort_keys=False, default_flow_style=False)


def plan(cfg: dict, hood, name: str, values: dict) -> dict:
    """Что изменится: {"text", "changes", "errors", "section"}. Ничего не записывает.

    values — {ключ: значение}: текст из формы/командной строки или значение из JSON. Пусто — убрать ключ
    из config.tools.yaml (значение вернётся из config.yaml или по умолчанию).
    """
    cls, skill = _lookup(cfg, name, hood.root)
    schema = {f["key"]: f for f in cls.schema()}
    sections = _file_sections(hood)
    section = dict(sections.get(name) or {})
    inherited = _inherited(cfg, name, skill)
    before = dict(inherited, **section)
    errors = []
    for key, raw in (values or {}).items():
        key = str(key).strip()
        f = schema.get(key)
        if f is None and not cls.has_schema():
            f = S.field(key, key)                     # старое лезвие без схемы: ключ как текст
        if f is None:
            if (raw is None or str(raw).strip() == "") and key in section:
                section.pop(key)                      # убрать опечатку из config.tools.yaml
                continue
            guess = S.validate(list(schema.values()), {key: raw})["unknown"]
            errors.append(f"{name}: " + (guess[0] if guess else f"неизвестный ключ «{key}»"))
            continue
        try:
            value = S.coerce(f, raw)
        except ValueError as e:
            errors.append(str(e))
            continue
        if value is None or (key in inherited and inherited[key] == value):
            section.pop(key, None)
        else:
            section[key] = value
    after = dict(inherited, **section)
    changes = []
    for key in sorted(set(before) | set(after), key=lambda k: list(schema).index(k) if k in schema else 999):
        if before.get(key) != after.get(key):
            f = schema.get(key) or S.field(key, key)
            if key == "programmer_voltage" and f.get("default") is None:
                f = dict(f, default=f"общее {(cfg.get('safety') or {}).get('programmer_voltage')}")
            changes.append({"key": key, "title": f["title"], "safety": f.get("safety"),
                            "before": S.show(f, before.get(key, f.get("default"))) or "—",
                            "after": S.show(f, after.get(key, f.get("default"))) or "—"})
    new_sections = copy.deepcopy(sections)
    new_sections[name] = section
    return {"text": render(new_sections), "changes": changes, "errors": errors, "section": section,
            "settings": after}


def check_text(cfg: dict, root: Path, text: str, old_text: str | None) -> dict:
    """Проверка содержимого config.tools.yaml (для капота): структура, схема каждого лезвия, напряжение."""
    res = {"errors": [], "warnings": [], "hints": []}
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        res["errors"].append(f"YAML: {e}")
        return res
    if not isinstance(data, dict):
        res["errors"].append("ожидался словарь с секцией plugins:")
        return res
    extra = [k for k in data if k != "plugins"]
    if extra:
        res["errors"].append(f"в {TOOLS_FILE} бывает только секция plugins: (лишнее: {', '.join(map(str, extra))}) — "
                             f"остальные настройки — в config.yaml")
    plugins = data.get("plugins") or {}
    if not isinstance(plugins, dict) or not all(isinstance(v, dict) or v is None for v in plugins.values()):
        res["errors"].append("ожидалось plugins: <имя>: <ключ>: <значение>")
        return res
    try:
        old = (yaml.safe_load(old_text) or {}).get("plugins") or {} if old_text else {}
        if not isinstance(old, dict):
            old = {}
    except (yaml.YAMLError, AttributeError):
        old = {}
    classes = plugin_classes(cfg, root)
    for name, section in plugins.items():
        section = section or {}
        if name not in classes:
            res["warnings"].append(f"лезвия «{name}» нет — секция не действует")
            continue
        cls, _ = classes[name]
        schema = cls.schema()
        issues = S.validate(schema, section, enabled_only=True)
        if not cls.has_schema():
            issues["unknown"] = []
        res["errors"] += [f"{name}: {t}" for t in issues["type"]]
        res["warnings"] += [f"{name}: {t}" for t in issues["unknown"]]
        old_section = old.get(name) or {} if isinstance(old.get(name) or {}, dict) else {}
        for f in schema:
            if f.get("safety") == "voltage" and section.get(f["key"]) != old_section.get(f["key"]):
                res["warnings"].append(f"{name}: меняется «{f['title']}»: {S.show(f, old_section.get(f['key'])) or 'не задано'}"
                                       f" → {S.show(f, section.get(f['key'])) or 'не задано'}. Проверьте, что "
                                       f"переходник действительно выдаёт это напряжение")
    res["hints"].append(f"лезвий в файле: {len(plugins)}")
    return res


def status_with(cfg: dict, name: str, section: dict | None = None) -> dict:
    """Строка отчёта лезвия (как в «Прибор») — с текущими настройками или с предлагаемой секцией
    config.tools.yaml. Железо не опрашивается: только available()."""
    from ..interface.guide import open_readonly
    view = dict(cfg)
    if section is not None:
        view["plugins"] = copy.deepcopy(cfg.get("plugins") or {})
        view["_plugins_tools"] = copy.deepcopy(cfg.get("_plugins_tools") or {})
        from ..core.config import apply_tools
        tools = copy.deepcopy(view["_plugins_tools"])
        tools[name] = section
        apply_tools(view, tools, cfg.get("_plugins_tools") or {})
    with open_readonly(view, False) as c:
        row = next((r for r in c.plugin_report if r["name"] == name), None)
    return row or {"name": name, "active": False, "status": "не найдено", "warnings": []}


def check(cfg: dict, hood, name: str, values: dict) -> dict:
    """«Проверить» — без записи: план, проверка файла, каким станет статус лезвия."""
    p = plan(cfg, hood, name, values)
    out = {"changes": p["changes"], "errors": list(p["errors"]), "warnings": [], "hints": [], "needs_word": True,
           "word": hood.word}
    if not p["errors"]:
        old = _tools_path(hood).read_text(encoding="utf-8") if _tools_path(hood).is_file() else None
        res = check_text(cfg, hood.root, p["text"], old)
        out["errors"] += res["errors"]
        out["warnings"] += res["warnings"]
        if not out["errors"]:
            out["status"] = status_with(cfg, name, p["section"])
    return out


def save(cfg: dict, hood, name: str, values: dict, word=None, actor: str = "") -> dict:
    """«Сохранить»: проверка, слово «ДА», запись config.tools.yaml через капот, статус лезвия заново."""
    p = plan(cfg, hood, name, values)
    if p["errors"]:
        return {"ok": False, "errors": p["errors"], "warnings": [], "hints": [], "changes": p["changes"]}
    if not p["changes"] and not _tools_path(hood).is_file():
        return {"ok": True, "unchanged": True, "errors": [], "warnings": [], "hints": ["ничего не меняется"],
                "changes": [], "status": status_with(cfg, name)}
    res = hood.save(TOOLS_FILE, p["text"], word, actor, note=f"настройки {name}")
    res["changes"] = p["changes"]
    if res.get("ok"):
        reload_tools(cfg)
        res["status"] = status_with(cfg, name)
    return res


# ---------- текст: командная строка и мастер в терминале ----------
def show_fields(ui, desc: dict) -> None:
    ui.header(f"{desc['title']} ({desc['name']}" + (f", навык {desc['skill']}" if desc["skill"] else "") + ")")
    for f in desc["fields"]:
        mark = "⚡" if f.get("safety") == "voltage" else ("*" if f["required"] else " ")
        value = f["shown"] or (f"(общее {f['default_shown']})" if f["key"] == "programmer_voltage" and f["default_shown"]
                               else "—")
        ui.out(f" {mark} {f['key']:<18} = {value:<22} [{f['source_title']}]  {f['title']}")
    for t in desc["unknown"] + desc["type_errors"]:
        ui.warn(t)
    ui.info("* — обязательное; ⚡ — влияет на напряжение (меняется только со словом подтверждения)")


def show_result(ui, res: dict) -> None:
    for ch in res.get("changes") or []:
        ui.out(f"   {'⚡ ' if ch.get('safety') == 'voltage' else ''}{ch['title']} ({ch['key']}): "
               f"{ch['before']} → {ch['after']}")
    for t in res.get("errors") or []:
        ui.error(t)
    for t in res.get("warnings") or []:
        ui.warn(t)
    st = res.get("status")
    if st:
        (ui.ok if st.get("active") else ui.info)(f"статус лезвия: {st['status']}")


def apply_interactive(cfg: dict, hood, ui, name: str, values: dict, check_only: bool = False,
                      actor: str = "терминал прибора") -> int:
    """Проверить и (после слова) сохранить. 0 — сохранено или проверено без ошибок, 1 — нет."""
    res = check(cfg, hood, name, values)
    if not res["changes"] and not res["errors"]:
        ui.info("ничего не меняется")
        return 0
    ui.info(f"изменения в {TOOLS_FILE}:")
    show_result(ui, res)
    if res["errors"]:
        return 1
    if check_only:
        ui.info("только проверка — ничего не записано")
        return 0
    volt = any(ch.get("safety") == "voltage" for ch in res["changes"])
    text = ("Меняется напряжение адаптера — ошибка может сжечь чип. " if volt else "") + \
        f"Сохранить настройки {name} в {TOOLS_FILE}?"
    if not ui.confirm_word(text, hood.word):
        ui.warn("отменено — ничего не записано")
        return 1
    out = save(cfg, hood, name, values, hood.word, actor)
    if not out.get("ok"):
        show_result(ui, out)
        return 1
    ui.ok(f"сохранено в {TOOLS_FILE} (прежняя версия — в истории капота)")
    st = out.get("status")
    if st:
        (ui.ok if st.get("active") else ui.info)(f"статус лезвия: {st['status']}")
    return 0


def cli(cfg: dict, name: str | None, sets: list, check_only: bool, ui) -> int:
    from .hood import Hood
    hood = Hood(cfg)
    if not name:
        from ..interface.guide import open_readonly
        with open_readonly(cfg, False) as c:
            rows = c.plugin_report
        ui.header("Инструменты: python3 -m dirizher tool <имя> [--set ключ=значение]")
        for row in rows:
            ui.out(f" {'✔' if row['active'] else '·'} {row['name']:<14} {row['status']}")
            for t in row.get("warnings") or []:
                ui.warn(f"   {t}")
        return 0
    try:
        desc = describe(cfg, name, hood.root)
    except ToolSettingsError as e:
        ui.error(str(e))
        return 1
    if not sets:
        show_fields(ui, desc)
        ui.info(f"статус: {status_with(cfg, name)['status']}")
        ui.info(f"изменить: python3 -m dirizher tool {name} --set ключ=значение (пусто после = — вернуть унаследованное)")
        return 0
    values = {}
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            ui.error(f"«{item}»: пишите ключ=значение, например enabled=да")
            return 1
        values[key.strip()] = value.strip()
    try:
        return apply_interactive(cfg, hood, ui, name, values, check_only)
    except ToolSettingsError as e:
        ui.error(str(e))
        return 1


def ask_fields(cfg: dict, hood, ui, ask_text, name: str) -> int | None:
    """Мастер: поле за полем (Enter — оставить, «-» — вернуть унаследованное)."""
    desc = describe(cfg, name, hood.root)
    show_fields(ui, desc)
    values = {}
    for f in desc["fields"]:
        hint = f["type_title"]
        if f.get("choices"):
            hint = " / ".join(map(str, f["choices"]))
        elif f.get("example_shown"):
            hint += f", например {f['example_shown']}"
        if f.get("safety") == "voltage":
            ui.warn(f"{f['title']}: влияет на напряжение — ставьте то, что на самом деле выдаёт переходник")
        raw = ask_text(f"   {f['title']} ({f['key']}) = {f['shown'] or '—'} [{hint}] — новое "
                       f"(Enter — оставить, «-» — убрать из {TOOLS_FILE}): ")
        if raw == "-":
            values[f["key"]] = ""
        elif raw:
            values[f["key"]] = raw
    if not values:
        ui.info("ничего не меняем")
        return None
    return apply_interactive(cfg, hood, ui, name, values)
