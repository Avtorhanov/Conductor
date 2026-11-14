"""Хранилище резервных копий.

Каждый дамп хранится под именем = SHA-256 своего содержимого:
  * копию невозможно незаметно подменить — при чтении хеш проверяется заново;
  * одинаковые дампы не занимают место дважды;
  * файлы ставятся «только для чтения», программа их никогда не удаляет;
  * при желании каждая копия дублируется во второе место (mirror_dir).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


class IntegrityError(Exception):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BackupStore:
    def __init__(self, root: Path, mirror: Path | None = None):
        self.root = Path(root)
        self.mirror = Path(mirror) if mirror else None
        self.root.mkdir(parents=True, exist_ok=True)
        self.mirror_error: str | None = None

    @staticmethod
    def _path(base: Path, sha: str) -> Path:
        return base / sha[:2] / f"{sha}.bin"

    def _write(self, base: Path, sha: str, data: bytes) -> Path:
        path = self._path(base, sha)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # атомарно: файл либо целый, либо его нет
        try:
            path.chmod(0o444)
        except OSError:
            pass
        return path

    def put(self, data: bytes) -> str:
        sha = sha256(data)
        self._write(self.root, sha, data)
        if self.mirror:
            try:
                self._write(self.mirror, sha, data)
                self.mirror_error = None
            except OSError as e:
                self.mirror_error = f"зеркало недоступно: {e}"
        return sha

    def get(self, sha: str) -> bytes:
        bases = [self.root] + ([self.mirror] if self.mirror else [])
        for base in bases:
            p = self._path(base, sha)
            if p.exists():
                data = p.read_bytes()
                if sha256(data) == sha:
                    return data
        raise IntegrityError(f"копия {sha[:12]}… не найдена или повреждена "
                             f"(проверены основное хранилище и зеркало)")

    def export(self, sha: str, dest: Path) -> Path:
        dest = Path(dest)
        dest.write_bytes(self.get(sha))
        return dest
