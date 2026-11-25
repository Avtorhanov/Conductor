"""Плагин SPI-памяти через flashrom (CH341A, Bus Pirate, Raspberry Pi SPI и др.).

flashrom — внешняя программа (лицензия GPL), поэтому она вызывается отдельным
процессом, а не встраивается в код.

ВНИМАНИЕ: формат вывода flashrom может немного отличаться между версиями.
На первом запуске с настоящим чипом выполните `python3 -m dirizher detect` и посмотрите
журнал сеанса (data/logs/): если JEDEC ID не распознался, поправьте регулярные
выражения ниже по реальному выводу `flashrom -p ch341a_spi -V`.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection
from ..core.plugin import Plugin
from ._common import tool_version

FOUND_RE = re.compile(r'Found (.+?) flash chip "(.+?)" \((\d+) kB, (\w+)\)')
RDID_RE = re.compile(r"rdid\w*:\s*id1 0x([0-9a-fA-F]+),\s*id2 0x([0-9a-fA-F]+)", re.IGNORECASE)
NOT_FOUND = "No EEPROM/flash device found"


class FlashromSPIPlugin(Plugin):
    name = "flashrom_spi"
    title = "SPI-память через flashrom"
    target_class = "memory.spi_nor"
    hardware = True

    @property
    def programmer(self) -> str:
        return str(self.settings.get("programmer", "ch341a_spi"))

    def _base(self, chip: str | None = None) -> list:
        cmd = ["flashrom", "-p", self.programmer]
        if chip:
            cmd += ["-c", chip]
        return cmd

    def available(self) -> tuple:
        if not shutil.which("flashrom"):
            return False, "flashrom не установлен (sudo apt install flashrom)"
        return True, f"программатор: {self.programmer}"

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: `flashrom --version` печатает «flashrom v1.3.0 on Linux …» (или без «v»).
        return tool_version(self, ["flashrom", "--version"], r"flashrom\s+v?(\d+\.\d+[\w.+-]*)")

    def probe(self):
        # Без операции flashrom только опознаёт чип — ничего не читает и не пишет.
        res = self.run(self._base() + ["-V"], timeout=int(self.settings.get("probe_timeout", 120)))
        text = res.out + "\n" + res.err
        if NOT_FOUND in text:
            return None
        found = FOUND_RE.findall(text)
        if not found:
            if not res.ok:
                raise RuntimeError(res.tail() or f"flashrom завершился с кодом {res.code}")
            return None
        vendor, chip_name, kb, bus = found[0]
        names = sorted({f[1] for f in found})
        jedec = None
        m = RDID_RE.search(text)
        if m:
            jedec = f"{int(m.group(1), 16):02X}{int(m.group(2), 16):04X}"
        size = int(kb) * 1024
        fingerprint = {"name": names[0], "vendor": vendor, "size": size, "bus": bus,
                       "flashrom_chip": names[0]}
        if jedec:
            fingerprint["jedec_id"] = jedec
        summary = f"{vendor} {names[0]}, {size // 1024} КБ ({bus})"
        confidence = 0.95
        if len(names) > 1:
            # Несколько определений с одинаковым ID: для чтения/записи берётся первое.
            summary += f" — подходят определения: {', '.join(names)}; выбрано первое"
            confidence = 0.85
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fingerprint,
                         confidence=confidence, summary=summary)

    def read(self, det) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            res = self.run(self._base(det.fingerprint.get("flashrom_chip")) + ["-r", str(out)],
                           timeout=int(self.settings.get("read_timeout", 900)))
            if not res.ok or not out.exists():
                raise RuntimeError(res.tail() or "flashrom не смог прочитать чип")
            return out.read_bytes()

    def write(self, det, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            # flashrom сам сверяет после записи; ядро потом сверит ещё раз своим чтением.
            res = self.run(self._base(det.fingerprint.get("flashrom_chip")) + ["-w", str(img)],
                           timeout=int(self.settings.get("write_timeout", 1800)))
            if not res.ok:
                raise RuntimeError(res.tail() or "flashrom не смог записать чип")
