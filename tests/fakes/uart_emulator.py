"""Эмулятор платы, печатающей журнал загрузки в UART, — на псевдотерминале, для тестов без железа.

Модуль dirizher/extras/uart.py открывает /dev/pts/N как настоящий /dev/ttyUSB0. Эмулятор держит свою
сторону псевдотерминала и смотрит, какую скорость выставил приёмник (termios общий у обоих концов):
на «своей» скорости печатает журнал в стиле U-Boot, на чужой — мусор, как настоящий UART на неверной
скорости. Всё, что приёмник отправил бы в плату, копится в `received` (должно остаться пустым).
"""
from __future__ import annotations

import errno
import os
import pty
import random
import termios
import threading
import time
import tty

UBOOT_LOG = """\
U-Boot 2016.11 (Jun 12 2021 - 10:22:41 +0800)

DRAM:  128 MiB
SF: Detected w25q64 with page size 256 Bytes, erase size 4 KiB, total 8 MiB
Hit any key to stop autoboot:  0
## Booting kernel from Legacy Image at 9f050000 ...
   Image Name:   Linux-4.14.90
   Verifying Checksum ... OK
Starting kernel ...

[    0.000000] Linux version 4.14.90 (builder@sdk) (gcc version 7.5.0) #1 SMP
[    0.000000] Memory: 121412K/131072K available
[    1.204511] VFS: Mounted root (squashfs filesystem) readonly on device 31:2.
"""

BROKEN_LOG = """\
U-Boot 2016.11 (Jun 12 2021 - 10:22:41 +0800)

DRAM:  128 MiB
Hit any key to stop autoboot:  0
## Booting kernel from Legacy Image at 9f050000 ...
   Verifying Checksum ... Bad Data CRC
ERROR: can't get kernel image!
"""

SPEEDS = {getattr(termios, f"B{b}"): b for b in (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)}


class UartEmulator:
    def __init__(self, log: str = UBOOT_LOG, baud: int = 115200, rate: int = 4000, silent: bool = False):
        self.log = log.replace("\n", "\r\n").encode()
        self.baud = baud
        self.rate = rate                         # байт в секунду
        self.silent = silent
        self.received = bytearray()
        self.master, self.slave = pty.openpty()
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)
        self.port = os.ttyname(self.slave)
        self._rnd = random.Random(7)
        self._pos = 0
        self._stop = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop = True
        self._thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)

    def speed(self):
        try:
            return SPEEDS.get(termios.tcgetattr(self.slave)[5])
        except termios.error:
            return None

    def _chunk(self, n: int) -> bytes:
        if self.speed() != self.baud:
            return bytes(self._rnd.choice(range(0x80, 0x100)) if self._rnd.random() < 0.7
                         else self._rnd.randrange(0, 0x20) for _ in range(n))
        size = len(self.log)
        out = bytes(self.log[(self._pos + i) % size] for i in range(n))
        self._pos = (self._pos + n) % size
        return out

    def _loop(self) -> None:
        step = 0.02
        while not self._stop:
            try:
                self.received += os.read(self.master, 4096)
            except (BlockingIOError, OSError):
                pass
            if not self.silent:
                try:
                    os.write(self.master, self._chunk(max(1, int(self.rate * step))))
                except OSError as e:                 # буфер полон — никто не читает
                    if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EIO):
                        raise
            time.sleep(step)
