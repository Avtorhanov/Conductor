"""Журнал загрузки по UART — «замер» в порядке «замер → чтение → запись».

Многие платы (роутеры, ТВ-приставки, камеры, ESP) печатают при включении журнал загрузки в свой
последовательный порт: загрузчик (U-Boot, ROM ESP), ядро, ошибки. По нему видно, где останавливается
загрузка, — ещё до того, как что-то читать или писать в микросхему.

Это ТОЛЬКО ПРИЁМ: порт открывается на чтение, в устройство не посылается ни байта (провод TX
переходника можно вовсе не подключать). Поэтому это не лезвие (нет read/write), а отдельная команда:

    python3 -m dirizher.extras.uart --port /dev/ttyUSB0 --baud 115200 --time 15
    python3 -m dirizher.extras.uart --port /dev/ttyUSB0 --baud auto

Настройки по умолчанию — секция uart: в config.yaml (необязательна): port, baud (число или auto),
time_s, max_kb, bauds (список для подбора), sample_s.

Подбор скорости (baud: auto): порт слушается по очереди на распространённых скоростях; на верной
скорости почти все принятые байты — печатные символы, на неверной — мусор. Устройство при этом должно
печатать (включите или перезагрузите его во время подбора). Журнал загрузки печатается один раз —
надёжнее узнать скорость один раз и записать её в настройки.

Результат: сырые байты (.bin) и текст (.txt) в data/captures/, приметы (загрузчик, ядро, ошибки) и
начало журнала — на экране и в журнале сеанса вида «замер».

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: открытие порта поднимает DTR/RTS (Linux делает это сам) — на платах ESP с
автосбросом это перезагрузка, что для журнала загрузки даже удобно; уровни TX платы — только 3,3 В
(логика 1,8 В у некоторых SoC — нужен свой переходник); подбор скорости на настоящем журнале U-Boot.
"""
from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time
from pathlib import Path

from ..core.config import load_config
from ..core.pipeline import OUTCOMES, Conductor
from ..core.ui import ConsoleUI

ROOT = Path(__file__).resolve().parent.parent.parent

OUTCOMES.setdefault("captured", "сигналы записаны")
OUTCOMES.setdefault("capture_failed", "запись сигналов не удалась")

# 74880 — скорость ROM-загрузчика ESP8266; есть не на всех переходниках (тогда пропускается)
BAUDS = (115200, 57600, 38400, 19200, 9600, 230400, 460800, 921600, 74880)
DEFAULTS = {"port": "/dev/ttyUSB0", "baud": 115200, "time_s": 15, "max_kb": 1024, "sample_s": 1.5,
            "bauds": list(BAUDS)}
MIN_SAMPLE = 24          # меньше байт — не о чем судить
GOOD_RATIO = 0.85        # доля печатных символов на верной скорости
SHOW_LINES = 20

# Приметы в журнале: (выражение, состояние, текст). Порядок — как идёт загрузка.
MARKERS = [
    (re.compile(r"U-Boot\s+[\w.\-]+(?:\s*\([^)]*\))?", re.I), "info", "загрузчик U-Boot: {0}"),
    (re.compile(r"rst:0x[0-9a-f]+\s*\([^)]*\)[^\n]*", re.I), "info", "ROM ESP: {0}"),
    (re.compile(r"Hit any key to stop autoboot", re.I), "info",
     "загрузчик ждёт нажатия клавиши — консоль U-Boot доступна (Дирижёр ничего не посылает)"),
    (re.compile(r"Starting kernel", re.I), "ok", "загрузчик передал управление ядру"),
    (re.compile(r"Linux version [^\s]+", re.I), "ok", "ядро запустилось: {0}"),
    (re.compile(r"Bad (?:Data |Header )?CRC|Wrong Image Format|bad magic", re.I), "warn", "загрузчик нашёл испорченный образ: «{0}» — смотрите разделы флешки"),
    (re.compile(r"Kernel panic[^\n]*", re.I), "warn", "ядро остановилось: «{0}»"),
    (re.compile(r"Unable to mount root fs|VFS: Cannot open root device", re.I), "warn",
     "ядро не нашло корневую файловую систему: «{0}»"),
    (re.compile(r"invalid header|flash read err|ets_main\.c", re.I), "warn",
     "ROM ESP не может загрузить программу из флеша: «{0}»"),
    (re.compile(r"Guru Meditation Error[^\n]*|abort\(\) was called[^\n]*"), "warn", "программа ESP упала: «{0}»"),
]

