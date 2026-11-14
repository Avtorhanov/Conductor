"""Тесты Bus Pirate 5 (I2C EEPROM) без железа: эмулятор терминала BP5 на псевдотерминале.

Помощник dirizher/helpers/bp5_i2c.py открывает /dev/pts/N как настоящий /dev/ttyACM0,
плагин запускает его отдельным процессом — проверяется весь путь, кроме самого железа.

Запуск из корня проекта:   python3 -m unittest -v
"""
from __future__ import annotations

import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.helpers import bp5_i2c as helper
from tests.fakes import fake_tools as ft
from tests.fakes.bp5_emulator import BP5Emulator

ROOT = Path(__file__).resolve().parent.parent


class BusPirateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_bp5_"))
        self.emu = BP5Emulator(ft.healthy_24c02(), "24C02")
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        self.cfg["plugins"]["bp5_i2c"] = {"enabled": True, "port": self.emu.port, "eeprom": "24C02",
                                          "address": 0x50, "psu_voltage": 3.3, "pullups": True}
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        self.emu.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conductor(self, answers=None, confirm=True) -> Conductor:
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=False)
        self.opened.append(c)
        return c

    def outcome(self, c, sid):
        return c.memory.session(sid)["outcome"]

    def text(self, c):
        return "\n".join(c.ui.lines)

    def args(self, *op):
        return ["--port", self.emu.port, "--eeprom", "24C02", "--psu", "3.3", *op]

    @staticmethod
    def helper(argv) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return helper.main(argv)

    # ---------- помощник напрямую ----------
    def test_helper_reads_whole_eeprom(self):
        out = self.tmp / "dump.bin"
        self.assertEqual(self.helper(self.args("read", str(out))), 0)
        self.assertEqual(out.read_bytes(), ft.healthy_24c02())
        self.assertIn("m", self.emu.lines)                      # прошёл меню смены режима
        self.assertFalse(self.emu.psu)                          # питание выключено после работы

    def test_helper_write_respects_pages(self):
        img = bytes((i * 7 + 3) & 0xFF for i in range(256))   # все байты другие
        src = self.tmp / "img.bin"
        src.write_bytes(img)
        self.assertEqual(self.helper(self.args("write", str(src))), 0)
        self.assertEqual(bytes(self.emu.mem), img)              # ни один кусок не перешёл границу страницы

    def test_helper_probe_nack(self):
        self.emu.present = False
        self.assertEqual(self.helper(self.args("probe")), 3)

    def test_helper_no_power_means_nack(self):
        self.assertEqual(self.helper(["--port", self.emu.port, "--eeprom", "24C02", "--psu", "0", "probe"]), 3)

    # ---------- через ядро ----------
    def test_full_cycle(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertEqual(c.memory.session(sid)["profile_id"], "eeprom_24c02")
        self.assertIn("рассчитан на 3.3 В", self.text(c))     # напряжение адаптера = psu_voltage BP5
        self.emu.mem[0x10:0x30] = b"\x00" * 0x20                 # порча
        c2 = self.conductor()
        sid2 = c2.run()
        self.assertEqual(self.outcome(c2, sid2), "repaired", self.text(c2))
        self.assertEqual(bytes(self.emu.mem), ft.healthy_24c02())

    def test_not_present(self):
        self.emu.present = False
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "not_detected")

    def test_write_protect_pin_fails_safely(self):
        self.conductor().run()                                   # эталон
        self.emu.mem[0:4] = b"BAD!"
        broken = bytes(self.emu.mem)
        self.emu.wp = True                                       # WP на питании: запись не проходит
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "write_failed", self.text(c))
        self.assertEqual(bytes(self.emu.mem), broken)            # чип в состоянии «до»
        self.assertIn("сверка НЕ совпала", self.text(c))

    def test_unknown_model_unavailable(self):
        self.cfg["plugins"]["bp5_i2c"]["eeprom"] = "24C9999"
        row = next(r for r in self.conductor().plugin_report if r["name"] == "bp5_i2c")
        self.assertFalse(row["active"])

    def test_model_name_normalized(self):
        self.cfg["plugins"]["bp5_i2c"]["eeprom"] = "AT24C02"
        c = self.conductor()
        sid = c.detect_only()
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        self.assertIn('"i2c_address": "0x50"', c.memory.session(sid)["fingerprint"])


if __name__ == "__main__":
    unittest.main()
