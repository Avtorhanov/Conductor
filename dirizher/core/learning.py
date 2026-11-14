"""Обучение — это накопление проверенного знания, а не догадки.

  * незнакомый чип  → черновик профиля в data/drafts/ (человек проверяет и переносит в profiles/);
  * исправное устройство → эталонный дамп;
  * отзыв «заработало / нет» → статистика рецептов; лучшие предлагаются первыми.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import yaml

from .checks import DEFAULT_CHECKS

DEFAULT_RECIPES = [
    {"id": "restore_reference", "title": "Записать эталонную прошивку", "action": "write_reference"},
    {"id": "restore_image", "title": "Записать официальный образ (--image)", "action": "write_image"},
]

DRAFT_HEADER = """\
# ЧЕРНОВИК ПРОФИЛЯ — создан автоматически {today}.
# Система встретила устройство, для которого нет точного профиля.
# Что сделать:
#   1. Найдите даташит по маркировке чипа и впишите voltage (напряжение питания).
#   2. Проверьте и при желании переименуйте id и name.
#   3. Перенесите файл в profiles/chips/ — с этого момента профиль активен.
"""


class Learning:
    def __init__(self, conductor):
        self.c = conductor
        self.drafts = conductor.data_dir / "drafts"

    def record_unknown(self, det) -> Path:
        self.drafts.mkdir(parents=True, exist_ok=True)
        fp = det.fingerprint
        ident = fp.get("jedec_id") or fp.get("name") or "unknown"
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", str(ident)).strip("_") or "unknown"
        path = self.drafts / f"{det.target_class.replace('.', '_')}_{safe}.yaml"
        if path.exists():
            return path
        draft = {
            "id": f"draft_{safe.lower()}",
            "name": det.summary,
            "class": det.target_class,
            "match": {"jedec_id": fp["jedec_id"]} if fp.get("jedec_id") else {"name": fp.get("name")},
            "priority": 10,
            "voltage": None,
            "size": fp.get("size"),
            "allow_write": True,
            "checks": DEFAULT_CHECKS,
            "recipes": DEFAULT_RECIPES,
            "notes": f"Отпечаток при опознании: {fp}",
        }
        path.write_text(DRAFT_HEADER.format(today=date.today().isoformat())
                        + yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.c.log(f"создан черновик профиля: {path}")
        return path

    def rank(self, profile_id: str, recipes: list) -> list:
        """Сортировка рецептов по проверенной успешности (с поправкой на малую статистику)."""
        scored = []
        for i, r in enumerate(recipes):
            s, f = self.c.memory.recipe_stats(profile_id, r["id"])
            rate = (s + 1) / (s + f + 2)
            scored.append((rate, -i, r, s, f))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [(r, s, f) for _, _, r, s, f in scored]

    def result(self, profile_id: str, recipe_id: str, ok: bool) -> None:
        self.c.memory.bump_recipe(profile_id, recipe_id, ok)