SPEEDS = {b: getattr(termios, f"B{b}") for b in BAUDS if hasattr(termios, f"B{b}")}


def settings(cfg: dict, **override) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("uart") or {})
    s.update({k: v for k, v in override.items() if v not in (None, "")})
    return s


# ======================= разбор (без порта) =======================
def printable_ratio(data: bytes) -> float:
    """Доля печатных символов: ASCII 0x20–0x7E, \\t \\r \\n, а также байты UTF-8 (для русских сообщений)."""
    if not data:
        return 0.0
    good = sum(1 for b in data if 0x20 <= b < 0x7F or b in (9, 10, 13))
    high = sum(1 for b in data if b >= 0x80)
    if high and any(_utf8(data[:len(data) - cut]) for cut in range(4)):   # хвост мог оборваться посреди буквы
        good += high
    return good / len(data)


def _utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def pick_baud(samples: dict) -> tuple:
    """{скорость: принятые байты} → (лучшая скорость или None, {скорость: доля}). Только по данным."""
    scores = {b: round(printable_ratio(d), 3) for b, d in samples.items() if len(d) >= MIN_SAMPLE}
    if not scores:
        return None, scores
    best = max(scores, key=lambda b: (scores[b], len(samples[b])))
    return (best if scores[best] >= GOOD_RATIO else None), scores


def to_text(data: bytes) -> str:
    """Байты → текст для человека: UTF-8 с заменой, управляющие символы (кроме \\n и \\t) убраны."""
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", text)
    return "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 0x20 and ord(ch) != 0x7F)


def markers(text: str) -> list:
    """Приметы загрузки: [(состояние, текст)] в порядке появления; одинаковые — один раз."""
    found = []
    for rx, status, msg in MARKERS:
        m = rx.search(text)
        if m:
            found.append((m.start(), status, msg.format(m.group(0).strip())))
    seen, out = set(), []
    for _, status, msg in sorted(found):
        if msg not in seen:
            seen.add(msg)
            out.append((status, msg))
    return out


