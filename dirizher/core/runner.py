"""Безопасный запуск внешних программ (flashrom, probe-rs, binwalk...).

Каждый инструмент работает отдельным процессом: с таймаутом, с записью команды
и её вывода в журнал. Ошибка инструмента не роняет ядро.

С 0.7.0 полный вывод каждой команды (до 1 МБ на поток) сохраняется отдельным файлом рядом
с журналом сеанса — его забирает «журнал для разработки» (core/journal.py). Если в выводе
данные чипа (например, `probe-rs read` печатает дамп), плагин зовёт run(..., keep_output=False):
тогда вывод не сохраняется нигде, кроме памяти процесса.
"""
from __future__ import annotations

import shlex
import subprocess
import time
from dataclasses import dataclass


@dataclass
class RunResult:
    cmd: list
    code: int
    out: str
    err: str
    seconds: float

    @property
    def ok(self) -> bool:
        return self.code == 0

    def tail(self, n: int = 8) -> str:
        lines = (self.err.strip() or self.out.strip()).splitlines()
        return "\n".join(lines[-n:])


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


DATA_NOTE = "(вывод не сохранён: в нём данные чипа)"


class Runner:
    def __init__(self, log, keep=None):
        self.log = log
        self.keep = keep           # keep(res, keep_output) -> dict: куда сохранён полный вывод

    def run(self, cmd: list, timeout: int = 120, keep_output: bool = True) -> RunResult:
        cmd = [str(c) for c in cmd]
        self.log("$ " + " ".join(shlex.quote(c) for c in cmd), "cmd")
        t0 = time.monotonic()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            res = RunResult(cmd, p.returncode, p.stdout, p.stderr, time.monotonic() - t0)
        except FileNotFoundError:
            res = RunResult(cmd, 127, "", f"программа не найдена: {cmd[0]}", 0.0)
        except subprocess.TimeoutExpired as e:
            res = RunResult(cmd, -1, _text(e.stdout),
                            _text(e.stderr) + f"\nпревышено время ожидания ({timeout} с)", float(timeout))
        data = {"stdout": res.out[-3000:] if keep_output else DATA_NOTE, "stderr": res.err[-3000:]}
        if self.keep is not None:
            try:
                data.update(self.keep(res, keep_output) or {})
            except OSError as e:                     # журнал не должен ронять работу с чипом
                data["file_error"] = str(e)
        self.log(f"код возврата {res.code}, {res.seconds:.1f} с", "cmd", data)
        return res
