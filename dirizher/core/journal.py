"""Журнал для разработки: полный вывод команд, пакет сеанса без личных данных, повтор вывода.

Зачем. Разбор вывода flashrom, probe-rs и других программ написан по документации. Первый же
прогон на железе должен стать постоянным тестом. Для этого:

1. Runner сохраняет полный stdout/stderr каждой команды (до 1 МБ на поток) отдельным файлом
   `data/logs/session_NNNNN/cmd_NNN.json` (save_command). Дампы туда не попадают: плагины читают
   чип во временные файлы, а вывод с данными чипа помечают keep_output=False.
2. bundle() собирает «пакет журнала»: команды, коды возврата, полный вывод, версии программ,
   отпечаток, итог. scrub() убирает MAC, серийные номера, имя хоста и домашние пути.
3. tools/log_to_test.py превращает пакет в фикстуру tests/fixtures/real/…json, а replay_probe()
   прогоняет фикстуру через настоящий плагин с подменённым Runner (ReplayRunner).

Только чтение памяти и добавление новых файлов: ничего в data/ не переписывается.
"""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
from pathlib import Path

LIMIT = 1024 * 1024                       # байт на поток (stdout, stderr)
FORMAT = "dirizher-journal"
FIXTURE_FORMAT = "dirizher-fixture"


# ================= полный вывод команд =================
def session_dir(logs_dir: Path, sid: int) -> Path:
    return Path(logs_dir) / f"session_{sid:05d}"


def _cut(text: str) -> tuple:
    raw = (text or "").encode("utf-8", errors="replace")
    if len(raw) <= LIMIT:
        return text or "", False
    return raw[:LIMIT].decode("utf-8", errors="ignore"), True


def save_command(logs_dir: Path, sid: int, res, keep_output: bool = True) -> str:
    """Записать команду и её полный вывод новым файлом. Возвращает путь относительно logs_dir."""
    folder = session_dir(logs_dir, sid)
    folder.mkdir(parents=True, exist_ok=True)
    out, out_cut = _cut(res.out) if keep_output else ("", False)
    err, err_cut = _cut(res.err)
    record = {"argv": list(res.cmd), "code": res.code, "seconds": round(res.seconds, 3),
              "stdout": out, "stderr": err, "kept": bool(keep_output),
              "truncated": {"stdout": out_cut, "stderr": err_cut}}
    n = len(list(folder.glob("cmd_*.json"))) + 1
    while True:
        path = folder / f"cmd_{n:03d}.json"
        try:
            with open(path, "x", encoding="utf-8") as f:     # «x» — никогда не переписывать
                json.dump({"n": n, **record}, f, ensure_ascii=False, indent=1)
            break
        except FileExistsError:
            n += 1
    return f"{folder.name}/{path.name}"


def commands(logs_dir: Path, sid: int) -> list:
    folder = session_dir(logs_dir, sid)
    rows = []
    for p in sorted(folder.glob("cmd_*.json")) if folder.is_dir() else []:
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            rows.append({"file": p.name, "error": "файл не читается"})
    return rows


# ================= очистка личных данных =================
MAC_RE = re.compile(r"(?<![0-9A-Fa-f:-])([0-9A-Fa-f]{2}([:-])(?:[0-9A-Fa-f]{2}\2){4}[0-9A-Fa-f]{2})(?![0-9A-Fa-f:-])")
SERIAL_RE = re.compile(r"(?i)\b(serial\s+(?:code|number|no\.?|num)|serial|s/n|sn)(\s*[:=#]\s*)([^\s,;\"']+)")
USB_SERIAL_RE = re.compile(r"\b([0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}):([0-9A-Za-z]{6,})\b")   # VID:PID:серийник
BY_ID_RE = re.compile(r"(/dev/serial/by-id/)[^\s\"']+")
HOME_RE = re.compile(r"(?:/home|/Users)/[^/\s\"']+|/root(?=/|\b)|[A-Za-z]:\\Users\\[^\\\s\"']+")


class Scrubber:
    """Заменяет личное на постоянные заглушки: один и тот же MAC — одна и та же заглушка."""

    def __init__(self, hostname: str | None = None):
        host = socket.gethostname() if hostname is None else hostname
        self.host_re = (re.compile(r"(?<![\w-])" + re.escape(host) + r"(?![\w-])", re.IGNORECASE)
                        if host and len(host) >= 3 and host.lower() != "localhost" else None)
        self.maps = {"mac": {}, "serial": {}}
        self.counts = {"mac": 0, "serial": 0, "host": 0, "home": 0}

    def _alias(self, kind: str, value: str, make) -> str:
        key = value.lower().replace("-", ":")
        table = self.maps[kind]
        if key not in table:
            table[key] = make(len(table) + 1)
        self.counts[kind] += 1
        return table[key]

    def text(self, s: str) -> str:
        def mac(m):
            return self._alias("mac", m.group(1), lambda n: "02:00:00:%02x:%02x:%02x"
                               % ((n >> 16) & 255, (n >> 8) & 255, n & 255))

        def serial(m):
            return m.group(1) + m.group(2) + self._alias("serial", m.group(3), lambda n: f"SERIAL-{n}")

        def usb(m):
            return m.group(1) + ":" + self._alias("serial", m.group(2), lambda n: f"SERIAL-{n}")

        def by_id(m):
            self.counts["serial"] += 1
            return m.group(1) + "usb-SERIAL"

        def home(m):
            self.counts["home"] += 1
            return "~"

        s = MAC_RE.sub(mac, s)
        s = SERIAL_RE.sub(serial, s)
        s = USB_SERIAL_RE.sub(usb, s)
        s = BY_ID_RE.sub(by_id, s)
        s = HOME_RE.sub(home, s)
        if self.host_re is not None:
            s, n = self.host_re.subn("host", s)
            self.counts["host"] += n
        return s

    def __call__(self, obj):
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, list):
            return [self(x) for x in obj]
        if isinstance(obj, dict):
            return {self(k) if isinstance(k, str) else k: self(v) for k, v in obj.items()}
        return obj


