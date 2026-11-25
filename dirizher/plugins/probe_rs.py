"""Микроконтроллеры ARM Cortex-M / RISC-V через probe-rs (SWD/JTAG): ST-Link, CMSIS-DAP, J-Link…

probe-rs (лицензия MIT/Apache-2.0) вызывается отдельным процессом.

Что делает плагин:
  probe — `probe-rs list` (есть ли отладчик) и `probe-rs info` (отвечает ли ядро). Только чтение.
  read  — `probe-rs read --chip … b8 <адрес> <число байт>` — текстовый дамп флеша.
  write — `probe-rs download --chip … <--binary-format|--format> bin --base-address <адрес> файл`.

Модель МК probe-rs сам по SWD не определяет: её задаёт человек в config.yaml (chip, flash_size).
Поэтому уверенность опознания 0.75 — «модель из настроек, связь с ядром подтверждена».

inspect (0.7.0, только чтение): регистр ID из профиля (id_register: address, mask, expect) —
`probe-rs read … b32 <адрес> 1`; совпало — модель, выбранная человеком, подтверждена; не совпало —
запись запрещена, пока модель не выбрана верно.

Запрещено (guard): --allow-erase-all, --chip-erase, подкоманда erase. Снятие защиты чтения (RDP)
стирает прошивку и вне задач проекта.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: формат вывода `probe-rs info` и `probe-rs read b8` в вашей версии
(разбор терпимый: «12 34», «0x12 0x34», «адрес: 12 34»), имя флага формата у download.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection, Finding
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import PREFLIGHT_SWD, as_int, extra_args, fail_text, guard, parse_hex_bytes, tool_version

FORBIDDEN = ["--allow-erase-all", "--chip-erase", "erase"]
PROBE_LINE_RE = re.compile(r"^\s*\[\d+\]:\s*(.+)$", re.MULTILINE)
TARGET_RE = re.compile(r"Debug Port|DPIDR|IDCODE|ARM Chip|RISC-V|Xtensa|Cortex", re.IGNORECASE)
CORE_RE = re.compile(r"(Cortex-M\d+\+?|Cortex-A\d+|RISC-V|Xtensa)", re.IGNORECASE)
IDCODE_RE = re.compile(r"(?:DPIDR|IDCODE)\s*[:=]?\s*(?:0x)?([0-9a-fA-F]{8})", re.IGNORECASE)
WORD_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{1,8})$")


def parse_word(text: str):
    """Первое 32-битное слово из вывода `probe-rs read b32 …`: «0xe0042000: 20036410» или «0x20036410».
    ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: точный формат вашей версии."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if ":" in line:
            head, _, line = line.partition(":")
            if not re.fullmatch(r"(0x)?[0-9a-fA-F]+", head.strip()):
                continue
        toks = line.split()
        if toks and all(WORD_RE.match(t) for t in toks):
            return int(WORD_RE.match(toks[0]).group(1), 16)
    return None


