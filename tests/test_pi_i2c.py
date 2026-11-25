"""Тесты лезвия pi_i2c (I2C EEPROM через шину самого Raspberry Pi) без железа.

Транспорт помощника dirizher/helpers/i2c_eeprom.py подменяется эмулятором EEPROM
(tests/fakes/i2c_eeprom_emulator.py): напрямую — для логики, отдельным процессом — для пути
«ядро → лезвие → помощник» (состояние эмулятора живёт в JSON-файле между запусками).
"""
from __future__ import annotations

import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.helpers import i2c_eeprom as helper
from dirizher.plugins import pi_i2c
from tests.fakes import fake_tools as ft
from tests.fakes import i2c_eeprom_emulator as emu_mod
from tests.fakes.i2c_eeprom_emulator import EepromEmulator

ROOT = Path(__file__).resolve().parent.parent
EMU = Path(emu_mod.__file__).resolve()


def quiet(fn, *a, **kw):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return fn(*a, **kw)


class HelperLogicTests(unittest.TestCase):
    """Логика 24Cxx на эмуляторе — в том же процессе."""

    def chip(self, emu, **kw):
        return helper.Eeprom(emu, emu.model, emu.address, write_cycle_s=0.2, **kw)

    def test_read_all_24c02(self):
        emu = EepromEmulator(ft.healthy_24c02())
        self.assertEqual(self.chip(emu).read_all(), ft.healthy_24c02())

    def test_read_24c16_uses_block_addresses(self):
        mem = bytes((i * 13) & 0xFF for i in range(2048))
        emu = EepromEmulator(mem, "24C16")
        self.assertEqual(self.chip(emu).read_all(), mem)
        self.assertEqual({a for _, a, _ in emu.log}, set(range(0x50, 0x58)))

    def test_read_24c256_two_byte_address(self):
        mem = bytes((i * 7 + i // 256) & 0xFF for i in range(32768))
        emu = EepromEmulator(mem, "24C256")
        self.assertEqual(self.chip(emu).read_all(), mem)

    def test_write_only_changed_pages_with_wait(self):
        old = ft.healthy_24c02()
        new = bytearray(old)
        new[0x11] ^= 0xFF                    # страница 0x10
        new[0x87] ^= 0xFF                    # страница 0x80
        emu = EepromEmulator(old, busy_polls=3)
        chip = self.chip(emu)
        n = chip.write_all(bytes(new), chip.read_all())
        self.assertEqual(n, 2)
        self.assertEqual(bytes(emu.mem), bytes(new))
        self.assertGreaterEqual(chip.polls, 2 * 4)        # ждал, пока микросхема занята

    def test_write_whole_image_respects_pages(self):
        img = bytes((i * 7 + 3) & 0xFF for i in range(256))
        emu = EepromEmulator(b"\xff" * 256)
        chip = self.chip(emu)
        chip.write_all(img, chip.read_all())
        self.assertEqual(bytes(emu.mem), img)              # ни одна страница не завернулась

    def test_too_big_page_would_wrap(self):
        """Страница больше паспортной портит данные — поэтому по умолчанию берётся наименьшая."""
        img = bytes((i * 7 + 3) & 0xFF for i in range(256))
        emu = EepromEmulator(b"\xff" * 256, page=8)
        chip = self.chip(emu, page=16)
        chip.write_all(img, chip.read_all())
        self.assertNotEqual(bytes(emu.mem), img)

    def test_busy_forever_is_clear_error(self):
        emu = EepromEmulator(b"\xff" * 256, busy_polls=10 ** 9)
        chip = helper.Eeprom(emu, "24C02", write_cycle_s=0.02)
        with self.assertRaisesRegex(RuntimeError, "не закончила запись"):
            chip.write_all(b"\x00" * 256, b"\xff" * 256)

    def test_wrong_size_refused(self):
        emu = EepromEmulator(b"\xff" * 256)
        with self.assertRaisesRegex(RuntimeError, "размер образа"):
            self.chip(emu).write_all(b"\x00" * 128, b"\xff" * 256)

    def test_scan_and_probe_output(self):
        emu = EepromEmulator(b"\xff" * 1024, "24C08")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = helper.main(["--eeprom", "24C08", "probe"], bus=emu)
        self.assertEqual(code, 0)
        self.assertIn("ACK=0x50,0x51,0x52,0x53", out.getvalue())
        self.assertTrue(all(op == "r" for op, _, _ in emu.log))     # probe только читает

    def test_probe_nack(self):
        emu = EepromEmulator(b"\xff" * 256, present=False)
        self.assertEqual(quiet(helper.main, ["--eeprom", "24C02", "probe"], bus=emu), 3)

    def test_missing_bus_is_clear_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = helper.main(["--bus", "/nonexistent/i2c-9", "--eeprom", "24C02", "probe"])
        self.assertEqual(code, 2)
        self.assertIn("raspi-config", err.getvalue())

    def test_kernel_struct_sizes(self):
        """struct i2c_msg и i2c_rdwr_ioctl_data — как sizeof в ядре (64 бит: 16 и 16, 32 бит: 12 и 8)."""
        import struct
        ptr = struct.calcsize("P")
        self.assertEqual(struct.calcsize(helper.MSG_FMT), 16 if ptr == 8 else 12)
        self.assertEqual(struct.calcsize(helper.RDWR_FMT), 16 if ptr == 8 else 8)

    def test_linux_transport_packs_messages(self):
        """LinuxI2C без шины: подменный ioctl разбирает структуры, как ядро (ctypes — только в тесте)."""
        import ctypes
        import errno
        import fcntl
        import struct
        seen = []

        def fake_ioctl(fd, req, arg):
            self.assertEqual(req, helper.I2C_RDWR)
            msgs_ptr, n = struct.unpack_from("@PI", bytes(arg))
            size = struct.calcsize(helper.MSG_FMT)
            raw = ctypes.string_at(msgs_ptr, size * n)
            for i in range(n):
                addr, flags, ln, buf = struct.unpack_from(helper.MSG_FMT, raw, i * size)
                if addr == 0x57:
                    raise OSError(errno.EREMOTEIO, "Remote I/O error")
                if flags & helper.I2C_M_RD:
                    ctypes.memmove(buf, bytes(range(0xA0, 0xA0 + ln)), ln)
                    seen.append(("r", addr, ln))
                else:
                    seen.append(("w", addr, ctypes.string_at(buf, ln)))
            return 0

        bus = helper.LinuxI2C.__new__(helper.LinuxI2C)
        bus.fd = -1
        with mock.patch.object(fcntl, "ioctl", fake_ioctl):
            self.assertEqual(bus.write_read(0x50, b"\x00\x10", 3), b"\xa0\xa1\xa2")
            bus.write(0x51, b"\x05\x01\x02")
            with self.assertRaises(helper.NoAck):
                bus.read(0x57, 1)
        self.assertEqual(seen, [("w", 0x50, b"\x00\x10"), ("r", 0x50, 3), ("w", 0x51, b"\x05\x01\x02")])

    def test_bad_page_refused(self):
        with self.assertRaisesRegex(RuntimeError, "степень двойки"):
            helper.Eeprom(EepromEmulator(b"\xff" * 256), "24C02", page=12)


class PiI2CConductorTests(unittest.TestCase):
    """Ядро → лезвие → помощник отдельным процессом → эмулятор."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_pi_i2c_"))
        self.state = self.tmp / "eeprom.json"
        self.set_emu(EepromEmulator(ft.healthy_24c02()))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        # шина — любой существующий файл: available() проверяет только, что путь есть
        self.cfg["plugins"]["pi_i2c"] = {"enabled": True, "bus": str(self.state), "eeprom": "24C02"}
        self.opened = []
        patcher = mock.patch.object(pi_i2c.PiI2CPlugin, "helper_cmd",
                                    lambda _self: [sys.executable, str(EMU), "--state", str(self.state), "--"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        for c in self.opened:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def set_emu(self, emu):
        emu_mod.save_state(self.state, emu)

    def emu(self) -> EepromEmulator:
        return emu_mod.load_state(self.state)[0]

    def conductor(self, answers=None, confirm=True) -> Conductor:
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=False)
        self.opened.append(c)
        return c

    def outcome(self, c, sid):
        return c.memory.session(sid)["outcome"]

    def text(self, c):
        return "\n".join(c.ui.lines)

    def test_schema_and_available(self):
        row = next(r for r in self.conductor().plugin_report if r["name"] == "pi_i2c")
        self.assertTrue(row["active"], row)
        keys = {f["key"] for f in pi_i2c.PiI2CPlugin.schema()}
        self.assertTrue({"bus", "eeprom", "address", "page_size", "programmer_voltage"} <= keys)

    def test_missing_bus_unavailable_with_advice(self):
        self.cfg["plugins"]["pi_i2c"]["bus"] = str(self.tmp / "i2c-9")
        row = next(r for r in self.conductor().plugin_report if r["name"] == "pi_i2c")
        self.assertFalse(row["active"])
        self.assertIn("raspi-config", row["status"])

    def test_full_cycle_and_repair(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertEqual(c.memory.session(sid)["profile_id"], "eeprom_24c02")
        self.assertIn("рассчитан на 3.3 В", self.text(c))
        emu = self.emu()
        emu.mem[0x10:0x30] = b"\x00" * 0x20                      # порча
        self.set_emu(emu)
        c2 = self.conductor()
        sid2 = c2.run()
        self.assertEqual(self.outcome(c2, sid2), "repaired", self.text(c2))
        emu = self.emu()
        self.assertEqual(bytes(emu.mem), ft.healthy_24c02())
        self.assertEqual(emu.page_writes, 4)                      # 0x20 байт = 4 страницы по 8

    def test_not_present(self):
        emu = self.emu()
        emu.present = False
        self.set_emu(emu)
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")

    def test_write_protect_fails_safely(self):
        self.conductor().run()                                    # эталон
        emu = self.emu()
        emu.mem[0:4] = b"BAD!"
        emu.wp = True
        broken = bytes(emu.mem)
        self.set_emu(emu)
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "write_failed", self.text(c))
        self.assertEqual(bytes(self.emu().mem), broken)
        self.assertIn("сверка НЕ совпала", self.text(c))

    def test_inspect_warns_about_extra_addresses(self):
        mem = bytes(1024)
        self.set_emu(EepromEmulator(mem, "24C08"))                # на шине 24C08, а выбрана 24C02
        c = Conductor(self.cfg, AutoUI(), sim=False, dry_run=True)
        self.opened.append(c)
        sid = c.run()
        self.assertNotEqual(self.outcome(c, sid), "not_detected", self.text(c))
        self.assertIn("отвечают ещё адреса 0x51, 0x52, 0x53", self.text(c))
        self.assertIn("i2c_blocks", (self.tmp / "data" / "logs" / f"session_{sid:05d}.log").read_text("utf-8"))

    def test_model_from_profile_tool(self):
        """Модель приходит из профиля (tool: pi_i2c: eeprom), в настройках её нет."""
        from dirizher.core.profiles import ProfileDB
        db = ProfileDB(ROOT / "profiles")
        prof = next(p for p in db.profiles if p.id == "eeprom_24c256")
        self.assertEqual(prof.tool["pi_i2c"]["eeprom"], "24C256")
        self.assertEqual(pi_i2c.PiI2CPlugin.image_size({"eeprom": "24C256"}), 32768)


if __name__ == "__main__":
    unittest.main()
