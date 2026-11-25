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
"""
from __future__ import annotations


class Plugin:
    name = "base"                    # уникальное имя (как в config.yaml → plugins)
    title = "Базовый плагин"         # понятное человеку название
    target_class = "unknown"         # класс целей, например "memory.spi_nor"
    hardware = True                  # False — виртуальный (симулятор)
    # Проверка «напряжение чипа = напряжение адаптера». False — для приборов, которые питаются
    # сами и связаны развязанной шиной (RS-485, Ethernet): тогда ядро покажет voltage_note.
    voltage_check = True
    voltage_note = ""

    def __init__(self, conductor, settings: dict | None = None):
        self.c = conductor
        self.settings = settings or {}

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
