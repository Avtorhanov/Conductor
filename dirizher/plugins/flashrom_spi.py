"""Плагин SPI-памяти через flashrom (CH341A, Bus Pirate, Raspberry Pi SPI и др.).

flashrom — внешняя программа (лицензия GPL), поэтому она вызывается отдельным
процессом, а не встраивается в код.

ВНИМАНИЕ: формат вывода flashrom может немного отличаться между версиями.
На первом запуске с настоящим чипом выполните `python3 -m dirizher detect` и посмотрите
журнал сеанса (data/logs/): если JEDEC ID не распознался, поправьте регулярные
выражения ниже по реальному выводу `flashrom -p ch341a_spi -V`.

inspect (0.7.0, только чтение): `flashrom … --wp-status` (есть с flashrom 1.3) — состояние защиты записи.
Объясняет «записалось, но не изменилось»: защищённую область flashrom не сотрёт и не перепишет.
Дирижёр защиту не снимает (никаких --wp-disable / --wp-range).
ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строки «Protection range: start=0x… length=0x… (…)» и «Protection mode: …».
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection, Finding
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import tool_version

FOUND_RE = re.compile(r'Found (.+?) flash chip "(.+?)" \((\d+) kB, (\w+)\)')
RDID_RE = re.compile(r"rdid\w*:\s*id1 0x([0-9a-fA-F]+),\s*id2 0x([0-9a-fA-F]+)", re.IGNORECASE)
NOT_FOUND = "No EEPROM/flash device found"
WP_RANGE_RE = re.compile(r"Protection range:\s*start=0x([0-9a-fA-F]+)\s+length=0x([0-9a-fA-F]+)(?:\s*\(([^)]*)\))?",
                         re.IGNORECASE)
WP_MODE_RE = re.compile(r"Protection mode:\s*([\w-]+)", re.IGNORECASE)
WP_MIN_VERSION = (1, 3)
WP_MODES = {"disabled": "выключена", "hardware": "аппаратная (пока вывод WP на земле, её не снять)",
            "power_cycle": "до выключения питания", "permanent": "постоянная (навсегда)"}


class FlashromSPIPlugin(Plugin):
    name = "flashrom_spi"
    title = "SPI-память через flashrom"
    target_class = "memory.spi_nor"
    hardware = True
    common_settings = ("enabled", "programmer_voltage", "probe_timeout", "read_timeout", "write_timeout")
    settings_defaults = {"probe_timeout": 120, "read_timeout": 900, "write_timeout": 1800}
    settings_schema = [
        field("programmer", "Программатор flashrom (-p)", default="ch341a_spi", example="ch341a_spi",
              help="ch341a_spi; buspirate_spi:dev=/dev/ttyACM0; linux_spi:dev=/dev/spidev0.0,spispeed=8000"),
    ]

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
        if not hasattr(self, "_version"):            # один запуск на сеанс: inspect тоже спрашивает версию
            self._version = tool_version(self, ["flashrom", "--version"], r"flashrom\s+v?(\d+\.\d+[\w.+-]*)")
        return self._version

    def inspect(self, det):
        """Состояние защиты записи (--wp-status) — только чтение регистров статуса чипа."""
        ver = self.version()
        nums = tuple(int(x) for x in re.findall(r"\d+", ver or "")[:2])
        if len(nums) < 2 or nums < WP_MIN_VERSION:
            self.c.log(f"flashrom {ver or '?'}: --wp-status есть с 1.3 — защита записи не проверялась")
            return None
        res = self.run(self._base(det.fingerprint.get("flashrom_chip")) + ["--wp-status"],
                       timeout=int(self.settings.get("probe_timeout", 120)))
        text = res.out + "\n" + res.err
        rng, mode = WP_RANGE_RE.search(text), WP_MODE_RE.search(text)
        if not rng and not mode:
            return [Finding("write_protect", "info",
                            "состояние защиты записи flashrom для этого чипа не сообщил (ПРОВЕРИТЬ НА ЖЕЛЕЗЕ)")]
        mode_id = mode.group(1).lower() if mode else ""
        mode_ru = WP_MODES.get(mode_id, mode_id or "не указан")
        start, length = (int(rng.group(1), 16), int(rng.group(2), 16)) if rng else (0, 0)
        if length == 0 and mode_id in ("", "disabled"):
            return [Finding("write_protect", "ok", "защита записи выключена")]
        if length == 0:
            return [Finding("write_protect", "info", f"режим защиты записи: {mode_ru}, но защищённой области нет")]
        size = int(det.fingerprint.get("size") or 0)
        whole = " — весь чип" if size and start == 0 and length >= size else ""
        return [Finding("write_protect", "warn",
                        f"включена защита записи: область 0x{start:X}+0x{length:X}{whole}, режим {mode_ru}. "
                        f"Запись туда не пройдёт — сверка покажет «не совпало», а прежнее содержимое останется. "
                        f"Дирижёр защиту не снимает: если она мешает, разберитесь по даташиту чипа и платы.")]

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