class ProbeRsPlugin(Plugin):
    name = "probe_rs"
    title = "Микроконтроллеры ARM/RISC-V через probe-rs (SWD/JTAG)"
    target_class = "mcu.arm"
    hardware = True
    model_setting = "chip"               # модель — из профиля (tool: probe_rs: chip) или настроек
    size_setting = "flash_size"          # размер — из size профиля
    preflight = PREFLIGHT_SWD
    settings_schema = [
        field("chip", "Чип (имя из probe-rs chip list)", required=True, example="STM32F103C8",
              help="Модель МК по SWD надёжно не узнать — укажите её сами."),
        field("protocol", "Протокол", "choice", default="swd", choices=["swd", "jtag"]),
        field("flash_base", "Начало flash", "int", default=0x08000000, example=0x08000000, hex=True),
        field("flash_size", "Размер flash, байт", "int", required=True, example=65536),
        field("probe", "Отладчик (VID:PID)", example="0483:3748",
              help="Если подключено несколько отладчиков."),
        field("speed", "Скорость, кГц", "int", example=1000),
        field("target_class", "Класс цели", default="mcu.arm", example="mcu.arm"),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.target_class = str(self.settings.get("target_class", self.target_class))
        self._fmt_flag = None

    # ---------- настройки ----------
    @property
    def chip(self) -> str:
        return str(self.settings.get("chip") or "")

    @property
    def flash_base(self) -> int:
        return as_int(self.settings.get("flash_base"), 0x08000000)

    @property
    def flash_size(self) -> int:
        return as_int(self.settings.get("flash_size"), 0)

    def _conn(self, with_chip: bool = True) -> list:
        args = ["--protocol", str(self.settings.get("protocol", "swd"))]
        if with_chip:
            args = ["--chip", self.chip] + args
        if self.settings.get("probe"):
            args += ["--probe", str(self.settings["probe"])]
        if self.settings.get("speed"):
            args += ["--speed", str(self.settings["speed"])]
        return args + extra_args(self.settings)

    def _cmd(self, sub: str, *rest, with_chip: bool = True) -> list:
        return guard("probe-rs", ["probe-rs", sub] + self._conn(with_chip) + [str(r) for r in rest],
                     FORBIDDEN)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if not shutil.which("probe-rs"):
            return False, "probe-rs не установлен (как поставить — карточка «probe-rs»: python3 -m dirizher catalog probe_rs)"
        if not self.chip or not self.flash_size:
            return False, "укажите chip и flash_size — «Прибор» → «Инструменты» → probe_rs → «Настроить»"
        return True, f"чип из настроек: {self.chip}, флеш {self.flash_size // 1024} КБ @ 0x{self.flash_base:08X}"

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: `probe-rs --version` печатает «probe-rs 0.24.0 (git commit: …)».
        return tool_version(self, ["probe-rs", "--version"], r"probe-rs\s+v?(\d+\.\d+[\w.+-]*)")

    def probe(self):
        listing = self.run(guard("probe-rs", ["probe-rs", "list"], FORBIDDEN), timeout=30)
        probes = PROBE_LINE_RE.findall(listing.out + "\n" + listing.err)
        if not probes:
            return None                                     # отладчик не подключён
        info = self.run(self._cmd("info", with_chip=False),
                        timeout=int(self.settings.get("probe_timeout", 60)))
        text = info.out + "\n" + info.err
        if not TARGET_RE.search(text):
            raise RuntimeError(f"отладчик найден ({probes[0].strip()}), но микроконтроллер не отвечает: "
                               f"проверьте SWDIO/SWCLK/GND, питание платы и NRST.\n" + fail_text(info, ""))
        core = CORE_RE.search(text)
        idcode = IDCODE_RE.search(text)
        fp = {"name": self.chip.upper(), "size": self.flash_size, "flash_base": f"0x{self.flash_base:08X}"}
        if core:
            fp["core"] = core.group(1)
        if idcode:
            fp["idcode"] = idcode.group(1).upper()
        summary = (f"{self.chip} (модель из настроек), {fp.get('core', 'ядро отвечает')}, "
                   f"флеш {self.flash_size // 1024} КБ; отладчик: {probes[0].strip()}")
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.75, summary=summary)

    def inspect(self, det):
        """Прочитать регистр ID из профиля и сверить с ожидаемым. Только чтение."""
        reg = getattr(det.profile, "id_register", None) or {}
        if not reg:
            return None
        res = self.run(self._cmd("read", "b32", f"0x{reg['address']:08X}", 1), timeout=30)
        word = parse_word(res.out) if res.ok else None
        title = reg["title"]
        if word is None:
            return [Finding("id_register", "info", f"{title} не прочитан — модель проверьте по маркировке "
                                                   f"(ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: формат `probe-rs read b32`)")]
        got = word & reg["mask"]
        want = ", ".join(f"0x{e:X}" for e in reg["expect"])
        if got in reg["expect"]:
            return [Finding("id_register", "ok", f"{title} = 0x{got:X} — модель {self.chip} подтверждена")]
        return [Finding("id_register", "block", f"{title} = 0x{got:X}, а у {self.chip} должно быть {want}: "
                                                f"выбрана не та модель (или это клон). Выберите модель по "
                                                f"маркировке — до этого запись запрещена")]

    def read(self, det) -> bytes:
        res = self.run(self._cmd("read", "b8", f"0x{self.flash_base:08X}", self.flash_size),
                       timeout=int(self.settings.get("read_timeout", 600)),
                       keep_output=False)          # в stdout — дамп: в журнал разработки не попадает
        if not res.ok:
            raise RuntimeError(fail_text(res, "probe-rs не смог прочитать флеш"))
        data = parse_hex_bytes(res.out)
        if len(data) != self.flash_size:
            raise RuntimeError(f"разобрано {len(data)} байт из {self.flash_size}: формат вывода `probe-rs read` "
                               f"не распознан (ПРОВЕРИТЬ НА ЖЕЛЕЗЕ, см. журнал сеанса)")
        return data

    def _format_flag(self) -> str:
        """У старых версий download — `--format bin`, у новых — `--binary-format bin`."""
        if self._fmt_flag is None:
            res = self.run(["probe-rs", "download", "--help"], timeout=30)
            text = res.out + res.err
            self._fmt_flag = "--binary-format" if "--binary-format" in text or "--format" not in text else "--format"
        return self._fmt_flag

    def write(self, det, data: bytes) -> None:
        if len(data) != self.flash_size:
            raise RuntimeError(f"размер образа {len(data)} не равен flash_size {self.flash_size}")
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd("download", self._format_flag(), "bin",
                                     "--base-address", f"0x{self.flash_base:08X}", img),
                           timeout=int(self.settings.get("write_timeout", 900)))
            if not res.ok:
                raise RuntimeError(fail_text(res, "probe-rs не смог записать флеш"))
