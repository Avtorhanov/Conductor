"""ESP8266 / ESP32 (все серии) через esptool — по USB-UART в режиме загрузчика.

esptool (лицензия GPL-2.0) вызывается отдельным процессом. Поддерживаются обе ветки:
v4 (`esptool.py flash_id`, подчёркивания) и v5 (`esptool flash-id`, дефисы) — версия
определяется командой `version`.

Что делает плагин:
  probe — `flash_id`: тип чипа, MAC, ID и размер SPI-флеша. Флеш не пишется (загрузчик в ОЗУ).
  read  — `read_flash 0 <размер> файл` — весь флеш.
  write — `write_flash --flash_mode keep --flash_freq keep --flash_size keep 0x0 файл`
          («keep» — чтобы esptool не правил заголовок загрузчика и сверка ядра совпала).

MAC входит в отпечаток: откат возможен только на ту же самую плату.

Запрещено (guard): --encrypt, --force, игнорирование настроек шифрования флеша, erase_flash/erase-flash.
Если на чипе включено шифрование флеша, дамп — шифротекст: эталон с ДРУГОЙ платы её не оживит.

inspect (0.7.0, только чтение): MAC и ID флеша из опознания; если установлен espefuse — `espefuse summary`
(только печать eFuse): включено ли шифрование флеша и безопасная загрузка (secure boot). Никаких burn_*:
команда собирается только с «summary» и проверяется перед запуском.
ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строки «FLASH_CRYPT_CNT / SPI_BOOT_CRYPT_CNT … = … (0b…)», «ABS_DONE_0/1»,
«SECURE_BOOT_EN … = True|False» в вашей версии espefuse.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: строки «Chip is …» / «Chip type: …», «MAC: …», «Manufacturer/Device»,
«Detected flash size» в вашей версии esptool.
"""
from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from ..core.models import Detection, Finding
from ..core.plugin import Plugin
from ..core.settings import field
from ._common import PREFLIGHT_UART, extra_args, fail_text, guard, parse_size

FORBIDDEN = ["--encrypt", "--encrypt-files", "--encrypt_files", "--force",
             "--ignore-flash-encryption-efuse-setting", "--ignore_flash_encryption_efuse_setting",
             "--ignore-flash-enc-efuse", "erase_flash", "erase-flash", "erase_region", "erase-region",
             "--erase-all", "--erase_all", "-e"]
VERSION_RE = re.compile(r"v?(\d+)\.(\d+)(?:\.\d+)?[\w.+-]*")
FAMILY_LINE_RE = re.compile(r"Detecting chip type\.*\s*(ESP[0-9A-Z-]+)", re.IGNORECASE)
CHIP_RE = re.compile(r"Chip (?:is|type:)\s*([^\n(]+)", re.IGNORECASE)
FAMILY_RE = re.compile(r"(ESP32-(?:S2|S3|C2|C3|C5|C61|C6|H2|H4|P4)|ESP32|ESP8266|ESP8285)", re.IGNORECASE)
MAC_RE = re.compile(r"MAC:\s*([0-9a-f]{2}(?::[0-9a-f]{2}){5})", re.IGNORECASE)
MANUF_RE = re.compile(r"Manufacturer:\s*([0-9a-f]+)", re.IGNORECASE)
DEVICE_RE = re.compile(r"Device:\s*([0-9a-f]+)", re.IGNORECASE)
FLASH_SIZE_RE = re.compile(r"Detected flash size:\s*([^\n]+)", re.IGNORECASE)
NO_CONNECT_RE = re.compile(r"Failed to connect|No serial data received|Timed out waiting", re.IGNORECASE)
CRYPT_RE = re.compile(r"^\s*(FLASH_CRYPT_CNT|SPI_BOOT_CRYPT_CNT)\b[^\n=]*=\s*([^\n]*)$", re.MULTILINE)
SECURE_RE = re.compile(r"^\s*(ABS_DONE_0|ABS_DONE_1|SECURE_BOOT_EN)\b[^\n=]*=\s*([^\n]*)$", re.MULTILINE)
BITS_RE = re.compile(r"\(0b([01]+)\)")
EFUSE_FORBIDDEN = ("burn", "write_protect", "write-protect", "read_protect", "read-protect", "set_flash_voltage",
                   "set-flash-voltage", "execute_scripts", "execute-scripts")
NO_PORT_RE = re.compile(r"could not open port|No such file or directory|Permission denied", re.IGNORECASE)


