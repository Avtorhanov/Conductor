"""I2C EEPROM 24Cxx (24C01…24C512) через Bus Pirate 5.

Bus Pirate 5 управляется через свой текстовый терминал (USB CDC, /dev/ttyACM0). Работу с портом
делает помощник dirizher/helpers/bp5_i2c.py — он запускается ОТДЕЛЬНЫМ ПРОЦЕССОМ через Runner,
как любая внешняя программа: таймаут, журнал, зависание порта не роняет ядро.

Что делает плагин:
  probe — адресный байт на шину (`[0xA0]`): отвечает ли микросхема (ACK). Данные не пишутся.
  read  — вся память кусками по 16 байт.
  write — только отличающиеся куски по 8 байт; ядро потом читает обратно и сверяет.

У 24Cxx нет ID: модель (eeprom) и адрес задаёт человек, уверенность опознания 0.7.
Напряжение адаптера = psu_voltage (BP5 питает чип сам); если psu_voltage: 0, питание даёт плата,
а логика BP5 подстраивается под неё — тогда задайте programmer_voltage вручную.

Вывод WP микросхемы при записи должен быть на GND, иначе запись не пройдёт сверку (это безопасно:
ядро предложит вернуть копию «до»).

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: см. docs/INTEGRATIONS.md, раздел Bus Pirate 5.
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from ..core.models import Detection
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import as_int

HELPER = Path(__file__).resolve().parent.parent / "helpers" / "bp5_i2c.py"
EEPROM_SIZES = {"24C01": 128, "24C02": 256, "24C04": 512, "24C08": 1024, "24C16": 2048, "24C32": 4096,
                "24C64": 8192, "24C128": 16384, "24C256": 32768, "24C512": 65536}


class BusPirateI2CPlugin(Plugin):
    name = "bp5_i2c"
    title = "I2C EEPROM 24Cxx через Bus Pirate 5"
    target_class = "memory.i2c_eeprom"
    hardware = True
    common_settings = ("enabled", "programmer_voltage", "probe_timeout", "read_timeout", "write_timeout")
    settings_defaults = {"probe_timeout": 30, "read_timeout": 600, "write_timeout": 1200}
    settings_schema = [
        field("port", "Порт Bus Pirate", "port", default="/dev/ttyACM0", example="/dev/ttyACM0"),
        field("eeprom", "Микросхема", required=True, example="24C02", help="24C01 … 24C512"),
        field("address", "Адрес I2C", "int", default=0x50, example=0x50, hex=True),
        field("psu_voltage", "Питание чипа от BP5, В", "float", default=3.3, example=3.3, safety="voltage",
              help="BP5 питает чип этим напряжением; 0 — питание от платы (тогда задайте напряжение адаптера)."),
        field("pullups", "Подтяжки шины", "bool", default=True),
        field("command_timeout", "Ожидание ответа BP5, с", "float", default=3),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, dict(settings or {}))
        psu = float(self.settings.get("psu_voltage", 3.3) or 0)
        if psu and "programmer_voltage" not in self.settings:
            self.settings["programmer_voltage"] = psu        # BP5 сам питает чип этим напряжением

    @property
    def model(self) -> str:
        """«AT24C02», «24LC02B», «24c02» → «24C02»."""
        m = re.search(r"24(?:C|LC|AA|FC|CS)(\d+)", str(self.settings.get("eeprom", "")).upper())
        return f"24C{m.group(1)}" if m else str(self.settings.get("eeprom", "")).upper()

    @property
    def address(self) -> str:
        """Адрес микросхемы на шине (7 бит): 0x50 из YAML приходит числом 80 — приводим к «0x50»."""
        return f"0x{as_int(self.settings.get('address'), 0x50):02X}"

    @property
    def port(self) -> str:
        return str(self.settings.get("port", "/dev/ttyACM0"))

    def _cmd(self, op: str, path=None) -> list:
        cmd = [sys.executable, str(HELPER), "--port", self.port, "--eeprom", self.model,
               "--address", self.address,
               "--psu", str(float(self.settings.get("psu_voltage", 3.3) or 0)),
               "--timeout", str(self.settings.get("command_timeout", 3))]
        if not self.settings.get("pullups", True):
            cmd.append("--no-pullups")
        cmd.append(op)
        if path is not None:
            cmd.append(str(path))
        return cmd

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if self.model not in EEPROM_SIZES:
            return False, f"укажите eeprom в config.yaml → plugins: bp5_i2c ({', '.join(EEPROM_SIZES)})"
        if not Path(self.port).exists():
            return False, f"Bus Pirate не найден на {self.port} (проверьте кабель и группу dialout)"
        return True, f"{self.port}, {self.model} @ {self.address}"

    def probe(self):
        res = self.run(self._cmd("probe"), timeout=int(self.settings.get("probe_timeout", 30)))
        if res.code == 3:
            return None                                       # NACK — микросхема не ответила
        if not res.ok:
            raise RuntimeError(res.tail() or "помощник Bus Pirate завершился с ошибкой")
        size = EEPROM_SIZES[self.model]
        fp = {"name": self.model, "size": size, "i2c_address": self.address}
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp, confidence=0.7,
                         summary=f"I2C EEPROM отвечает по адресу {fp['i2c_address']}; "
                                 f"модель из настроек: {self.model} ({size} байт)")

    def read(self, det) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            res = self.run(self._cmd("read", out), timeout=int(self.settings.get("read_timeout", 600)))
            if not res.ok or not out.exists():
                raise RuntimeError(res.tail() or "не удалось прочитать EEPROM")
            return out.read_bytes()

    def write(self, det, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd("write", img), timeout=int(self.settings.get("write_timeout", 1200)))
            if not res.ok:
                raise RuntimeError(res.tail() or "не удалось записать EEPROM")
