"""Капот: код и настройки каждого раздела — в окне, с проверкой, историей версий и откатом.

Править можно (когда капот открыт):
  навыки (skills/**), профили (profiles/**), справку (help/*.md), вопросы Штурмана (guide.yaml),
  лезвия (dirizher/plugins/*.py — кроме общих помощников _common.py и учебного симулятора).
Только смотреть:
  ядро (dirizher/core/**) — порядок работы и защита; config.yaml; интерфейсы; помощники-процессы.
  Их меняют через git с тестами (PRINCIPLES.md, п. 1–4 и 11).

Сохранение = проверка → прежняя версия в историю → атомарная запись → новая версия в историю.
  * YAML: разбор; профиль — как его загрузит ядро (и без повторов id); паспорт навыка; Штурман;
  * код: синтаксис, сторож (core/skills.lint) и пробный импорт ОТДЕЛЬНЫМ процессом;
  * код и профили влияют на работу с железом — для них нужно слово подтверждения («ДА»).
История (data/hood/) не удаляется; любую версию можно вернуть — это тоже правка с историей.
Капот закрыт по умолчанию: hood.edit: true в config.yaml или «Открыть капот» на экране прибора.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath

import yaml

from ..core.profiles import Profile, ProfileDB
from ..core.skills import SkillDB, lint, validate_manifest
from ..core.storage import sha256

TEXT_SUFFIXES = {".py", ".yaml", ".yml", ".md", ".txt"}
MAX_BYTES = 512 * 1024
GUIDE = "dirizher/interface/guide.yaml"
CORE_REASON = "ядро: порядок работы и защита — меняется только через git с тестами (PRINCIPLES.md, п. 1–4)"
SERVICE_REASON = "служебный код — меняется через git с тестами"
VIEW_REASONS = {
    "config.yaml": "настройки прибора — правятся на самом приборе (там же открывается капот)",
    "dirizher/plugins/_common.py": "общий сторож лезвий (запрет опасных параметров) — только через git с тестами",
    "dirizher/plugins/simulator.py": "учебный симулятор — эталон для автотестов, только через git",
    "dirizher/plugins/__init__.py": SERVICE_REASON,
    "dirizher/plugins/_template.py": "шаблон лезвия — новые лезвия добавляйте навыком",
}
AREAS = {
    "guide": ("Штурман", [GUIDE, "dirizher/interface/guide.py"]),
    "work": ("Работа", ["dirizher/core/pipeline.py", "dirizher/core/safety.py", "dirizher/core/params.py",
                        "dirizher/core/checks.py"]),
    "log": ("Журнал", ["dirizher/core/memory.py", "dirizher/core/learning.py", "dirizher/core/storage.py"]),
    "dev": ("Навыки", ["skills/**/*", "profiles/**/*.yaml", "dirizher/core/skills.py"]),
    "device": ("Прибор", ["config.yaml", "dirizher/plugins/*.py", "dirizher/helpers/*.py"]),
    "help": ("Справка", ["help/*.md"]),
}
HELP_FOR = {".py": "hood", ".md": "help_write"}

IMPORT_CHECK = r"""
import importlib.util, inspect, json, sys
path, name = sys.argv[1], sys.argv[2]
from dirizher.core.checks import CHECKS
from dirizher.core.plugin import Plugin
before = set(CHECKS)
spec = importlib.util.spec_from_file_location(name, path)
mod = importlib.util.module_from_spec(spec)
sys.modules[name] = mod
spec.loader.exec_module(mod)
plugins = []
for _, obj in inspect.getmembers(mod, inspect.isclass):
    if issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == name:
        missing = [m for m in ("available", "probe", "read", "write") if getattr(obj, m) is getattr(Plugin, m)]
        plugins.append({"name": getattr(obj, "name", "?"), "title": getattr(obj, "title", "?"), "missing": missing})
print("DIRIZHER-CHECK " + json.dumps({"plugins": plugins, "checks": sorted(set(CHECKS) - before)},
                                     ensure_ascii=False))
