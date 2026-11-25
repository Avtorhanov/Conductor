"""Профили — база знаний «что это за устройство и как его лечить».

Профили — это YAML-файлы в папке profiles/. Их пишет и проверяет человек.
Файлы, имя которых начинается с «_», не загружаются (это шаблоны).

Два уровня:
  * профиль чипа — узнаётся по отпечатку (например, JEDEC ID) ещё до чтения;
  * профиль платы — уточняет профиль чипа по содержимому дампа (content_match)
    и знает уникальные области платы (preserve), которые нельзя затирать.

Необязательно: params — карта параметров (core/params.py) для сценария «Настройка»;
bus — настройки шины для лезвия (например, окно регистров Modbus: start, count).
tool — настройки лезвий для этой модели: {probe_rs: {chip: …}, openocd: {target: …, mcu: …}}; размер
берётся из size (core/target.py). Сверка с схемой лезвия — core/target.check().
Профили навыков (skills/<id>/profiles/) загружаются вместе с основными.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .params import normalize as normalize_params

KNOWN_ACTIONS = {"write_reference", "write_image"}


def to_int(value) -> int:
    """Число из YAML: 4096, 0x1000 или "0x1000"."""
    return int(value, 0) if isinstance(value, str) else int(value)


@dataclass
class Profile:
    id: str
    name: str
    target_class: str
    path: Path
    match: dict = field(default_factory=dict)
    content_match: list = field(default_factory=list)
    priority: int = 0
    voltage: float | None = None
    size: int | None = None
    allow_write: bool = True
    checks: list = field(default_factory=list)
    recipes: list = field(default_factory=list)
    preserve: list = field(default_factory=list)
    notes: str = ""
    params: list = field(default_factory=list)
    bus: dict = field(default_factory=dict)
    tool: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict, path: Path) -> "Profile":
        if not isinstance(d, dict):
            raise ValueError("файл не содержит описания профиля")
        for key in ("id", "name", "class"):
            if not d.get(key):
                raise ValueError(f"не заполнено обязательное поле «{key}»")
        recipes = d.get("recipes") or []
        for r in recipes:
            if not r.get("id") or not r.get("action"):
                raise ValueError("у каждого рецепта должны быть id и action")
            if r["action"] not in KNOWN_ACTIONS:
                raise ValueError(f"неизвестное действие рецепта «{r['action']}» "
                                 f"(доступны: {', '.join(sorted(KNOWN_ACTIONS))})")
            r.setdefault("title", r["id"])
        for reg in d.get("preserve") or []:
            to_int(reg["offset"]), to_int(reg["length"])      # проверка формата
        for sig in d.get("content_match") or []:
            to_int(sig["offset"]), bytes.fromhex(sig["hex"])
        bus = d.get("bus") or {}
        if not isinstance(bus, dict):
            raise ValueError("bus — словарь настроек шины")
        tool = d.get("tool") or {}
        if not isinstance(tool, dict) or not all(isinstance(v, dict) for v in tool.values()):
            raise ValueError("tool — настройки лезвий: tool: {<лезвие>: {<ключ>: <значение>}}")
        tool = {str(k): {str(kk): vv for kk, vv in v.items()} for k, v in tool.items()}
        params = normalize_params(d.get("params"), bus)
        size = to_int(d["size"]) if d.get("size") is not None else None
        for p in params:
            if size and p["offset"] + p["size"] > size:
                raise ValueError(f"параметр «{p['id']}» выходит за размер {size} байт")
        return cls(
            id=str(d["id"]), name=str(d["name"]), target_class=str(d["class"]), path=path,
            match=d.get("match") or {}, content_match=d.get("content_match") or [],
            priority=int(d.get("priority", 0)),
            voltage=float(d["voltage"]) if d.get("voltage") is not None else None,
            size=to_int(d["size"]) if d.get("size") is not None else None,
            allow_write=bool(d.get("allow_write", True)),
            checks=d.get("checks") or [], recipes=recipes,
            preserve=d.get("preserve") or [], notes=d.get("notes") or "",
            params=params, bus=bus, tool=tool,
        )

    @property
    def is_generic(self) -> bool:
        return not self.match and not self.content_match

    def match_score(self, det) -> float:
        """1.0 — точное совпадение, 0.3 — общий профиль класса, 0 — не подходит."""
        if det.target_class != self.target_class:
            return 0.0
        if not self.match:
            return 0.3
        for key, expected in self.match.items():
            got = det.fingerprint.get(key)
            options = expected if isinstance(expected, list) else [expected]
            if got is None or str(got).upper() not in {str(o).upper() for o in options}:
                return 0.0
        return 1.0

    def content_matches(self, data: bytes) -> bool:
        if not self.content_match:
            return False
        for sig in self.content_match:
            off, want = to_int(sig["offset"]), bytes.fromhex(sig["hex"])
            if data[off:off + len(want)] != want:
                return False
        return True

    def recipe(self, recipe_id: str) -> dict | None:
        return next((r for r in self.recipes if r["id"] == recipe_id), None)

    def check_specs(self, default: list) -> list:
        """Проверки профиля в едином виде: [{"type": имя, ...параметры}]."""
        specs = []
        for item in self.checks or default:
            specs.append({"type": item} if isinstance(item, str) else dict(item))
        return specs


class ProfileDB:
    def __init__(self, root: Path, extra_roots=()):
        self.root = Path(root)
        self.extra_roots = [Path(r) for r in extra_roots]     # профили навыков
        self.profiles: list = []
        self.errors: list = []
        self.reload()

    def files(self) -> list:
        out = sorted(self.root.rglob("*.yaml")) if self.root.exists() else []
        for extra in self.extra_roots:
            out += sorted(extra.rglob("*.yaml"))
        return out

    def reload(self) -> None:
        self.profiles, self.errors = [], []
        if not self.root.exists():
            self.errors.append((self.root, "папка профилей не найдена"))
        seen = {}
        for path in self.files():
            if path.name.startswith("_"):
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    prof = Profile.from_dict(yaml.safe_load(f), path)
                if prof.id in seen:
                    raise ValueError(f"id «{prof.id}» уже занят файлом {seen[prof.id].name}")
                seen[prof.id] = path
                self.profiles.append(prof)
            except Exception as e:                      # плохой профиль не должен ронять систему
                self.errors.append((path, str(e)))

    def get(self, profile_id: str) -> Profile | None:
        return next((p for p in self.profiles if p.id == profile_id), None)

    def best_for(self, det) -> tuple:
        """Лучший профиль по отпечатку (без чтения содержимого)."""
        scored = [(p, p.match_score(det)) for p in self.profiles if not p.content_match]
        scored = [x for x in scored if x[1] > 0]
        if not scored:
            return None, 0.0
        scored.sort(key=lambda x: (x[1], x[0].priority), reverse=True)
        return scored[0]

    def refine(self, det, data: bytes) -> Profile | None:
        """Уточнение по содержимому дампа: узнать конкретную плату."""
        found = [p for p in self.profiles
                 if p.content_match and p.match_score(det) > 0 and p.content_matches(data)]
        found.sort(key=lambda p: (p.match_score(det), p.priority), reverse=True)
        return found[0] if found else None
