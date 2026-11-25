"""Микроконтроллеры AVR (ATmega, ATtiny…) через avrdude: USBasp, AVRISP, Arduino-as-ISP, UPDI…

avrdude (лицензия GPL-2.0) вызывается отдельным процессом.

Что делает плагин:
  probe — `avrdude -c <программатор> -p <МК> -n`: режим «ничего не писать», читается подпись.
  read  — `-U flash:r:файл:r -U eeprom:r:файл:r` → один образ [flash][eeprom].
  write — `-U flash:w:… -U eeprom:w:…` одной командой.

Почему образ = flash + EEPROM: при записи flash avrdude сначала стирает весь чип (chip erase),
а это стирает и EEPROM (если не задан фьюз EESAVE). Поэтому EEPROM входит в копию «до», в сверку
и восстанавливается той же командой. В профиле EEPROM помечена как preserve — настройки и
калибровки устройства берутся с текущего чипа.

avrdude не сохраняет хвост флеша из 0xFF — плагин дополняет до flash_size (обязателен в настройках).

Запрещено (guard): -u (отключить safemode фьюзов), -F (игнорировать подпись), -D (без стирания —
испортит запись), -e, -t, а также любые -U в extra_args (фьюзы и lock-биты не трогаем никогда).

inspect (0.7.0, только чтение): если в профиле есть `fuses:` — `avrdude … -n -U lfuse:r:-:h …` (режим
«ничего не писать», значения в stdout) и расшифровка по профилю (core/fuses.py): тактирование, BOD,
загрузчик, защита. Lock-биты «чтение запрещено» запрещают запись (копия была бы недостоверной).
Запись фьюзов по-прежнему запрещена: в команде только «:r:», это проверяется перед запуском.
ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: вывод `-U lfuse:r:-:h` — по строке «0x..» на каждую память, в порядке -U.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строку «Device signature = …» вашей версии (6.x: «0x1e950f», 7–8.x: «1E 95 0F»).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core import fuses as fuses_mod
from ..core.models import STATUS_ORDER, Detection, Finding
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import PREFLIGHT_ISP, as_int, extra_args, fail_text, guard, tool_version

FORBIDDEN = ["-u", "-F", "-D", "-e", "-t", "-U"]
SIG_RE = re.compile(r"device signature\s*=\s*(?:0x)?([0-9a-f]{2})\s*([0-9a-f]{2})\s*([0-9a-f]{2})",
                    re.IGNORECASE)
SIG_NAME_RE = re.compile(r"device signature[^\n(]*\(([^)]+)\)", re.IGNORECASE)
NO_ANSWER_RE = re.compile(r"initiali[sz]ation failed|target doesn't answer|cannot find usb device|"
                          r"not responding|programmer is not responding|could not find", re.IGNORECASE)
MISMATCH_RE = re.compile(r"expected signature", re.IGNORECASE)
FUSE_MEMS = ("lfuse", "hfuse", "efuse", "fuse", "lock")
FUSE_VALUE_RE = re.compile(r"^\s*0x([0-9a-fA-F]{1,2})\s*$", re.MULTILINE)


class AvrdudePlugin(Plugin):
    name = "avrdude"
    title = "Микроконтроллеры AVR через avrdude"
    target_class = "mcu.avr"
    hardware = True
    model_setting = "part"               # модель — из профиля (tool: avrdude: part, flash_size, eeprom_size)
    preflight = PREFLIGHT_ISP
    settings_schema = [
        field("programmer", "Программатор (-c)", default="usbasp", example="usbasp"),
        field("part", "Микроконтроллер (-p)", required=True, example="m328p"),
        field("port", "Порт (-P)", "port", example="/dev/ttyUSB0", help="Для arduino и serialupdi."),
        field("bitclock", "Медленнее такт (-B)", "int", example=10, help="Медленнее — надёжнее на «тихих» МК."),
        field("memories", "Что читать", "list", default=["flash", "eeprom"], example=["flash", "eeprom"],
              help="flash и/или eeprom. Фьюзы и lock-биты не трогаются никогда."),
        field("flash_size", "Размер flash, байт", "int", example=32768),
        field("eeprom_size", "Размер EEPROM, байт", "int", example=1024),
    ]

    @classmethod
    def image_size(cls, settings: dict):
        """Образ = выбранные памяти подряд (flash + eeprom)."""
        mems = settings.get("memories") or ["flash", "eeprom"]
        mems = [mems] if isinstance(mems, str) else mems
        sizes = [as_int(settings.get(f"{m}_size"), 0) for m in mems]
        return sum(sizes) if all(sizes) else None

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        mems = self.settings.get("memories") or ["flash", "eeprom"]
        self.memories = [str(m) for m in ([mems] if isinstance(mems, str) else mems)]

    def _sizes(self) -> dict:
        return {"flash": as_int(self.settings.get("flash_size"), 0),
                "eeprom": as_int(self.settings.get("eeprom_size"), 0)}

    def _base(self) -> list:
        cmd = ["avrdude", "-c", str(self.settings.get("programmer", "usbasp")),
               "-p", str(self.settings.get("part", ""))]
        if self.settings.get("port"):
            cmd += ["-P", str(self.settings["port"])]
        if self.settings.get("bitclock"):
            cmd += ["-B", str(self.settings["bitclock"])]
        guard("avrdude", extra_args(self.settings), FORBIDDEN)        # в extra_args — без -U и опасного
        return cmd + extra_args(self.settings)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if not shutil.which("avrdude"):
            return False, "avrdude не установлен (sudo apt install avrdude)"
        if not self.settings.get("part"):
            return False, "укажите part (например, m328p) в config.yaml → plugins: avrdude"
        bad = [m for m in self.memories if m not in ("flash", "eeprom")]
        if bad:
            return False, f"memories может содержать только flash и eeprom (указано: {', '.join(bad)})"
        sizes = self._sizes()
        missing = [f"{m}_size" for m in self.memories if not sizes[m]]
        if missing:
            return False, "укажите в config.yaml → plugins: avrdude: " + ", ".join(missing)
        return True, (f"{self.settings.get('programmer', 'usbasp')} → {self.settings['part']}, "
                      f"образ: {' + '.join(self.memories)}")

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: у avrdude нет отдельной команды версии; `avrdude -?` печатает справку
        # и строку «avrdude version 7.1, …» (в 8.x — так же). Ничего не подключает и не пишет.
        return tool_version(self, ["avrdude", "-?"], r"avrdude version\s+v?(\d+\.\d+[\w.+-]*)")

    def probe(self):
        res = self.run(self._base() + ["-n"], timeout=int(self.settings.get("probe_timeout", 60)))
        text = res.out + "\n" + res.err
        m = SIG_RE.search(text)
        if not m:
            if NO_ANSWER_RE.search(text) or not res.ok:
                raise RuntimeError("AVR не отвечает: проверьте разъём ISP (MISO/MOSI/SCK/RESET/GND), питание "
                                   "платы, тактирование (кварц по фьюзам) и скорость -B (bitclock).\n"
                                   + fail_text(res, ""))
            return None
        sig = "".join(m.groups()).upper()
        if sig in ("000000", "FFFFFF"):
            raise RuntimeError(f"подпись 0x{sig}: связи с МК нет (контакт, RESET, тактирование, bitclock)")
        if MISMATCH_RE.search(text):
            raise RuntimeError(f"подпись 0x{sig} не совпадает с part «{self.settings['part']}» в настройках")
        sizes = self._sizes()
        total = sum(sizes[mem] for mem in self.memories)
        name = SIG_NAME_RE.search(text)
        fp = {"name": str(self.settings["part"]).upper(), "signature": sig, "size": total,
              "memories": "+".join(self.memories)}
        summary = f"AVR, подпись 0x{sig}" + (f" ({name.group(1).strip()})" if name else "") \
                  + f"; образ {' + '.join(self.memories)} = {total} байт"
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.9, summary=summary)

    def fuse_command(self, mems: list) -> list:
        """Команда чтения фьюзов: -n (ничего не писать) и только «<память>:r:-:h»."""
        cmd = self._base() + ["-n"]
        for mem in mems:
            if mem not in FUSE_MEMS:
                raise RuntimeError(f"avrdude: «{mem}» — не фьюз и не lock")
            cmd += ["-U", f"{mem}:r:-:h"]
        specs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-U"]
        if not all(sp.split(":")[1:2] == ["r"] for sp in specs):           # страховка: только чтение
            raise RuntimeError("avrdude: фьюзы и lock-биты Дирижёр только читает")
        return cmd

    def inspect(self, det):
        """Фьюзы и lock-биты: прочитать (только чтение) и объяснить по данным профиля."""
        spec = getattr(det.profile, "fuses", None) or {}
        if not spec:
            return None
        mems = [m for m in spec if m in FUSE_MEMS]
        res = self.run(self.fuse_command(mems), timeout=int(self.settings.get("probe_timeout", 60)))
        values = [int(v, 16) for v in FUSE_VALUE_RE.findall(res.out or "")]
        if not res.ok or len(values) != len(mems):
            return [Finding("fuses", "info", "фьюзы не прочитаны — разбор вывода avrdude не сошёлся "
                                             "(ПРОВЕРИТЬ НА ЖЕЛЕЗЕ)")]
        raw = dict(zip(mems, values))
        out = []
        for mem, value, parts in fuses_mod.decode(spec, raw):
            status = max((p[2] for p in parts), key=lambda st: STATUS_ORDER.get(st, 0), default="ok")
            text = "; ".join(f"{name} — {txt}" for name, txt, _st, _why in parts)
            why = " ".join(w for _n, _t, st, w in parts if w and st in ("warn", "block"))
            out.append(Finding(f"fuses {mem}", "info" if status == "ok" else status,
                               f"0x{value:02X}: {text}" + (f". {why}" if why else "")))
        return out

    def read(self, det) -> bytes:
        sizes = self._sizes()
        with tempfile.TemporaryDirectory() as td:
            files = {mem: Path(td) / f"{mem}.bin" for mem in self.memories}
            cmd = self._base()
            for mem, path in files.items():
                cmd += ["-U", f"{mem}:r:{path}:r"]
            res = self.run(cmd, timeout=int(self.settings.get("read_timeout", 600)))
            if not res.ok or not all(p.exists() for p in files.values()):
                raise RuntimeError(fail_text(res, "avrdude не смог прочитать память"))
            out = bytearray()
            for mem, path in files.items():
                chunk = path.read_bytes()
                if len(chunk) > sizes[mem]:
                    raise RuntimeError(f"{mem}: прочитано {len(chunk)} байт, больше {mem}_size={sizes[mem]}")
                out += chunk + b"\xff" * (sizes[mem] - len(chunk))      # avrdude срезает хвост из 0xFF
            return bytes(out)

    def write(self, det, data: bytes) -> None:
        sizes = self._sizes()
        total = sum(sizes[mem] for mem in self.memories)
        if len(data) != total:
            raise RuntimeError(f"размер образа {len(data)} не равен {total} ({' + '.join(self.memories)})")
        with tempfile.TemporaryDirectory() as td:
            cmd, pos = self._base(), 0
            for mem in self.memories:
                path = Path(td) / f"{mem}.bin"
                path.write_bytes(data[pos:pos + sizes[mem]])
                pos += sizes[mem]
                cmd += ["-U", f"{mem}:w:{path}:r"]
            res = self.run(cmd, timeout=int(self.settings.get("write_timeout", 900)))
            if not res.ok:
                raise RuntimeError(fail_text(res, "avrdude не смог записать память"))
