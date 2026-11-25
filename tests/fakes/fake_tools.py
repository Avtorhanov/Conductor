#!/usr/bin/env python3
"""Подставные программы для тестов без железа (MANUAL.md, 2.4).

Тесты кладут в PATH короткие обёртки с настоящими именами (probe-rs, openocd, minipro, avrdude,
esptool, esptool.py, binwalk, sigrok-cli), которые вызывают:  fake_tools.py <имя> <аргументы…>

Состояние «микросхем» — файлы в $FAKE_DIR (нет файла — устройство не подключено).
Каждый вызов записывается в $FAKE_DIR/calls.jsonl — тесты проверяют, что опасных команд не было.

Переменные окружения:
  FAKE_ABSENT=1         — нет самого программатора/отладчика;
  FAKE_PROTECTED=1      — у чипа включена защита чтения;
  FAKE_ESPTOOL_V5=1     — esptool v5 (команды через дефис);
  FAKE_PRS_OLD=1        — старый probe-rs (у download флаг --format, а не --binary-format);
  FAKE_OPENOCD_SWD=1    — OpenOCD печатает «SWD DPIDR …» (иначе — стиль ST-Link/HLA);
  FAKE_AVRDUDE_V8=1     — avrdude 8: «Device signature = 1E 95 0F (ATmega328P)».
  Для inspect (0.7.0):
  FAKE_TARGET_VOLTAGE=… — что OpenOCD печатает в «Target voltage:» (по умолчанию 3.241);
  FAKE_DEV_ID=0x…       — слово DBGMCU_IDCODE у probe-rs (по умолчанию 0x20036410, STM32F1 medium-density);
  FAKE_WP=1             — flashrom --wp-status: весь чип защищён (режим hardware);
  FAKE_FLASHROM_V=1.2   — версия flashrom (до 1.3 --wp-status нет);
  FAKE_BAD_PIN=N        — minipro -z: плохой контакт вывода N;
  FAKE_ESP_ENCRYPTED=1, FAKE_ESP_SECURE=1 — espefuse summary: шифрование флеша / secure boot включены;
  $FAKE_DIR/avr_fuses.json — фьюзы AVR {"lfuse": 255, …} (по умолчанию Arduino: FF DA FD, lock FF).

ВАЖНО: форматы вывода здесь — по документации и типичным журналам, а не с железа.
Это проверка разбора и всего цикла; реальный формат — ПРОВЕРИТЬ НА ЖЕЛЕЗЕ.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
from pathlib import Path

DIR = Path(os.environ.get("FAKE_DIR", "."))
ABSENT = os.environ.get("FAKE_ABSENT") == "1"
PROTECTED = os.environ.get("FAKE_PROTECTED") == "1"

# ---------- файлы «микросхем» ----------
MCU = "mcu.bin"                    # STM32F103C8: 64 КБ с 0x08000000 (probe-rs, openocd)
MCU_BASE = 0x08000000
AVR_FLASH, AVR_EEPROM = "avr_flash.bin", "avr_eeprom.bin"
ESP = "esp.bin"
MINIPRO = "minipro.bin"


def path(name: str) -> Path:
    return DIR / name


def log_call(tool: str, argv: list) -> None:
    with open(DIR / "calls.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"tool": tool, "argv": argv}, ensure_ascii=False) + "\n")


def calls(fake_dir: Path) -> list:
    p = Path(fake_dir) / "calls.jsonl"
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------- исправные «прошивки» для тестов ----------
def healthy_stm32() -> bytes:
    rnd = random.Random(103)
    img = bytearray(b"\xff" * 65536)
    code = rnd.randbytes(20000)
    img[0:8] = (0x20005000).to_bytes(4, "little") + (0x08000131).to_bytes(4, "little")  # SP, Reset
    img[8:8 + len(code)] = code
    return bytes(img)


def healthy_avr() -> tuple:
    rnd = random.Random(328)
    flash = bytearray(b"\xff" * 32768)
    flash[0:9000] = rnd.randbytes(9000)
    eeprom = bytearray(b"\xff" * 1024)
    eeprom[0:16] = b"CAL=0x5A;ID=0042"
    return bytes(flash), bytes(eeprom)


# Таблица разделов ESP-IDF «singleapp»: (метка, тип, подтип, смещение, размер)
ESP_PARTITIONS = [("nvs", 1, 0x02, 0x9000, 0x6000), ("phy_init", 1, 0x01, 0xF000, 0x1000),
                  ("factory", 0, 0x00, 0x10000, 0x100000)]


def esp_partition_table(parts=ESP_PARTITIONS, md5: bool = True) -> bytes:
    """Двоичная таблица разделов, как её пишет gen_esp32part.py: записи по 32 байта, MD5, конец 0xFF."""
    out = b""
    for label, ptype, sub, off, size in parts:
        out += (b"\xAA\x50" + bytes([ptype, sub]) + off.to_bytes(4, "little") + size.to_bytes(4, "little")
                + label.encode().ljust(16, b"\x00") + (0).to_bytes(4, "little"))
    if md5:
        out += b"\xEB\xEB" + b"\xFF" * 14 + hashlib.md5(out).digest()
    return out + b"\xFF" * 32


def healthy_esp() -> bytes:
    rnd = random.Random(32)
    img = bytearray(b"\xff" * (4 * 1024 * 1024))
    img[0x1000] = 0xE9                                   # заголовок загрузчика
    img[0x1001:0x7000] = rnd.randbytes(0x7000 - 0x1001)
    table = esp_partition_table()                        # таблица разделов (с 0.7.0 S4b — настоящая)
    img[0x8000:0x8000 + len(table)] = table
    img[0x9000:0x9010] = b"NVS:wifi=home123"             # «уникальные» данные (раздел nvs)
    img[0x10000:0x10000 + 300000] = rnd.randbytes(300000)
    return bytes(img)


def healthy_24c02() -> bytes:
    data = bytearray(b"\xff" * 256)
    data[0:24] = b"SN=DRZ-24C02-0001;VOL=12"
    data[0x80:0x90] = bytes(range(16))
    return bytes(data)


def healthy_w25q64() -> bytes:
    rnd = random.Random(64)
    img = bytearray(b"\xff" * (8 * 1024 * 1024))
    img[0:4] = b"\x27\x05\x19\x56"                       # uImage
    img[64:64 + 100000] = rnd.randbytes(100000)
    img[0x200000:0x200004] = b"hsqs"                     # squashfs
    return bytes(img)


def corrupt(data: bytes, start: int, length: int) -> bytes:
    buf = bytearray(data)
    buf[start:start + length] = b"\xff" * len(buf[start:start + length])
    return bytes(buf)


def out(text: str = "") -> None:
    sys.stdout.write(text + ("\n" if text and not text.endswith("\n") else ""))


def err(text: str) -> None:
    sys.stderr.write(text + ("\n" if not text.endswith("\n") else ""))


# ======================================================================
def probe_rs(argv: list) -> int:
    sub = argv[0] if argv else ""
    if argv == ["--version"]:
        out("probe-rs 0.24.0 (git commit: crates.io)")
        return 0
    if sub == "list":
        if ABSENT:
            out("No debug probes were found.")
            return 0
        out("The following debug probes were found:\n[0]: STLink V2 -- 0483:3748:0001 (ST-LINK)")
        return 0
    if ABSENT:
        err("Error: No connected probes were found.")
        return 1
    if sub == "download" and "--help" in argv:
        flag = "--format <FORMAT>" if os.environ.get("FAKE_PRS_OLD") == "1" else "--binary-format <BINARY_FORMAT>"
        out(f"Download memory to attached target\n\nOptions:\n      {flag}\n      --base-address <ADDR>")
        return 0
    chip = path(MCU)
    if sub == "info":
        if not chip.exists():
            out("Probing target via SWD")
            err("Error: Connecting to the chip was unsuccessful.")
            return 1
        out("Probing target via SWD\n\nARM Chip with debug port Default:\nDebug Port: DPv1, DP Designer: ARM Ltd\n"
            "└── 0 MemoryAP\n    └── ROM Table (Class 1), Designer: STMicroelectronics\n"
            "        ├── Cortex-M3 SCS   (Generic IP component)")
        return 0
    if "--chip" not in argv:
        err("error: the following required arguments were not provided: --chip <CHIP>")
        return 2
    if "--allow-erase-all" in argv or sub == "erase":
        chip.write_bytes(b"\xff" * 65536)               # так выглядело бы «снятие защиты»
        return 0
    if sub == "read" and "b32" in argv:                   # регистр ID (inspect)
        i = argv.index("b32")
        addr = int(argv[i + 1], 0)
        word = int(os.environ.get("FAKE_DEV_ID", "0x20036410"), 0) if addr == 0xE0042000 else 0
        out(f"0x{addr:08x}: {word:08x}")
        return 0
    if sub == "read":
        if PROTECTED:
            err("Error: Failed to read memory: the chip is read protected (RDP level 1)")
            return 1
        i = argv.index("b8")
        addr, n = int(argv[i + 1], 0), int(argv[i + 2])
        data = chip.read_bytes()[addr - MCU_BASE: addr - MCU_BASE + n]
        out("WARN probe_rs::config: something harmless")
        for off in range(0, len(data), 16):
            out(f"0x{addr + off:08x}: " + " ".join(f"{b:02x}" for b in data[off:off + 16]))
        return 0
    if sub == "download":
        flag = "--format" if os.environ.get("FAKE_PRS_OLD") == "1" else "--binary-format"
        if flag not in argv:
            err(f"error: unexpected argument found (expected {flag})")
            return 2
        if PROTECTED:
            err("Error: flash is write protected")
            return 1
        base = int(argv[argv.index("--base-address") + 1], 0)
        img = Path(argv[-1]).read_bytes()
        buf = bytearray(chip.read_bytes())
        buf[base - MCU_BASE: base - MCU_BASE + len(img)] = img
        chip.write_bytes(bytes(buf))
        out("      Erasing ✔ \n  Programming ✔ \n    Finished in 1.2s")
        return 0
    err(f"unknown subcommand {sub}")
    return 2


# ======================================================================
def openocd(argv: list) -> int:
    cmds = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    err("Open On-Chip Debugger 0.12.0")
    if argv == ["--version"]:
        err("Licensed under GNU GPL v2")
        return 0
    if ABSENT:
        err("Error: open failed")
        return 1
    chip = path(MCU)
    for c in cmds:
        if c == "init":
            if not chip.exists():
                err("Error: init mode failed (unable to connect to the target)")
                return 1
            err("Info : STLINK V2J37S7 (API v2) VID:PID 0483:3748\nInfo : Target voltage: "
                + os.environ.get("FAKE_TARGET_VOLTAGE", "3.241"))
            if os.environ.get("FAKE_OPENOCD_SWD") == "1":
                err("Info : SWD DPIDR 0x1ba01477")
            else:
                err("Info : [stm32f1x.cpu] Cortex-M3 r1p1 processor detected")
        elif c.endswith("options_read 0"):
            err("option byte register = 0x3fffffc\nwrite protection register = 0xffffffff\n"
                f"read protection: {'on' if PROTECTED else 'off'}")
        elif c in ("reset halt", "halt"):
            err("[stm32f1x.cpu] halted due to debug-request, current mode: Thread")
        elif c.startswith("dump_image"):
            m = re.match(r"dump_image\s+\{?([^}]+?)\}?\s+(\S+)\s+(\S+)$", c)
            if PROTECTED:
                err("Error: Failed to read memory at 0x08000000: flash is read-protected (RDP)")
                return 1
            p, addr, n = m.group(1), int(m.group(2), 0), int(m.group(3), 0)
            Path(p).write_bytes(chip.read_bytes()[addr - MCU_BASE: addr - MCU_BASE + n])
            err(f"dumped {n} bytes in 0.4s")
        elif c.startswith("program"):
            m = re.match(r"program\s+\{?([^}]+?)\}?\s+verify\s+(\S+)$", c)
            img = Path(m.group(1)).read_bytes()
            base = int(m.group(2), 0)
            buf = bytearray(chip.read_bytes())
            buf[base - MCU_BASE: base - MCU_BASE + len(img)] = img
            chip.write_bytes(bytes(buf))
            err("** Programming Started **\n** Programming Finished **\n** Verify Started **\n** Verified OK **")
        elif re.search(r"\b(unlock|mass_erase)\b", c):
            chip.write_bytes(b"\xff" * 65536)
        elif c == "shutdown":
            err("shutdown command invoked")
            return 0
    return 0


# ======================================================================
MINIPRO_CHIPS = {"W25Q64BV": ("EF4017", 8 * 1024 * 1024), "AT24C02": (None, 256), "AT28C256": (None, 32768)}


def minipro(argv: list) -> int:
    if argv == ["--version"]:
        out("minipro version 0.7.2     A free and open TL866 series programmer")
        return 0
    if argv == ["--help"]:
        err("minipro version 0.7.2\nUsage: minipro [options]\n  -D, --read_id        Just read the chip ID\n"
            "  -z, --pin_check      Check for bad pin contact\n  -r <filename>        Read memory")
        return 0
    if ABSENT:
        err("No programmer found.")
        return 1
    dev = argv[argv.index("-p") + 1] if "-p" in argv else ""
    if dev not in MINIPRO_CHIPS:
        err(f"Device {dev} not found!")
        return 1
    chip_id, size = MINIPRO_CHIPS[dev]
    chip = path(MINIPRO)
    err("Found T48 00.1.31 (0x11f)\nDevice code: 46A16257\nSerial code: HSSCVO9LARFMOYKYOMVE5123")
    if "-z" in argv:
        bad = os.environ.get("FAKE_BAD_PIN")
        if not chip.exists() or bad:
            out(f"Bad contact on pin:{bad or 1}")
            return 1
        out("Pin test passed.")
        return 0
    if "-D" in argv:
        if not chip.exists():
            err(f"Invalid Chip ID: expected 0x{chip_id or '00'}, got 0x000000 (unknown)")
            return 1
        if chip_id:
            out(f"Chip ID: 0x{chip_id}  OK")
        else:
            out("This chip doesn't have a chip ID.")
        return 0
    if not chip.exists():
        err("Error: chip not inserted")
        return 1
    if "-r" in argv:
        Path(argv[argv.index("-r") + 1]).write_bytes(chip.read_bytes())
        out("Reading Code...  0.31Sec  OK")
        return 0
    if "-w" in argv:
        img = Path(argv[argv.index("-w") + 1]).read_bytes()
        if len(img) != size and "-s" not in argv:
            err(f"Incorrect file size: {len(img)} (needed {size})")
            return 1
        chip.write_bytes(img)
        out("Erasing... 0.10Sec OK\nWriting Code...  1.20Sec  OK\nReading Code...  0.31Sec  OK\nVerification OK")
        return 0
    err("nothing to do")
    return 1


# ======================================================================
def avrdude(argv: list) -> int:
    if argv == ["-?"]:
        err("Usage: avrdude [options]\nOptions:\n  -p <partno>  Specify AVR device\n\n"
            + ("avrdude version 8.0, https://github.com/avrdudes/avrdude" if os.environ.get("FAKE_AVRDUDE_V8") == "1"
               else "avrdude version 7.1, URL: <https://github.com/avrdudes/avrdude>"))
        return 1
    if ABSENT:
        err("avrdude: error: cannot find USB device with vid=0x16c0 pid=0x5dc")
        return 1
    flash_p, eeprom_p = path(AVR_FLASH), path(AVR_EEPROM)
    if not flash_p.exists():
        err("avrdude: error: program enable: target doesn't answer. 1\navrdude: initialization failed, rc=-1")
        return 1
    err("avrdude: AVR device initialized and ready to accept instructions\n"
        "Reading | ################################################## | 100% 0.00s")
    if os.environ.get("FAKE_AVRDUDE_V8") == "1":
        err("avrdude: Device signature = 1E 95 0F (ATmega328P, ATA6614Q, LGT8F328P)")
    else:
        err("avrdude: Device signature = 0x1e950f (probably m328p)")
    no_write = "-n" in argv
    specs = [argv[i + 1] for i, a in enumerate(argv) if a == "-U"]
    mems = {"flash": flash_p, "eeprom": eeprom_p}
    if any(s.split(":")[1] == "w" for s in specs) and not no_write and "-D" not in argv:
        err('avrdude: NOTE: "flash" memory has been specified, an erase cycle will be performed\n'
            "avrdude: erasing chip")
        flash_p.write_bytes(b"\xff" * len(flash_p.read_bytes()))
        eeprom_p.write_bytes(b"\xff" * len(eeprom_p.read_bytes()))   # chip erase стирает и EEPROM
    fuses_p = path("avr_fuses.json")
    fuses = {"lfuse": 0xFF, "hfuse": 0xDA, "efuse": 0xFD, "lock": 0xFF}
    if fuses_p.exists():
        fuses.update(json.loads(fuses_p.read_text(encoding="utf-8")))
    for spec in specs:
        mem, op, fname, _fmt = spec.split(":")
        if mem in fuses:
            if op != "r":
                path("fuses_written").write_text(spec)          # так быть не должно никогда
                err(f"avrdude: {mem} {op} — запись фьюзов (в тестах так быть не должно)")
                return 1
            if fname == "-":
                out(f"0x{fuses[mem]:02x}")
            err(f"avrdude: reading {mem} memory: | 100% | writing output file <stdout>")
            continue
        if mem not in mems:
            err(f"avrdude: {mem} {op} — неизвестная память")
            continue
        if op == "r":
            data = mems[mem].read_bytes()
            if mem == "flash":
                data = data.rstrip(b"\xff")              # avrdude срезает хвост из 0xFF
            Path(fname).write_bytes(data)
            err(f"avrdude: reading {mem} memory: | 100% | writing output file {fname}")
        elif op == "w" and not no_write:
            img = Path(fname).read_bytes()
            buf = bytearray(mems[mem].read_bytes())
            buf[0:len(img)] = img
            mems[mem].write_bytes(bytes(buf))
            err(f"avrdude: {len(img)} bytes of {mem} written\navrdude: {len(img)} bytes of {mem} verified")
    err("avrdude done.  Thank you.")
    return 0


# ======================================================================
def esptool(argv: list) -> int:
    v5 = os.environ.get("FAKE_ESPTOOL_V5") == "1"
    if argv == ["version"]:
        out("esptool v5.0.2" if v5 else "esptool.py v4.8.1")
        return 0
    rest, i = [], 0
    while i < len(argv):                                  # убрать --port/--baud/--chip
        if argv[i] in ("--port", "--baud", "--chip", "-p", "-b", "-c"):
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    if v5 and any("_" in a for a in rest if not a.startswith("/")):
        err("Warning: Deprecated: underscores in names, use dashes")
    sub = rest[0].replace("-", "_")
    out("esptool v5.0.2" if v5 else "esptool.py v4.8.1\nSerial port /dev/ttyUSB0\nConnecting....")
    chip = path(ESP)
    if ABSENT or not chip.exists():
        err("A fatal error occurred: Failed to connect to ESP32: No serial data received.")
        return 2
    if v5:
        out("Connected to ESP32 on /dev/ttyUSB0:\nChip type:          ESP32-D0WD-V3 (revision v3.1)\n"
            "Features:           Wi-Fi, BT, Dual Core, 240MHz\nCrystal frequency:  40MHz\n"
            "MAC:                24:6f:28:aa:bb:cc\n\nStub flasher running.")
    else:
        out("Detecting chip type... ESP32\nChip is ESP32-D0WD-V3 (revision v3.1)\n"
            "Features: WiFi, BT, Dual Core, 240MHz, VRef calibration in efuse\nCrystal is 40MHz\n"
            "MAC: 24:6f:28:aa:bb:cc\nUploading stub...\nRunning stub...\nStub running...")
    if sub == "flash_id":
        out("Manufacturer: 20\nDevice: 4016\nDetected flash size: 4MB\nHard resetting via RTS pin...")
        return 0
    if sub == "read_flash":
        if PROTECTED:
            err("A fatal error occurred: Secure Download Mode is enabled, read_flash is not supported")
            return 2
        start, size, fname = int(rest[1], 0), int(rest[2], 0), rest[3]
        Path(fname).write_bytes(chip.read_bytes()[start:start + size])
        out(f"Read {size} bytes at 0x{start:08x} in 1.0 seconds")
        return 0
    if sub == "write_flash":
        opts = [a.replace("-", "_") for a in rest]
        img = bytearray(Path(rest[-1]).read_bytes())
        base = int(rest[-2], 0)
        keep = all(f"__flash_{k}" in opts and opts[opts.index(f"__flash_{k}") + 1] == "keep"
                   for k in ("mode", "freq", "size"))
        if not keep and len(img) > 0x1003:
            img[0x1002] ^= 0x01                          # esptool поправил бы заголовок загрузчика
        buf = bytearray(chip.read_bytes())
        buf[base:base + len(img)] = img
        chip.write_bytes(bytes(buf))
        out("Wrote %d bytes\nHash of data verified." % len(img))
        return 0
    err(f"unknown command {sub}")
    return 2


# ======================================================================
def espefuse(argv: list) -> int:
    sub = [a for a in argv if not a.startswith("-") and a not in ("/dev/ttyUSB0", "auto", "esp32")]
    if sub != ["summary"]:
        path("efuse_written").write_text(" ".join(argv))         # так быть не должно никогда
        err(f"espefuse: {' '.join(argv)} — не сводка (в тестах так быть не должно)")
        return 2
    if ABSENT or not path(ESP).exists():
        err("A fatal error occurred: Failed to connect to Espressif device: No serial data received.")
        return 2
    enc = os.environ.get("FAKE_ESP_ENCRYPTED") == "1"
    sec = os.environ.get("FAKE_ESP_SECURE") == "1"
    out("espefuse.py v4.8.1\nConnecting....\nDetecting chip type... ESP32\n\n=== Run \"summary\" command ===\n"
        "EFUSE_NAME (Block) Description  = [Meaningful Value] [Readable/Writeable] (Hex Value)\n"
        "----------------------------------------------------------------------------------------\n"
        "Security fuses:\n"
        f"FLASH_CRYPT_CNT (BLOCK0)    Flash encryption mode counter   = {1 if enc else 0} R/W "
        f"(0b000000{1 if enc else 0})\n"
        "UART_DOWNLOAD_DIS (BLOCK0)  Disable UART download mode      = False R/W (0b0)\n"
        f"ABS_DONE_0 (BLOCK0)         Secure boot V1 is enabled for bootloader image = {sec} R/W (0b{int(sec)})\n"
        "ABS_DONE_1 (BLOCK0)         Secure boot V2 is enabled for bootloader image = False R/W (0b0)")
    return 0


# ======================================================================
def binwalk(argv: list) -> int:
    data = Path(argv[-1]).read_bytes()
    out("\nDECIMAL       HEXADECIMAL     DESCRIPTION\n" + "-" * 80)
    sigs = [(b"\x27\x05\x19\x56", "uImage header, header size: 64 bytes, OS: Linux"),
            (b"hsqs", "Squashfs filesystem, little endian, version 4.0, compression:xz")]
    for magic, desc in sigs:
        pos = data.find(magic)
        if pos >= 0:
            out(f"{pos:<14}0x{pos:<14X}{desc}")
    return 0


# ======================================================================
def sigrok_cli(argv: list) -> int:
    if "--scan" in argv:
        if not ABSENT:
            out("The following devices were found:\nfx2lafw - Saleae Logic with 8 channels: D0 D1 D2 D3 D4 D5 D6 D7")
        return 0
    if "-i" in argv:
        out("spi-1: 9F\nspi-1: EF 40 17\nspi-1: 03 00 00 00")
        return 0
    if ABSENT:
        err("No devices found.")
        return 1
    Path(argv[argv.index("-o") + 1]).write_bytes(b"SR-FAKE-CAPTURE")
    return 0


# ---------- flashrom (для теста «журнал → фикстура»; вывод — по документации, не с железа) ----------
SPI = "spi.bin"                     # W25Q64: 8 МБ


def flashrom(argv: list) -> int:
    if argv == ["--version"]:
        out(f"flashrom v{os.environ.get('FAKE_FLASHROM_V', '1.3.0')} on Linux 6.6.31+rpt-rpi-v8 (aarch64)")
        return 0
    out("flashrom v1.3.0 on Linux 6.6.31+rpt-rpi-v8 (aarch64)\nflashrom is free software, get the source code "
        "at https://flashrom.org\n\nUsing clock_gettime for delay loops (clk_id: 1, resolution: 1ns).")
    if ABSENT:
        err("Error: No CH341A device found.")
        return 1
    chip = path(SPI)
    out("Initializing ch341a_spi programmer\nDevice revision is 3.0.4\nThe following protocols are supported: SPI.")
    out("Probing for AMIC A25L05PT, 64 kB: probe_spi_rdid_generic: id1 0xef, id2 0x4017"
        if chip.exists() else "Probing for AMIC A25L05PT, 64 kB: probe_spi_rdid_generic: id1 0xff, id2 0xffff")
    for i in range(400):                                   # длинный «-V»: нужное — далеко от хвоста
        out(f"Probing for Vendor{i} CHIP{i}, {64 * (i % 9 + 1)} kB: compare_id: id1 0xef, id2 0x4017")
    if not chip.exists():
        out("No EEPROM/flash device found.")
        return 2
    out('Found Winbond flash chip "W25Q64BV/W25Q64CV/W25Q64FV" (8192 kB, SPI) on ch341a_spi.')
    if "--wp-status" in argv:                              # только чтение регистров статуса
        if os.environ.get("FAKE_WP") == "1":
            out("Protection range: start=0x00000000 length=0x00800000 (all)\nProtection mode: hardware")
        else:
            out("Protection range: start=0x00000000 length=0x00000000 (none)\nProtection mode: disabled")
        return 0
    if any(a.startswith("--wp-") for a in argv):
        path("wp_changed").write_text(" ".join(argv))           # так быть не должно никогда
        return 1
    out("No operations were specified.")
    if "-r" in argv:
        Path(argv[argv.index("-r") + 1]).write_bytes(chip.read_bytes())
    elif "-w" in argv:
        chip.write_bytes(Path(argv[argv.index("-w") + 1]).read_bytes())
    return 0


TOOLS = {"flashrom": flashrom, "probe-rs": probe_rs, "openocd": openocd, "minipro": minipro, "avrdude": avrdude,
         "esptool": esptool, "esptool.py": esptool, "binwalk": binwalk, "sigrok-cli": sigrok_cli,
         "espefuse": espefuse, "espefuse.py": espefuse}


def main(tool: str, argv: list) -> int:
    log_call(tool, argv)
    return TOOLS[tool](argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
