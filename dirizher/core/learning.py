"""Обучение — это накопление проверенного знания, а не догадки.

  * незнакомый чип  → черновик профиля в data/drafts/ (человек проверяет и переносит в profiles/);
  * исправное устройство → эталонный дамп;
  * отзыв «заработало / нет» → статистика рецептов; лучшие предлагаются первыми;
  * неудача (не опознано, не прочиталось, сверка не совпала…) → человек записывает, что помогло;
    в следующий раз при такой же неудаче Дирижёр подскажет это первым. Чем чаще подтверждено —
    тем выше подсказка.
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

    # ---------- «что помогло» ----------
    FAIL_OUTCOMES = ("not_detected", "read_failed", "blocked", "blocked_voltage", "no_action", "write_failed",
                     "not_repaired", "rollback_failed", "tune_failed", "no_params", "capture_failed", "interrupted")

    @staticmethod
    def _norm(text: str) -> str:
        t = re.sub(r"0x[0-9a-f]+|\b[0-9a-f]{6,}\b|\d+", "#", (text or "").lower())
        t = re.sub(r"/[\w./-]+", "<путь>", t)
        return re.sub(r"\s+", " ", t).strip()[:80]

    def signature(self, sid: int) -> str | None:
        """Сигнатура неудачи сеанса: итог | инструмент | первая строка последней ошибки."""
        s = self.c.memory.session(sid)
        if s is None or s["outcome"] not in self.FAIL_OUTCOMES:
            return None
        err = ""
        for e in reversed(self.c.memory.events(sid)):
            if e["level"] == "error":
                err = e["message"].splitlines()[0]
                break
        who = s["plugin"] or s["target_class"] or "-"
        return f"{s['outcome']}|{who}|{self._norm(err)}"

    def remember_fix(self, sid: int, text: str) -> int:
        sig = self.signature(sid)
        text = (text or "").strip()
        if sig is None:
            raise ValueError(f"сеанс №{sid} не был неудачным — записывать нечего")
        if not text:
            raise ValueError("напишите, что помогло")
        fid = self.c.memory.add_fix(sig, text[:300], sid)
        self.c.memory.log(sid, "info", f"записано, что помогло: {text[:300]}")
        return fid

    def confirm_fix(self, fix_id: int, sid: int | None = None) -> None:
        self.c.memory.bump_fix(int(fix_id))
        if sid is not None:
            self.c.memory.log(sid, "info", f"подсказка №{fix_id} помогла снова")

    def hints(self, sid: int, limit: int = 3) -> list:
        """Что помогало раньше при такой же неудаче: сначала точное совпадение, затем тот же итог."""
        sig = self.signature(sid)
        if sig is None:
            return []
        outcome, who, _ = sig.split("|", 2)
        rows, seen = [], set()
        for like in (sig, f"{outcome}|{who}|"):
            for r in self.c.memory.fixes(like):
                key = r["text"].strip().lower()
                if key in seen:
                    continue
                seen.add(key)
                rows.append({"id": r["id"], "text": r["text"], "helped": r["helped"]})
        return rows[:limit]
