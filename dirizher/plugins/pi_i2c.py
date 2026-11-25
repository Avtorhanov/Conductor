"""I2C EEPROM 24Cxx (24C01…24C512) через I2C самого Raspberry Pi — без покупок.

Шину /dev/i2c-1 (выводы 3 SDA и 5 SCL гребёнки) открывает помощник dirizher/helpers/i2c_eeprom.py —
он запускается ОТДЕЛЬНЫМ ПРОЦЕССОМ через Runner, как любая внешняя программа: таймаут, журнал,
зависание шины не роняет ядро. Только стандартная библиотека (fcntl.ioctl I2C_RDWR).

Что делает лезвие:
  probe   — «чтение текущего адреса» (1 байт) по адресам 0x50…0x57: отвечает ли микросхема.
            Данные не пишутся.
  inspect — только чтение: сколько адресов EEPROM отвечает. Больше, чем занимает выбранная модель, —
            подсказка «проверьте модель» (24C08 занимает 4 адреса, 24C02 — один).
  read    — вся память кусками по 32 байта.
  write   — только отличающиеся СТРАНИЦЫ с ожиданием цикла записи; ядро потом читает обратно и сверяет.

У 24Cxx нет ID: модель (eeprom) — из профиля (tool: pi_i2c: eeprom) или настроек, уверенность 0,7.
Логика Pi — 3,3 В, и микросхему питает вывод 1 (3,3 В): напряжение адаптера — 3,3 В. Микросхему на
1,8 В так подключать нельзя — проверка «Напряжение» остановит сеанс по профилю.

Вывод WP микросхемы при записи должен быть на GND, иначе запись не пройдёт сверку (это безопасно:
ядро предложит вернуть копию «до»).

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: см. docs/INTEGRATIONS.md, раздел «I2C самого Raspberry Pi».
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

from ..core.models import Detection, Finding, Inspection
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import CONFIRM_LINE, as_int

HELPER = Path(__file__).resolve().parent.parent / "helpers" / "i2c_eeprom.py"
ACK_RE = re.compile(r"^ACK=([0-9A-Fa-fx,]*)\s*$", re.MULTILINE)
EEPROM_SIZES = {"24C01": 128, "24C02": 256, "24C04": 512, "24C08": 1024, "24C16": 2048, "24C32": 4096,
                "24C64": 8192, "24C128": 16384, "24C256": 32768, "24C512": 65536}
BLOCKS = {"24C04": 2, "24C08": 4, "24C16": 8}        # сколько адресов на шине занимает модель


def _model(value) -> str:
    m = re.search(r"24(?:C|LC|AA|FC|CS)(\d+)", str(value or "").upper())
    return f"24C{m.group(1)}" if m else str(value or "").upper()


class PiI2CPlugin(Plugin):
    name = "pi_i2c"
    title = "I2C EEPROM 24Cxx через I2C самого Raspberry Pi"
    target_class = "memory.i2c_eeprom"
    hardware = True
    model_setting = "eeprom"                 # у 24Cxx нет ID — модель из профиля (tool: pi_i2c: eeprom)
    preflight = [
        "Pi выключен, пока подключаете провода. Микросхема питается от вывода 1 (3,3 В) — только 24Cxx, "
        "которые работают от 3,3 В; плату-донор не включайте.",
        "Провода: вывод 3 → SDA (5), вывод 5 → SCL (6), вывод 1 → VCC (8), вывод 6 → GND (4); "
        "A0–A2 (1–3) — на GND, тогда адрес 0x50.",
        "Для записи вывод WP (7) — на GND, иначе запись не пройдёт сверку.",
        CONFIRM_LINE,
    ]
    common_settings = ("enabled", "programmer_voltage", "probe_timeout", "read_timeout", "write_timeout")
    settings_defaults = {"programmer_voltage": 3.3, "probe_timeout": 30, "read_timeout": 300,
                         "write_timeout": 600}
    settings_schema = [
        field("bus", "Шина I2C", "port", default="/dev/i2c-1", example="/dev/i2c-1",
              help="У Raspberry Pi — /dev/i2c-1 (выводы 3 и 5). Появляется после raspi-config → I2C."),
        field("eeprom", "Микросхема", required=True, example="24C02", help="24C01 … 24C512"),
        field("address", "Адрес I2C", "int", default=0x50, example=0x50, hex=True),
        field("page_size", "Страница записи, байт", "int", example=8,
              help="Пусто — по модели (наименьшая у распространённых производителей). Больше паспортной "
                   "ставить нельзя: байты завернутся в начало страницы."),
        field("chunk", "Кусок чтения, байт", "int", default=32, example=32),
    ]

    @classmethod
    def image_size(cls, settings: dict):
        return EEPROM_SIZES.get(_model(settings.get("eeprom")))

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, dict(settings or {}))
        self.settings.setdefault("programmer_voltage", 3.3)   # логика и питание от Pi — 3,3 В
        self._ack = None

    @property
    def model(self) -> str:
        return _model(self.settings.get("eeprom"))

    @property
    def address(self) -> int:
        return as_int(self.settings.get("address"), 0x50)

    @property
    def bus(self) -> str:
        return str(self.settings.get("bus", "/dev/i2c-1"))

    def helper_cmd(self) -> list:
        """Как запустить помощника (в тестах подменяется помощником с эмулятором EEPROM)."""
        return [sys.executable, str(HELPER)]

    def _cmd(self, op: str, path=None) -> list:
        cmd = self.helper_cmd() + ["--bus", self.bus, "--eeprom", self.model, "--address", f"0x{self.address:02X}"]
        if self.settings.get("page_size"):
            cmd += ["--page", str(as_int(self.settings["page_size"], 0))]
        if self.settings.get("chunk"):
            cmd += ["--chunk", str(as_int(self.settings["chunk"], 32))]
        cmd.append(op)
        if path is not None:
            cmd.append(str(path))
        return cmd

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if self.model not in EEPROM_SIZES:
            return False, f"укажите eeprom в config.yaml → plugins: pi_i2c ({', '.join(EEPROM_SIZES)})"
        if not Path(self.bus).exists():
            return False, (f"шины {self.bus} нет — включите I2C: sudo raspi-config → Interface Options → I2C, "
                           "затем перезагрузка (карточка rpi_i2c)")
        return True, f"{self.bus}, {self.model} @ 0x{self.address:02X}"

    def probe(self):
        res = self.run(self._cmd("probe"), timeout=int(self.settings.get("probe_timeout", 30)))
        if res.code == 3:
            return None                                       # NACK — микросхема не ответила
        if not res.ok:
            raise RuntimeError(res.tail() or "помощник I2C завершился с ошибкой")
        m = ACK_RE.search(res.out or "")
        self._ack = [int(x, 16) for x in m.group(1).split(",") if x] if m else None
        size = EEPROM_SIZES[self.model]
        fp = {"name": self.model, "size": size, "i2c_address": f"0x{self.address:02X}"}
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp, confidence=0.7,
                         summary=f"I2C EEPROM отвечает по адресу {fp['i2c_address']} ({self.bus}); "
                                 f"модель из настроек: {self.model} ({size} байт)")

    def inspect(self, det):
        """Сколько адресов EEPROM отвечает — сверка с выбранной моделью. Только чтение (из опознания)."""
        if not self._ack:
            return None
        own = set(range(self.address, self.address + BLOCKS.get(self.model, 1)))
        alive = ", ".join(f"0x{a:02X}" for a in self._ack)
        missing = sorted(own - set(self._ack))
        extra = sorted(set(self._ack) - own)
        findings = []
        if missing:
            findings.append(Finding("i2c_blocks", "warn",
                                    f"{self.model} занимает адреса {', '.join(f'0x{a:02X}' for a in sorted(own))}, "
                                    f"а отвечают {alive}. Возможно, модель меньше выбранной — проверьте "
                                    "маркировку, иначе копия будет с повторами."))
        if extra:
            findings.append(Finding("i2c_blocks", "info",
                                    f"на шине отвечают ещё адреса {', '.join(f'0x{a:02X}' for a in extra)}: "
                                    "вторая микросхема или модель больше выбранной (24C04/08/16 занимают "
                                    "2/4/8 адресов). Проверьте маркировку."))
        if not findings:
            findings.append(Finding("i2c_blocks", "ok", f"отвечают адреса {alive} — как у {self.model}"))
        return Inspection(findings=findings)

    def read(self, det) -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            res = self.run(self._cmd("read", out), timeout=int(self.settings.get("read_timeout", 300)))
            if not res.ok or not out.exists():
                raise RuntimeError(res.tail() or "не удалось прочитать EEPROM")
            return out.read_bytes()

    def write(self, det, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd("write", img), timeout=int(self.settings.get("write_timeout", 600)))
            if not res.ok:
                raise RuntimeError(res.tail() or "не удалось записать EEPROM")
