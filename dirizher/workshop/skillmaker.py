"""Новые навыки и статьи справки — из шаблонов, прямо из интерфейса.

Виды навыков:
  map   — карта параметров (только YAML): прибор уже виден лезвию (например, Modbus RTU),
          нужно описать, какие у него настройки, — код не нужен;
  tool  — новое лезвие: свой протокол или программа (skill.py с available/probe/read/write);
  check — своя проверка диагностики (skill.py с @register).
Файлы создаются через капот: проверка, история, ничего не перезаписывается.
Лезвие нового навыка выключено (settings: enabled: false), пока человек его не допишет и не включит.
"""
from __future__ import annotations

import json
import re

from ..core.skills import ID_RE, KINDS
from .hood import HoodError

SECTIONS = {"guide": "Штурман", "work": "Работа", "log": "Журнал", "catalog": "Справочник", "dev": "Навыки",
            "device": "Прибор"}

MANIFEST = """\
# Паспорт навыка «{title}». Справка — help.md рядом, статья «Навыки».
id: {id}
title: {qtitle}
kind: {kind}
version: 1
enabled: true            # false — навык не загружается совсем
summary: {qsummary}
{settings}"""

TOOL_SETTINGS = """\
settings:                # настройки лезвия по умолчанию (config.yaml → plugins: {id}: … важнее)
  enabled: false         # включите, когда допишете skill.py
  port: ""               # например /dev/ttyUSB0
"""

TOOL_CODE = '''\
"""Навык «{title}»: новое лезвие — свой протокол связи с прибором.

Что дописать (подробно — help.md рядом и статья «Навыки»):
  available()  — на месте ли порт или программа;
  probe()      — ТОЛЬКО чтение идентификатора (модель, адрес, версия) — сторож следит;
  read(det)    — прочитать окно данных целиком и вернуть bytes;
  write(det, data) — записать (лучше только изменившееся). Копии, сверку и «ДА» делает ядро.
Порт или программа — отдельным процессом через self.run([...]): таймаут и журнал.
Готовый пример помощника-процесса — skills/modbus_rtu/rtu.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

from dirizher.core.models import Detection
from dirizher.core.plugin import Plugin

HELPER = Path(__file__).with_name("helper.py")      # свой помощник-процесс, если понадобится


class {cls}Plugin(Plugin):
    name = "{id}"
    title = "{title}"
    target_class = "device.{id}"
    hardware = True

    def available(self) -> tuple:
        port = str(self.settings.get("port") or "")
        if not port:
            return False, "укажите port в skill.yaml → settings"
        if not Path(port).exists():
            return False, f"порт {{port}} не найден"
        return True, f"порт {{port}}"

    def probe(self):
        # Только чтение! Например:
        #   res = self.run([sys.executable, str(HELPER), "--port", port, "id"], timeout=20)
        #   return Detection(plugin=self.name, target_class=self.target_class,
        #                    fingerprint={{"name": res.out.strip()}}, confidence=0.8, summary=res.out.strip())
        raise NotImplementedError("навык «{id}»: опишите опознание в probe()")

    def read(self, det) -> bytes:
        raise NotImplementedError("навык «{id}»: опишите чтение в read()")

    def write(self, det, data: bytes) -> None:
        raise NotImplementedError("навык «{id}»: опишите запись в write()")
'''

CHECK_CODE = '''\
"""Навык «{title}»: своя проверка диагностики.

Проверка только читает дамп (ci.data) и возвращает Finding. Подключается в профиле:
    checks:
      - type: {id}
        offset: 0x0
        length: 0x100
        crc: 0x12345678        # без crc — проверка просто покажет текущую сумму
Статусы: ok, info, warn, fail (есть способ лечения), block — только если запись правда опасна.
"""
from __future__ import annotations

import zlib

from dirizher.core.checks import CheckInput, register
from dirizher.core.models import Finding
from dirizher.core.profiles import to_int


@register("{id}")
def check_{id}(ci: CheckInput) -> Finding:
    off = to_int(ci.params.get("offset", 0))
    length = to_int(ci.params.get("length", len(ci.data) - off))
    got = zlib.crc32(ci.data[off:off + length])
    want = ci.params.get("crc")
    if want is None:
        return Finding("{id}", "info", f"CRC32 области 0x{{off:X}}+0x{{length:X}}: 0x{{got:08X}} — впишите crc в профиль")
    if got == to_int(want):
        return Finding("{id}", "ok", "контрольная сумма области верна")
    return Finding("{id}", "fail", f"CRC32 0x{{got:08X}}, ожидалось 0x{{to_int(want):08X}}",
                   ["restore_reference", "restore_image"])
'''

