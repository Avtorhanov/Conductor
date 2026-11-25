"""Интерфейс плагина («лезвия»). Все плагины говорят на одном языке.

Чтобы добавить новый инструмент, скопируйте dirizher/plugins/_template.py,
переименуйте файл (без «_» в начале) и реализуйте четыре метода:

    available()  — установлен ли инструмент / подключено ли железо
    probe()      — опознать, что подключено. ТОЛЬКО ЧТЕНИЕ идентификаторов!
    read(det)    — прочитать содержимое целиком (bytes)
    write(det, data) — записать содержимое целиком

Сверку после записи и резервные копии делает ядро — плагину об этом думать не нужно.

Необязательно: version() — версия внешней программы (строка или None). Ядро зовёт её один раз
за сеанс после опознания и пишет в журнал; в отпечаток (fingerprint) версия не входит.

Настройки лезвия описывает `settings_schema` (см. core/settings.py): по ней строится форма
«Прибор → Инструменты → Настроить», проверяются опечатки и обязательные поля. Общие поля (enabled,
programmer_voltage, extra_args, таймауты) описаны один раз — выберите нужные в `common_settings`,
свои значения по умолчанию — в `settings_defaults`.

Модель цели из профиля (0.7.0): у профиля чипа может быть поле `tool: {<лезвие>: {ключ: значение}}` —
настройки лезвия для этой модели (core/target.py). `model_setting` — ключ, которым лезвие называет модель
(chip, part, device…): если он задан, модель по проводам не узнаётся и Штурман спрашивает её списком
профилей. `size_setting` — ключ размера, который берётся из `size` профиля, если в `tool:` его нет.
`image_size(settings)` — размер образа при таких настройках (для сверки с `size` профиля).
`preflight` — строки шага «Подготовка» для этого лезвия (по умолчанию — общий текст ядра).
"""
from __future__ import annotations

from . import settings as settings_mod


class Plugin:
    name = "base"                    # уникальное имя (как в config.yaml → plugins)
    title = "Базовый плагин"         # понятное человеку название
    target_class = "unknown"         # класс целей, например "memory.spi_nor"
    hardware = True                  # False — виртуальный (симулятор)
    # Проверка «напряжение чипа = напряжение адаптера». False — для приборов, которые питаются
    # сами и связаны развязанной шиной (RS-485, Ethernet): тогда ядро покажет voltage_note.
    voltage_check = True
    voltage_note = ""
    # Схема настроек: свои поля (settings.field(...)), какие общие нужны и их значения по умолчанию.
    settings_schema: list = []
    common_settings: tuple = settings_mod.COMMON_KEYS
    settings_defaults: dict = {}
    # Модель цели из профиля (core/target.py) и подготовка именно для этого инструмента.
    model_setting: str | None = None
    size_setting: str | None = None
    preflight: list | None = None

    def __init__(self, conductor, settings: dict | None = None):
        self.c = conductor
        self.settings = settings or {}

    @classmethod
    def schema(cls) -> list:
        """Все поля настроек: общие (выбранные) + свои. Для лезвий без проверки напряжения
        programmer_voltage не показывается."""
        common = tuple(k for k in cls.common_settings if cls.voltage_check or k != "programmer_voltage")
        return settings_mod.build_schema(cls.settings_schema, common, cls.settings_defaults)

    @classmethod
    def has_schema(cls) -> bool:
        """Описало ли лезвие свои настройки (settings_schema). Нет — неизвестные ключи не проверяются."""
        return any("settings_schema" in vars(k) for k in cls.__mro__
                   if isinstance(k, type) and issubclass(k, Plugin) and k is not Plugin)

    @classmethod
    def image_size(cls, settings: dict):
        """Размер образа (байт) при таких настройках или None (не знаю). Только по настройкам."""
        if cls.size_setting and settings.get(cls.size_setting) is not None:
            try:
                return settings_mod.coerce({"key": cls.size_setting, "title": cls.size_setting, "type": "int"},
                                           settings[cls.size_setting])
            except ValueError:
                return None
        return None

    def available(self) -> tuple:
        return False, "не реализовано"

    def probe(self):
        """Вернуть Detection или None. Ничего не записывать!"""
        return None

    def read(self, det) -> bytes:
        raise NotImplementedError

    def write(self, det, data: bytes) -> None:
        raise NotImplementedError

    def version(self):
        """Версия программы-движка строкой или None (не знаю). Только чтение, без железа."""
        return None

    # удобный доступ к запуску внешних программ с журналом и таймаутом.
    # keep_output=False — в выводе данные чипа: полный вывод не сохранять в журнал.
    def run(self, cmd: list, timeout: int = 120, keep_output: bool = True):
        if keep_output:
            return self.c.runner.run(cmd, timeout)
        return self.c.runner.run(cmd, timeout, keep_output=False)
