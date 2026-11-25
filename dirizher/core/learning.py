"""Обучение — это накопление проверенного знания, а не догадки.

  * незнакомый чип  → черновик профиля в data/drafts/ (человек проверяет и переносит в profiles/);
  * исправное устройство → эталонный дамп и черновик профиля ПЛАТЫ (подпись в начале дампа,
    разметка из определителей, сигнатуры binwalk) — тоже в data/drafts/;
  * отзыв «заработало / нет» → статистика рецептов; лучшие предлагаются первыми;
  * неудача (не опознано, не прочиталось, сверка не совпала…) → человек записывает, что помогло;
    в следующий раз при такой же неудаче Дирижёр подскажет это первым. Чем чаще подтверждено —
    тем выше подсказка.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from datetime import date
from pathlib import Path

import yaml

from . import layout_detect
from .checks import DEFAULT_CHECKS, is_fill
from .profiles import to_int

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

BOARD_HEADER = """\
# ЧЕРНОВИК ПРОФИЛЯ ПЛАТЫ — создан автоматически {today}, когда дамп сохранили как эталон.
# Основа — профиль чипа «{base}». Что сделать:
#   1. content_match — первые непустые байты дампа. Убедитесь, что они отличают именно эту плату
#      (а не любую с таким же загрузчиком); при необходимости возьмите подпись подальше
#      (версия прошивки, название модели).
#   2. layout (если есть) построен по содержимому — сверьте разделы с прошивкой.
#   3. binwalk_expect (если есть) — сигнатуры, которые binwalk нашёл в ИСПРАВНОМ дампе.
#   4. Переименуйте id и name и перенесите файл в profiles/boards/ — профиль станет активным.
"""
SIGNATURE_LEN = 16
SIGNATURE_SCAN = 0x10000          # подпись ищется в первых 64 КБ
BINWALK_LINE = re.compile(r"^\s*\d+\s+0x[0-9A-Fa-f]+\s+(.+?)\s*$")


def content_signature(data: bytes, preserve=()) -> dict | None:
    """Первые SIGNATURE_LEN непустых байтов (не 0xFF и не 0x00 целиком), вне уникальных областей."""
    skip = [(to_int(r["offset"]), to_int(r["offset"]) + to_int(r["length"])) for r in preserve or []]
    for off in range(0, min(len(data), SIGNATURE_SCAN) - SIGNATURE_LEN + 1, SIGNATURE_LEN):
        chunk = data[off:off + SIGNATURE_LEN]
        if is_fill(chunk, 0xFF) or is_fill(chunk, 0x00):
            continue
        if any(a < off + SIGNATURE_LEN and off < b for a, b in skip):
            continue
        return {"offset": f"0x{off:X}", "hex": chunk.hex().upper()}
    return None


def binwalk_signatures(text: str, limit: int = 8) -> list:
    """Названия сигнатур из вывода binwalk (2.x и 3.x): описание до первой запятой, без повторов.
    ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: вывод binwalk 3 на настоящем дампе."""
    out = []
    for line in text.splitlines():
        m = BINWALK_LINE.match(line)
        if not m:
            continue
        name = m.group(1).split(",", 1)[0].strip()
        if name and name not in out:
            out.append(name)
    return out[:limit]


def _raw_layout(rows: list) -> list:
    out = []
    for r in rows:
        item = {"id": r["id"], "title": r["title"], "offset": f"0x{r['offset']:X}",
                "length": f"0x{r['length']:X}", "kind": r["kind"]}
        if r["preserve"]:
            item["preserve"] = True
        item["restore"] = r["restore"]
        if r.get("help"):
            item["help"] = r["help"]
        out.append(item)
    return out


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

    def record_board(self, profile, target_class: str, data: bytes, sha: str) -> Path | None:
        """Черновик профиля платы по эталонному дампу. Уже есть такой файл — возвращает его.
        Без подписи (дамп пустой) черновик не создаётся."""
        layout, auto = list(profile.layout), None
        if not layout and profile.layout_auto:
            auto = layout_detect.detect(data, profile.layout_auto, profile.size or len(data))
            if auto is not None and auto.ok:
                layout = auto.rows
        preserve = [r for r in profile.preserve]
        if layout and not profile.layout:
            have = {(to_int(r["offset"]), to_int(r["length"])) for r in preserve}
            preserve += [{"offset": r["offset"], "length": r["length"], "why": r["title"]}
                         for r in layout if r["preserve"] and (r["offset"], r["length"]) not in have]
        sig = content_signature(data, preserve)
        if sig is None:
            return None
        self.drafts.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9_]+", "_", profile.id.lower()).strip("_") or "board"
        path = self.drafts / f"board_{safe}_{sha[:8]}.yaml"
        if path.exists():
            return path
        checks = [dict(c) if isinstance(c, dict) else c for c in (profile.checks or DEFAULT_CHECKS)]
        found = self._binwalk(data)
        if found:
            checks = [c for c in checks if not (isinstance(c, dict) and c.get("type") == "binwalk_expect")]
            checks.append({"type": "binwalk_expect", "expect": found})
        draft = {
            "id": f"draft_board_{safe}_{sha[:8]}",
            "name": f"Плата на «{profile.name}» (черновик)",
            "class": target_class,
            "match": dict(profile.match),
            "content_match": [sig],
            "priority": int(profile.priority) + 10,
            "voltage": profile.voltage,
            "size": profile.size or len(data),
            "allow_write": profile.allow_write,
            "checks": checks,
            "recipes": [dict(r) for r in profile.recipes] or DEFAULT_RECIPES,
        }
        old_preserve = [r for r in profile.preserve
                        if not any((to_int(r["offset"]), to_int(r["length"])) == (x["offset"], x["length"])
                                   for x in profile.layout if x["preserve"])]
        if old_preserve:
            draft["preserve"] = [{"offset": f"0x{to_int(r['offset']):X}", "length": f"0x{to_int(r['length']):X}",
                                  "why": r.get("why", "уникальные данные платы")} for r in old_preserve]
        if layout:
            draft["layout"] = _raw_layout(layout)
            if not any(r["action"] == "write_reference_regions" for r in draft["recipes"]):
                draft["recipes"].append({"id": "restore_regions", "title": "Восстановить только повреждённые разделы",
                                         "action": "write_reference_regions"})
        if profile.tool:
            draft["tool"] = {k: dict(v) for k, v in profile.tool.items()}
        notes = [f"Эталон sha256 {sha}."]
        if auto is not None and auto.ok:
            notes.append(f"Разделы — по содержимому ({auto.title}).")
        draft["notes"] = " ".join(notes)
        path.write_text(BOARD_HEADER.format(today=date.today().isoformat(), base=profile.id)
                        + yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
        self.c.log(f"создан черновик профиля платы: {path}")
        return path

    def _binwalk(self, data: bytes) -> list:
        if not shutil.which("binwalk"):
            return []
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "reference.bin"
            path.write_bytes(data)
            res = self.c.runner.run(["binwalk", str(path)], timeout=300)
        return binwalk_signatures(res.out) if res.ok else []

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
