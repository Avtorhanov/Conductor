#!/usr/bin/env python3
"""Помощник: I2C EEPROM 24Cxx через шину I2C самого Raspberry Pi (/dev/i2c-1).

Запускается лезвием pi_i2c ОТДЕЛЬНЫМ ПРОЦЕССОМ (как flashrom или помощник Bus Pirate): зависание
шины не роняет ядро, таймаут и журнал даёт Runner. Только стандартная библиотека Python
(fcntl.ioctl I2C_RDWR, структуры — struct и array).

    python3 i2c_eeprom.py --bus /dev/i2c-1 --eeprom 24C02 [--address 0x50] probe
    python3 i2c_eeprom.py ... read  ФАЙЛ
    python3 i2c_eeprom.py ... write ФАЙЛ

Коды выхода: 0 — успех; 3 — микросхема не ответила (NACK); 2 — ошибка.

Устройство кода — транспорт отдельно от логики:
  * LinuxI2C — транспорт: три операции шины (write, read, write_read с повторным START);
  * Eeprom — логика 24Cxx: адресация (1 или 2 байта адреса; у 24C04/08/16 старшие биты адреса —
    в адресе устройства), чтение кусками, запись СТРАНИЦАМИ только там, где байты отличаются,
    ожидание цикла записи опросом (ACK polling).
В тестах транспорт подменяется эмулятором EEPROM (tests/fakes/i2c_eeprom_emulator.py).

probe ничего не пишет: «чтение текущего адреса» по 1 байту (так же — опрос адресов 0x50…0x57
и ожидание после записи). Ответ probe: «FOUND …» и «ACK=0x50,0x51» — какие адреса EEPROM отвечают.

Страница записи — наименьшая у распространённых производителей (Atmel/Microchip, ST, ON): 24C01/02 — 8,
24C04/08/16 — 16, 24C32/64 — 32, 24C128/256 — 64, 24C512 — 128 байт. Меньшая страница безопасна
(кусок выровнен и не пересекает границу большей), большая — нет: байты «завернутся» в начало страницы.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: коды ошибок NACK драйвера i2c-bcm2835 (EREMOTEIO/ENXIO), ожидание цикла записи
опросом на настоящей 24Cxx, наибольший кусок чтения за одно сообщение, скорость шины по умолчанию.
"""
from __future__ import annotations

import argparse
import array
import errno
import os
import re
import struct
import sys
import time

# объём, байт адреса, страница записи
EEPROMS = {
    "24C01": (128, 1, 8), "24C02": (256, 1, 8), "24C04": (512, 1, 16), "24C08": (1024, 1, 16),
    "24C16": (2048, 1, 16), "24C32": (4096, 2, 32), "24C64": (8192, 2, 32), "24C128": (16384, 2, 64),
    "24C256": (32768, 2, 64), "24C512": (65536, 2, 128),
}
SCAN_RANGE = range(0x50, 0x58)          # адреса EEPROM; другие устройства платы не трогаем
CHUNK_READ = 32
WRITE_CYCLE_MAX_S = 0.05                # tWR у 24Cxx — до 5–10 мс; ждём с запасом
POLL_STEP_S = 0.001

# linux/i2c-dev.h, linux/i2c.h
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
NACK_ERRNO = {errno.EREMOTEIO, errno.ENXIO, errno.EIO}


class NoAck(Exception):
    pass


def normalize(model: str) -> str:
    """«AT24C02», «24LC02B», «24c02» → «24C02»."""
    m = re.search(r"24(?:C|LC|AA|FC|CS)(\d+)", str(model).upper())
    return f"24C{m.group(1)}" if m else str(model).upper()


# ======================= транспорт: шина Linux =======================
# struct i2c_msg {__u16 addr; __u16 flags; __u16 len; __u8 *buf;} и struct i2c_rdwr_ioctl_data
# {struct i2c_msg *msgs; __u32 nmsgs;} — упакованы struct в родном выравнивании («@»), адреса буферов —
# array.buffer_info() (без ctypes: сторож кода его не пропускает).
MSG_FMT = "@HHHP"
RDWR_FMT = "@PI0P"                      # «0P» — хвостовое выравнивание, как sizeof в ядре


class LinuxI2C:
    """Шина /dev/i2c-N: сообщения I2C_RDWR (несколько сообщений — один обмен с повторным START)."""

    def __init__(self, path: str):
        self.fd = os.open(path, os.O_RDWR)

    def _transfer(self, addr: int, parts: list) -> list:
        import fcntl
        bufs = [array.array("B", bytes(data) or b"\0") for _, data in parts]      # живут до конца ioctl
        packed = b"".join(struct.pack(MSG_FMT, addr, flags, len(data), buf.buffer_info()[0])
                          for (flags, data), buf in zip(parts, bufs))
        msgs = array.array("B", packed)
        rdwr = bytearray(struct.pack(RDWR_FMT, msgs.buffer_info()[0], len(parts)))
        try:
            fcntl.ioctl(self.fd, I2C_RDWR, rdwr)
        except OSError as e:
            if e.errno in NACK_ERRNO:
                raise NoAck(f"0x{addr:02X}: нет ответа ({os.strerror(e.errno)})") from None
            raise
        return [buf.tobytes()[:len(data)] for buf, (_, data) in zip(bufs, parts)]

    def write(self, addr: int, data: bytes) -> None:
        self._transfer(addr, [(0, bytes(data))])

    def read(self, addr: int, n: int) -> bytes:
        return self._transfer(addr, [(I2C_M_RD, bytes(n))])[0]

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        return self._transfer(addr, [(0, bytes(data)), (I2C_M_RD, bytes(n))])[1]

    def close(self) -> None:
        os.close(self.fd)


