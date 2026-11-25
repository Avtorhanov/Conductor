"""Навык «Modbus RTU»: приборы с настройками в регистрах — термопреобразователи, регуляторы,
счётчики, частотники — через USB–RS-485 переходник.

Лезвие видит прибор как «память» из регистров настроек: окно [start, start+count) читается целиком
(2 байта на регистр, старший байт первым). Поэтому работают все механизмы Дирижёра:
  * «Настройка» — именованные параметры по карте профиля (core/params.py);
  * копия «до» / «после», сверка, откат, эталон настроек и ремонт по эталону.
Запись — только изменившихся регистров (функция 6, или 16 при write_function: 16).

Порт обслуживает помощник rtu.py ОТДЕЛЬНЫМ ПРОЦЕССОМ (как flashrom или bp5_i2c).
У Modbus нет надёжного ID модели — модель задаёт человек (settings: model), уверенность 0.7.
Прибор питается сам, шина развязана — проверку напряжения логики заменяет напоминание о подключении.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: см. help.md рядом (эхо переходника, функция 16, паузы на вашей скорости).
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from dirizher.core.models import Detection
from dirizher.core.plugin import Plugin
from dirizher.core.settings import field

HELPER = Path(__file__).with_name("rtu.py")
EXIT_TEXT = {3: "прибор не ответил", 4: "прибор ответил исключением Modbus"}


class ModbusRTUPlugin(Plugin):
    name = "modbus_rtu"
    title = "Приборы по Modbus RTU (RS-485)"
    target_class = "device.modbus"
    hardware = True
    voltage_check = False
    model_setting = "model"                  # у Modbus нет ID модели — из профиля (tool:) или настроек
    preflight = [
        "Прибор питается сам; переходник USB–RS-485: A → A, B → B, общий провод — если есть.",
        "Адрес и скорость прибора известны (паспорт или меню прибора) и совпадают с настройками.",
        "Запись будет только после копии текущих настроек и вашего подтверждения.",
    ]
    common_settings = ("enabled", "probe_timeout", "read_timeout", "write_timeout")
    settings_defaults = {"probe_timeout": 20, "read_timeout": 60, "write_timeout": 60}
    settings_schema = [
        field("port", "Порт переходника USB–RS-485", "port", default="/dev/ttyUSB0", example="/dev/ttyUSB0"),
        field("baud", "Скорость, бод", "int", default=9600, example=9600, help="Как в меню прибора."),
        field("parity", "Чётность", "choice", default="N", choices=["N", "E", "O"]),
        field("stopbits", "Стоп-биты", "choice", default=1, choices=[1, 2]),
        field("unit", "Сетевой адрес прибора", "int", default=1, example=1, help="1…247"),
        field("model", "Модель", example="THERMO-EXAMPLE", help="По ней выбирается карта параметров."),
        field("timeout", "Ожидание ответа, с", "float", default=1.0),
        field("retries", "Повторы", "int", default=2),
        field("write_function", "Функция записи", "choice", default=6, choices=[6, 16],
              help="6 — по одному регистру (понимают все); 16 — пачкой."),
        field("echo", "Переходник возвращает эхо", "bool", default=False),
        field("start", "Первый регистр окна", "int", default=0, help="Если в профиле нет bus."),
        field("count", "Регистров в окне", "int", default=16),
        field("function", "Функция чтения", "choice", default=3, choices=[3, 4]),
        field("probe_register", "Регистр для опознания", "int", help="Пусто — первый регистр окна."),
    ]
    voltage_note = ("прибор питается сам, связь по RS-485 — напряжение логики не сверяется. "
                    "Проверьте: A → A, B → B, общий GND; не путайте RS-485 с питанием 24 В")

    # ---------- настройки ----------
    @property
    def port(self) -> str:
        return str(self.settings.get("port") or "/dev/ttyUSB0")

    @property
    def unit(self) -> int:
        return int(self.settings.get("unit", 1))

    @property
    def model(self) -> str:
        return str(self.settings.get("model") or "MODBUS").strip().upper()

    def window(self, det=None) -> tuple:
        """Окно регистров: из профиля (bus), иначе из настроек."""
        bus = dict(self.settings)
        if det is not None and det.profile is not None:
            bus.update(det.profile.bus or {})
        start, count = int(str(bus.get("start", 0)), 0), int(str(bus.get("count", 16)), 0)
        function = int(bus.get("function", 3))
        if not (0 <= start <= 0xFFFF and 1 <= count <= 2000 and function in (3, 4)):
            raise RuntimeError("окно регистров: start 0…65535, count 1…2000, function 3 или 4")
        return start, count, function

    def _cmd(self, *args, function: int = 3) -> list:
        cmd = [sys.executable, str(HELPER), "--port", self.port, "--baud", str(int(self.settings.get("baud", 9600))),
               "--parity", str(self.settings.get("parity", "N")), "--stopbits", str(int(self.settings.get("stopbits", 1))),
               "--unit", str(self.unit), "--timeout", str(float(self.settings.get("timeout", 1.0))),
               "--retries", str(int(self.settings.get("retries", 2))), "--function", str(function)]
        if self.settings.get("echo"):
            cmd.append("--echo")
        if int(self.settings.get("write_function", 6)) == 16:
            cmd.append("--multi")
        return cmd + [str(a) for a in args]

    def _fail(self, res, what: str) -> RuntimeError:
        return RuntimeError(f"{what}: {res.tail() or EXIT_TEXT.get(res.code, f'код {res.code}')}")

    # ---------- лезвие ----------
    def available(self) -> tuple:
        if not 1 <= self.unit <= 247:
            return False, "unit (адрес прибора) — 1…247"
        if not Path(self.port).exists():
            return False, f"переходник не найден на {self.port} (кабель, группа dialout)"
        return True, f"{self.port}, {self.settings.get('baud', 9600)} бод, адрес {self.unit}, модель {self.model}"

    def probe(self):
        start, _, function = self.window()
        reg = int(str(self.settings.get("probe_register", start)), 0)
        res = self.run(self._cmd("probe", reg, function=function), timeout=int(self.settings.get("probe_timeout", 20)))
        if res.code == 3:
            return None
        if res.code not in (0, 4):
            raise self._fail(res, "опознание")
        note = "" if res.ok else " (на пробный регистр ответил исключением — адрес регистра из настроек?)"
        return Detection(plugin=self.name, target_class=self.target_class,
                         fingerprint={"name": self.model, "unit": self.unit, "bus": "rs485"},
                         confidence=0.7 if res.ok else 0.6,
                         summary=f"прибор Modbus отвечает: адрес {self.unit} на {self.port}; "
                                 f"модель из настроек: {self.model}{note}")

    def read(self, det) -> bytes:
        start, count, function = self.window(det)
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "regs.bin"
            res = self.run(self._cmd("read", start, count, out, function=function),
                           timeout=int(self.settings.get("read_timeout", 60)))
            if not res.ok or not out.exists():
                raise self._fail(res, "чтение регистров")
            data = out.read_bytes()
        if len(data) != count * 2:
            raise RuntimeError(f"прочитано {len(data)} байт вместо {count * 2}")
        return data

    def write(self, det, data: bytes) -> None:
        start, count, function = self.window(det)
        if function == 4:
            raise RuntimeError("окно — входные регистры (функция 4): их нельзя записать")
        if len(data) != count * 2:
            raise RuntimeError(f"размер образа {len(data)} байт, а окно — {count * 2}")
        current = self.read(det)
        pairs = [f"{start + i // 2}={int.from_bytes(data[i:i + 2], 'big')}"
                 for i in range(0, len(data), 2) if data[i:i + 2] != current[i:i + 2]]
        if not pairs:
            return
        res = self.run(self._cmd("write", *pairs, function=function), timeout=int(self.settings.get("write_timeout", 60)))
        if not res.ok:
            raise self._fail(res, "запись регистров")
