"""Общие структуры данных: результат опознания и результат проверки."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def chip_key(target_class: str, fingerprint: dict) -> str:
    """Ключ «такой же чип». По нему находятся эталоны с других плат на таком же чипе."""
    ident = fingerprint.get("jedec_id") or fingerprint.get("name") or "unknown"
    return f"{target_class}:{str(ident).upper()}"


@dataclass
class Detection:
    """Что плагин увидел на щупах. Получается только чтением идентификаторов."""

    plugin: str                  # имя плагина, который узнал устройство
    target_class: str            # класс цели, например "memory.spi_nor"
    fingerprint: dict            # «отпечаток»: jedec_id, имя, размер...
    confidence: float            # уверенность плагина, 0..1
    summary: str                 # понятное человеку описание
    profile: Any = None          # подобранный профиль (Profile)
    profile_score: float = 0.0   # насколько точно подошёл профиль, 0..1

    @property
    def score(self) -> float:
        """Итоговая уверенность: плагин × точность профиля."""
        return round(self.confidence * (self.profile_score if self.profile else 0.1), 3)

    def chip_key(self) -> str:
        return chip_key(self.target_class, self.fingerprint)


# Статусы находок по возрастанию серьёзности.
# block — запись запрещена, пока проблема не устранена.
STATUS_ORDER = {"ok": 0, "info": 1, "warn": 2, "fail": 3, "block": 4}


@dataclass
class Finding:
    """Результат одной проверки диагностики."""

    check: str
    status: str                                   # ok | info | warn | fail | block
    message: str
    suggest: list = field(default_factory=list)   # id рецептов, которые могут помочь
