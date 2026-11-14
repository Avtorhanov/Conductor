#!/usr/bin/env python3
"""Помощник: I2C EEPROM 24Cxx через текстовый терминал Bus Pirate 5.

Запускается плагином bp5_i2c ОТДЕЛЬНЫМ ПРОЦЕССОМ (как flashrom или avrdude): зависание порта
не роняет ядро, а таймаут и журнал даёт Runner. Только стандартная библиотека Python (termios).

    python3 bp5_i2c.py --port /dev/ttyACM0 --eeprom 24C02 [--address 0x50] [--psu 3.3] [--no-pullups] probe
    python3 bp5_i2c.py ... read  ФАЙЛ
    python3 bp5_i2c.py ... write ФАЙЛ

Коды выхода: 0 — успех; 3 — микросхема не ответила (NACK); 2 — ошибка.

Синтаксис Bus Pirate 5 (docs.buspirate.com): «[» — START, «]» — STOP, «0xA0» — байт, «r:16» — прочитать
16 байт. Ответ: «TX: 0xA0 ACK 0x00 ACK», «RX: 0x12 ACK 0x34 ACK … 0xFF NACK», «I2C STOP», приглашение «I2C>».
Чтение — в два шага, как в документации BP5: установить указатель адреса, затем читать.
Запись — кусками по 8 байт, выровненными по 8: страница любой 24Cxx кратна 8, граница не пересекается.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: меню смены режима (`m`), вопросы после `W 3.3`, раскраску вывода вашей прошивки.
"""
from __future__ import annotations

import argparse
import os
import re
import select
import sys
import termios
import time
import tty

# объём, байт адреса
EEPROMS = {
    "24C01": (128, 1), "24C02": (256, 1), "24C04": (512, 1), "24C08": (1024, 1), "24C16": (2048, 1),
    "24C32": (4096, 2), "24C64": (8192, 2), "24C128": (16384, 2), "24C256": (32768, 2), "24C512": (65536, 2),
}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[()][A-Za-z0-9]|\x07")
MODE_MENU_RE = re.compile(r"(\d+)\.\s*I2C\b")
RX_BYTE_RE = re.compile(r"0x([0-9A-Fa-f]{2})\s+N?ACK")
CHUNK_READ = 16
CHUNK_WRITE = 8
WRITE_CYCLE_S = 0.01           # tWR у 24Cxx до 5–10 мс


class NoAck(Exception):
    pass


def normalize(model: str) -> str:
    m = re.search(r"24(?:C|LC|AA|FC|CS)(\d+)", model.upper())
    return f"24C{m.group(1)}" if m else model.upper()


