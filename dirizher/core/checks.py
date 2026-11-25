"""Проверки диагностики. Каждая проверка только читает данные и возвращает Finding.

Профиль выбирает, какие проверки запускать (поле checks). Чтобы добавить новую
проверку, напишите функцию и пометьте её @register("имя").
"""
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import layout as layout_mod
from .models import Finding
from .profiles import to_int

SECTOR = 4096
CHECKS: dict = {}
DEFAULT_CHECKS = ["stable_read", "not_zero", "not_blank", "size_matches", "compare_reference"]


@dataclass
class CheckInput:
    data: bytes              # копия «до»
    consistent: bool         # совпали ли повторные чтения
    profile: Any             # Profile
    reference: dict | None   # {"data", "label", "sha", "profile_id", "preserve"} или None
    params: dict             # параметры проверки из профиля
    conductor: Any           # доступ к runner и журналу


def register(name: str) -> Callable:
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


# ---------- вспомогательное ----------
def is_fill(data: bytes, value: int) -> bool:
    return len(data) > 0 and data.count(value) == len(data)


def masked(data: bytes, preserve) -> bytes:
    """Обнулить уникальные области, чтобы они не считались отличием."""
    if not preserve:
        return data
    buf = bytearray(data)
    for reg in preserve:
        off, ln = to_int(reg["offset"]), to_int(reg["length"])
        chunk = buf[off:off + ln]
        buf[off:off + ln] = b"\x00" * len(chunk)
    return bytes(buf)


def diff_sectors(a: bytes, b: bytes, preserve=()) -> list:
    a, b = masked(a, preserve), masked(b, preserve)
    n = max(len(a), len(b))
    return [off for off in range(0, n, SECTOR) if a[off:off + SECTOR] != b[off:off + SECTOR]]


def fmt_offsets(offsets: list, limit: int = 5) -> str:
    shown = ", ".join(f"0x{o:06X}" for o in offsets[:limit])
    return shown + (" …" if len(offsets) > limit else "")


# ---------- проверки ----------
@register("stable_read")
def stable_read(ci: CheckInput) -> Finding:
    if ci.consistent:
        return Finding("stable_read", "ok", "повторные чтения совпали — контакт надёжный")
    return Finding("stable_read", "block",
                   "повторные чтения НЕ совпадают: плохой контакт прищепки, питание или помехи. "
                   "Запись заблокирована, пока чтение не станет стабильным.")


@register("not_zero")
def not_zero(ci: CheckInput) -> Finding:
    if is_fill(ci.data, 0x00):
        return Finding("not_zero", "block",
                       "одни нули: чип не отвечает или нет контакта (проверьте вывод 1 и питание)")
    return Finding("not_zero", "ok", "данные не нулевые")


@register("not_blank")
def not_blank(ci: CheckInput) -> Finding:
    if is_fill(ci.data, 0xFF):
        return Finding("not_blank", "warn",
                       "память пустая (стёрта). Если устройство не стартует — прошивку нужно восстановить.",
                       ["restore_reference", "restore_image"])
    return Finding("not_blank", "ok", "память не пустая")


@register("size_matches")
def size_matches(ci: CheckInput) -> Finding:
    if ci.profile.size and len(ci.data) != ci.profile.size:
        return Finding("size_matches", "warn",
                       f"размер дампа {len(ci.data)} байт, а по профилю {ci.profile.size}")
    return Finding("size_matches", "ok", f"размер {len(ci.data)} байт")


@register("magic")
def magic(ci: CheckInput) -> Finding:
    off, want = to_int(ci.params.get("offset", 0)), bytes.fromhex(ci.params["hex"])
    title = ci.params.get("title", f"сигнатура по смещению 0x{off:X}")
    got = ci.data[off:off + len(want)]
    if got == want:
        return Finding("magic", "ok", f"{title} на месте")
    return Finding("magic", "fail", f"{title} повреждена: ожидалось {want.hex()}, найдено {got.hex()}",
                   ["restore_reference", "restore_image"])


