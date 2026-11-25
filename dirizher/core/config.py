"""Загрузка config.yaml. Относительные пути считаются от папки, где лежит config.yaml.

Рядом может лежать config.tools.yaml — настройки инструментов из формы «Прибор → Инструменты»
(сохраняет капот: проверка по схеме, история версий, слово «ДА»). Из него берётся только секция
`plugins:`; она читается после config.yaml и перекрывает его ключ за ключом. Пустое значение (null)
не перекрывает ничего. Сам config.yaml программа не правит.
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS = {
    "data_dir": "./data",
    "mirror_dir": "",
    "profiles_dir": "./profiles",
    "skills_dir": "./skills",
    "help_dir": "./help",
    "safety": {
        "programmer_voltage": 3.3,
        "voltage_tolerance": 0.3,
        "read_passes": 2,
        "confirm_word": "ДА",
        "confidence_threshold": 0.6,
    },
    "plugins": {},
}
TOOLS_FILE = "config.tools.yaml"


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def read_tools(path: Path) -> tuple:
    """Секция plugins: из config.tools.yaml → ({имя: {ключ: значение}}, ошибка или None)."""
    path = Path(path)
    if not path.is_file():
        return {}, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return {}, f"{path.name} не читается: {e}"
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict) or not all(isinstance(v, dict) for v in plugins.values()):
        return {}, f"{path.name}: ожидалась секция plugins: <имя>: <ключ>: <значение>"
    return {str(k): {str(kk): vv for kk, vv in v.items() if vv is not None} for k, v in plugins.items()}, None


def apply_tools(cfg: dict, tools: dict, old_tools: dict | None = None) -> None:
    """Наложить секции config.tools.yaml на cfg["plugins"]. old_tools — что было наложено раньше:
    эти ключи сначала возвращаются к значениям из config.yaml (или убираются)."""
    base = cfg.get("_plugins_base") or {}
    plugins = cfg.setdefault("plugins", {})
    for name in set(old_tools or {}) | set(tools):
        section = dict(plugins.get(name) or {})
        for key in (old_tools or {}).get(name, {}):
            if key in (base.get(name) or {}):
                section[key] = copy.deepcopy(base[name][key])
            else:
                section.pop(key, None)
        section.update(copy.deepcopy(tools.get(name) or {}))
        plugins[name] = section
    cfg["_plugins_tools"] = copy.deepcopy(tools)


def reload_tools(cfg: dict) -> None:
    """Перечитать config.tools.yaml после сохранения (cfg меняется на месте — его видят все)."""
    path = Path(cfg.get("_tools_path") or Path(cfg.get("_path") or "config.yaml").parent / TOOLS_FILE)
    tools, err = read_tools(path)
    apply_tools(cfg, tools, cfg.get("_plugins_tools") or {})
    cfg["_tools_error"] = err


def load_config(path: Path) -> dict:
    path = Path(path).resolve()
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            _merge(cfg, yaml.safe_load(f) or {})
    if not isinstance(cfg.get("plugins"), dict):
        cfg["plugins"] = {}
    cfg["_plugins_base"] = copy.deepcopy(cfg["plugins"])
    cfg["_tools_path"] = path.parent / TOOLS_FILE
    tools, err = read_tools(cfg["_tools_path"])
    apply_tools(cfg, tools)
    cfg["_tools_error"] = err
    base = path.parent
    for key in ("data_dir", "mirror_dir", "profiles_dir", "skills_dir", "help_dir"):
        value = cfg.get(key)
        if value:
            p = Path(str(value)).expanduser()
            cfg[key] = p if p.is_absolute() else (base / p).resolve()
        else:
            cfg[key] = None
    cfg["_path"] = path
    return cfg
