"""Тесты интеграций 0.2.0 без железа: подставные программы (tests/fakes/fake_tools.py)
и эмулятор терминала Bus Pirate 5 на псевдотерминале (tests/fakes/bp5_emulator.py).

Запуск из корня проекта:   python3 -m unittest -v

Что проверяется для каждого «лезвия»:
  * полный цикл: исправное → эталон; порча → ремонт → на «чипе» снова исправная прошивка;
  * опознание только читает (по журналу вызовов подставной программы);
  * опасные параметры отклоняются до запуска программы;
  * защита чтения: чип не трогается, человеку объясняется причина;
  * разные версии программ (esptool v4/v5, старый probe-rs, avrdude 6/8, OpenOCD SWD/HLA).
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from dirizher.core.checks import CHECKS, CheckInput
from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from tests.fakes import fake_tools as ft

ROOT = Path(__file__).resolve().parent.parent
FAKE_TOOLS = ROOT / "tests" / "fakes" / "fake_tools.py"
SHIMS = ["probe-rs", "openocd", "minipro", "avrdude", "esptool", "esptool.py", "binwalk", "sigrok-cli"]
FAKE_FLAGS = ["FAKE_ABSENT", "FAKE_PROTECTED", "FAKE_ESPTOOL_V5", "FAKE_PRS_OLD", "FAKE_OPENOCD_SWD",
              "FAKE_AVRDUDE_V8"]

SETTINGS = {
    "probe_rs": {"enabled": True, "chip": "STM32F103C8", "flash_base": 0x08000000, "flash_size": 65536},
    "openocd": {"enabled": True, "interface": "interface/stlink.cfg", "target": "target/stm32f1x.cfg",
                "mcu": "STM32F103C8", "flash_base": 0x08000000, "flash_size": 65536},
    "minipro": {"enabled": True, "device": "W25Q64BV"},
    "avrdude": {"enabled": True, "programmer": "usbasp", "part": "m328p", "programmer_voltage": 5.0,
                "memories": ["flash", "eeprom"], "flash_size": 32768, "eeprom_size": 1024},
    "esptool": {"enabled": True, "port": "/dev/ttyUSB0"},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_int_"))
        self.fake = self.tmp / "fake"
        self.fake.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for name in SHIMS:
            p = self.bin / name
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS}" "{name}" "$@"\n')
            p.chmod(0o755)
        self._env = {k: os.environ.get(k) for k in ["PATH", "FAKE_DIR"] + FAKE_FLAGS}
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["FAKE_DIR"] = str(self.fake)
        for k in FAKE_FLAGS:
            os.environ.pop(k, None)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- помощники ----------
    def use(self, plugin: str, **extra) -> None:
        self.cfg["plugins"][plugin] = {**SETTINGS.get(plugin, {"enabled": True}), **extra}

    def conductor(self, answers=None, confirm=True, dry_run=False) -> Conductor:
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=False, dry_run=dry_run)
        self.opened.append(c)
        return c

    def outcome(self, c, sid) -> str:
        return c.memory.session(sid)["outcome"]

    def text(self, c) -> str:
        return "\n".join(c.ui.lines)

    def calls(self, tool=None) -> list:
        return [x["argv"] for x in ft.calls(self.fake) if tool is None or x["tool"] == tool]

    def reset_calls(self) -> None:
        (self.fake / "calls.jsonl").unlink(missing_ok=True)

    def cycle(self, corrupt, state, healthy):
        """Исправное → эталон; порча → ремонт → снова исправное. Возвращает (conductor, сеанс)."""
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        corrupt()
        self.assertNotEqual(state(), healthy)
        c2 = self.conductor()
        sid2 = c2.run()
        self.assertEqual(self.outcome(c2, sid2), "repaired", self.text(c2))
        self.assertEqual(state(), healthy)
        return c2, sid2


# =====================================================================
class ProbeRsTests(Base):
    def setUp(self):
        super().setUp()
        self.use("probe_rs")
        self.chip = self.fake / ft.MCU
        self.chip.write_bytes(ft.healthy_stm32())

    def test_full_cycle(self):
        c, sid = self.cycle(lambda: self.chip.write_bytes(ft.corrupt(ft.healthy_stm32(), 0x1000, 0x800)),
                            self.chip.read_bytes, ft.healthy_stm32())
        self.assertEqual(c.memory.session(sid)["profile_id"], "stm32f103c8")
        self.assertTrue(any(a[0] == "download" and "--binary-format" in a for a in self.calls("probe-rs")))

    def test_old_probe_rs_uses_format_flag(self):
        os.environ["FAKE_PRS_OLD"] = "1"
        self.cycle(lambda: self.chip.write_bytes(ft.corrupt(ft.healthy_stm32(), 0, 0x400)),
                   self.chip.read_bytes, ft.healthy_stm32())
        self.assertTrue(any(a[0] == "download" and "--format" in a for a in self.calls("probe-rs")))

    def test_probe_is_read_only(self):
        self.conductor().detect_only()
        subs = {a[0] for a in self.calls("probe-rs")}
        self.assertTrue(subs <= {"list", "info"}, subs)

    def test_no_debug_probe(self):
        os.environ["FAKE_ABSENT"] = "1"
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")

    def test_read_protection_is_not_bypassed(self):
        os.environ["FAKE_PROTECTED"] = "1"
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "read_failed")
        self.assertIn("защит", self.text(c))
        self.assertEqual(self.chip.read_bytes(), ft.healthy_stm32())
        self.assertFalse(any("--allow-erase-all" in a or a[0] == "erase" for a in self.calls("probe-rs")))

    def test_forbidden_extra_args_rejected(self):
        self.use("probe_rs", extra_args=["--allow-erase-all"])
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertIn("запрещён", self.text(c))
        self.assertFalse(any("--allow-erase-all" in a for a in self.calls("probe-rs")))

    def test_unavailable_without_chip_setting(self):
        self.cfg["plugins"]["probe_rs"] = {"enabled": True}
        c = self.conductor()
        row = next(r for r in c.plugin_report if r["name"] == "probe_rs")
        self.assertFalse(row["active"])
        self.assertIn("chip", row["status"])


# =====================================================================
class OpenOCDTests(Base):
    def setUp(self):
        super().setUp()
        self.use("openocd")
        self.chip = self.fake / ft.MCU
        self.chip.write_bytes(ft.healthy_stm32())

    def test_full_cycle(self):
        c, sid = self.cycle(lambda: self.chip.write_bytes(ft.corrupt(ft.healthy_stm32(), 0x2000, 0x1000)),
                            self.chip.read_bytes, ft.healthy_stm32())
        self.assertIn("напряжение цели 3.24 В", self.text(c))

    def test_swd_dpidr_detected(self):
        os.environ["FAKE_OPENOCD_SWD"] = "1"
        c = self.conductor()
        sid = c.detect_only()
        self.assertIn("1BA01477", c.memory.session(sid)["fingerprint"])

    def test_probe_is_read_only(self):
        self.conductor().detect_only()
        cmds = {a[i + 1] for a in self.calls("openocd") for i, x in enumerate(a) if x == "-c"}
        self.assertEqual(cmds, {"init", "shutdown"})

    def test_unlock_command_rejected(self):
        self.use("openocd", pre_init=["stm32f1x unlock 0"])
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertEqual(self.calls("openocd"), [])            # OpenOCD даже не запускался
        self.assertEqual(self.chip.read_bytes(), ft.healthy_stm32())

    def test_adapter_voltage_per_plugin_blocks(self):
        self.use("openocd", programmer_voltage=5.0)             # чип 3.3 В, адаптер 5 В
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "blocked_voltage")
        self.assertFalse(any("dump_image" in " ".join(a) for a in self.calls("openocd")))

    def test_read_protection(self):
        os.environ["FAKE_PROTECTED"] = "1"
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "read_failed")
        self.assertIn("защит", self.text(c))


# =====================================================================
class MiniproTests(Base):
    def setUp(self):
        super().setUp()
        self.chip = self.fake / ft.MINIPRO

    def test_spi_flash_uses_existing_profiles_and_binwalk(self):
        self.use("minipro")
        self.chip.write_bytes(ft.healthy_w25q64())
        c, sid = self.cycle(lambda: self.chip.write_bytes(ft.corrupt(ft.healthy_w25q64(), 0x10000, 0x8000)),
                            self.chip.read_bytes, ft.healthy_w25q64())
        s = c.memory.session(sid)
        self.assertEqual(s["profile_id"], "winbond_w25q64")
        self.assertIn('"jedec_id": "EF4017"', s["fingerprint"])
        self.assertIn("uImage", self.text(c))                  # проверка binwalk отработала

    def test_i2c_eeprom_without_id(self):
        self.use("minipro", device="AT24C02")
        self.chip.write_bytes(ft.healthy_24c02())
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        s = c.memory.session(sid)
        self.assertEqual((s["target_class"], s["profile_id"]), ("memory.i2c_eeprom", "eeprom_24c02"))
        self.assertIn("ID не читается", self.text(c))

    def test_probe_is_read_only(self):
        self.use("minipro")
        self.chip.write_bytes(ft.healthy_w25q64())
        self.conductor().detect_only()
        for argv in self.calls("minipro"):
            self.assertIn("-D", argv)
            self.assertFalse({"-w", "-r", "-E"} & set(argv))

    def test_wrong_chip_reported(self):
        self.use("minipro")                                    # чип не вставлен → ID не совпал
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertIn("ID чипа не совпал", self.text(c))

    def test_microcontrollers_refused(self):
        self.use("minipro", device="ATMEGA328P")
        row = next(r for r in self.conductor().plugin_report if r["name"] == "minipro")
        self.assertFalse(row["active"])
        self.assertIn("микроконтроллер", row["status"])

    def test_ignore_id_flag_rejected(self):
        self.use("minipro", extra_args=["-y"])
        self.chip.write_bytes(ft.healthy_w25q64())
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertEqual(self.calls("minipro"), [])

    def test_class_guess(self):
        from dirizher.plugins.minipro import guess_class
        self.assertEqual(guess_class("AT24C256"), "memory.i2c_eeprom")
        self.assertEqual(guess_class("24LC02B"), "memory.i2c_eeprom")
        self.assertEqual(guess_class("MX25L6406E"), "memory.spi_nor")
        self.assertEqual(guess_class("AT93C46"), "memory.microwire")
        self.assertEqual(guess_class("AT28C256"), "memory.parallel")


# =====================================================================
class AvrdudeTests(Base):
    def setUp(self):
        super().setUp()
        self.use("avrdude")
        self.flash, self.eeprom = self.fake / ft.AVR_FLASH, self.fake / ft.AVR_EEPROM
        f, e = ft.healthy_avr()
        self.flash.write_bytes(f)
        self.eeprom.write_bytes(e)

    def test_full_cycle_keeps_this_devices_eeprom(self):
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "healthy", self.text(c))
        f, _ = ft.healthy_avr()
        own = b"CAL=0x77;ID=0999" + b"\xff" * (1024 - 16)        # другое устройство той же модели
        self.flash.write_bytes(ft.corrupt(f, 0x100, 0x400))
        self.eeprom.write_bytes(own)
        c2 = self.conductor()
        sid = c2.run()
        self.assertEqual(self.outcome(c2, sid), "repaired", self.text(c2))
        self.assertEqual(self.flash.read_bytes(), f)              # flash — из эталона
        self.assertEqual(self.eeprom.read_bytes(), own)           # EEPROM — своя, пережила chip erase
        self.assertEqual(c2.memory.session(sid)["profile_id"], "atmega328p")

    def test_trimmed_flash_is_padded(self):
        c = self.conductor()
        sid = c.run()
        before = c.memory.snapshot(sid, "before")
        self.assertEqual(before["size"], 32768 + 1024)

    def test_probe_is_read_only(self):
        self.conductor().detect_only()
        for argv in self.calls("avrdude"):
            self.assertIn("-n", argv)
            self.assertNotIn("-U", argv)

    def test_adapter_voltage_used_in_question(self):
        c = self.conductor()
        c.run()
        self.assertIn("рассчитан на 5.0 В", self.text(c))

    def test_never_touches_fuses(self):
        self.cycle(lambda: self.flash.write_bytes(ft.corrupt(ft.healthy_avr()[0], 0, 0x200)),
                   self.flash.read_bytes, ft.healthy_avr()[0])
        specs = [a[i + 1] for a in self.calls("avrdude") for i, x in enumerate(a) if x == "-U"]
        self.assertTrue(specs)
        self.assertTrue(all(s.split(":")[0] in ("flash", "eeprom") for s in specs), specs)
        self.assertFalse(any({"-u", "-F", "-D"} & set(a) for a in self.calls("avrdude")))

    def test_safemode_off_rejected(self):
        self.use("avrdude", extra_args=["-u"])
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertEqual(self.calls("avrdude"), [])

    def test_avrdude8_signature_format(self):
        os.environ["FAKE_AVRDUDE_V8"] = "1"
        c = self.conductor()
        sid = c.detect_only()
        self.assertIn("1E950F", c.memory.session(sid)["fingerprint"])
        self.assertIn("ATmega328P", self.text(c))

    def test_no_target(self):
        self.flash.unlink()
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertIn("AVR не отвечает", self.text(c))


# =====================================================================
class EsptoolTests(Base):
    def setUp(self):
        super().setUp()
        self.use("esptool")
        self.chip = self.fake / ft.ESP
        self.chip.write_bytes(ft.healthy_esp())

    def corrupt(self):
        self.chip.write_bytes(ft.corrupt(ft.healthy_esp(), 0x10000, 0x20000))

    def test_full_cycle_v4(self):
        c, sid = self.cycle(self.corrupt, self.chip.read_bytes, ft.healthy_esp())
        s = c.memory.session(sid)
        self.assertEqual(s["profile_id"], "esp32_4mb")
        self.assertIn("24:6f:28:aa:bb:cc", s["fingerprint"])
        writes = [a for a in self.calls("esptool") if "write_flash" in a]
        self.assertTrue(writes and all(a.count("keep") == 3 for a in writes))

    def test_full_cycle_v5_uses_dashes(self):
        os.environ["FAKE_ESPTOOL_V5"] = "1"
        self.cycle(self.corrupt, self.chip.read_bytes, ft.healthy_esp())
        used = [x for a in self.calls("esptool") for x in a if x.startswith("--flash") or "flash" in x]
        self.assertTrue(used)
        self.assertFalse([x for x in used if "_" in x and not x.startswith("/")], used)

    def test_bootloader_magic_check(self):
        c = self.conductor()
        c.run()
        self.assertIn("заголовок загрузчика ESP32 (0x1000) на месте", self.text(c))

    def test_probe_is_read_only(self):
        self.conductor().detect_only()
        subs = {x for a in self.calls("esptool") for x in a if x in
                ("version", "flash_id", "flash-id", "read_flash", "write_flash", "erase_flash")}
        self.assertTrue(subs <= {"version", "flash_id", "flash-id"}, subs)

    def test_secure_download_mode_not_bypassed(self):
        os.environ["FAKE_PROTECTED"] = "1"
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "read_failed")
        self.assertIn("защит", self.text(c))

    def test_not_in_bootloader(self):
        self.chip.unlink()
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")
        self.assertIn("режим загрузчика", self.text(c))

    def test_family_parsing(self):
        from dirizher.plugins.esptool_flash import FAMILY_RE
        self.assertEqual(FAMILY_RE.search("ESP32-D0WD-V3").group(1), "ESP32")
        self.assertEqual(FAMILY_RE.search("ESP32-S3 (QFN56)").group(1), "ESP32-S3")
        self.assertEqual(FAMILY_RE.search("ESP32-C3 (QFN32)").group(1), "ESP32-C3")
        self.assertEqual(FAMILY_RE.search("ESP8266EX").group(1), "ESP8266")


# =====================================================================
class BinwalkAndSigrokTests(Base):
    def check(self, data: bytes, expect: list):
        c = self.conductor()
        prof = c.profiles.get("winbond_w25q64")
        return CHECKS["binwalk_expect"](CheckInput(data, True, prof, None, {"expect": expect}, c))

    def test_binwalk_expect_ok_and_fail(self):
        good = ft.healthy_w25q64()
        f = self.check(good, ["uImage header", "squashfs filesystem"])
        self.assertEqual(f.status, "ok", f.message)
        bad = ft.corrupt(good, 0x200000, 4)
        f = self.check(bad, ["uImage header", "Squashfs filesystem"])
        self.assertEqual(f.status, "fail")
        self.assertIn("Squashfs", f.message)
        self.assertIn("restore_reference", f.suggest)

    def test_binwalk_missing_is_info(self):
        os.environ["PATH"] = str(self.tmp / "empty")
        f = self.check(ft.healthy_w25q64(), ["uImage header"])
        self.assertEqual(f.status, "info")

    def test_sigrok_capture_and_decode(self):
        from dirizher.extras import sigrok
        c = self.conductor()
        sid = sigrok.capture(c, self.cfg, decode="spi:clk=D0:mosi=D1:miso=D2:cs=D3", annotation="spi")
        self.assertEqual(self.outcome(c, sid), "captured", self.text(c))
        sr = c.data_dir / "captures" / f"session_{sid:05d}.sr"
        self.assertTrue(sr.exists())
        self.assertIn("EF 40 17", sr.with_suffix(".txt").read_text(encoding="utf-8"))
        self.assertIn("замер", self.text(c))

    def test_sigrok_without_analyzer(self):
        from dirizher.extras import sigrok
        os.environ["FAKE_ABSENT"] = "1"
        c = self.conductor()
        self.assertEqual(self.outcome(c, sigrok.capture(c, self.cfg)), "capture_failed")

    def test_sigrok_scan(self):
        from dirizher.extras import sigrok
        c = self.conductor()
        sid = sigrok.scan(c)
        self.assertEqual(self.outcome(c, sid), "captured")
        self.assertIn("fx2lafw", self.text(c))

    def test_capture_cli_arguments(self):
        from dirizher.extras.sigrok import build_parser
        a = build_parser().parse_args(["--driver", "fx2lafw", "--time", "500", "--decode", "spi"])
        self.assertEqual((a.driver, a.time, a.decode, a.scan), ("fx2lafw", 500, "spi", False))


# =====================================================================
class CommonTests(unittest.TestCase):
    def test_parse_hex_bytes_formats(self):
        from dirizher.plugins._common import parse_hex_bytes
        self.assertEqual(parse_hex_bytes("12 34 ab"), b"\x12\x34\xab")
        self.assertEqual(parse_hex_bytes("0x12 0x34\n0xAB"), b"\x12\x34\xab")
        self.assertEqual(parse_hex_bytes("0x08000000: 12 34\n0x08000002: ab cd"), b"\x12\x34\xab\xcd")
        self.assertEqual(parse_hex_bytes("[0x12, 0x34]"), b"\x12\x34")
        self.assertEqual(parse_hex_bytes("WARN probe_rs: 12 34\nFinished in 0.1s\n12 34"), b"\x12\x34")

    def test_parse_size(self):
        from dirizher.plugins._common import parse_size
        self.assertEqual(parse_size("4MB"), 4 * 1024 * 1024)
        self.assertEqual(parse_size("512KB"), 512 * 1024)
        self.assertIsNone(parse_size("unknown"))

    def test_config_snippet_matches_plugins(self):
        import yaml
        from dirizher.core.registry import discover
        snippet = yaml.safe_load((ROOT / "docs" / "config-snippet-0.3.0.yaml").read_text(encoding="utf-8"))
        found, errors = discover()
        self.assertEqual(errors, [])
        for name, settings in snippet["plugins"].items():
            self.assertIn(name, found)
            self.assertFalse(settings["enabled"])                  # всё выключено по умолчанию

    def test_guard(self):
        from dirizher.plugins._common import guard
        self.assertEqual(guard("x", ["a", "-b"], ["-u"]), ["a", "-b"])
        with self.assertRaises(RuntimeError):
            guard("x", ["--force=yes"], ["--force"])
        with self.assertRaises(RuntimeError):
            guard("openocd", ["-c", "stm32f1x mass_erase 0"], forbidden_words=["mass_erase"])
        guard("openocd", ["-c", "adapter speed 1000"], forbidden_words=["lock"])   # «clock» и пр. не задеты


if __name__ == "__main__":
    unittest.main()
