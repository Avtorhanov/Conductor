"""Модель цели из профиля: настройки лезвия для конкретной модели чипа (поле профиля `tool:`).

Зачем: модель МК (chip, part, device, eeprom) и размер флеша раньше вписывались в настройки лезвия
и повторяли `size` профиля; при смене платы снова правился config. Теперь человек выбирает модель
(профиль) перед сеансом, а настройки лезвия собираются так:

    config.yaml  <  config.tools.yaml  <  profile.tool[<лезвие>]  (+ размер из profile.size)

Что можно в `tool:` — только «свойства модели»: ключи схемы лезвия (core/settings.py), кроме включения,
дополнительных параметров программы и полей, влияющих на напряжение (это свойства переходника, а не чипа).
Проверка (`check`) только читает.
"""
from __future__ import annotations

from .settings import validate

# свойства переходника и человека, а не модели чипа — в профиле запрещены
FORBIDDEN_KEYS = ("enabled", "extra_args", "programmer_voltage")


def settings_for(profile, cls) -> dict:
    """Настройки лезвия cls из профиля: profile.tool[имя] + размер из size (если ключ размера не задан)."""
    section = dict((profile.tool or {}).get(cls.name) or {})
    key = getattr(cls, "size_setting", None)
    if key and profile.size and key not in section:
        section[key] = int(profile.size)
    return section


def overlay(profile, classes: dict) -> dict:
    """{лезвие: настройки} для всех лезвий, упомянутых в profile.tool (и известных системе)."""
    out = {}
    for name in (profile.tool or {}):
        entry = classes.get(name)
        if entry is None:
            continue
        cls = entry[0] if isinstance(entry, tuple) else entry
        out[name] = settings_for(profile, cls)
    return out


def check(profile, classes: dict) -> list:
    """Ошибки поля tool: профиля по схемам лезвий. classes — {имя: класс или (класс, навык)}."""
    errors = []
    for name, section in (profile.tool or {}).items():
        entry = classes.get(name)
        if entry is None:
            errors.append(f"tool.{name}: нет такого лезвия (есть: {', '.join(sorted(classes))})")
            continue
        cls = entry[0] if isinstance(entry, tuple) else entry
        schema = cls.schema()
        voltage = {f["key"] for f in schema if f.get("safety") == "voltage"}
        for key in section:
            if key in FORBIDDEN_KEYS or key in voltage:
                errors.append(f"tool.{name}.{key}: это свойство переходника, а не модели чипа — "
                              f"задаётся в «Прибор → Инструменты», не в профиле")
        issues = validate(schema, {k: v for k, v in section.items() if k not in FORBIDDEN_KEYS},
                          enabled_only=True)
        errors += [f"tool.{name}: {t}" for t in issues["unknown"] + issues["type"]]
        size = cls.image_size(settings_for(profile, cls)) if hasattr(cls, "image_size") else None
        if size is not None and profile.size and size != profile.size:
            errors.append(f"tool.{name}: образ лезвия {size} байт, а size профиля {profile.size} байт")
    return errors


def candidates(profiles, plugin_names, classes: tuple | list = ()) -> list:
    """Профили, из которых можно выбрать модель для этих лезвий: точные (не общие), с tool: для
    хотя бы одного из лезвий; classes — ограничить классами целей (пусто — любые)."""
    out = []
    for p in profiles:
        if p.is_generic or p.content_match or not p.tool:
            continue
        if classes and p.target_class not in classes:
            continue
        if any(n in p.tool for n in plugin_names):
            out.append(p)
    out.sort(key=lambda p: (p.target_class, -p.priority, p.name.lower()))
    return out