MAP_PROFILE = """\
# Карта параметров «{title}». Адреса, типы и пределы — ТОЛЬКО из паспорта (руководства) прибора.
# ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: сначала «Прочитать параметры» и сверить значения с экраном прибора.
id: {id}
name: {qtitle}
class: {target_class}
match:
  name: "{model}"            # модель — как её сообщает лезвие (у Modbus — model в настройках)
priority: 20
voltage: null
allow_write: true
{bus}params:
  - id: setpoint
    title: Уставка
    {addr}
    type: i16be
    scale: 0.1
    unit: "°C"
    min: -50
    max: 150
    help: Пример. Замените адрес, тип и пределы по паспорту прибора.
checks: [stable_read]
recipes: []
notes: |
  Откуда карта (документ, страница), что проверено на приборе.
"""

HELP = """\
---
title: {qtitle}
section: {section}
tags: [{tags}]
---
# {title}

{body}
"""

HELP_BODY = {
    "map": "Что это за прибор и где взята карта параметров (паспорт, страница).\n\n"
           "1. «Штурман» → «Настроить прибор» → «Прочитать параметры».\n"
           "2. Сверьте значения с экраном прибора — только потом меняйте.\n",
    "tool": "Как подключить прибор (провода, порт, скорость) и что умеет лезвие.\n\n"
            "1. Допишите skill.py (капот).\n2. Включите: settings → enabled: true.\n"
            "3. «Узнать, что за микросхема» — только чтение.\n",
    "check": "Что проверяет проверка и как её подключить в профиле (checks).\n",
}


def _q(text: str) -> str:
    """Строка для YAML в кавычках — двоеточия и # в названии не ломают файл."""
    return json.dumps(text, ensure_ascii=False)


def class_name(sid: str) -> str:
    return "".join(part.capitalize() for part in sid.split("_")) or "My"


def _clean_title(title: str) -> str:
    title = re.sub(r"[\r\n\"\\{}]+", " ", str(title or "")).strip()
    if not title:
        raise HoodError("напишите название")
    return title[:80]


def create_skill(hood, kind: str, sid: str, title: str, target_class: str = "device.modbus",
                 actor: str = "") -> dict:
    """Создать навык из шаблона. Возвращает {"files": [...], "open": главный файл}."""
    if kind not in KINDS:
        raise HoodError(f"вид навыка — одно из: {', '.join(KINDS)}")
    if not ID_RE.match(sid or ""):
        raise HoodError("имя навыка — латиница, цифры и _, с буквы (например thermo_trm)")
    title = _clean_title(title)
    if (hood.root / "skills" / sid).exists():
        raise HoodError(f"навык «{sid}» уже есть")
    if not re.match(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", target_class or ""):
        raise HoodError("класс устройства — вида device.modbus или memory.i2c_eeprom")
    base = f"skills/{sid}"
    summary = {"map": "Карта параметров прибора: что можно прочитать и изменить.",
               "tool": "Новое лезвие: свой протокол связи с прибором.",
               "check": "Своя проверка диагностики."}[kind]
    files = {f"{base}/skill.yaml": MANIFEST.format(
        id=sid, title=title, qtitle=_q(title), kind=kind, qsummary=_q(summary),
        settings=TOOL_SETTINGS.format(id=sid) if kind == "tool" else "")}
    if kind == "tool":
        files[f"{base}/skill.py"] = TOOL_CODE.format(id=sid, title=title, cls=class_name(sid))
    elif kind == "check":
        files[f"{base}/skill.py"] = CHECK_CODE.format(id=sid, title=title)
    else:
        modbus = target_class == "device.modbus"
        files[f"{base}/profiles/{sid}.yaml"] = MAP_PROFILE.format(
            id=sid, title=title, qtitle=_q(title), target_class=target_class, model=sid.upper(),
            bus="bus:                       # окно чтения: только регистры НАСТРОЕК, без измеряемых величин\n"
                "  start: 0\n  count: 16\n" if modbus else "",
            addr="register: 0              # номер регистра из паспорта" if modbus
            else "offset: 0x0              # смещение в памяти, байт")
    files[f"{base}/help.md"] = HELP.format(title=title, qtitle=_q(title), section="dev", tags=f"навык, {kind}",
                                          body=HELP_BODY[kind])
    created = []
    for rel, text in files.items():
        hood.create(rel, text, actor)
        created.append(rel)
    main = next((r for r in created if r.endswith(("skill.py", f"{sid}.yaml"))), created[0])
    return {"files": created, "open": main}


def create_article(hood, aid: str, title: str, section: str = "dev", actor: str = "") -> str:
    if not re.match(r"^[a-z0-9][a-z0-9_-]{1,40}$", aid or ""):
        raise HoodError("имя статьи — латиница, цифры, _ и -, например my_router")
    if section not in SECTIONS:
        raise HoodError(f"раздел — одно из: {', '.join(SECTIONS)}")
    title = _clean_title(title)
    rel = f"help/{aid}.md"
    hood.create(rel, HELP.format(title=title, qtitle=_q(title), section=section, tags="",
                                 body="Коротко: зачем это и когда нужно.\n\n1. Первый шаг.\n2. Второй шаг.\n"),
                actor)
    return rel