# ======================= логика 24Cxx =======================
class Eeprom:
    def __init__(self, bus, model: str, address: int = 0x50, page: int | None = None, chunk: int = CHUNK_READ,
                 write_cycle_s: float = WRITE_CYCLE_MAX_S):
        self.bus = bus
        self.model = normalize(model)
        if self.model not in EEPROMS:
            raise RuntimeError(f"неизвестная модель {model}; поддерживаются: {', '.join(EEPROMS)}")
        self.size, self.abytes, self.page = EEPROMS[self.model]
        if page:
            if page & (page - 1) or not 1 <= page <= 256:
                raise RuntimeError(f"страница {page}: нужна степень двойки от 1 до 256")
            self.page = page
        self.chunk = max(1, min(int(chunk), 256))
        self.address = address
        self.write_cycle_s = write_cycle_s
        self.polls = 0                     # сколько раз опрашивали после записи (для журнала)

    def _dev_and_addr(self, addr: int) -> tuple:
        if self.abytes == 1:               # 24C04/08/16: старшие биты адреса — в адресе устройства
            return self.address | ((addr >> 8) & 0x07), bytes([addr & 0xFF])
        return self.address, bytes([(addr >> 8) & 0xFF, addr & 0xFF])

    def present(self, address: int | None = None) -> bool:
        try:
            self.bus.read(self.address if address is None else address, 1)   # чтение текущего адреса
            return True
        except NoAck:
            return False

    def scan(self) -> list:
        """Какие адреса EEPROM (0x50…0x57) отвечают. Только чтение."""
        return [a for a in SCAN_RANGE if self.present(a)]

    def read_all(self) -> bytes:
        out = bytearray()
        for addr in range(0, self.size, self.chunk):
            n = min(self.chunk, self.size - addr)
            if self.abytes == 1:           # кусок не пересекает блок 256 байт (другой адрес устройства)
                n = min(n, 256 - (addr & 0xFF))
            dev, a = self._dev_and_addr(addr)
            got = self.bus.write_read(dev, a, n)
            if len(got) != n:
                raise RuntimeError(f"адрес 0x{addr:04X}: получено {len(got)} байт из {n}")
            out += got
        if len(out) != self.size:
            raise RuntimeError(f"прочитано {len(out)} байт из {self.size}")
        return bytes(out)

    def _wait_ready(self, dev: int) -> None:
        """Ожидание цикла записи: микросхема не отвечает, пока пишет (ACK polling)."""
        deadline = time.monotonic() + self.write_cycle_s
        while True:
            self.polls += 1
            try:
                self.bus.read(dev, 1)
                return
            except NoAck:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"микросхема не закончила запись за {self.write_cycle_s * 1000:.0f} мс "
                                       "(вывод WP на питании? провод SDA?)") from None
                time.sleep(POLL_STEP_S)

    def write_all(self, data: bytes, current: bytes) -> int:
        """Записать отличающиеся страницы. Вернуть число записанных страниц."""
        if len(data) != self.size:
            raise RuntimeError(f"размер образа {len(data)} не равен объёму {self.model} ({self.size})")
        if len(current) != self.size:
            raise RuntimeError(f"текущее содержимое {len(current)} байт, а не {self.size}")
        written = 0
        for addr in range(0, self.size, self.page):
            chunk = data[addr:addr + self.page]
            if chunk == current[addr:addr + self.page]:
                continue                                    # не изнашиваем ячейки зря
            dev, a = self._dev_and_addr(addr)
            self.bus.write(dev, a + chunk)
            self._wait_ready(dev)
            written += 1
        return written


# ======================= командная строка =======================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="I2C EEPROM 24Cxx через I2C самого Raspberry Pi")
    p.add_argument("--bus", default="/dev/i2c-1")
    p.add_argument("--eeprom", required=True)
    p.add_argument("--address", default="0x50")
    p.add_argument("--page", type=int, default=0, help="страница записи, байт (0 — по модели)")
    p.add_argument("--chunk", type=int, default=CHUNK_READ, help="кусок чтения, байт")
    p.add_argument("op", choices=["probe", "read", "write"])
    p.add_argument("file", nargs="?")
    return p


def main(argv=None, bus=None) -> int:
    """bus — готовый транспорт (в тестах — эмулятор); иначе открывается --bus."""
    a = build_parser().parse_args(argv)
    own = bus is None
    try:
        if own:
            bus = LinuxI2C(a.bus)
        chip = Eeprom(bus, a.eeprom, int(a.address, 0), page=a.page or None, chunk=a.chunk)
        if a.op == "probe":
            alive = chip.scan()
            if chip.address not in alive and not chip.present():
                print("NOT_FOUND")
                return 3
            print(f"FOUND {chip.model} @0x{chip.address:02X} size={chip.size} page={chip.page}")
            print("ACK=" + ",".join(f"0x{x:02X}" for x in alive))
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
        print(f"WROTE {n} pages of {chip.page}")
        return 0
    except NoAck as e:
        print(f"NOT_FOUND: микросхема не ответила (NACK) {e}", file=sys.stderr)
        return 3
    except FileNotFoundError as e:
        print(f"ОШИБКА: нет шины {e.filename} — включите I2C (sudo raspi-config → Interface Options → I2C)",
              file=sys.stderr)
        return 2
    except PermissionError as e:
        print(f"ОШИБКА: нет прав на {e.filename} — sudo usermod -aG i2c $USER и войдите заново", file=sys.stderr)
        return 2
    except Exception as e:                                   # noqa: BLE001 — текст уйдёт в журнал
        print(f"ОШИБКА: {e}", file=sys.stderr)
        return 2
    finally:
        if own and bus is not None:
            bus.close()


if __name__ == "__main__":
    sys.exit(main())