class EsptoolPlugin(Plugin):
    name = "esptool"
    title = "ESP8266/ESP32 через esptool (USB-UART)"
    target_class = "mcu.esp"
    hardware = True
    preflight = PREFLIGHT_UART               # модель esptool узнаёт сам — выбирать не нужно
    settings_defaults = {"probe_timeout": 60, "read_timeout": 900, "write_timeout": 1800}
    settings_schema = [
        field("port", "Порт", "port", default="/dev/ttyUSB0", example="/dev/ttyUSB0"),
        field("baud", "Скорость, бод", "int", default=460800, example=460800),
        field("chip", "Чип", default="auto", example="esp32", help="auto — esptool узнает сам."),
        field("cmd", "Программа", example="esptool.py", help="Пусто — esptool, если есть, иначе esptool.py."),
        field("target_class", "Класс цели", default="mcu.esp", example="mcu.esp"),
    ]

    def __init__(self, conductor, settings=None):
        super().__init__(conductor, settings)
        self.target_class = str(self.settings.get("target_class", self.target_class))
        self._major = None
        self._version = None

    @property
    def exe(self) -> str:
        if self.settings.get("cmd"):
            return str(self.settings["cmd"])
        return "esptool" if shutil.which("esptool") else "esptool.py"

    def _version_match(self):
        if self._version is None:
            res = self.run([self.exe, "version"], timeout=30)
            self._version = VERSION_RE.search(res.out + res.err) or False
        return self._version or None

    def major(self) -> int:
        if self._major is None:
            m = self._version_match()
            self._major = int(m.group(1)) if m else 4
        return self._major

    def version(self):
        # ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: `esptool version` печатает «esptool.py v4.7.0» / «esptool v5.0.0».
        m = self._version_match()
        return m.group(0).lstrip("v") if m else None

    def _n(self, name: str) -> str:
        """Имя команды/параметра в стиле нужной версии: v5 — через дефис."""
        return name.replace("_", "-") if self.major() >= 5 else name

    def _cmd(self, sub: str, *rest) -> list:
        cmd = [self.exe, "--port", str(self.settings.get("port", "/dev/ttyUSB0")),
               "--baud", str(self.settings.get("baud", 460800)),
               "--chip", str(self.settings.get("chip", "auto"))] + extra_args(self.settings)
        cmd += [self._n(sub)] + [self._n(r) if str(r).startswith("--") else str(r) for r in rest]
        return guard("esptool", cmd, FORBIDDEN)

    # ---------- интерфейс плагина ----------
    def available(self) -> tuple:
        if not (shutil.which(self.exe)):
            return False, "esptool не установлен (pip install esptool или sudo apt install esptool)"
        return True, f"порт {self.settings.get('port', '/dev/ttyUSB0')}"

    def probe(self):
        res = self.run(self._cmd("flash_id"), timeout=int(self.settings.get("probe_timeout", 60)))
        text = res.out + "\n" + res.err
        if NO_CONNECT_RE.search(text):
            raise RuntimeError("ESP не отвечает: переведите его в режим загрузчика (GPIO0 к GND при сбросе/"
                               "включении), проверьте TX↔RX, GND и скорость baud.\n" + fail_text(res, ""))
        if not res.ok:
            if NO_PORT_RE.search(text):
                raise RuntimeError(f"порт {self.settings.get('port', '/dev/ttyUSB0')} недоступен: проверьте "
                                   f"кабель и группу dialout.\n" + fail_text(res, ""))
            raise RuntimeError(fail_text(res, "esptool завершился с ошибкой"))
        chip = CHIP_RE.search(text)
        fam = FAMILY_LINE_RE.search(text) or (FAMILY_RE.search(chip.group(1)) if chip else None)
        size = parse_size(FLASH_SIZE_RE.search(text).group(1)) if FLASH_SIZE_RE.search(text) else None
        if not size:
            raise RuntimeError("esptool не сообщил размер флеша (Detected flash size) — ПРОВЕРИТЬ НА ЖЕЛЕЗЕ")
        family = fam.group(1).upper() if fam else "ESP"
        fp = {"name": family, "size": size}
        if chip:
            fp["chip"] = chip.group(1).strip()
        mac = MAC_RE.search(text)
        if mac:
            fp["mac"] = mac.group(1).lower()
        manuf, dev = MANUF_RE.search(text), DEVICE_RE.search(text)
        if manuf and dev:
            fp["flash_id"] = f"{int(manuf.group(1), 16):02X}{int(dev.group(1), 16):04X}"
        summary = f"{fp.get('chip', family)}, флеш {size // 1024 // 1024 or size // 1024}" \
                  f"{' МБ' if size >= 1024 * 1024 else ' КБ'}" + (f", MAC {fp['mac']}" if mac else "")
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint=fp,
                         confidence=0.9, summary=summary)

    # ---------- только чтение: сведения и eFuse ----------
    def efuse_exe(self):
        for exe in ("espefuse", "espefuse.py"):
            if shutil.which(exe):
                return exe
        return None

    def efuse_command(self, exe: str) -> list:
        """`espefuse --port … --chip … summary` — только печать; любое «burn…» отклоняется."""
        cmd = [exe, "--port", str(self.settings.get("port", "/dev/ttyUSB0")),
               "--chip", str(self.settings.get("chip", "auto")), "summary"]
        if cmd[-1] != "summary" or any(bad in a.lower() for a in cmd for bad in EFUSE_FORBIDDEN):
            raise RuntimeError("espefuse: Дирижёр только читает сводку eFuse")
        return cmd

    @staticmethod
    def _flag(value: str):
        """«= 0 R/W (0b0000000)» → False; «= Enable» → True; «= True» → True. None — не понять."""
        v = value.strip()
        bits = BITS_RE.search(v)
        word = v.split()[0].lower() if v.split() else ""
        if word in ("true", "enable", "enabled"):
            return True
        if word in ("false", "disable", "disabled"):
            return False
        if bits:
            return bits.group(1).count("1") % 2 == 1      # счётчик шифрования: нечётное число единиц — вкл.
        try:
            return bin(int(word, 0)).count("1") % 2 == 1
        except ValueError:
            return None

    def inspect(self, det):
        fp = det.fingerprint
        found = []
        known = [f"MAC {fp['mac']}" if fp.get("mac") else "", f"ID флеша 0x{fp['flash_id']}" if fp.get("flash_id") else ""]
        if any(known):
            found.append(Finding("chip_id", "info", ", ".join(k for k in known if k)
                                 + (" — MAC входит в отпечаток: откат только на эту же плату" if fp.get("mac") else "")))
        exe = self.efuse_exe()
        if not exe:
            self.c.log("espefuse не установлен — шифрование флеша и secure boot не проверялись")
            return found
        res = self.run(self.efuse_command(exe), timeout=int(self.settings.get("probe_timeout", 60)))
        text = res.out + "\n" + res.err
        crypt = [self._flag(m.group(2)) for m in CRYPT_RE.finditer(text)]
        secure = [self._flag(m.group(2)) for m in SECURE_RE.finditer(text)]
        if not res.ok or (not crypt and not secure):
            found.append(Finding("efuse", "info", "сводка eFuse не получена или не разобрана (ПРОВЕРИТЬ НА ЖЕЛЕЗЕ)"))
            return found
        if any(crypt):
            found.append(Finding("flash_encryption", "warn",
                                 "включено шифрование флеша: копия — шифротекст. Вернуть её можно только на эту же "
                                 "плату; эталон или образ с другой платы её не оживит."))
        elif crypt:
            found.append(Finding("flash_encryption", "ok", "шифрование флеша выключено"))
        if any(secure):
            found.append(Finding("secure_boot", "warn",
                                 "включена безопасная загрузка (secure boot): чип запустит только подписанную "
                                 "прошивку. Дирижёр подписи не обходит — пишите копию с этой же платы или "
                                 "подписанный образ производителя."))
        elif secure:
            found.append(Finding("secure_boot", "ok", "безопасная загрузка (secure boot) выключена"))
        return found

    def read(self, det) -> bytes:
        size = int(det.fingerprint["size"])
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "read.bin"
            res = self.run(self._cmd("read_flash", "0", size, out),
                           timeout=int(self.settings.get("read_timeout", 900)))
            if not res.ok or not out.exists():
                raise RuntimeError(fail_text(res, "esptool не смог прочитать флеш"))
            data = out.read_bytes()
        if len(data) != size:
            raise RuntimeError(f"esptool вернул {len(data)} байт вместо {size}")
        return data

    def write(self, det, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "write.bin"
            img.write_bytes(data)
            res = self.run(self._cmd("write_flash", "--flash_mode", "keep", "--flash_freq", "keep",
                                     "--flash_size", "keep", "0x0", img),
                           timeout=int(self.settings.get("write_timeout", 1800)))
            if not res.ok:
                raise RuntimeError(fail_text(res, "esptool не смог записать флеш"))