# ======================= порт: только приём =======================
class Receiver:
    """Последовательный порт только на чтение: O_RDONLY, сырой режим, 8N1, без управления потоком."""

    def __init__(self, path: str, baud: int):
        if baud not in SPEEDS:
            raise ValueError(f"скорость {baud} не поддерживается (есть: {', '.join(map(str, SPEEDS))})")
        self.fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            self.set_baud(baud)
        except Exception:
            os.close(self.fd)
            raise

    def set_baud(self, baud: int) -> None:
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = termios.IGNBRK                                        # iflag: без преобразований
        attrs[1] = 0                                                     # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL         # cflag: 8N1, без модема
        attrs[3] = 0                                                     # lflag: без эха и строк
        attrs[4] = attrs[5] = SPEEDS[baud]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIFLUSH)

    def listen(self, seconds: float, limit: int) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + seconds
        while len(buf) < limit:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], min(left, 0.2))
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except BlockingIOError:
                    continue
                buf += chunk
        return bytes(buf[:limit])

    def close(self) -> None:
        os.close(self.fd)


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def capture(c, cfg: dict, **override) -> int:
    s = settings(cfg, **override)
    sid = c._start("capture")
    c.ui.header(f"Сеанс №{sid}: журнал загрузки по UART (только приём, в устройство ничего не посылается)")
    port = str(s["port"])
    auto = str(s["baud"]).strip().lower() == "auto"
    if not auto and _int(s["baud"], 0) not in SPEEDS:
        c.ui.error(f"скорость «{s['baud']}» не поддерживается; есть: {', '.join(map(str, SPEEDS))} или auto")
        return c._finish("capture_failed")
    if not Path(port).exists():
        c.ui.error(f"порта {port} нет — проверьте кабель USB-UART (ls /dev/ttyUSB* /dev/ttyACM*)")
        return c._finish("capture_failed")
    limit = max(1, _int(s["max_kb"], 1024)) * 1024
    try:
        rx = Receiver(port, _int(s["baud"], 0) if not auto else next(iter(SPEEDS)))
    except PermissionError:
        c.ui.error(f"нет прав на {port} — sudo usermod -aG dialout $USER и войдите заново")
        return c._finish("capture_failed")
    except (OSError, ValueError) as e:
        c.ui.error(f"порт {port} не открылся: {e}")
        return c._finish("capture_failed")
    try:
        baud = _int(s["baud"], 0)
        if auto:
            c.ui.info("подбор скорости: включите или перезагрузите устройство — оно должно печатать…")
            samples = {}
            for b in [x for x in (_int(v, 0) for v in s.get("bauds") or BAUDS) if x in SPEEDS]:
                rx.set_baud(b)
                samples[b] = rx.listen(float(s["sample_s"]), limit)
            baud, scores = pick_baud(samples)
            c.log("подбор скорости UART", data={"scores": scores})
            for b, r in sorted(scores.items(), key=lambda kv: -kv[1])[:4]:
                c.ui.info(f"  {b}: печатных символов {r:.0%}")
            if baud is None:
                c.ui.error("скорость не подобрана: устройство молчит или печатает не текст. "
                           "Проверьте GND и провод TX платы → RX переходника; включите устройство во время подбора")
                return c._finish("capture_failed")
            c.ui.ok(f"скорость {baud} (запишите её в настройки uart: baud — журнал печатается один раз)")
            rx.set_baud(baud)
        c.ui.info(f"слушаю {port} на {baud} бод {s['time_s']} с — включите устройство…")
        data = rx.listen(float(s["time_s"]), limit)
    finally:
        rx.close()
    c.log(f"UART {port} {baud} бод: принято {len(data)} байт", data={"port": port, "baud": baud, "bytes": len(data)})
    if not data:
        c.ui.error("ничего не принято: проверьте GND, провод TX платы → RX переходника и скорость (попробуйте auto)")
        return c._finish("capture_failed")
    out_dir = c.data_dir / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / f"session_{sid:05d}_uart.bin"
    raw.write_bytes(data)
    text = to_text(data)
    txt = raw.with_suffix(".txt")
    txt.write_text(text, encoding="utf-8")
    ratio = printable_ratio(data)
    c.ui.ok(f"принято {len(data)} байт → {txt}")
    if ratio < GOOD_RATIO:
        c.ui.warn(f"печатных символов только {ratio:.0%} — похоже на неверную скорость (попробуйте baud: auto)")
    for status, msg in markers(text):
        getattr(c.ui, {"ok": "ok", "warn": "warn"}.get(status, "info"))(msg)
        c.log(f"[{status}] uart: {msg}")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for ln in lines[:SHOW_LINES]:
        c.ui.info("│ " + ln[:160])
    if len(lines) > SHOW_LINES:
        c.ui.info(f"… ещё {len(lines) - SHOW_LINES} строк — в {txt.name}")
    return c._finish("captured")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m dirizher.extras.uart",
                                description="Журнал загрузки по UART — только приём, в устройство ничего не посылается")
    p.add_argument("--config", default=str(ROOT / "config.yaml"), help="путь к config.yaml")
    p.add_argument("--port", help="порт переходника, например /dev/ttyUSB0")
    p.add_argument("--baud", help="скорость (115200…) или auto — подобрать")
    p.add_argument("--time", type=float, dest="time_s", help="сколько слушать, с")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    cfg = load_config(Path(a.config))
    c = Conductor(cfg, ConsoleUI())
    try:
        sid = capture(c, cfg, port=a.port, baud=a.baud, time_s=a.time_s)
        return 0 if c.memory.session(sid)["outcome"] == "captured" else 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
