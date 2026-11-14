"""Загрузка config.yaml. Относительные пути считаются от папки, где лежит config.yaml."""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS = {
    "data_dir": "./data",
    "mirror_dir": "",
    "profiles_dir": "./profiles",
    "safety": {
        "programmer_voltage": 3.3,
        "voltage_tolerance": 0.3,
        "read_passes": 2,
        "confirm_word": "ДА",
        "confidence_threshold": 0.6,
    },
    "plugins": {},
}


def _merge(base: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_config(path: Path) -> dict:
    path = Path(path).resolve()
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        with open(path, encoding="utf-8") as f:
            _merge(cfg, yaml.safe_load(f) or {})
    base = path.parent
    for key in ("data_dir", "mirror_dir", "profiles_dir"):
        value = cfg.get(key)
        if value:
            p = Path(str(value)).expanduser()
            cfg[key] = p if p.is_absolute() else (base / p).resolve()
        else:
            cfg[key] = None
    cfg["_path"] = path
    return cfg
