"""Навыки — расширения «Дирижёра», которые добавляются без правки пакета.

    skills/<id>/
        skill.yaml        паспорт: id, title, kind, summary, enabled, settings (настройки лезвия)
        skill.py          код (необязательно): плагины-лезвия и проверки (@register)
        profiles/*.yaml   профили, в том числе карты параметров (необязательно)
        help.md, help/*.md  справка (необязательно)

Папки и файлы на «_» не загружаются (шаблоны). Навык с enabled: false не загружается совсем.
Изменённый skill.py подхватывается без перезапуска (по времени изменения файла).

Сторож (lint) проверяет код навыка ДО загрузки и не пускает то, что нарушает принципы:
удаление файлов (копии не удаляются никогда), прямой запуск процессов в обход self.run()
(таймаут и журнал), eval/exec, запись из probe(), доступ к хранилищу копий.
Это не песочница: навык выполняется с правами «Дирижёра». Сторож ловит ошибки, а не злой умысел.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {
    "tool": "Инструмент (протокол)",
    "map": "Карта параметров",
    "check": "Проверка",
}
ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")

# ---------- сторож ----------
DELETE_CALLS = {"unlink", "rmtree", "removedirs", "rmdir"}       # у любого объекта (Path.unlink…)
OS_DELETE_CALLS = {"remove"}                                       # только os.remove (list.remove — можно)
SPAWN_CALLS = {"system", "popen", "spawnl", "spawnv", "spawnlp", "spawnvp", "execv", "execl", "execvp",
               "execlp", "execve", "fork", "forkpty"}
FORBIDDEN_IMPORTS = {"subprocess": "внешние программы — только через self.run(): таймаут и журнал",
                     "ctypes": "прямой доступ к памяти процесса не нужен навыку"}
NETWORK_IMPORTS = {"socket", "urllib", "http", "requests", "ftplib", "smtplib"}
WRITE_IN_PROBE = {"write", "apply", "erase", "program", "flash", "download"}


def lint(source: str, filename: str = "skill.py") -> tuple:
    """(запреты, предупреждения). При запретах код не загружается и не сохраняется."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as e:
        return [f"строка {e.lineno}: синтаксическая ошибка — {e.msg}"], []
    blocks, warns = [], []

    def where(node) -> str:
        return f"строка {getattr(node, 'lineno', '?')}"

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for name in names:
                top = name.split(".")[0]
                if top in FORBIDDEN_IMPORTS:
                    blocks.append(f"{where(node)}: import {top} — {FORBIDDEN_IMPORTS[top]}")
                if top in NETWORK_IMPORTS:
                    warns.append(f"{where(node)}: сеть ({top}) — Дирижёр работает без интернета (принцип 10); "
                                 f"для Modbus TCP и приборов в локальной сети это нормально")
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
            owner = fn.value.id if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) else ""
            if name in DELETE_CALLS or (name in OS_DELETE_CALLS and owner in ("os", "shutil")):
                blocks.append(f"{where(node)}: {name}() — удалять файлы навыку нельзя (копии не удаляются "
                              f"никогда); временные файлы — tempfile.TemporaryDirectory()")
            elif name in SPAWN_CALLS and owner in ("os", "pty"):
                blocks.append(f"{where(node)}: {name}() — запуск программ только через self.run()")
            elif isinstance(fn, ast.Name) and name in ("eval", "exec", "compile", "__import__"):
                blocks.append(f"{where(node)}: {name}() запрещён")
        elif isinstance(node, ast.Attribute) and node.attr == "store" and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "c":
            blocks.append(f"{where(node)}: self.c.store — хранилище копий трогает только ядро")
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "probe":
                    for sub in ast.walk(item):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                            attr = sub.func.attr
                            if attr in WRITE_IN_PROBE or attr.startswith("erase"):
                                blocks.append(f"{where(sub)}: probe() вызывает {attr}() — опознание только "
                                              f"читает (принцип 1)")
    return blocks, warns


