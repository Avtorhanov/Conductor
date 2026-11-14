"""Общие помощники для плагинов. Имя начинается с «_» — как плагин этот файл не загружается.

Здесь собрано то, что повторяется у разных «лезвий»:
  * запрет опасных параметров (guard) — защита от случайного снятия защиты, стирания всего чипа,
    записи фьюзов и обхода проверок, даже если такой параметр вписан в extra_args;
  * понятный текст ошибки с подсказкой про защиту от чтения (PRINCIPLES.md, п. 12);
  * разбор чисел, размеров и текстовых шестнадцатеричных дампов.
"""
from __future__ import annotations

import re

from ..core.profiles import to_int

PROTECTION_RE = re.compile(
    r"read.?out.?protect|read.?protect|\bRDP\b|security bit|chip is locked|device is locked|"
    r"locked device|protected|flash encryption|secure download|secure boot",
    re.IGNORECASE)

PROTECTION_HINT = ("Похоже, у чипа включена защита от чтения или записи. «Дирижёр» её не снимает: "
                   "снятие защиты обычно стирает прошивку, а обход защит производителя — вне задач "
                   "проекта (PRINCIPLES.md, п. 12).")


def fail_text(res, default: str) -> str:
    """Текст ошибки внешней программы + подсказка, если похоже на защиту чипа."""
    msg = res.tail() or default
    if PROTECTION_RE.search((res.err or "") + "\n" + (res.out or "")):
        msg += "\n" + PROTECTION_HINT
    return msg


def guard(tool: str, cmd: list, forbidden_tokens=(), forbidden_words=()) -> list:
    """Не дать запустить команду с запрещённым параметром.

    forbidden_tokens — точные аргументы («-u», «--allow-erase-all»; также «--opt=значение»);
    forbidden_words — слова внутри аргументов (для команд OpenOCD вида «-c "stm32f1x unlock 0"»).
    """
    for arg in map(str, cmd):
        for bad in forbidden_tokens:
            if arg == bad or arg.startswith(bad + "="):
                raise RuntimeError(f"{tool}: параметр «{arg}» запрещён правилами безопасности «Дирижёра» "
                                   f"(снятие защиты, стирание всего чипа, фьюзы или отключение проверок)")
        for word in forbidden_words:
            if re.search(rf"\b{re.escape(word)}\b", arg, re.IGNORECASE):
                raise RuntimeError(f"{tool}: команда «{arg}» запрещена правилами безопасности «Дирижёра» "
                                   f"(содержит «{word}»)")
    return cmd


def extra_args(settings: dict, key: str = "extra_args") -> list:
    """Дополнительные параметры из config.yaml: строка или список."""
    value = settings.get(key) or []
    if isinstance(value, str):
        return value.split()
    return [str(v) for v in value]


def as_int(value, default=None):
    if value is None or value == "":
        return default
    return to_int(value)


SIZE_RE = re.compile(r"(\d+)\s*([KMG]?)B", re.IGNORECASE)


def parse_size(text: str):
    """«4MB» → 4194304, «512KB» → 524288. None, если не разобрать."""
    m = SIZE_RE.search(text or "")
    if not m:
        return None
    mult = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}[m.group(2).upper()]
    return int(m.group(1)) * mult


_ADDR_RE = re.compile(r"(0x)?[0-9a-fA-F]+")
_BYTE_RE = re.compile(r"[0-9a-fA-F]{1,2}")


def parse_hex_bytes(text: str) -> bytes:
    """Разобрать текстовый дамп байтов: «12 34 ab», «0x12 0x34», «0x08000000: 12 34 …», «[0x12, 0x34]».

    Строки, где есть что-то кроме байтов (служебные сообщения), пропускаются целиком.
    """
    out = bytearray()
    for raw in (text or "").splitlines():
        line = raw.strip().replace("[", " ").replace("]", " ").replace(",", " ")
        if not line:
            continue
        if ":" in line:
            head, _, tail = line.partition(":")
            if not _ADDR_RE.fullmatch(head.strip()):
                continue
            line = tail
        values = []
        for tok in line.split():
            t = tok[2:] if tok.lower().startswith("0x") else tok
            if not _BYTE_RE.fullmatch(t):
                values = None
                break
            values.append(int(t, 16))
        if values:
            out.extend(values)
    return bytes(out)
