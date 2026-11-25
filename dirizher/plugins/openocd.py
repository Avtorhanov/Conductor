"""Микроконтроллеры через OpenOCD (ST-Link, CMSIS-DAP, J-Link, FT2232, GPIO Raspberry Pi…).

OpenOCD (лицензия GPL-2.0) вызывается отдельным процессом с файлами конфигурации
интерфейса и цели, например interface/stlink.cfg + target/stm32f1x.cfg.

Что делает плагин:
  probe — `-c init -c shutdown`: подключение и чтение IDCODE/DPIDR, напряжение цели. Только чтение.
  read  — `-c init -c "reset halt" -c "dump_image файл адрес размер" -c shutdown`.
  write — `-c "program файл verify адрес" -c shutdown` (стирает только нужные сектора).

Модель МК и размер флеша задаёт человек (mcu, flash_size); уверенность опознания 0.75.

Запрещено (guard): команды со словами unlock, lock, mass_erase, protect, option(s)_write/load,
erase_address — снятие/установка защиты и стирание всего чипа.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строки «DPIDR 0x…», «tap/device found: 0x…», «processor detected»,
«Target voltage: …» в вашей версии OpenOCD; нужен ли «reset halt» для вашей платы (halt_cmd).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import PREFLIGHT_SWD, as_int, fail_text, guard, tool_version

FORBIDDEN_WORDS = ["unlock", "lock", "mass_erase", "protect", "option_write", "options_write",
                   "option_load", "options_load", "erase_address"]
IDCODE_RE = re.compile(r"(?:DPIDR|tap/device found:)\s*0x([0-9a-fA-F]{8})", re.IGNORECASE)
CORE_RE = re.compile(r"(Cortex-M\d+\+?|Cortex-A\d+|RISC-V)[^\n]*processor detected", re.IGNORECASE)
VOLTAGE_RE = re.compile(r"Target voltage:\s*([\d.]+)", re.IGNORECASE)
NO_ADAPTER = re.compile(r"open failed|unable to open|no device found|couldn't open", re.IGNORECASE)


class OpenOCDPlugin(Plugin):
    name = "openocd"
    title = "Микроконтроллеры через OpenOCD (SWD/JTAG)"
    target_class = "mcu.arm"
    hardware = True
    model_setting = "mcu"                # модель — из профиля (tool: openocd: mcu, target) или настроек
    size_setting = "flash_size"
    preflight = PREFLIGHT_SWD
    settings_schema = [
        field("interface", "Файл интерфейса OpenOCD", "path", required=True, example="interface/stlink.cfg"),
        field("target", "Файл цели OpenOCD", "path", required=True, example="target/stm32f1x.cfg"),
        field("mcu", "Модель МК", required=True, example="STM32F103C8", help="По этому имени выбирается профиль."),
        field("flash_base", "Начало flash", "int", default=0x08000000, example=0x08000000, hex=True),
        field("flash_size", "Размер flash, байт", "int", required=True, example=65536),
        field("pre_init", "Команды до init", "list", example=["adapter speed 1000"],
              help="Через запятую или пробел; каждая — команда OpenOCD."),
        field("halt_cmd", "Команда остановки", default="reset halt", example="halt"),
        field("target_class", "Класс цели", default="mcu.arm", example="mcu.arm"),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.target_class = str(self.settings.get("target_class", self.target_class))

    @property
    def mcu(self) -> str:
        return str(self.settings.get("mcu") or "")

    @property
    def flash_base(self) -> int:
        return as_int(self.settings.get("flash_base"), 0x08000000)

    @property
    def flash_size(self) -> int:
        return as_int(self.settings.get("flash_size"), 0)

    def _cmd(self, *commands: str) -> list:
        cmd = ["openocd"]
        for f in (self.settings.get("interface"), self.settings.get("target")):
            cmd += ["-f", str(f)]
        pre = self.settings.get("pre_init") or []          # например: ["adapter speed 1000"]
        for c in ([pre] if isinstance(pre, str) else pre) + list(commands):
            cmd += ["-c", str(c)]
        return guard("openocd", cmd, forbidden_words=FORBIDDEN_WORDS)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if not shutil.which("openocd"):
            return False, "OpenOCD не установлен (sudo apt install openocd)"
        missing = [k for k in ("interface", "target", "mcu", "flash_size") if not self.settings.get(k)]
        if missing:
            return False, "укажите в config.yaml → plugins: openocd: " + ", ".join(missing)
        return True, f"{self.settings['interface']} + {self.settings['target']}, МК из настроек: {self.mcu}"

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: `openocd --version` печатает в stderr «Open On-Chip Debugger 0.12.0 …».
        return tool_version(self, ["openocd", "--version"], r"Open On-Chip Debugger\s+v?(\d+\.\d+[\w.+-]*)")

    def probe(self):
        res = self.run(self._cmd("init", "shutdown"), timeout=int(self.settings.get("probe_timeout", 60)))
        text = res.out + "\n" + res.err
        idcode = IDCODE_RE.search(text)
        core = CORE_RE.search(text)
        if not idcode and not core:
            if NO_ADAPTER.search(text):
                raise RuntimeError("OpenOCD не нашёл адаптер: проверьте USB и interface в config.yaml.\n"
                                   + fail_text(res, ""))
            if not res.ok:
                raise RuntimeError("адаптер есть, но микроконтроллер не отвечает: проверьте SWDIO/SWCLK/GND, "
                                   "питание платы и NRST.\n" + fail_text(res, ""))
            return None
        fp = {"name": self.mcu.upper(), "size": self.flash_size, "flash_base": f"0x{self.flash_base:08X}"}
        if idcode:
            fp["idcode"] = idcode.group(1).upper()
        if core:
            fp["core"] = core.group(1)
        summary = f"{self.mcu} (модель из настроек), {fp.get('core') or 'IDCODE ' + fp['idcode']}"
        volt = VOLTAGE_RE.search(text)
        if volt:
            summary += f", напряжение цели {float(volt.group(1)):.2f} В"
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.75, summary=summary)

    def read(self, det) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            halt = str(self.settings.get("halt_cmd", "reset halt"))
            res = self.run(self._cmd("init", halt,
                                     f"dump_image {{{out}}} 0x{self.flash_base:08X} {self.flash_size}",
                                     "shutdown"),
                           timeout=int(self.settings.get("read_timeout", 600)))
            if not res.ok or not out.exists():
                raise RuntimeError(fail_text(res, "OpenOCD не смог прочитать флеш"))
            data = out.read_bytes()
        if len(data) != self.flash_size:
            raise RuntimeError(f"OpenOCD вернул {len(data)} байт вместо {self.flash_size}")
        return data

    def write(self, det, data: bytes) -> None:
        if len(data) != self.flash_size:
            raise RuntimeError(f"размер образа {len(data)} не равен flash_size {self.flash_size}")
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd(f"program {{{img}}} verify 0x{self.flash_base:08X}", "shutdown"),
                           timeout=int(self.settings.get("write_timeout", 900)))
            if not res.ok:
                raise RuntimeError(fail_text(res, "OpenOCD не смог записать флеш"))