# ---------- паспорт ----------
def validate_manifest(d, folder_name: str | None = None) -> list:
    errors = []
    if not isinstance(d, dict):
        return ["skill.yaml пуст или испорчен"]
    sid = str(d.get("id") or "")
    if not ID_RE.match(sid):
        errors.append("id — латиница, цифры и _, с буквы, 2–40 знаков")
    elif folder_name and sid != folder_name:
        errors.append(f"id «{sid}» должен совпадать с именем папки «{folder_name}»")
    if not d.get("title"):
        errors.append("нет title")
    if d.get("kind", "tool") not in KINDS:
        errors.append(f"kind — одно из: {', '.join(KINDS)}")
    if not isinstance(d.get("settings") or {}, dict):
        errors.append("settings — словарь (настройки лезвия по умолчанию)")
    return errors


@dataclass
class Skill:
    id: str
    title: str
    kind: str
    path: Path
    summary: str = ""
    enabled: bool = True
    settings: dict = field(default_factory=dict)
    version: str = "1"
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def code_path(self) -> Path:
        return self.path / "skill.py"

    @property
    def profiles_dir(self) -> Path:
        return self.path / "profiles"

    def help_files(self) -> list:
        files = [self.path / "help.md"] if (self.path / "help.md").is_file() else []
        if (self.path / "help").is_dir():
            files += sorted(p for p in (self.path / "help").glob("*.md") if not p.name.startswith("_"))
        return files


class SkillDB:
    def __init__(self, root):
        self.root = Path(root) if root else None
        self.skills: list = []
        self.errors: list = []          # (папка, текст)
        self.reload()

    def reload(self) -> None:
        self.skills, self.errors = [], []
        if self.root is None or not self.root.is_dir():
            return
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            if folder.name.startswith(("_", ".")) or folder.name == "__pycache__":
                continue
            man = folder / "skill.yaml"
            if not man.is_file():
                self.errors.append((folder, "нет skill.yaml"))
                continue
            try:
                d = yaml.safe_load(man.read_text(encoding="utf-8"))
            except Exception as e:
                self.errors.append((folder, f"skill.yaml: {e}"))
                continue
            problems = validate_manifest(d, folder.name)
            if problems:
                self.errors.append((folder, "; ".join(problems)))
                continue
            self.skills.append(Skill(
                id=d["id"], title=str(d["title"]), kind=d.get("kind", "tool"), path=folder,
                summary=str(d.get("summary") or ""), enabled=bool(d.get("enabled", True)),
                settings=dict(d.get("settings") or {}), version=str(d.get("version", "1"))))

    def get(self, sid: str):
        return next((s for s in self.skills if s.id == sid), None)

    @property
    def active(self) -> list:
        return [s for s in self.skills if s.enabled]

    def profile_dirs(self) -> list:
        return [s.profiles_dir for s in self.active if s.profiles_dir.is_dir()]


# ---------- загрузка кода ----------
_CACHE: dict = {}       # путь → (время изменения, модуль, предупреждения сторожа)


def module_name(skill_id: str) -> str:
    return f"dirizher_skill_{skill_id}"


def load_code(skill: Skill):
    """Модуль skill.py (или None, если кода нет). Ошибки — в skill.errors, система не падает."""
    path = skill.code_path
    if not path.is_file():
        return None
    try:
        mtime = path.stat().st_mtime_ns
        cached = _CACHE.get(str(path))
        if cached and cached[0] == mtime:
            skill.warnings.extend(cached[2])
            return cached[1]
        source = path.read_text(encoding="utf-8")
        blocks, warns = lint(source, str(path))
        skill.warnings.extend(warns)
        if blocks:
            skill.errors.append("сторож не пустил код: " + "; ".join(blocks))
            return None
        name = module_name(skill.id)
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        _CACHE[str(path)] = (mtime, mod, warns)
        return mod
    except Exception as e:                     # сломанный навык не роняет систему
        skill.errors.append(f"skill.py: {type(e).__name__}: {e}")
        return None