"""


class HoodError(ValueError):
    pass


def classify(rel: str) -> tuple:
    """("edit" | "view" | None, причина). None — капот такой файл не открывает."""
    rel = str(rel or "").replace("\\", "/")
    pp = PurePosixPath(rel)
    parts = pp.parts
    if not rel or rel.startswith("/") or ".." in parts or any(p.startswith(".") for p in parts):
        return None, "недопустимый путь"
    if "__pycache__" in parts or parts[0] in ("data", "tests"):
        return None, "этот файл в капоте не открывается"
    if pp.suffix not in TEXT_SUFFIXES:
        return None, "капот открывает только текст: .py .yaml .md .txt"
    if parts[0] == "skills" and len(parts) >= 3:
        return "edit", ""
    if parts[0] == "profiles" and pp.suffix in (".yaml", ".yml"):
        return "edit", ""
    if parts[0] == "help" and len(parts) == 2 and pp.suffix == ".md":
        return "edit", ""
    if rel == GUIDE:
        return "edit", ""
    if rel in VIEW_REASONS:
        return "view", VIEW_REASONS[rel]
    if parts[:2] == ("dirizher", "plugins") and len(parts) == 3 and pp.suffix == ".py":
        return "edit", ""
    if parts[:2] == ("dirizher", "core"):
        return "view", CORE_REASON
    if parts[0] == "dirizher":
        return "view", SERVICE_REASON
    if parts[0] == "docs" or rel in ("PRINCIPLES.md", "README.md", "CHANGELOG.md"):
        return "view", "документация проекта — обновляется в конце сессии разработки"
    return None, "этот файл в капоте не открывается"


def kind_of(rel: str) -> str:
    suffix = PurePosixPath(rel).suffix
    return {".py": "python", ".yaml": "yaml", ".yml": "yaml", ".md": "markdown"}.get(suffix, "text")


def is_profile(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    if PurePosixPath(rel).suffix not in (".yaml", ".yml") or PurePosixPath(rel).name.startswith("_"):
        return False
    return parts[0] == "profiles" or (parts[0] == "skills" and len(parts) >= 4 and parts[2] == "profiles")


def needs_word(rel: str) -> bool:
    """Код и профили влияют на работу с железом — для них нужно слово подтверждения."""
    k = kind_of(rel)
    if k == "python":
        return True
    return k == "yaml" and rel != GUIDE


class Hood:
    def __init__(self, cfg: dict, root: Path | None = None):
        self.cfg = cfg
        self.root = Path(root or Path(cfg.get("_path") or "config.yaml").resolve().parent)
        self.store = Path(cfg["data_dir"]) / "hood"
        self.history_dir = self.store / "history"
        self.journal = self.store / "journal.jsonl"
        self.word = str((cfg.get("safety") or {}).get("confirm_word", "ДА"))

    # ---------- пути ----------
    def path(self, rel: str, must_exist: bool = True) -> Path:
        mode, reason = classify(rel)
        if mode is None:
            raise HoodError(reason)
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise HoodError("путь вне папки Дирижёра")
        if must_exist and not p.is_file():
            raise HoodError(f"файла {rel} нет")
        return p

    def word_ok(self, word) -> bool:
        return str(word or "").strip().lower() in {self.word.lower(), "yes"}

    # ---------- обзор ----------
    def areas(self) -> list:
        return [{"id": k, "title": v[0]} for k, v in AREAS.items()]

    def files(self, area: str) -> list:
        if area not in AREAS:
            raise HoodError(f"нет раздела «{area}»")
        seen, out = set(), []
        for pattern in AREAS[area][1]:
            for p in sorted(self.root.glob(pattern)) if any(ch in pattern for ch in "*?[") \
                    else [self.root / pattern]:
                if not p.is_file():
                    continue
                rel = p.relative_to(self.root).as_posix()
                mode, reason = classify(rel)
                if mode is None or rel in seen:
                    continue
                seen.add(rel)
                out.append({"path": rel, "mode": mode, "reason": reason, "size": p.stat().st_size,
                            "kind": kind_of(rel)})
        return out

    def read(self, rel: str) -> dict:
        p = self.path(rel)
        if p.stat().st_size > MAX_BYTES:
            raise HoodError("файл слишком большой для капота")
        mode, reason = classify(rel)
        return {"path": rel, "text": p.read_text(encoding="utf-8", errors="replace"), "mode": mode,
                "reason": reason, "kind": kind_of(rel), "needs_word": needs_word(rel),
                "versions": len(self.history(rel))}

    # ---------- проверка ----------
    def _profiles_except(self, rel: str) -> list:
        db = ProfileDB(self.root / "profiles", SkillDB(self.root / "skills").profile_dirs())
        target = (self.root / rel).resolve()
        return [p for p in db.profiles if Path(p.path).resolve() != target]

    def _import_check(self, rel: str, text: str) -> dict:
        parts = PurePosixPath(rel).parts
        stem = PurePosixPath(rel).stem
        if parts[:2] == ("dirizher", "plugins"):
            name = f"dirizher.plugins._hood_check_{stem}"
        else:
            name = f"dirizher_skill_check_{'_'.join(parts[1:-1])}_{stem}"
        with tempfile.TemporaryDirectory(prefix="dirizher_hood_") as td:
            tmp = Path(td) / f"{stem}.py"
            tmp.write_text(text, encoding="utf-8")
            env = dict(os.environ, PYTHONPATH=str(self.root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                       PYTHONDONTWRITEBYTECODE="1")
            try:
                p = subprocess.run([sys.executable, "-c", IMPORT_CHECK, str(tmp), name], cwd=str(self.root),
                                   env=env, capture_output=True, text=True, timeout=30)
            except subprocess.TimeoutExpired:
                return {"error": "пробный импорт не закончился за 30 с — код что-то ждёт при загрузке"}
        for line in p.stdout.splitlines():
            if line.startswith("DIRIZHER-CHECK "):
                return json.loads(line[len("DIRIZHER-CHECK "):])
        tail = [ln for ln in (p.stderr or "").strip().splitlines() if ln.strip()]
        return {"error": "пробный импорт не прошёл: " + (tail[-1] if tail else f"код {p.returncode}")}

    def check(self, rel: str, text: str) -> dict:
        res = {"errors": [], "warnings": [], "hints": [], "needs_word": needs_word(rel)}
        mode, reason = classify(rel)
        if mode != "edit":
            res["errors"].append(reason or "этот файл только для просмотра")
            return res
        if not isinstance(text, str):
            res["errors"].append("ожидался текст")
            return res
        if len(text.encode("utf-8")) > MAX_BYTES:
            res["errors"].append(f"больше {MAX_BYTES // 1024} КБ")
            return res
        k = kind_of(rel)
        parts = PurePosixPath(rel).parts
        if k == "python":
            blocks, warns = lint(text, rel)
            res["errors"] += blocks
            res["warnings"] += warns
            if not blocks:
                out = self._import_check(rel, text)
                if "error" in out:
                    res["errors"].append(out["error"])
                else:
                    for pl in out["plugins"]:
                        res["hints"].append(f"лезвие «{pl['name']}» — {pl['title']}")
                        if pl["missing"]:
                            res["warnings"].append(f"лезвие «{pl['name']}»: не написаны {', '.join(pl['missing'])}")
                    if out["checks"]:
                        res["hints"].append("проверки: " + ", ".join(out["checks"]))
        elif k == "yaml":
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as e:
                mark = getattr(e, "problem_mark", None)
                res["errors"].append(f"YAML: {getattr(e, 'problem', None) or e}"
                                     + (f" (строка {mark.line + 1})" if mark else ""))
                return res
            if rel == GUIDE:
                from ..interface import guide
                res["errors"] += guide.validate(data)
            elif parts[0] == "skills" and parts[-1] == "skill.yaml":
                res["errors"] += validate_manifest(data, parts[1])
                if not res["errors"] and not (data.get("settings") or {}).get("enabled", False):
                    res["hints"].append("лезвие навыка выключено (settings: enabled: false)")
            elif is_profile(rel):
                try:
                    prof = Profile.from_dict(data, self.root / rel)
                except Exception as e:
                    res["errors"].append(f"профиль: {e}")
                else:
                    other = next((p for p in self._profiles_except(rel) if p.id == prof.id), None)
                    if other is not None:
                        res["errors"].append(f"id «{prof.id}» уже занят файлом {Path(other.path).name}")
                    if prof.voltage is None and prof.target_class.split(".")[0] in ("memory", "mcu"):
                        res["warnings"].append("напряжение не указано — при работе система спросит вас")
                    if prof.params:
                        res["hints"].append(f"параметров в карте: {len(prof.params)}")
        elif k == "markdown" and text.startswith("---"):
            head = text.split("---", 2)
            try:
                meta = yaml.safe_load(head[1]) if len(head) == 3 else None
                if not isinstance(meta, dict):
                    raise ValueError("ожидался словарь")
            except Exception as e:
                res["errors"].append(f"шапка статьи (между ---): {e}")
        return res

    # ---------- история ----------
    def _hist(self, rel: str) -> Path:
        return self.history_dir / rel

    def history(self, rel: str) -> list:
        index = self._hist(rel) / "index.jsonl"
        if not index.is_file():
            return []
        rows = [json.loads(ln) for ln in index.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return list(reversed(rows))

    def version(self, rel: str, vid: str) -> str:
        if not vid or "/" in vid or vid.startswith("."):
            raise HoodError("неизвестная версия")
        p = self._hist(rel) / f"{vid}.txt"
        if not p.is_file():
            raise HoodError("такой версии нет")
        return p.read_text(encoding="utf-8")

    def _remember(self, rel: str, text: str, note: str, good, actor: str) -> str:
        folder = self._hist(rel)
        folder.mkdir(parents=True, exist_ok=True)
        sha = sha256(text.encode("utf-8"))
        last = self.history(rel)
        if last and last[0]["sha"] == sha:
            return last[0]["vid"]
        vid = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1000000:06d}-{sha[:8]}"
        (folder / f"{vid}.txt").write_text(text, encoding="utf-8")
        with open(folder / "index.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"vid": vid, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "sha": sha, "note": note,
                                "good": good, "actor": actor}, ensure_ascii=False) + "\n")
        return vid

    def _journal(self, **row) -> None:
        self.store.mkdir(parents=True, exist_ok=True)
        with open(self.journal, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"), **row), ensure_ascii=False) + "\n")

    def journal_rows(self, limit: int = 50) -> list:
        if not self.journal.is_file():
            return []
        rows = [json.loads(ln) for ln in self.journal.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return list(reversed(rows))[:limit]

    # ---------- запись ----------
    def _write(self, p: Path, text: str) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.hood-{os.getpid()}")
        tmp.write_text(text, encoding="utf-8")
        if p.exists():
            os.chmod(tmp, p.stat().st_mode & 0o777)
        os.replace(tmp, p)

    def save(self, rel: str, text: str, word=None, actor: str = "", note: str = "правка") -> dict:
        p = self.path(rel, must_exist=False)
        res = self.check(rel, text)
        if res["errors"]:
            return {"ok": False, **res}
        if res["needs_word"] and not self.word_ok(word):
            return {"ok": False, **res, "errors": [f"введите слово «{self.word}» — этот файл влияет на работу с железом"]}
        old = p.read_text(encoding="utf-8") if p.is_file() else None
        if old == text:
            return {"ok": True, **res, "unchanged": True}
        if old is not None:
            self._remember(rel, old, "до правки", None, actor)
        self._write(p, text)
        vid = self._remember(rel, text, note, True, actor)
        self._journal(path=rel, action=note, sha_before=sha256(old.encode("utf-8")) if old is not None else None,
                      sha_after=sha256(text.encode("utf-8")), actor=actor)
        return {"ok": True, **res, "version": vid}

    def create(self, rel: str, text: str, actor: str = "") -> dict:
        """Новый файл (навык, статья). Существующий не перезаписывается."""
        p = self.path(rel, must_exist=False)
        if p.exists():
            raise HoodError(f"{rel} уже есть — ничего не перезаписываю")
        res = self.check(rel, text)
        if res["errors"]:
            raise HoodError(f"{rel}: " + "; ".join(res["errors"]))
        self._write(p, text)
        self._remember(rel, text, "создан", True, actor)
        self._journal(path=rel, action="создан", sha_before=None, sha_after=sha256(text.encode("utf-8")),
                      actor=actor)
        return res

    def restore(self, rel: str, vid: str, word=None, actor: str = "") -> dict:
        return self.save(rel, self.version(rel, vid), word, actor, note=f"возврат версии {vid}")

    # ---------- самоисцеление правок ----------
    def last_good(self, rel: str) -> str | None:
        """Новейшая версия из истории, которая отличается от текущей и проходит проверку."""
        p = self.root / rel
        current = sha256(p.read_bytes()) if p.is_file() else None
        for row in self.history(rel):
            if row["sha"] == current:
                continue
            if not self.check(rel, self.version(rel, row["vid"]))["errors"]:
                return row["vid"]
        return None

    def heal(self, rel: str, word=None, actor: str = "самопроверка", require_word: bool = True) -> dict:
        vid = self.last_good(rel)
        if vid is None:
            raise HoodError(f"для {rel} в истории капота нет рабочей версии — верните файл через git: "
                            f"git checkout -- {rel}")
        if not require_word:
            word = self.word
        return self.save(rel, self.version(rel, vid), word, actor, note=f"самоисцеление: версия {vid}")