def scrub(obj, hostname: str | None = None) -> tuple:
    """(очищенная копия, сколько чего заменено)."""
    s = Scrubber(hostname)
    return s(obj), dict(s.counts)


# ================= пакет журнала =================
def _json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def bundle(conductor, sid: int, hostname: str | None = None):
    """Пакет журнала сеанса для разработки (без дампов, с очисткой). None — нет такого сеанса."""
    from .. import __version__

    mem = conductor.memory
    s = mem.session(sid)
    if s is None:
        return None
    events, tools = [], []
    for e in mem.events(sid):
        data = _json(e["data"])
        if isinstance(data, dict) and e["level"] == "cmd":
            data = {k: v for k, v in data.items() if k not in ("stdout", "stderr")} or None
        if isinstance(data, dict) and "tool" in data and "version" in data:
            tools.append({"plugin": data["tool"], "version": data.get("version"),
                          "settings": data.get("settings") or {}})
        events.append({"ts": e["ts"], "level": e["level"], "message": e["message"],
                       **({"data": data} if data else {})})
    session = {k: s[k] for k in s.keys()}
    session["fingerprint"] = _json(session.get("fingerprint"))
    raw = {
        "format": FORMAT, "format_version": 1, "dirizher": __version__,
        "sim": bool(getattr(conductor, "sim", False)),
        "note": "Журнал для разработки: команды, коды возврата, полный вывод, версии, отпечаток, итог. "
                "Дампов нет. MAC, серийные номера, имя хоста и домашние пути заменены заглушками.",
        "session": session,
        "tools": tools,
        "snapshots": [{"kind": x["kind"], "sha": x["sha"], "size": x["size"], "verified": bool(x["verified"])}
                      for x in mem.snapshots(sid)],
        "commands": commands(mem.logs_dir, sid),
        "events": events,
    }
    clean, counts = scrub(raw, hostname)
    clean["scrubbed"] = counts
    return clean


def filename(sid: int, sim: bool = False) -> str:
    return f"dirizher_journal_s{sid}{'_sim' if sim else ''}.json"


# ================= повтор записанного вывода =================
TMP_RE = re.compile(r"^(?:/tmp/|/var/tmp/|/var/folders/)|[/\\]tmp[\w.-]*[/\\]")


def _is_tmp(arg: str) -> bool:
    return bool(TMP_RE.search(arg)) or arg.startswith(tempfile.gettempdir() + os.sep)


def _key(argv) -> tuple:
    """Команда без временных путей (они меняются от запуска к запуску)."""
    return tuple("<tmp>" if _is_tmp(str(a)) else str(a) for a in argv)


class ReplayRunner:
    """Подменный Runner: на команду отвечает записанным выводом. Незаписанная команда — ошибка."""

    def __init__(self, recorded: list):
        self.recorded = [dict(r) for r in recorded]
        self.used: list = []

    def run(self, cmd: list, timeout: int = 120, keep_output: bool = True):
        from .runner import RunResult

        key = _key(cmd)
        for r in self.recorded:
            if not r.get("_used") and _key(r["argv"]) == key:
                r["_used"] = True
                self.used.append(list(cmd))
                return RunResult([str(c) for c in cmd], int(r["code"]), r.get("stdout") or "",
                                 r.get("stderr") or "", 0.0)
        raise LookupError("команды нет в записи: " + " ".join(str(c) for c in cmd))


def plugin_class(name: str, skills_root=None):
    from .registry import discover
    from .skills import SkillDB

    skills = SkillDB(skills_root).skills if skills_root else []
    found, _ = discover(skills)
    if name not in found:
        raise KeyError(f"лезвие «{name}» не найдено")
    return found[name][0]


def replay_probe(fixture: dict, data_dir: Path, skills_root=None):
    """Прогнать probe() настоящего плагина на записанном выводе. → (Detection или None, runner)."""
    from types import SimpleNamespace

    cls = plugin_class(fixture["plugin"], skills_root)
    runner = ReplayRunner(fixture.get("commands") or [])
    stub = SimpleNamespace(runner=runner, data_dir=Path(data_dir), sim=False, cfg={})
    plugin = cls(stub, dict(fixture.get("settings") or {}))
    return plugin.probe(), runner


def compare(expect: dict, det) -> list:
    """Расхождения ожидаемого и разобранного. Пустой список — совпало."""
    problems = []
    if not expect.get("detected", True):
        if det is not None:
            problems.append(f"ожидалось «ничего не найдено», а опознано: {det.summary}")
        return problems
    if det is None:
        return ["ожидалось опознание, а плагин ничего не нашёл"]
    if expect.get("target_class") and det.target_class != expect["target_class"]:
        problems.append(f"класс: ожидался {expect['target_class']}, получен {det.target_class}")
    for k, v in (expect.get("fingerprint") or {}).items():
        got = det.fingerprint.get(k)
        if got != v:
            problems.append(f"{k}: ожидалось {v!r}, получено {got!r}")
    return problems
