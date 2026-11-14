"""Память: журнал сеансов, событий, снимков, эталонов и статистика рецептов.

Хранится в одном файле SQLite (data/memory.sqlite), плюс читаемый текстовый журнал
каждого сеанса в data/logs/. Ничего не делается «молча».
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started TEXT NOT NULL,
    finished TEXT,
    kind TEXT NOT NULL,              -- run | detect | rollback
    parent_session INTEGER,          -- для отката: какой сеанс откатываем
    plugin TEXT,
    target_class TEXT,
    fingerprint TEXT,                -- JSON
    profile_id TEXT,
    recipe_id TEXT,
    outcome TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    ts TEXT,
    level TEXT,
    message TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER,
    kind TEXT,                       -- before | after | written
    sha TEXT,
    size INTEGER,
    verified INTEGER,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS refs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id TEXT,
    label TEXT,
    chip_key TEXT,
    sha TEXT,
    size INTEGER,
    session_id INTEGER,
    ts TEXT,
    note TEXT
);
CREATE TABLE IF NOT EXISTS recipe_stats(
    profile_id TEXT,
    recipe_id TEXT,
    success INTEGER NOT NULL DEFAULT 0,
    fail INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(profile_id, recipe_id)
);
"""

SESSION_FIELDS = {"finished", "plugin", "target_class", "fingerprint",
                  "profile_id", "recipe_id", "outcome", "note"}


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Memory:
    def __init__(self, db_path: Path, logs_dir: Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(db_path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _exec(self, sql: str, args=()):
        cur = self.db.execute(sql, args)
        self.db.commit()
        return cur

    # ---------- сеансы ----------
    def start_session(self, kind: str, parent: int | None = None) -> int:
        cur = self._exec("INSERT INTO sessions(started, kind, parent_session) VALUES (?,?,?)",
                         (now(), kind, parent))
        return cur.lastrowid

    def update_session(self, sid: int, **fields) -> None:
        unknown = set(fields) - SESSION_FIELDS
        if unknown:
            raise ValueError(f"неизвестные поля сеанса: {unknown}")
        if "fingerprint" in fields and not isinstance(fields["fingerprint"], str):
            fields["fingerprint"] = json.dumps(fields["fingerprint"], ensure_ascii=False, sort_keys=True)
        sets = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE sessions SET {sets} WHERE id=?", (*fields.values(), sid))

    def session(self, sid: int):
        return self.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()

    def sessions(self, limit: int = 20):
        return self.db.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------- журнал ----------
    def log(self, sid: int, level: str, message: str, data: dict | None = None) -> None:
        ts = now()
        self._exec("INSERT INTO events(session_id, ts, level, message, data) VALUES (?,?,?,?,?)",
                   (sid, ts, level, message, json.dumps(data, ensure_ascii=False) if data else None))
        with open(self.logs_dir / f"session_{sid:05d}.log", "a", encoding="utf-8") as f:
            f.write(f"{ts} [{level}] {message}\n")
            for key, value in (data or {}).items():
                if value:
                    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    f.write(f"    {key}: {text.strip()[-1500:]}\n")

    def events(self, sid: int):
        return self.db.execute("SELECT * FROM events WHERE session_id=? ORDER BY id", (sid,)).fetchall()

    # ---------- снимки ----------
    def add_snapshot(self, sid: int, kind: str, sha: str, size: int, verified: bool) -> None:
        self._exec("INSERT INTO snapshots(session_id, kind, sha, size, verified, ts) VALUES (?,?,?,?,?,?)",
                   (sid, kind, sha, size, int(bool(verified)), now()))

    def snapshots(self, sid: int):
        return self.db.execute("SELECT * FROM snapshots WHERE session_id=? ORDER BY id", (sid,)).fetchall()

    def snapshot(self, sid: int, kind: str):
        return self.db.execute("SELECT * FROM snapshots WHERE session_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                               (sid, kind)).fetchone()

    # ---------- эталоны ----------
    def add_reference(self, profile_id: str, label: str, chip_key: str, sha: str, size: int,
                      sid: int | None, note: str | None = None) -> None:
        self._exec("INSERT INTO refs(profile_id, label, chip_key, sha, size, session_id, ts, note) "
                   "VALUES (?,?,?,?,?,?,?,?)", (profile_id, label, chip_key, sha, size, sid, now(), note))

    def references(self, chip_key: str):
        """Все эталоны для такого чипа (с разных плат и профилей), свежие первыми."""
        return self.db.execute("SELECT * FROM refs WHERE chip_key=? ORDER BY id DESC",
                               (chip_key,)).fetchall()

    # ---------- статистика рецептов (обучение) ----------
    def recipe_stats(self, profile_id: str, recipe_id: str) -> tuple:
        row = self.db.execute("SELECT success, fail FROM recipe_stats WHERE profile_id=? AND recipe_id=?",
                              (profile_id, recipe_id)).fetchone()
        return (row["success"], row["fail"]) if row else (0, 0)

    def bump_recipe(self, profile_id: str, recipe_id: str, ok: bool) -> None:
        col = "success" if ok else "fail"
        self._exec(f"INSERT INTO recipe_stats(profile_id, recipe_id, {col}) VALUES (?,?,1) "
                   f"ON CONFLICT(profile_id, recipe_id) DO UPDATE SET {col}={col}+1",
                   (profile_id, recipe_id))
