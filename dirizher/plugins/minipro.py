"""Память в панельке через программатор T48 / TL866II+ (программа minipro).

minipro (лицензия GPL-3.0) вызывается отдельным процессом. Тип микросхемы задаёт человек
(device — как в `minipro -L <часть имени>`), программатор сам подаёт нужные напряжения.

Что делает плагин:
  probe — `minipro -p <device> -D`: только чтение ID чипа.
  read  — `minipro -p <device> -r файл`.
  write — `minipro -p <device> -w файл` (minipro сам стирает, пишет и сверяет).

Класс цели определяется по имени device (можно задать вручную: target_class):
  24Cxx/24LCxx → memory.i2c_eeprom;  W25/MX25/GD25… → memory.spi_nor (ID → jedec_id,
  тогда подходят уже имеющиеся профили SPI NOR);  93Cxx → memory.microwire;  прочее → memory.parallel.
Микроконтроллеры (ATmega, PIC…) через этот плагин не обслуживаются: стирание при записи затронет
EEPROM и фьюзы — для AVR есть плагин avrdude.

Запрещено (guard): -y (игнорировать неверный ID), -s/-S (игнорировать размер), -v (без сверки),
-E (стирание), -c config / --fuses / --lock (фьюзы и биты защиты).

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строку «Chip ID…» в выводе `minipro -D` вашей версии; как minipro
отвечает на -D для чипов без ID (EPROM 27Cxx).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import extra_args, fail_text, guard, tool_version

FORBIDDEN = ["-y", "--no_id_error", "-s", "--no_size_error", "-S", "--no_size_warning", "-v",
             "--skip_verify", "-E", "--erase", "-c", "--fuses", "--lock", "--uid", "-x", "--skip_id"]
CHIP_ID_RE = re.compile(r"Chip ID[^:\n]*:\s*(?:0x)?([0-9A-Fa-f]+)", re.IGNORECASE)
MISMATCH_RE = re.compile(r"mismatch|expected|invalid chip id|incorrect chip id", re.IGNORECASE)
NO_PROGRAMMER_RE = re.compile(r"no programmer found|error opening device|unable to open|programmer not found",
                              re.IGNORECASE)
MCU_RE = re.compile(r"^(AT)?(MEGA|TINY|XMEGA|90S)|^ATMEGA|^ATTINY|^PIC|^DSPIC|^AT89|^STC|^P87|^AVR", re.I)

CLASS_RULES = [
    (re.compile(r"^(AT|M|BR|CAT|FM|ST)?24(C|LC|AA|FC|CS)\d", re.I), "memory.i2c_eeprom"),
    (re.compile(r"^(W25|MX25|EN25|GD25|S25FL|SST25|M25P|AT25DF|AT25SF|IS25|XM25|BY25|ZB25|FM25Q|PM25|XT25)", re.I),
     "memory.spi_nor"),
    (re.compile(r"^(AT|M|CAT|ST)?93(C|LC|AA)\d", re.I), "memory.microwire"),
]


def guess_class(device: str) -> str:
    for rule, cls in CLASS_RULES:
        if rule.search(device):
            return cls
    return "memory.parallel"


class MiniproPlugin(Plugin):
    name = "minipro"
    title = "Память в панельке через T48 / TL866II+ (minipro)"
    target_class = "memory.parallel"
    hardware = True
    settings_defaults = {"probe_timeout": 60, "read_timeout": 900, "write_timeout": 1800}
    settings_schema = [
        field("device", "Тип микросхемы (minipro -L)", required=True, example="AT24C02"),
        field("target_class", "Класс цели", example="memory.i2c_eeprom",
              help="Обычно определяется по имени сам."),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.target_class = str(self.settings.get("target_class") or guess_class(self.device))

    @property
    def device(self) -> str:
        return str(self.settings.get("device") or "")

    def _cmd(self, *rest) -> list:
        return guard("minipro", ["minipro", "-p", self.device] + extra_args(self.settings)
                     + [str(r) for r in rest], FORBIDDEN)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if not shutil.which("minipro"):
            return False, "minipro не установлен (см. docs/INTEGRATIONS.md, раздел minipro)"
        if not self.device:
            return False, "укажите device в config.yaml → plugins: minipro (имя из `minipro -L …`)"
        if MCU_RE.search(self.device):
            return False, (f"«{self.device}» — микроконтроллер: через minipro не обслуживается "
                           f"(для AVR используйте плагин avrdude)")
        return True, f"тип из настроек: {self.device} → {self.target_class}"

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: `minipro --version` печатает «minipro version 0.7.2 …».
        return tool_version(self, ["minipro", "--version"], r"minipro version\s+v?(\d+\.\d+[\w.+-]*)")

    def probe(self):
        res = self.run(self._cmd("-D"), timeout=int(self.settings.get("probe_timeout", 60)))
        text = res.out + "\n" + res.err
        if NO_PROGRAMMER_RE.search(text):
            raise RuntimeError("программатор T48/TL866 не найден: проверьте USB и права (udev).\n"
                               + fail_text(res, ""))
        m = CHIP_ID_RE.search(text)
        if MISMATCH_RE.search(text) or (not res.ok and m):
            raise RuntimeError(f"ID чипа не совпал с типом «{self.device}»: проверьте тип в настройках, "
                               f"ориентацию и посадку в панельке.\n" + fail_text(res, ""))
        if not res.ok:
            return None
        fp = {"name": self.device.upper()}
        if m:
            chip_id = m.group(1).upper()
            fp["chip_id"] = chip_id
            if self.target_class == "memory.spi_nor" and len(chip_id) == 6:
                fp["jedec_id"] = chip_id
            return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                             confidence=0.9, summary=f"{self.device} в панельке, ID 0x{chip_id}")
        # У части микросхем (например, EPROM 27Cxx) ID нет — тип берём из настроек.
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.6, summary=f"{self.device} (ID не читается — тип из настроек)")

    def read(self, det) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            res = self.run(self._cmd("-r", out), timeout=int(self.settings.get("read_timeout", 900)))
            if not res.ok or not out.exists():
                raise RuntimeError(fail_text(res, "minipro не смог прочитать чип"))
            return out.read_bytes()

    def write(self, det, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd("-w", img), timeout=int(self.settings.get("write_timeout", 1800)))
            if not res.ok:
                raise RuntimeError(fail_text(res, "minipro не смог записать чип"))
