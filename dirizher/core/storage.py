"""Хранилище резервных копий.

Каждый дамп хранится под именем = SHA-256 своего содержимого:
  * копию невозможно незаметно подменить — при чтении хеш проверяется заново;
  * одинаковые дампы не занимают место дважды;
  * файлы ставятся «только для чтения», программа их никогда не удаляет;
  * при желании каждая копия дублируется во второе место (mirror_dir);
  * самоисцеление (repair): испорченная или пропавшая копия восстанавливается из второго места,
    испорченный файл не удаляется, а переносится в карантин.
"""
from __future__ import annotations

import hashlib
import os
import time
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

    # ---------- самопроверка и самоисцеление ----------
    def _state(self, base: Path | None, sha: str) -> str:
        if base is None:
            return "off"
        p = self._path(base, sha)
        if not p.exists():
            return "missing"
        return "ok" if sha256(p.read_bytes()) == sha else "bad"

    def has(self, sha: str) -> bool:
        """Есть ли файл копии в основном хранилище (без пересчёта хеша — быстро)."""
        return self._path(self.root, sha).exists()

    def health(self, sha: str) -> dict:
        """Состояние копии: {"primary": ok|missing|bad, "mirror": ok|missing|bad|off}."""
        return {"primary": self._state(self.root, sha), "mirror": self._state(self.mirror, sha)}

    def repair(self, sha: str, quarantine: Path) -> list:
        """Восстановить копию там, где она испорчена или пропала, из исправного места.

        Ничего не удаляется: испорченный файл переносится в quarantine. Возвращает список действий."""
        state = self.health(sha)
        good = self.root if state["primary"] == "ok" else self.mirror if state["mirror"] == "ok" else None
        if good is None:
            raise IntegrityError(f"копия {sha[:12]}… испорчена везде — восстановить не из чего")
        data = self._path(good, sha).read_bytes()
        done = []
        for name, base in (("основное хранилище", self.root), ("второе место", self.mirror)):
            st = state["primary" if base is self.root else "mirror"]
            if base is None or st == "ok":
                continue
            path = self._path(base, sha)
            if st == "bad":
                quarantine.mkdir(parents=True, exist_ok=True)
                dest = quarantine / f"{sha}.{time.strftime('%Y%m%d-%H%M%S')}.{'mirror' if base is self.mirror else 'main'}.bin"
                os.replace(path, dest)
                done.append(f"{name}: испорченный файл перенесён в карантин ({dest.name})")
            self._write(base, sha, data)
            done.append(f"{name}: копия {sha[:12]}… восстановлена")
        return done
