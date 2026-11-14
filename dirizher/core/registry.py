"""Поиск и загрузка плагинов из папки dirizher/plugins/."""
from __future__ import annotations

import importlib
import inspect
import pkgutil

from .plugin import Plugin


def discover() -> tuple:
    """Найти все классы плагинов. Возвращает (плагины, ошибки импорта)."""
    from .. import plugins as plugins_pkg

    found, errors = {}, []
    for mod_info in pkgutil.iter_modules(plugins_pkg.__path__):
        if mod_info.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"{plugins_pkg.__name__}.{mod_info.name}")
        except Exception as e:                       # сломанный плагин не роняет систему
            errors.append((mod_info.name, str(e)))
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == mod.__name__:
                found[obj.name] = obj
    return found, errors


def load_plugins(conductor, cfg: dict, sim: bool) -> tuple:
    """Создать включённые плагины.

    В режиме --sim работают ТОЛЬКО виртуальные плагины — чтобы учебный запуск
    случайно не тронул подключённое железо.
    """
    classes, errors = discover()
    active, report = [], []
    for name, cls in sorted(classes.items()):
        settings = (cfg.get("plugins") or {}).get(name, {}) or {}
        enabled = (not cls.hardware) if sim else (cls.hardware and bool(settings.get("enabled", False)))
        status = "выключен"
        if enabled:
            inst = cls(conductor, settings)
            ok, why = inst.available()
            status = why if ok else f"недоступен: {why}"
            if ok:
                active.append(inst)
            enabled = ok
        report.append({"name": name, "title": cls.title, "active": enabled, "status": status})
    for name, err in errors:
        report.append({"name": name, "title": "ошибка загрузки", "active": False, "status": err})
    return active, report
