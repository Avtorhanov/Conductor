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

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строку «Device signature = …» вашей версии (6.x: «0x1e950f», 7–8.x: «1E 95 0F»).
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection
from ..core.plugin import Plugin
from ._common import as_int, extra_args, fail_text, guard, tool_version

FORBIDDEN = ["-u", "-F", "-D", "-e", "-t", "-U"]
SIG_RE = re.compile(r"device signature\s*=\s*(?:0x)?([0-9a-f]{2})\s*([0-9a-f]{2})\s*([0-9a-f]{2})",
                    re.IGNORECASE)
SIG_NAME_RE = re.compile(r"device signature[^\n(]*\(([^)]+)\)", re.IGNORECASE)
NO_ANSWER_RE = re.compile(r"initiali[sz]ation failed|target doesn't answer|cannot find usb device|"
                          r"not responding|programmer is not responding|could not find", re.IGNORECASE)
MISMATCH_RE = re.compile(r"expected signature", re.IGNORECASE)


class AvrdudePlugin(Plugin):
    name = "avrdude"
    title = "Микроконтроллеры AVR через avrdude"
    target_class = "mcu.avr"
    hardware = True

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
