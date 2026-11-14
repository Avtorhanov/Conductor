"""Эмулятор терминала Bus Pirate 5 с подключённой I2C EEPROM — для тестов без железа.

Работает в отдельном потоке на псевдотерминале (pty): помощник dirizher/helpers/bp5_i2c.py
открывает /dev/pts/N как настоящий /dev/ttyACM0 и не знает, что общается с эмулятором.

Что эмулируется (по документации docs.buspirate.com, не с железа — ПРОВЕРИТЬ НА ЖЕЛЕЗЕ):
  * приглашения «HiZ>» / «I2C>» с цветовыми кодами VT100, эхо команд;
  * меню `m` → номер I2C → вопросы о скорости и clock stretching (Enter — по умолчанию);
  * `W 3.3` (с вопросом о токе), `w`, `P`;
  * синтаксис [ ] 0xNN r:N; ответы «I2C START», «TX: 0xA0 ACK …», «RX: 0x12 ACK … NACK»
    (длинное чтение переносится на несколько строк), «I2C STOP»;
  * 24Cxx: адресация 1/2 байта, страницы с переходом на начало страницы (как у настоящей микросхемы),
    вывод WP (запись игнорируется), без подтяжек и без питания — NACK.
"""
from __future__ import annotations

import os
import pty
import re
import select
import threading
import tty

PAGE = {"24C02": 8, "24C64": 32}
ABYTES = {"24C02": 1, "24C64": 2}
COLOR_PROMPT = "\x1b[0;33m{mode}\x1b[0m> "


class BP5Emulator:
    def __init__(self, data: bytes, model: str = "24C02", address: int = 0x50, menu_i2c: int = 5):
        self.mem = bytearray(data)
        self.model = model
        self.address = address
        self.menu_i2c = menu_i2c
        self.mode = "HiZ"
        self.psu = False
        self.pullups = False
        self.wp = False
        self.present = True
        self.pointer = 0
        self.pending = []            # ожидаемые ответы на вопросы меню
        self.lines: list = []        # все принятые команды
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    # ---------- поток ----------
    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)

    def _send(self, text: str) -> None:
        os.write(self.master, text.replace("\n", "\r\n").encode())

    def _loop(self) -> None:
        buf = b""
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                continue
            try:
                chunk = os.read(self.master, 4096)
            except OSError:
                continue
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                text = line.decode(errors="replace").strip("\n")
                self.lines.append(text)
                self._send(text + "\n" + self.handle(text))

    # ---------- терминал ----------
    def prompt(self) -> str:
        return COLOR_PROMPT.format(mode=self.mode)

    def handle(self, line: str) -> str:
        if self.pending:
            kind = self.pending.pop(0)
            return self._menu_answer(kind, line)
        if line == "":
            return self.prompt()
        if line == "m":
            self.pending = ["mode"]
            return ("\n1. HiZ\n2. 1-WIRE\n3. UART\n4. HDUART\n" + f"{self.menu_i2c}. I2C\n"
                    "6. SPI\n7. 2WIRE\n8. 3WIRE\nx. Exit\nMode > ")
        if self.mode == "HiZ":
            return "Can't do that in HiZ mode, use 'm' to select a mode\n" + self.prompt()
        if line.startswith("W"):
            parts = line.split()
            self.pending = ["current"]
            self._volts = float(parts[1]) if len(parts) > 1 else 3.3
            return "Current limit 0mA to 500mA\nx. Exit\nmA (300mA*) > "
        if line == "w":
            self.psu = False
            return "Power supply: Disabled\n" + self.prompt()
        if line == "P":
            self.pullups = True
            return "\x1b[0;32mPull-up resistors:\x1b[0m Enabled (10K ohms @ 3.3V)\n" + self.prompt()
        if line.startswith("["):
            return self.bus(line) + self.prompt()
        return "Invalid command\n" + self.prompt()

    def _menu_answer(self, kind: str, line: str) -> str:
        if kind == "mode":
            if line.strip() != str(self.menu_i2c):
                return "Invalid\n" + self.prompt()
            self.pending = ["speed", "stretch"]
            return "\nI2C speed\n 1KHz to 1000KHz\n x. Exit\nKHz (400KHz*) > "
        if kind == "speed":
            return "\nClock stretching\n 1. OFF*\n 2. ON\n x. Exit\n(1) > "
        if kind == "stretch":
            self.mode = "I2C"
            return "\nMode: I2C\n" + self.prompt()
        if kind == "current":
            self.psu = True
            return (f"\nVreg output: {self._volts:.1f}V, Vref/Vout pin: {self._volts:.1f}V\n"
                    "Current: 2.1mA\nPower supply: Enabled\n" + self.prompt())
        return self.prompt()

    # ---------- шина I2C ----------
    def _acks(self, dev7: int) -> bool:
        blocks = 8 if ABYTES[self.model] == 1 else 1
        return (self.present and self.psu and self.pullups
                and self.address <= dev7 < self.address + max(1, blocks) and dev7 - self.address < blocks)

    def bus(self, line: str) -> str:
        tokens = re.findall(r"\[|\]|0x[0-9A-Fa-f]+|r:\d+|r", line)
        out, i = [], 0
        while i < len(tokens):
            if tokens[i] != "[":
                i += 1
                continue
            out.append("I2C START")
            i += 1
            dev = int(tokens[i], 16)
            i += 1
            ack = self._acks(dev >> 1)
            if dev & 1 == 0:                                  # запись: адрес + данные
                payload = []
                while i < len(tokens) and tokens[i].startswith("0x"):
                    payload.append(int(tokens[i], 16))
                    i += 1
                if not ack:
                    out.append("TX: " + " ".join(f"0x{b:02X} NACK" for b in [dev] + payload))
                else:
                    out.append("TX: " + " ".join(f"0x{b:02X} ACK" for b in [dev] + payload))
                    self._write(dev, payload)
            else:                                             # чтение
                n = 0
                while i < len(tokens) and tokens[i].startswith("r"):
                    n += int(tokens[i][2:]) if ":" in tokens[i] else 1
                    i += 1
                out.append(f"TX: 0x{dev:02X} {'ACK' if ack else 'NACK'}")
                data = []
                for _ in range(n):
                    data.append(self.mem[self.pointer] if ack else 0xFF)
                    if ack:
                        self.pointer = (self.pointer + 1) % len(self.mem)
                if data:
                    words = [f"0x{b:02X} {'NACK' if k == len(data) - 1 else 'ACK'}" for k, b in enumerate(data)]
                    rows = [" ".join(words[k:k + 8]) for k in range(0, len(words), 8)]
                    out.append("RX: " + "\n".join(rows))
            if i < len(tokens) and tokens[i] == "]":
                out.append("I2C STOP")
                i += 1
        return "\n".join(out) + "\n"

    def _write(self, dev: int, payload: list) -> None:
        nab = ABYTES[self.model]
        if len(payload) < nab:
            return
        if nab == 1:
            addr = (((dev >> 1) - self.address) << 8) | payload[0]
        else:
            addr = (payload[0] << 8) | payload[1]
        addr %= len(self.mem)
        self.pointer = addr
        data = payload[nab:]
        if not data or self.wp:
            return
        page = PAGE[self.model]
        start = addr - addr % page
        for k, b in enumerate(data):                          # переход внутри страницы, как у 24Cxx
            self.mem[start + (addr - start + k) % page] = b
        self.pointer = start + (addr - start + len(data)) % page
