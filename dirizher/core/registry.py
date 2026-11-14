"""Поиск и загрузка плагинов: из папки dirizher/plugins/ и из навыков (skills/<id>/skill.py).

Изменённый файл плагина (например, после правки в «капоте») перечитывается при следующем
создании Conductor — перезапуск не нужен.
"""
from __future__ import annotations

import importlib
import inspect
import os
import pkgutil

from .plugin import Plugin
from .skills import load_code

_MTIMES: dict = {}          # имя модуля → время изменения файла при загрузке


def _import_fresh(name: str):
    mod = importlib.import_module(name)
    path = getattr(mod, "__file__", None)
    if path:
        mtime = os.stat(path).st_mtime_ns
        if name in _MTIMES and _MTIMES[name] != mtime:
            mod = importlib.reload(mod)
        _MTIMES[name] = mtime
    return mod


def _classes(mod) -> list:
    return [obj for _, obj in inspect.getmembers(mod, inspect.isclass)
            if issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == mod.__name__]


def discover(skills=()) -> tuple:
    """Найти все классы плагинов. Возвращает ({имя: (класс, навык или None)}, ошибки)."""
    from .. import plugins as plugins_pkg

    found, errors = {}, []
    for mod_info in pkgutil.iter_modules(plugins_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = _import_fresh(f"{plugins_pkg.__name__}.{mod_info.name}")
        except Exception as e:                       # сломанный плагин не роняет систему
            errors.append((mod_info.name, str(e)))
            continue
        for obj in _classes(mod):
            found[obj.name] = (obj, None)
    for skill in skills:
        mod = load_code(skill)
        if mod is None:
            if skill.errors:
                errors.append((f"навык {skill.id}", "; ".join(skill.errors)))
            continue
        for obj in _classes(mod):
            if obj.name in found:
                errors.append((f"навык {skill.id}", f"лезвие «{obj.name}» уже есть — переименуйте name"))
                continue
            found[obj.name] = (obj, skill)
    return found, errors


def load_plugins(conductor, cfg: dict, sim: bool, skills=()) -> tuple:
    """Создать включённые плагины.

    В режиме --sim работают ТОЛЬКО виртуальные плагины — чтобы учебный запуск
    случайно не тронул подключённое железо.
    Настройки лезвия из навыка = settings из skill.yaml, поверх — plugins: <имя> из config.yaml.
    """
    classes, errors = discover(skills)
    active, report = [], []
    for name, (cls, skill) in sorted(classes.items()):
        settings = dict(skill.settings) if skill else {}
        settings.update((cfg.get("plugins") or {}).get(name, {}) or {})
        enabled = (not cls.hardware) if sim else (cls.hardware and bool(settings.get("enabled", False)))
        status = "выключен"
        if enabled:
            try:
                inst = cls(conductor, settings)
                ok, why = inst.available()
            except Exception as e:                   # ошибка в навыке не роняет систему
                ok, why = False, f"ошибка: {e}"
            status = why if ok else f"недоступен: {why}"
            if ok:
                active.append(inst)
            enabled = ok
        report.append({"name": name, "title": cls.title, "active": enabled, "status": status,
                       "skill": skill.id if skill else None})
    for name, err in errors:
        report.append({"name": name, "title": "ошибка загрузки", "active": False, "status": err, "skill": None})
    return active, report
