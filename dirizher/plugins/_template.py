"""ШАБЛОН нового плагина («лезвия»). Этот файл не загружается (имя начинается с «_»).

Как сделать новый плагин:
  1. Скопируйте файл:  cp _template.py my_tool.py   (без «_» в начале!)
  2. Поменяйте name, title, target_class.
  3. Реализуйте available / probe / read / write.
  4. Опишите настройки в settings_schema (по ним строится форма «Прибор → Инструменты → Настроить»
     и ловятся опечатки) и включите плагин формой или в config.yaml:
         plugins:
           my_tool:
             enabled: true
  5. Создайте профиль(и) для нового класса целей в profiles/.
  6. Проверьте:  python3 -m dirizher plugins   и   python3 -m dirizher detect

Пример: следующим «лезвием» удобно сделать микроконтроллеры через probe-rs
(target_class = "mcu.arm"): probe — `probe-rs info` (только чтение IDCODE),
read — `probe-rs read` / выгрузка флеша, write — `probe-rs download`.
Сверьте точные команды с `probe-rs --help` своей версии.

Правила (см. PRINCIPLES.md):
  * probe() — ТОЛЬКО чтение идентификаторов. Никаких записей и стираний.
  * Внешние программы — через self.run(...): таймаут и журнал включены.
  * Ошибки — исключением с понятным текстом: ядро покажет его человеку.
  * Резервные копии, сверку и подтверждение делает ядро — не дублируйте.
"""
from __future__ import annotations

import shutil

from ..core.models import Detection
from ..core.plugin import Plugin
from ..core.settings import field


class TemplatePlugin(Plugin):
    name = "my_tool"                     # как в config.yaml
    title = "Мой инструмент"
    target_class = "memory.example"      # класс целей; профили с таким class будут подходить
    hardware = True
    # Общие поля (enabled, programmer_voltage, extra_args, таймауты) уже есть; здесь — свои.
    settings_schema = [
        field("port", "Порт", "port", example="/dev/ttyUSB0", help="Куда подключён адаптер."),
    ]

    def available(self) -> tuple:
        if not shutil.which("my-tool"):
            return False, "my-tool не установлен"
        return True, "готов"

    def probe(self):
        res = self.run(["my-tool", "--identify"], timeout=60)
        if not res.ok:
            return None
        ident = res.out.strip()                      # разберите вывод своего инструмента
        return Detection(plugin=self.name, target_class=self.target_class,
                         fingerprint={"name": ident}, confidence=0.9,
                         summary=f"найдено: {ident}")

    def read(self, det) -> bytes:
        raise NotImplementedError("прочитайте содержимое и верните bytes")

    def write(self, det, data: bytes) -> None:
        raise NotImplementedError("запишите data целиком; при ошибке — исключение")
