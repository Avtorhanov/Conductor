"""Собрать весь код, профили, настройки и тесты в один текстовый файл.

Нужен для «Файлов проекта» в приложении Claude: вместо десятков файлов загружается один.
Запуск из корня проекта:
    python3 tools/bundle.py          → dirizher_code.txt
После каждой сессии с изменениями кода замените этот файл в проекте.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = "dirizher_code.txt"
PATTERNS = ["*.py", "*.yaml", "*.rules", "requirements.txt", ".gitignore"]
SKIP_DIRS = {"data", "__pycache__", ".git", "docs"}


def collect() -> list:
    files = set()
    for pattern in PATTERNS:
        for path in ROOT.rglob(pattern):
            rel = path.relative_to(ROOT)
            if any(part in SKIP_DIRS for part in rel.parts) or path.name == OUTPUT:
                continue
            files.add(rel)
    return sorted(files, key=lambda p: (len(p.parts), str(p)))


def main() -> None:
    files = collect()
    parts = [
        "ДИРИЖЁР — весь код проекта одним файлом.\n"
        "Каждый файл начинается со строки «=== FILE: путь ===».\n"
        "Документация (STATUS, PRINCIPLES, MANUAL) лежит в проекте отдельными файлами.\n\n"
        "Список файлов:\n" + "\n".join(f"  {p}" for p in files) + "\n"
    ]
    for rel in files:
        text = (ROOT / rel).read_text(encoding="utf-8")
        parts.append(f"\n=== FILE: {rel} ===\n{text.rstrip()}\n")
    out = ROOT / OUTPUT
    out.write_text("".join(parts), encoding="utf-8")
    print(f"готово: {out} ({len(files)} файлов, {out.stat().st_size // 1024} КБ)")


if __name__ == "__main__":
    main()
