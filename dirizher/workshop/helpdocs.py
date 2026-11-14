"""Справка: статьи (help/*.md, справка навыков) и уроки Штурмана — один список с поиском.

Статья — Markdown с короткой шапкой:
    ---
    title: Капот
    section: dev            # раздел интерфейса: guide | work | log | dev | device
    tags: [код, правки]
    ---
    # Капот
    Текст…
Значок «?» в каждом разделе открывает сначала статьи этого раздела.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from ..core.skills import SkillDB


def parse(text: str) -> tuple:
    """(шапка, текст без шапки)."""
    meta, body = {}, text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            body = parts[2].lstrip("\n")
    if not isinstance(meta, dict):
        meta = {}
    if not meta.get("title"):
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        meta["title"] = m.group(1).strip() if m else "Без названия"
    return meta, body


def _summary(body: str) -> str:
    for block in re.split(r"\n\s*\n", body):
        line = block.strip()
        if line and not line.startswith(("#", "```", "|", "-", "1.")):
            line = re.sub(r"[*`_]", "", " ".join(line.split()))
            return line[:140] + ("…" if len(line) > 140 else "")
    return ""


def _root(cfg) -> Path:
    return Path(cfg.get("_path") or "config.yaml").resolve().parent


def _rel(cfg, path: Path) -> str:
    try:
        return path.resolve().relative_to(_root(cfg)).as_posix()
    except ValueError:
        return ""


def _files(cfg) -> list:
    """(id, путь, источник)."""
    out = []
    help_dir = Path(cfg["help_dir"]) if cfg.get("help_dir") else None
    if help_dir and help_dir.is_dir():
        for p in sorted(help_dir.glob("*.md")):
            if not p.name.startswith("_"):
                out.append((p.stem, p, "статья"))
    for skill in SkillDB(cfg.get("skills_dir")).active:
        for p in skill.help_files():
            aid = f"skill.{skill.id}" + ("" if p.name == "help.md" else f".{p.stem}")
            out.append((aid, p, f"навык {skill.id}"))
    return out


def articles(cfg, with_lessons: bool = True) -> list:
    items = []
    for aid, path, source in _files(cfg):
        meta, body = parse(path.read_text(encoding="utf-8", errors="replace"))
        tags = meta.get("tags") or []
        items.append({"id": aid, "title": str(meta["title"]), "section": str(meta.get("section") or ""),
                      "tags": [str(t) for t in tags] if isinstance(tags, list) else [str(tags)],
                      "summary": _summary(body), "source": source, "path": _rel(cfg, path),
                      "kind": "article", "_text": body})
    if with_lessons:
        from ..interface import guide
        try:
            lessons = guide.load_guide()["lessons"]
        except Exception:
            lessons = {}
        for lid, les in lessons.items():
            text = les.get("why", "") + "\n" + "\n".join(s.get("text", "") for s in les["steps"])
            items.append({"id": f"lesson.{lid}", "title": les["title"], "section": "", "tags": ["урок"],
                          "summary": les.get("why", ""), "source": "урок Штурмана", "path": "",
                          "kind": "lesson", "lesson": lid, "_text": text})
    return items


def search(items: list, q: str = "", section: str = "") -> list:
    words = [w for w in re.split(r"\s+", (q or "").lower()) if w]
    found = []
    for it in items:
        hay = " ".join([it["title"], it["summary"], " ".join(it["tags"]), it["_text"]]).lower()
        if all(w in hay for w in words):
            score = sum(3 for w in words if w in it["title"].lower())
            found.append((0 if section and it["section"] == section else 1,
                          1 if it["kind"] == "lesson" else 0, -score, it["title"], it))
    found.sort(key=lambda x: x[:4])
    return [{k: v for k, v in x[4].items() if k != "_text"} for x in found]


def article(cfg, aid: str) -> dict | None:
    for it in articles(cfg, with_lessons=False):
        if it["id"] == aid:
            text = it.pop("_text")
            return dict(it, text=text)
    return None