def clean(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def last_line(text: str) -> str:
    lines = [ln for ln in clean(text).split("\n") if ln.strip()]
    return lines[-1].strip() if lines else ""


class Serial:
    """Последовательный порт в «сыром» режиме (CDC ACM: скорость не важна)."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        tty.setraw(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[4] = attrs[5] = termios.B115200
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        termios.tcflush(self.fd, termios.TCIOFLUSH)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def read(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.fd], [], [], timeout)
        return os.read(self.fd, 4096) if r else b""

    def close(self) -> None:
        os.close(self.fd)


class BusPirate:
    def __init__(self, port, timeout: float = 3.0):
        self.io = port
        self.timeout = timeout

    def cmd(self, line: str) -> str:
        """Отправить строку и дождаться приглашения «…>»."""
        self.io.write(line.encode() + b"\r")
        buf, deadline = b"", time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            chunk = self.io.read(0.05)
            if chunk:
                buf += chunk
            if last_line(buf.decode(errors="replace")).endswith(">"):
                tail = self.io.read(0.03)                     # приглашение — последнее, что печатается
                if not tail:
                    return clean(buf.decode(errors="replace"))
                buf += tail
        raise RuntimeError(f"Bus Pirate не ответил на «{line}» за {self.timeout} с")

    def answer_defaults(self, text: str, mode: str = "I2C") -> str:
        """Пройти вопросы меню, принимая значения по умолчанию (Enter), до приглашения режима."""
        for _ in range(8):
            if last_line(text) == f"{mode}>":
                return text
            text = self.cmd("")
        raise RuntimeError(f"не удалось дойти до приглашения {mode}>: «{last_line(text)}»")

    def ensure_i2c(self) -> None:
        text = self.cmd("")
        if last_line(text) == "I2C>":
            return
        menu = self.cmd("m")
        m = MODE_MENU_RE.search(menu)
        if not m:
            raise RuntimeError("в меню режимов Bus Pirate не найден пункт I2C")
        self.answer_defaults(self.cmd(m.group(1)))

    def power(self, volts: float) -> None:
        self.answer_defaults(self.cmd(f"W {volts}"))

    def power_off(self) -> None:
        self.answer_defaults(self.cmd("w"))

    def pullups(self) -> None:
        self.answer_defaults(self.cmd("P"))

    def bus(self, syntax: str) -> str:
        out = self.cmd(syntax)
        if re.search(r"TX:[^\n]*\bNACK\b", out):
            raise NoAck(out)
        return out


class Eeprom:
    def __init__(self, bp: BusPirate, model: str, address: int):
        self.bp = bp
        self.model = normalize(model)
        if self.model not in EEPROMS:
            raise RuntimeError(f"неизвестная модель {model}; поддерживаются: {', '.join(EEPROMS)}")
        self.size, self.abytes = EEPROMS[self.model]
        self.address = address

    def _dev_and_addr(self, addr: int) -> tuple:
        if self.abytes == 1:   # 24C04/08/16: старшие биты адреса — в адресе устройства
            dev = (self.address | ((addr >> 8) & 0x07)) << 1
            return dev, f"0x{addr & 0xFF:02X}"
        return self.address << 1, f"0x{(addr >> 8) & 0xFF:02X} 0x{addr & 0xFF:02X}"

    def present(self) -> bool:
        try:
            self.bp.bus(f"[0x{self.address << 1:02X}]")
            return True
        except NoAck:
            return False

    def read_all(self) -> bytes:
        out = bytearray()
        for addr in range(0, self.size, CHUNK_READ):
            dev, a = self._dev_and_addr(addr)
            n = min(CHUNK_READ, self.size - addr)
            self.bp.bus(f"[0x{dev:02X} {a}]")
            text = self.bp.bus(f"[0x{dev | 1:02X} r:{n}]")
            rx = text[text.find("RX:"):] if "RX:" in text else ""
            got = [int(b, 16) for b in RX_BYTE_RE.findall(rx)]
            if len(got) != n:
                raise RuntimeError(f"адрес 0x{addr:04X}: получено {len(got)} байт из {n} (формат RX не распознан)")
            out += bytes(got)
        return bytes(out)

    def write_all(self, data: bytes, current: bytes) -> int:
        if len(data) != self.size:
            raise RuntimeError(f"размер образа {len(data)} не равен объёму {self.model} ({self.size})")
        written = 0
        for addr in range(0, self.size, CHUNK_WRITE):
            chunk = data[addr:addr + CHUNK_WRITE]
            if chunk == current[addr:addr + CHUNK_WRITE]:
                continue                                    # не изнашиваем ячейки зря
            dev, a = self._dev_and_addr(addr)
            self.bp.bus(f"[0x{dev:02X} {a} " + " ".join(f"0x{b:02X}" for b in chunk) + "]")
            time.sleep(WRITE_CYCLE_S)
            written += 1
        return written


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="I2C EEPROM 24Cxx через Bus Pirate 5")
    p.add_argument("--port", required=True)
    p.add_argument("--eeprom", required=True)
    p.add_argument("--address", default="0x50")
    p.add_argument("--psu", type=float, default=0.0, help="включить питание BP5, В (0 — плата питается сама)")
    p.add_argument("--no-pullups", action="store_true")
    p.add_argument("--timeout", type=float, default=3.0)
    p.add_argument("op", choices=["probe", "read", "write"])
    p.add_argument("file", nargs="?")
    a = p.parse_args(argv)
    port = None
    bp = None
    try:
        port = Serial(a.port)
        bp = BusPirate(port, a.timeout)
        bp.ensure_i2c()
        if a.psu:
            bp.power(a.psu)
        if not a.no_pullups:
            bp.pullups()
        chip = Eeprom(bp, a.eeprom, int(a.address, 0))
        if a.op == "probe":
            if not chip.present():
                print("NOT_FOUND")
                return 3
            print(f"FOUND {chip.model} @0x{chip.address:02X} size={chip.size}")
            return 0
        if not a.file:
            raise RuntimeError("не указан файл")
        if a.op == "read":
            data = chip.read_all()
            with open(a.file, "wb") as f:
                f.write(data)
            print(f"READ {len(data)}")
            return 0
        with open(a.file, "rb") as f:
            data = f.read()
        current = chip.read_all()
        n = chip.write_all(data, current)
        print(f"WROTE {n} chunks of {CHUNK_WRITE}")
        return 0
    except NoAck:
        print("NOT_FOUND: микросхема не ответила (NACK)", file=sys.stderr)
        return 3
    except Exception as e:                                   # noqa: BLE001 — текст уйдёт в журнал
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    finally:
        if bp is not None and a.psu:
            try:
                bp.power_off()
            except Exception:                                # noqa: BLE001
                pass
        if port is not None:
            port.close()


if __name__ == "__main__":
    sys.exit(main())
