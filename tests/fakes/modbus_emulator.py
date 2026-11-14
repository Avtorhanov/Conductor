"""Эмулятор прибора Modbus RTU на псевдотерминале — для тестов без железа.

Помощник skills/modbus_rtu/rtu.py открывает /dev/pts/N как настоящий /dev/ttyUSB0 и не знает,
что говорит с эмулятором. Протокол — по спецификации Modbus over Serial Line (не с железа).

Что эмулируется: функции 3, 4, 6, 16; CRC; исключения (нет регистра, только чтение);
адрес прибора (чужие кадры игнорируются); «молчит» (silent); «живой» регистр (меняется сам);
«упрямый» регистр (принимает запись, но хранит прежнее — сверка не совпадёт).
"""
from __future__ import annotations

import os
import pty
import select
import threading
import tty


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


# Учебный термопреобразователь (карта как в skills/modbus_rtu/profiles/thermo_example.yaml):
# тип Pt100, °C, диапазон 0.0…400.0, фильтр 2 с, адрес 1, код скорости 3, версия прошивки 0x0102.
THERMO = [0, 0, 0, 4000, 2, 1, 3, 0x0102]


class ModbusEmulator:
    def __init__(self, regs=None, unit: int = 1, readonly=(5, 6, 7), size: int | None = None):
        self.regs = list(regs if regs is not None else THERMO)
        if size:
            self.regs += [0] * (size - len(self.regs))
        self.unit = unit
        self.readonly = set(readonly)
        self.silent = False
        self.live = None            # номер регистра, который меняется при каждом чтении
        self.stubborn = set()       # регистры, которые «принимают» запись, но не меняются
        self.writes: list = []      # (регистр, значение) — что писали
        self.requests: list = []    # все принятые кадры (hex)
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        self.port = os.ttyname(self.slave)
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)

    # ---------- кадры ----------
    @staticmethod
    def _need(buf: bytes) -> int:
        if len(buf) < 2:
            return 8
        if buf[1] in (3, 4, 6):
            return 8
        if buf[1] == 16:
            return 9 + buf[6] if len(buf) >= 7 else 9
        return 4

    def _reply(self, pdu: bytes) -> None:
        frame = bytes([self.unit]) + pdu
        os.write(self.master, frame + crc16(frame))

    def _exc(self, func: int, code: int) -> None:
        self._reply(bytes([func | 0x80, code]))

    def _handle(self, frame: bytes) -> None:
        self.requests.append(frame.hex())
        if crc16(frame[:-2]) != frame[-2:] or frame[0] != self.unit or self.silent:
            return
        func = frame[1]
        if func in (3, 4):
            start, count = int.from_bytes(frame[2:4], "big"), int.from_bytes(frame[4:6], "big")
            if start + count > len(self.regs) or count == 0:
                return self._exc(func, 2)
            if self.live is not None:
                self.regs[self.live] = (self.regs[self.live] + 1) & 0xFFFF
            data = b"".join(v.to_bytes(2, "big") for v in self.regs[start:start + count])
            return self._reply(bytes([func, len(data)]) + data)
        if func == 6:
            reg, val = int.from_bytes(frame[2:4], "big"), int.from_bytes(frame[4:6], "big")
            if reg >= len(self.regs):
                return self._exc(func, 2)
            if reg in self.readonly:
                return self._exc(func, 3)
            self.writes.append((reg, val))
            if reg not in self.stubborn:
                self.regs[reg] = val
            return self._reply(frame[1:6])
        if func == 16:
            start, count = int.from_bytes(frame[2:4], "big"), int.from_bytes(frame[4:6], "big")
            if start + count > len(self.regs):
                return self._exc(func, 2)
            if any(r in self.readonly for r in range(start, start + count)):
                return self._exc(func, 3)
            for i in range(count):
                val = int.from_bytes(frame[7 + 2 * i:9 + 2 * i], "big")
                self.writes.append((start + i, val))
                if start + i not in self.stubborn:
                    self.regs[start + i] = val
            return self._reply(frame[1:6])
        return self._exc(func, 1)

    def _loop(self) -> None:
        buf = b""
        while not self._stop:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if not r:
                buf = b""                    # пауза между кадрами — начало нового кадра
                continue
            try:
                buf += os.read(self.master, 512)
            except OSError:
                return
            while len(buf) >= self._need(buf):
                n = self._need(buf)
                frame, buf = buf[:n], buf[n:]
                self._handle(frame)
