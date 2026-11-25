"""Симулятор: виртуальная SPI-флешка W25Q64 (8 МБ) с «прошивкой» учебной платы.

Нужен, чтобы освоить весь цикл без железа и без риска:
    python3 -m dirizher sim reset     — исправная прошивка
    python3 -m dirizher sim corrupt   — повредить часть прошивки (сбой обновления)
    python3 -m dirizher sim erase     — стереть всё (0xFF)
    python3 -m dirizher sim flaky     — «плохой контакт»: чтения не совпадают
    python3 -m dirizher sim steady    — контакт снова хороший
Работает только в режиме --sim и никогда не трогает настоящее железо.
"""
from __future__ import annotations

import os
import random
import zlib

from .. import __version__
from ..core.models import Detection
from ..core.plugin import Plugin


class SimulatorPlugin(Plugin):
    name = "simulator"
    title = "Симулятор: виртуальная SPI-флешка W25Q64 (8 МБ)"
    target_class = "memory.spi_nor"
    hardware = False
    common_settings = ("enabled",)
    settings_schema: list = []        # своих настроек нет

    SIZE = 8 * 1024 * 1024
    JEDEC = "EF4017"
    PAYLOAD_OFFSET = 0x1000
    PAYLOAD_SIZE = 3 * 1024 * 1024
    UNIQUE_OFFSET = 0x7FF000            # здесь «MAC и серийник» учебной платы

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.dir = conductor.data_dir / "sim"
        self.image = self.dir / "flash.bin"
        self.flaky_flag = self.dir / "flaky"

    # ---------- исправная «прошивка» ----------
    @classmethod
    def healthy(cls) -> bytes:
        rnd = random.Random(2026)
        img = bytearray(b"\xff" * cls.SIZE)
        payload = rnd.randbytes(cls.PAYLOAD_SIZE)
        header = (b"FWIM" + (1).to_bytes(2, "little") + len(payload).to_bytes(4, "little")
                  + zlib.crc32(payload).to_bytes(4, "little"))
        img[0:len(header)] = header
        img[cls.PAYLOAD_OFFSET:cls.PAYLOAD_OFFSET + len(payload)] = payload
        unique = b"MAC=02:00:00:12:34:56;SN=DRZ-000123"
        img[cls.UNIQUE_OFFSET:cls.UNIQUE_OFFSET + len(unique)] = unique
        return bytes(img)

    def _ensure(self) -> None:
        if not self.image.exists():
            self.dir.mkdir(parents=True, exist_ok=True)
            self._store(self.healthy())

    def _store(self, data: bytes) -> None:
        tmp = self.image.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, self.image)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        return True, "виртуальное устройство (железо не используется)"

    def version(self):
        return f"встроенный {__version__}"

    def probe(self):
        self._ensure()
        return Detection(
            plugin=self.name, target_class=self.target_class,
            fingerprint={"jedec_id": self.JEDEC, "name": "W25Q64 (виртуальная)", "size": self.SIZE},
            confidence=0.95, summary="Winbond W25Q64, 8 МБ SPI NOR (виртуальная)",
        )

    def read(self, det) -> bytes:
        self._ensure()
        data = self.image.read_bytes()
        if self.flaky_flag.exists():                    # имитация плохого контакта
            buf = bytearray(data)
            rnd = random.Random()
            for _ in range(rnd.randint(3, 30)):
                pos = rnd.randrange(self.PAYLOAD_OFFSET, self.PAYLOAD_OFFSET + self.PAYLOAD_SIZE)
                buf[pos] ^= 1 << rnd.randrange(8)
            data = bytes(buf)
        return data

    def write(self, det, data: bytes) -> None:
        if len(data) != self.SIZE:
            raise ValueError(f"размер образа {len(data)} не равен размеру чипа {self.SIZE}")
        self._store(data)

    # ---------- учебные поломки ----------
    def do_status(self) -> str:
        self._ensure()
        data = self.image.read_bytes()
        state = "исправна" if data == self.healthy() else (
            "стёрта" if data.count(0xFF) == len(data) else "отличается от исправной")
        contact = "плохой (flaky)" if self.flaky_flag.exists() else "хороший"
        return f"виртуальная флешка: {state}; контакт: {contact}"

    def do_reset(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._store(self.healthy())
        return "записана исправная прошивка"

    def do_corrupt(self) -> str:
        self._ensure()
        buf = bytearray(self.image.read_bytes())
        buf[0x40000:0x50000] = b"\xff" * 0x10000      # «оборвалось обновление»: стёртые сектора
        rnd = random.Random(7)
        for _ in range(200):                           # и россыпь испорченных байтов
            buf[rnd.randrange(0x100000, 0x200000)] = rnd.randrange(256)
        self._store(bytes(buf))
        return "прошивка повреждена (стёрто 16 секторов и испорчены отдельные байты)"

    def do_erase(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._store(b"\xff" * self.SIZE)
        return "чип полностью стёрт"

    def do_flaky(self) -> str:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.flaky_flag.touch()
        return "включён «плохой контакт»: чтения будут различаться"

    def do_steady(self) -> str:
        if self.flaky_flag.exists():
            self.flaky_flag.unlink()
        return "контакт снова хороший"