@register("compare_reference")
def compare_reference(ci: CheckInput) -> Finding:
    ref = ci.reference
    if ref is None:
        return Finding("compare_reference", "info",
                       "эталона пока нет. Когда убедитесь, что устройство исправно, "
                       "сохраните этот дамп как эталон — система предложит.")
    if len(ref["data"]) != len(ci.data):
        return Finding("compare_reference", "warn",
                       f"размер эталона «{ref['label']}» ({len(ref['data'])}) не совпадает с дампом ({len(ci.data)})")
    diffs = diff_sectors(ci.data, ref["data"], ref.get("preserve"))
    note = " (уникальные области платы не учитываются)" if ref.get("preserve") else ""
    if not diffs:
        return Finding("compare_reference", "ok", f"совпадает с эталоном «{ref['label']}»{note}")
    total = (len(ci.data) + SECTOR - 1) // SECTOR
    suggest, by_region = ["restore_reference"], ""
    layout = getattr(ci.profile, "layout", None)
    if layout:
        rows = region_diff(ci)
        if rows:
            by_region = f" По разделам: {layout_mod.describe(rows)}."
        if any(r["restore"] for r in rows):
            suggest.append("restore_regions")
    return Finding("compare_reference", "fail",
                   f"отличается {len(diffs)} из {total} секторов по 4 КБ от эталона «{ref['label']}»{note}."
                   f"{by_region} Первые: {fmt_offsets(diffs)}",
                   suggest)


def region_diff(ci: CheckInput) -> list:
    """Отличия от эталона по разделам карты (уникальные области не учитываются)."""
    ref = ci.reference
    if ref is None or not getattr(ci.profile, "layout", None) or len(ref["data"]) != len(ci.data):
        return []
    preserve = ref.get("preserve")
    return layout_mod.diff(masked(ci.data, preserve), masked(ref["data"], preserve), ci.profile.layout)


@register("layout")
def layout_check(ci: CheckInput) -> Finding:
    """Карта разделов профиля: пустой загрузчик — находка. Профиль без layout — проверка молчит (ok)."""
    layout = getattr(ci.profile, "layout", None)
    if not layout:
        return Finding("layout", "ok", "карты разделов в профиле нет — проверка по разделам пропущена")
    blank = layout_mod.blank_boot(ci.data, layout)
    if blank:
        names = ", ".join(f"«{r['title']}» ({layout_mod.span(r)})" for r in blank)
        return Finding("layout", "fail",
                       f"раздел загрузчика пустой: {names} — устройство не сможет запуститься",
                       ["restore_reference", "restore_regions", "restore_image"])
    return Finding("layout", "ok", f"разделов в карте: {len(layout)}, загрузчик на месте"
                   if any(r["kind"] == "boot" for r in layout) else f"разделов в карте: {len(layout)}")


def _binwalk_scan(ci: CheckInput):
    """Запустить binwalk (только сканирование, без распаковки) на копии дампа."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "dump.bin"
        path.write_bytes(ci.data)
        return ci.conductor.runner.run(["binwalk", str(path)], timeout=int(ci.params.get("timeout", 300)))


@register("binwalk")
def binwalk(ci: CheckInput) -> Finding:
    """Необязательная проверка: что лежит внутри дампа (загрузчик, ядро, ФС...)."""
    if not shutil.which("binwalk"):
        return Finding("binwalk", "info", "binwalk не установлен — разбор структуры пропущен")
    res = _binwalk_scan(ci)
    lines = [ln for ln in res.out.splitlines() if ln.strip()]
    if not res.ok or not lines:
        return Finding("binwalk", "info", "binwalk не нашёл знакомых структур")
    return Finding("binwalk", "info", "структура дампа (первые строки):\n      "
                   + "\n      ".join(lines[:12]))


@register("binwalk_expect")
def binwalk_expect(ci: CheckInput) -> Finding:
    """Структура прошивки на месте: binwalk должен найти все ожидаемые сигнатуры.

    В профиле:   - type: binwalk_expect
                   expect: ["uImage header", "Squashfs filesystem"]   # подстроки, регистр не важен
    Работает с binwalk 2.x и 3.x: ищутся подстроки во всём выводе. ПРОВЕРИТЬ НА ЖЕЛЕЗЕ:
    точные названия сигнатур — по выводу binwalk на ИСПРАВНОМ дампе этой платы.
    """
    expect = [str(x) for x in (ci.params.get("expect") or [])]
    if not expect:
        return Finding("binwalk_expect", "warn", "в профиле не задан список expect — проверка пропущена")
    if not shutil.which("binwalk"):
        return Finding("binwalk_expect", "info", "binwalk не установлен — проверка структуры пропущена")
    res = _binwalk_scan(ci)
    if not res.ok:
        return Finding("binwalk_expect", "warn", f"binwalk завершился с ошибкой: {res.tail(3)}")
    text = res.out.lower()
    missing = [e for e in expect if e.lower() not in text]
    if not missing:
        return Finding("binwalk_expect", "ok", "структура прошивки на месте: " + ", ".join(expect))
    return Finding("binwalk_expect", "fail",
                   "в дампе не найдено: " + ", ".join(missing) + " — прошивка, вероятно, повреждена",
                   ["restore_reference", "restore_image"])
