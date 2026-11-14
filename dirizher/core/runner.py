"""Безопасный запуск внешних программ (flashrom, probe-rs, binwalk...).

Каждый инструмент работает отдельным процессом: с таймаутом, с записью команды
и её вывода в журнал. Ошибка инструмента не роняет ядро.
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


class Runner:
    def __init__(self, log):
        self.log = log

    def run(self, cmd: list, timeout: int = 120) -> RunResult:
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
        self.log(f"код возврата {res.code}, {res.seconds:.1f} с", "cmd",
                 {"stdout": res.out[-3000:], "stderr": res.err[-3000:]})
        return res
