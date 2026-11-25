"""Определители разметки: карта разделов (core/layout.py) по содержимому дампа, когда в профиле её нет.

Каждый определитель — функция `fn(data: bytes) -> Detected | None`, помеченная @register("имя", "название").
  * None — подписи формата в дампе нет (это не ошибка: просто «не наш» дамп);
  * Detected с layout — разметка найдена (сырые разделы в формате поля профиля `layout:`);
  * Detected с problems — подпись есть, но таблица испорчена: понятная находка, а не падение.

Профиль включает определители полем `layout_auto: [esp_partitions, intel_ifd]` (или `layout_auto: all`).
Карта из профиля (`layout:`) всегда главнее: если она есть, определители не запускаются.
Только stdlib; ничего не пишут — разбирают уже снятую копию «до».

Форматы — по документации (ESP-IDF «Partition Tables», gen_esp32part.py; Intel «Flash Descriptor»,
ifdtool из coreboot). ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: на настоящих дампах (ESP32-WROOM, BIOS ноутбука).
"""
from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from typing import Callable

from . import layout as layout_mod

DETECTORS: dict = {}          # имя → функция
TITLES: dict = {}             # имя → название по-русски


def register(name: str, title: str) -> Callable:
    def deco(fn):
        DETECTORS[name] = fn
        TITLES[name] = title
        return fn
    return deco


@dataclass
class Detected:
    detector: str
    title: str
    layout: list = field(default_factory=list)      # сырые разделы (как в YAML профиля)
    notes: list = field(default_factory=list)       # сведения для человека
    problems: list = field(default_factory=list)    # что испорчено (тогда layout не используется)
    rows: list = field(default_factory=list)        # проверенная карта (layout.normalize)

    @property
    def ok(self) -> bool:
        return bool(self.rows) and not self.problems


def normalize_names(value) -> list:
    """Поле профиля layout_auto: список имён, «all»/true — все известные. Неизвестное имя — ValueError."""
    if value in (None, False, [], ""):
        return []
    if value is True or value == "all":
        return list(DETECTORS)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("layout_auto — список определителей разметки или all")
    out = []
    for name in value:
        name = str(name)
        if name == "all":
            out += [n for n in DETECTORS if n not in out]
            continue
        if name not in DETECTORS:
            raise ValueError(f"неизвестный определитель разметки «{name}» (доступны: {', '.join(DETECTORS)})")
        if name not in out:
            out.append(name)
    return out


def detect(data: bytes, names, size: int | None = None) -> Detected | None:
    """Первый определитель, узнавший свою подпись. Проверяет карту так же, как поле профиля layout."""
    for name in names:
        fn = DETECTORS.get(name)
        if fn is None:
            continue
        try:
            res = fn(data)
        except Exception as e:                       # разборщик не должен ронять сеанс
            res = Detected(name, TITLES.get(name, name), problems=[f"разбор не удался ({type(e).__name__}: {e})"])
        if res is None:
            continue
        if res.layout and not res.problems:
            try:
                res.rows = layout_mod.normalize(res.layout, size or len(data))
            except ValueError as e:
                res.problems.append(f"разметка противоречива: {e}")
        elif not res.problems:
            res.problems.append("подпись есть, а разделов не найдено")
        return res
    return None


def _hex(n: int) -> str:
    return f"0x{n:X}"


def _ident(label: str, used: set, fallback: str) -> str:
    rid = re.sub(r"[^a-z0-9_]+", "_", label.lower()).strip("_")[:40]
    if not rid or not rid[0].isalpha():
        rid = fallback
    base, n = rid, 2
    while rid in used:
        rid = f"{base[:36]}_{n}"
        n += 1
    used.add(rid)
    return rid


# ======================= ESP32: таблица разделов =======================
ESP_TABLE = 0x8000            # смещение по умолчанию (CONFIG_PARTITION_TABLE_OFFSET)
ESP_TABLE_MAX = 0xC00         # 3 КБ — 96 записей
ESP_MAGIC = b"\xAA\x50"       # 0x50AA, little-endian
ESP_MD5 = b"\xEB\xEB" + b"\xFF" * 14

# (тип, подтип) → (что это, kind, preserve, restore)  — restore None = по умолчанию для kind
ESP_DATA = {
    0x00: ("какое приложение запускать (OTA)", "config", False, None),
    0x01: ("калибровка радио", "calibration", True, None),
    0x02: ("настройки NVS (Wi-Fi, ключи, счётчики)", "config", True, None),
    0x03: ("дамп сбоя (coredump)", "data", False, False),
    0x04: ("ключи шифрования NVS", "unique", True, None),
    0x05: ("эмуляция eFuse", "unique", True, None),
    0x06: ("данные без типа", "data", False, False),
    0x80: ("файлы ESPHTTPD", "rootfs", False, False),
    0x81: ("файловая система FAT", "rootfs", False, False),
    0x82: ("файловая система SPIFFS", "rootfs", False, False),
    0x83: ("файловая система LittleFS", "rootfs", False, False),
}
FS_HELP = "может хранить файлы владельца — из эталона только по явному restore: true в профиле платы"


