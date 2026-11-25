"""Расшифровка фьюзов и lock-битов по данным профиля (поле `fuses:`) — только чтение.

Фьюзы читает лезвие (например, avrdude с -n и `-U lfuse:r:-:h`), а что значат их биты, знает профиль:

    fuses:
      lfuse:                                   # имя памяти — как у программы (lfuse, hfuse, efuse, lock)
        - name: Источник тактирования (CKSEL)
          mask: 0x0F                           # какие биты
          values: {0x0F: "внешний кварц 8–16 МГц", 0x02: "внутренний RC 8 МГц"}
          status: {0x00: warn}                 # необязательно: ok | info | warn | block для значения
          why: "…"                             # необязательно: пояснение к warn/block

Бит фьюза AVR «запрограммирован» = 0. Неизвестное значение показывается числом.
Запись фьюзов Дирижёр не делает никогда: здесь только чтение и объяснение.

Регистр ID (поле `id_register:`) — адрес, маска и ожидаемое значение, чтобы лезвие (probe-rs) подтвердило
модель, выбранную человеком:

    id_register: {address: 0xE0042000, mask: 0xFFF, expect: 0x410, title: "DBGMCU_IDCODE, DEV_ID"}
"""
from __future__ import annotations

STATUSES = ("ok", "info", "warn", "block")


def _int(value) -> int:
    return int(value, 0) if isinstance(value, str) else int(value)


def normalize(spec) -> dict:
    """Проверить и привести поле fuses: {память: [поле, …]}. ValueError — с понятным текстом."""
    if not spec:
        return {}
    if not isinstance(spec, dict):
        raise ValueError("fuses — словарь: {lfuse: [поля], hfuse: […], lock: […]}")
    out = {}
    for mem, fields in spec.items():
        if not isinstance(fields, list) or not fields:
            raise ValueError(f"fuses.{mem}: нужен список полей (name, mask, values)")
        rows = []
        for i, f in enumerate(fields):
            where = f"fuses.{mem}[{i}]"
            if not isinstance(f, dict) or not f.get("name") or f.get("mask") is None:
                raise ValueError(f"{where}: нужны name и mask")
            mask = _int(f["mask"])
            if not 0 < mask <= 0xFF:
                raise ValueError(f"{where}: mask — байт 0x01…0xFF")
            values = {_int(k) & mask: str(v) for k, v in (f.get("values") or {}).items()}
            status = {}
            for k, v in (f.get("status") or {}).items():
                if str(v) not in STATUSES:
                    raise ValueError(f"{where}: status — один из {', '.join(STATUSES)}")
                status[_int(k) & mask] = str(v)
            rows.append({"name": str(f["name"]), "mask": mask, "values": values, "status": status,
                         "why": str(f.get("why") or "")})
        out[str(mem)] = rows
    return out


def decode(spec: dict, raw: dict) -> list:
    """[(память, значение, [(поле, текст, статус, пояснение), …]), …] — в порядке профиля."""
    result = []
    for mem, rows in spec.items():
        if mem not in raw:
            continue
        value = raw[mem]
        parts = []
        for r in rows:
            bits = value & r["mask"]
            text = r["values"].get(bits)
            if text is None:
                text = f"значение 0x{bits:02X} (нет в профиле)"
            parts.append((r["name"], text, r["status"].get(bits, "ok"), r["why"]))
        result.append((mem, value, parts))
    return result


def normalize_id_register(spec) -> dict:
    if not spec:
        return {}
    if not isinstance(spec, dict) or spec.get("address") is None or spec.get("expect") is None:
        raise ValueError("id_register — {address: 0x…, expect: 0x… (или список), mask: 0x…, title: …}")
    expect = spec["expect"] if isinstance(spec["expect"], list) else [spec["expect"]]
    return {"address": _int(spec["address"]), "mask": _int(spec.get("mask", 0xFFFFFFFF)),
            "expect": [_int(e) for e in expect], "title": str(spec.get("title") or "регистр ID")}
