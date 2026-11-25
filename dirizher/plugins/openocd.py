"""Микроконтроллеры через OpenOCD (ST-Link, CMSIS-DAP, J-Link, FT2232, GPIO Raspberry Pi…).

OpenOCD (лицензия GPL-2.0) вызывается отдельным процессом с файлами конфигурации
интерфейса и цели, например interface/stlink.cfg + target/stm32f1x.cfg.

Что делает плагин:
  probe — `-c init -c shutdown`: подключение и чтение IDCODE/DPIDR, напряжение цели. Только чтение.
  read  — `-c init -c "reset halt" -c "dump_image файл адрес размер" -c shutdown`.
  write — `-c "program файл verify адрес" -c shutdown` (стирает только нужные сектора).

Модель МК и размер флеша задаёт человек (mcu, flash_size); уверенность опознания 0.75.

inspect (0.7.0, только чтение):
  * замеренное напряжение цели («Target voltage: …» — его печатают ST-Link и часть других адаптеров) —
    ядро сверяет его с профилем и адаптером и останавливает сеанс при расхождении. Если адаптер-клон
    показывает неверное число, замер можно выключить (measure_voltage: false — только в настройках
    переходника, не в профиле); прежняя проверка напряжения остаётся;
  * состояние защиты чтения, если для цели есть команда только чтения (rdp_cmd, например
    `stm32f1x options_read 0` у STM32F1 — задаётся в профиле, tool: openocd: rdp_cmd).

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

from ..core.models import Detection, Finding, Inspection
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import PREFLIGHT_SWD, PROTECTION_HINT, as_int, fail_text, guard, tool_version

FORBIDDEN_WORDS = ["unlock", "lock", "mass_erase", "protect", "option_write", "options_write",
                   "option_load", "options_load", "erase_address"]
IDCODE_RE = re.compile(r"(?:DPIDR|tap/device found:)\s*0x([0-9a-fA-F]{8})", re.IGNORECASE)
CORE_RE = re.compile(r"(Cortex-M\d+\+?|Cortex-A\d+|RISC-V)[^\n]*processor detected", re.IGNORECASE)
VOLTAGE_RE = re.compile(r"Target voltage:\s*([\d.]+)", re.IGNORECASE)
# ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: OpenOCD 0.12 у stm32f1x печатает «read protection: on|off»; у других целей иначе.
RDP_RE = re.compile(r"read(?:out)?[ _-]*protection\s*(?:level)?\s*[:=]?\s*(on|off|enabled|disabled|active|"
                    r"inactive|level\s*[0-2]|[0-2]\b)", re.IGNORECASE)
READ_ONLY_CMD_RE = re.compile(r"^\s*(\S+\s+\w*_read\b|flash\s+info\b|mdw\b)", re.IGNORECASE)
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
        field("rdp_cmd", "Команда чтения защиты", example="stm32f1x options_read 0",
              help="Только чтение (…_read, flash info, mdw). Обычно приходит из профиля модели."),
        field("measure_voltage", "Сверять замер напряжения", "bool", default=True, safety="voltage",
              help="Адаптер мерит напряжение цели (Target voltage) — ядро сверяет его с профилем. "
                   "Выключайте, только если клон показывает заведомо неверное число."),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.target_class = str(self.settings.get("target_class", self.target_class))
        self._voltage = None

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
        self._voltage = float(volt.group(1)) if volt else None        # замер — не в отпечаток
        if volt:
            summary += f", напряжение цели {float(volt.group(1)):.2f} В"
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.75, summary=summary)

    def inspect(self, det):
        """Замер напряжения из опознания и (если задана rdp_cmd) состояние защиты чтения. Только чтение."""
        measure = self.settings.get("measure_voltage", True)
        if isinstance(measure, str):
            measure = measure.strip().lower() not in ("false", "no", "0", "нет")
        findings = []
        if self._voltage is not None and not measure:
            findings.append(Finding("target_voltage", "info",
                                    f"адаптер показал {self._voltage:.2f} В, но сверка замера выключена "
                                    f"(measure_voltage: false) — напряжение сверено только по настройкам"))
        cmd = str(self.settings.get("rdp_cmd") or "").strip()
        if cmd:
            findings.append(self._read_protection(cmd))
        return Inspection(findings=findings, measured_voltage=self._voltage if measure else None)

    def _read_protection(self, cmd: str):
        if not READ_ONLY_CMD_RE.search(cmd):
            return Finding("read_protection", "warn", f"команда «{cmd}» не похожа на чтение — не запускаю "
                                                      f"(допустимы …_read, flash info, mdw)")
        try:
            res = self.run(self._cmd("init", cmd, "shutdown"), timeout=int(self.settings.get("probe_timeout", 60)))
        except RuntimeError as e:                                     # guard: запрещённое слово
            return Finding("read_protection", "warn", str(e))
        m = RDP_RE.search(res.out + "\n" + res.err)
        if not m:
            return Finding("read_protection", "info", "состояние защиты чтения не разобрано (ПРОВЕРИТЬ НА ЖЕЛЕЗЕ)")
        state = re.sub(r"\s+", " ", m.group(1).lower())
        if state in ("off", "disabled", "inactive", "0", "level 0"):
            return Finding("read_protection", "ok", "защита чтения выключена")
        return Finding("read_protection", "block", f"включена защита чтения ({state}): копия «до» будет "
                                                   f"недостоверной, запись запрещена. " + PROTECTION_HINT)

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