def _esp_kind(ptype: int, sub: int) -> tuple:
    if ptype == 0x00:
        if sub == 0x00:
            return "приложение (factory)", "app", False, None
        if 0x10 <= sub <= 0x1F:
            return f"приложение (ota_{sub - 0x10})", "app", False, None
        if sub == 0x20:
            return "приложение (test)", "app", False, None
        return f"приложение (подтип 0x{sub:02X})", "app", False, None
    if ptype == 0x01:
        return ESP_DATA.get(sub, (f"данные (подтип 0x{sub:02X})", "data", False, False))
    return f"раздел типа 0x{ptype:02X}/0x{sub:02X}", "data", False, False


@register("esp_partitions", "таблица разделов ESP32")
def esp_partitions(data: bytes) -> Detected | None:
    """Таблица разделов ESP-IDF: записи по 32 байта с 0x8000 —
    magic AA 50, тип, подтип, смещение (LE u32), размер (LE u32), метка 16 байт, флаги (LE u32).
    Запись EB EB FF…FF + 16 байт MD5 предыдущих записей (в новых IDF), конец — 32 байта 0xFF."""
    title = TITLES["esp_partitions"]
    if data[ESP_TABLE:ESP_TABLE + 2] != ESP_MAGIC:
        return None
    res = Detected("esp_partitions", title)
    size = len(data)
    used = {"bootloader", "partition_table"}
    if data[0x1000:0x1001] == b"\xE9":          # классический ESP32: загрузчик с 0x1000
        boot = 0x1000
    elif data[0:1] == b"\xE9":                  # ESP32-S3/C3 и др.: с 0x0
        boot = 0x0
    else:
        boot = None
        res.notes.append("заголовка загрузчика (0xE9) нет ни на 0x0, ни на 0x1000")
    if boot is not None:
        res.layout.append({"id": "bootloader", "title": "Загрузчик ESP", "offset": _hex(boot),
                           "end": _hex(ESP_TABLE), "kind": "boot"})
    res.layout.append({"id": "partition_table", "title": "Таблица разделов", "offset": _hex(ESP_TABLE),
                       "length": "0x1000", "kind": "boot",
                       "help": "без неё загрузчик не найдёт приложение"})
    md5_ok, entries = None, 0
    for n in range(ESP_TABLE_MAX // 32):
        pos = ESP_TABLE + n * 32
        raw = data[pos:pos + 32]
        if len(raw) < 32:
            res.problems.append(f"таблица обрывается на записи №{n + 1} (конец дампа)")
            break
        if raw == b"\xFF" * 32:
            break
        if raw[:16] == ESP_MD5:
            md5_ok = hashlib.md5(data[ESP_TABLE:pos]).digest() == raw[16:]
            if not md5_ok:
                res.problems.append("контрольная сумма MD5 таблицы не совпала — записи повреждены")
            continue
        if raw[:2] != ESP_MAGIC:
            res.problems.append(f"запись №{n + 1} (0x{pos:X}) испорчена: нет подписи AA 50")
            break
        ptype, sub, off, length = raw[2], raw[3], *struct.unpack_from("<II", raw, 4)
        label = raw[12:28].split(b"\x00", 1)[0].decode("ascii", "replace")
        entries += 1
        if length == 0 or off + length > size or off < ESP_TABLE + 0x1000:
            res.problems.append(f"раздел «{label or n + 1}» вне флеша или поверх таблицы "
                                f"(0x{off:X}+0x{length:X} при размере 0x{size:X})")
            continue
        what, kind, preserve, restore = _esp_kind(ptype, sub)
        part = {"id": _ident(label, used, f"part{n + 1}"), "title": f"{label or 'без метки'} — {what}",
                "offset": _hex(off), "length": _hex(length), "kind": kind}
        if preserve:
            part["preserve"] = True
        if restore is not None and not preserve:
            part["restore"] = restore
        if kind == "rootfs":
            part["help"] = FS_HELP
        res.layout.append(part)
    if not entries and not res.problems:
        res.problems.append("в таблице нет ни одной записи")
    if md5_ok is None and entries:
        res.notes.append("записи MD5 нет (старый ESP-IDF или отключена) — целостность таблицы не проверена")
    return res


# ======================= Intel Flash Descriptor =======================
IFD_SIGNATURE = b"\x5A\xA5\xF0\x0F"       # 0x0FF0A55A, little-endian, по смещению 0x10
IFD_REGIONS = [
    # (id, название, kind, preserve, restore, пояснение)
    ("descriptor", "Дескриптор (Flash Descriptor)", "boot", False, True,
     "карта флеша и права доступа; без него чипсет не стартует"),
    ("bios", "BIOS / UEFI", "boot", False, True,
     "основная прошивка; внутри могут быть настройки и серийник (NVRAM) — эталон лучше с этой же платы"),
    ("me", "Intel ME", "data", False, True,
     "прошивка Management Engine; из эталона — только с той же модели и версии"),
    ("gbe", "GbE — сетевая карта (MAC)", "unique", True, False, "MAC-адрес этой платы"),
    ("pdr", "Platform Data", "config", False, False, "данные производителя платы"),
    ("devexp1", "Device Expansion 1", "data", False, False, ""),
    ("bios2", "Второй BIOS", "data", False, False, ""),
    ("ucode", "Микрокод / резерв", "data", False, False, ""),
    ("ec", "EC — контроллер клавиатуры и питания", "boot", False, True,
     "прошивка встроенного контроллера (EC); бывает и в отдельном чипе"),
    ("devexp2", "Device Expansion 2", "data", False, False, ""),
    ("ie", "Innovation Engine", "data", False, False, ""),
    ("gbe10_a", "10GbE A", "unique", True, False, "MAC-адрес"),
    ("gbe10_b", "10GbE B", "unique", True, False, "MAC-адрес"),
    ("reserved13", "Резерв 13", "data", False, False, ""),
    ("reserved14", "Резерв 14", "data", False, False, ""),
    ("ptt", "PTT (встроенный TPM)", "unique", True, False, "ключи встроенного TPM"),
]


@register("intel_ifd", "дескриптор Intel (IFD)")
def intel_ifd(data: bytes) -> Detected | None:
    """Intel Flash Descriptor: подпись 5A A5 F0 0F по смещению 0x10; FLMAP0 (0x14): FCBA = биты 7:0 × 16,
    FRBA = биты 23:16 × 16; FLMAP1 (0x18): FMBA = биты 7:0 × 16. По FRBA — регистры FLREGn по 4 байта:
    база = (биты 14:0) << 12, предел = (биты 30:16) << 12 | 0xFFF; база > предела — региона нет."""
    title = TITLES["intel_ifd"]
    if data[0x10:0x14] != IFD_SIGNATURE:
        return None
    res = Detected("intel_ifd", title)
    if len(data) < 0x1000:
        res.problems.append("дамп короче 4 КБ — дескриптор не помещается")
        return res
    flmap0, flmap1 = struct.unpack_from("<II", data, 0x14)
    fcba, frba, fmba = (flmap0 & 0xFF) << 4, ((flmap0 >> 16) & 0xFF) << 4, (flmap1 & 0xFF) << 4
    if frba < 0x20 or frba + 4 > 0x1000:
        res.problems.append(f"FLMAP0 испорчен: таблица регионов по смещению 0x{frba:X}")
        return res
    stop = min([b for b in (fcba, fmba) if b > frba] + [frba + 4 * len(IFD_REGIONS), 0x1000])
    count = max(1, (stop - frba) // 4)
    size = len(data)
    for i in range(min(count, len(IFD_REGIONS))):
        reg = struct.unpack_from("<I", data, frba + 4 * i)[0]
        base, limit = (reg & 0x7FFF) << 12, ((reg >> 16) & 0x7FFF) << 12 | 0xFFF
        if base > limit or (i > 0 and reg == 0):
            continue                                         # региона нет
        rid, rtitle, kind, preserve, restore, hint = IFD_REGIONS[i]
        if limit >= size:
            res.problems.append(f"регион «{rtitle}» (0x{base:X}…0x{limit + 1:X}) выходит за размер дампа 0x{size:X}")
            continue
        part = {"id": rid, "title": rtitle, "offset": _hex(base), "end": _hex(limit + 1), "kind": kind,
                "preserve": preserve, "restore": restore}
        if hint:
            part["help"] = hint
        res.layout.append(part)
    if not res.layout and not res.problems:
        res.problems.append("в дескрипторе нет ни одного региона")
    elif res.layout and res.layout[0]["id"] != "descriptor":
        res.problems.append("регион дескриптора не описан — таблица регионов, вероятно, испорчена")
    elif res.layout and int(res.layout[0]["offset"], 0) != 0:
        res.problems.append("дескриптор должен начинаться с 0x0 — таблица регионов испорчена")
    return res
