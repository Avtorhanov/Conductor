"""Карта параметров и сценарий «Настройка» — без железа: симулятор и эмулятор прибора Modbus RTU.

Запуск:  python3 -m unittest tests.test_params -v
"""
from __future__ import annotations

import shutil
import struct
import tempfile
import unittest
from pathlib import Path

from dirizher.core import params as P
from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.profiles import Profile
from dirizher.core.ui import AutoUI
from dirizher.plugins.simulator import SimulatorPlugin
from tests.fakes.modbus_emulator import THERMO, ModbusEmulator

ROOT = Path(__file__).resolve().parent.parent


# ======================= карта параметров =======================
class ParamMapTests(unittest.TestCase):
    def norm(self, items, bus=None):
        return P.normalize(items, bus)

    def test_mistakes_are_reported(self):
        bad = [
            ([{"id": "A", "title": "x", "offset": 0}], "id"),
            ([{"id": "a", "title": "x", "offset": 0}, {"id": "a", "title": "y", "offset": 4}], "повторяется"),
            ([{"id": "a", "title": "x", "offset": 0, "type": "u12"}], "type"),
            ([{"id": "a", "title": "x"}], "offset или register"),
            ([{"id": "a", "title": "x", "offset": 0, "type": "u16be"},
              {"id": "b", "title": "y", "offset": 1}], "пересекается"),
            ([{"id": "a", "title": "x", "offset": 0, "min": 5, "max": 1}], "min больше max"),
            ([{"id": "a", "title": "x", "offset": 0, "scale": 0}], "scale"),
            ([{"id": "a", "offset": 0}], "title"),
        ]
        for items, text in bad:
            with self.subTest(text=text):
                with self.assertRaises(ValueError) as cm:
                    self.norm(items)
                self.assertIn(text, str(cm.exception))
        with self.assertRaises(ValueError):
            self.norm([{"id": "a", "title": "x", "register": 3}], {"start": 10})   # регистр раньше окна

    def test_register_is_counted_from_bus_start(self):
        p = self.norm([{"id": "a", "title": "x", "register": 13, "type": "u16be"}], {"start": 10})[0]
        self.assertEqual(p["offset"], 6)

    def test_every_type_roundtrips(self):
        values = {"u8": 200, "i8": -100, "u16be": 60000, "u16le": 60000, "i16be": -30000, "i16le": -30000,
                  "u32be": 4000000000, "u32le": 4000000000, "i32be": -2000000000, "i32le": -2000000000,
                  "f32be": 1.5, "f32le": -2.25}
        for t, v in values.items():
            with self.subTest(type=t):
                p = self.norm([{"id": "a", "title": "x", "offset": 2, "type": t}])
                img, lines, errors = P.apply_changes(bytes(8), p, {"a": v})
                self.assertEqual(errors, [])
                self.assertEqual(P.decode(img, p[0]), v)
                self.assertEqual(img[:2], b"\x00\x00")                     # соседние байты не задеты

    def test_scale_endianness_and_text(self):
        p = self.norm([{"id": "t", "title": "Граница", "offset": 0, "type": "i16be", "scale": 0.1, "unit": "°C",
                        "min": -50, "max": 600}])
        img, lines, errors = P.apply_changes(bytes(2), p, {"t": "-12,5"})
        self.assertEqual(errors, [])
        self.assertEqual(img, struct.pack(">h", -125))
        self.assertEqual(lines, ["Граница: 0.0 °C → -12.5 °C"])
        self.assertEqual(P.read_all(img, p)[0]["text"], "-12.5 °C")

    def test_bad_values_never_change_the_image(self):
        p = self.norm([
            {"id": "t", "title": "Граница", "offset": 0, "type": "i16be", "scale": 0.5, "min": -50, "max": 600},
            {"id": "ro", "title": "Версия", "offset": 2, "type": "u8", "writable": False},
            {"id": "u", "title": "Единицы", "offset": 3, "type": "u8", "choices": {0: "C", 1: "F"}},
            {"id": "b", "title": "Байт", "offset": 4, "type": "u8"},
        ])
        image = bytes(5)
        cases = {"t": 700, "ro": 1, "u": 5, "b": 300}
        for pid, value in cases.items():
            with self.subTest(pid=pid):
                img, lines, errors = P.apply_changes(image, p, {"t": 10, pid: value})
                self.assertTrue(errors)
                self.assertEqual(img, image)                               # ни одного байта
        for raw in ("abc", "nan", "inf", "0.3"):                            # 0.3 — не по шагу 0.5
            img, _, errors = P.apply_changes(image, p, {"t": raw})
            self.assertTrue(errors, raw)
        _, _, errors = P.apply_changes(image, p, {"nope": 1})
        self.assertIn("нет параметра", errors[0])

    def test_profile_checks_param_bounds(self):
        d = {"id": "x", "name": "x", "class": "memory.x", "size": 4,
             "params": [{"id": "a", "title": "a", "offset": 3, "type": "u16be"}]}
        with self.assertRaises(ValueError):
            Profile.from_dict(d, Path("x.yaml"))
        d["size"] = 8
        self.assertEqual(Profile.from_dict(d, Path("x.yaml")).params[0]["offset"], 3)


# ======================= «Настройка» на симуляторе =======================
class SimTuneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_tune_"))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conductor(self, answers=None, confirm=True, dry_run=False):
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=True, dry_run=dry_run)
        self.opened.append(c)
        return c

    def flash(self, c) -> bytes:
        return c.plugin("simulator").image.read_bytes()

    def outcome(self, c, sid):
        return c.memory.session(sid)["outcome"]

    def test_read_params_only_reads(self):
        c = self.conductor()
        c.plugin("simulator").do_reset()
        before = self.flash(c)
        sid = c.read_params()
        self.assertEqual(self.outcome(c, sid), "params_read")
        self.assertEqual(self.flash(c), before)
        self.assertTrue(any("Задержка загрузки: 255 с" in ln for ln in c.ui.lines))
        self.assertTrue(any("Регион радио: не задан (255)" in ln for ln in c.ui.lines))

    def test_tune_writes_only_the_fields_and_can_be_rolled_back(self):
        c = self.conductor()
        c.plugin("simulator").do_reset()
        healthy = SimulatorPlugin.healthy()
        sid = c.tune({"boot_delay": 3, "region": 1})
        self.assertEqual(self.outcome(c, sid), "tuned")
        now = self.flash(c)
        self.assertEqual(now[0x7FE000:0x7FE002], b"\x03\x01")
        self.assertEqual(now[:0x7FE000], healthy[:0x7FE000])
        self.assertEqual(now[0x7FE002:], healthy[0x7FE002:])
        before = c.memory.snapshot(sid, "before")
        self.assertEqual(c.store.get(before["sha"]), healthy)               # копия «до» — прежние настройки
        self.assertTrue(c.memory.snapshot(sid, "after")["verified"])
        c2 = self.conductor()
        rb = c2.rollback(sid)
        self.assertEqual(self.outcome(c2, rb), "rolled_back")
        self.assertEqual(self.flash(c2), healthy)

    def test_tune_guards(self):
        c = self.conductor()
        c.plugin("simulator").do_reset()
        healthy = self.flash(c)
        self.assertEqual(self.outcome(c, c.tune({"boot_delay": 99})), "blocked")            # вне 0…30
        self.assertEqual(self.outcome(c, c.tune({"fw_format": 2})), "blocked")              # только чтение
        self.assertEqual(self.outcome(c, c.tune({"region": 255})), "no_change")
        self.assertEqual(self.flash(c), healthy)
        c2 = self.conductor(confirm=False)
        self.assertEqual(self.outcome(c2, c2.tune({"boot_delay": 5})), "cancelled")
        c3 = self.conductor(dry_run=True)
        self.assertEqual(self.outcome(c3, c3.tune({"boot_delay": 5})), "dry_run")
        c4 = self.conductor()
        c4.plugin("simulator").do_flaky()
        self.assertEqual(self.outcome(c4, c4.tune({"boot_delay": 5})), "blocked")          # чтения не совпали
        c4.plugin("simulator").do_steady()
        self.assertEqual(self.flash(c4), healthy)
        with self.assertRaises(ValueError):
            c4.tune({})

    def test_no_map_no_write(self):
        c = self.conductor()
        sim = c.plugin("simulator")
        sim.JEDEC = "C84018"                                    # чип без профиля и без карты
        sid = c.read_params()
        self.assertEqual(self.outcome(c, sid), "no_params")
        self.assertIsNone(c.memory.snapshot(sid, "before"))      # даже не читали


# ======================= Modbus RTU: эмулятор прибора =======================
class ModbusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_modbus_"))
        self.emu = ModbusEmulator()
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        self.cfg["plugins"]["modbus_rtu"] = {"enabled": True, "port": self.emu.port, "timeout": 0.4, "retries": 1}
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        self.emu.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conductor(self, answers=None, confirm=True):
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=False)
        self.opened.append(c)
        return c

    def outcome(self, c, sid):
        return c.memory.session(sid)["outcome"]

    def test_crc_and_grouping(self):
        from skills.modbus_rtu import rtu
        self.assertEqual(rtu.crc16(bytes.fromhex("010300000001")), bytes.fromhex("840A"))   # пример из спецификации
        self.assertEqual(rtu.runs([(2, 1), (3, 2), (7, 3)]), [(2, [1, 2]), (7, [3])])

    def test_skill_is_loaded_and_detects_with_read_only(self):
        c = self.conductor()
        row = next(r for r in c.plugin_report if r["name"] == "modbus_rtu")
        self.assertTrue(row["active"])
        self.assertEqual(row["skill"], "modbus_rtu")
        c.detect_only()
        self.assertEqual(self.emu.writes, [])
        self.assertTrue(all(req[2:4] == "03" for req in self.emu.requests))             # только функция 3

    def test_change_range_full_cycle(self):
        c = self.conductor()
        sid = c.read_params()
        self.assertEqual(self.outcome(c, sid), "params_read")
        self.assertFalse(any("Вы проверили по даташиту" in ln for ln in c.ui.lines))   # RS-485: вопроса нет
        self.assertTrue(any("Верхняя граница диапазона: 400.0 °C" in ln for ln in c.ui.lines))
        sid = c.tune({"range_low": -50, "range_high": "600"})
        self.assertEqual(self.outcome(c, sid), "tuned")
        self.assertEqual(self.emu.writes, [(2, (-500) & 0xFFFF), (3, 6000)])               # только два регистра
        self.assertEqual(self.emu.regs[:2], THERMO[:2])
        c2 = self.conductor()
        self.assertEqual(self.outcome(c2, c2.rollback(sid)), "rolled_back")
        self.assertEqual(self.emu.regs, THERMO)

    def test_function_16(self):
        self.cfg["plugins"]["modbus_rtu"]["write_function"] = 16
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.tune({"range_low": 10, "range_high": 300})), "tuned")
        self.assertTrue(any(req[2:4] == "10" for req in self.emu.requests))
        self.assertEqual(self.emu.regs[2:4], [100, 3000])

    def test_device_that_ignores_write_fails_verify_and_offers_return(self):
        self.emu.stubborn = {3}
        c = self.conductor(["yes"])                          # «вернуть прежние настройки?» — да
        sid = c.tune({"range_low": -50, "range_high": 600})
        self.assertEqual(self.outcome(c, sid), "tune_failed")
        self.assertEqual(self.emu.regs, THERMO)             # возврат копии «до»

    def test_silent_or_other_address_is_not_detected(self):
        self.emu.silent = True
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.read_params()), "not_detected")
        self.emu.silent = False
        self.cfg["plugins"]["modbus_rtu"]["unit"] = 9
        c2 = self.conductor()
        self.assertEqual(self.outcome(c2, c2.read_params()), "not_detected")

    def test_live_register_blocks_writing(self):
        self.emu.live = 4                                      # «фильтр» меняется сам — окно выбрано неверно
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.tune({"range_high": 600})), "blocked")
        self.assertEqual(self.emu.writes, [])

    def test_reference_settings_keep_own_address(self):
        """Эталон настроек с одного прибора → на другой; адрес и скорость остаются свои (preserve)."""
        c = self.conductor(["yes"])                            # «исправно? сохранить эталон» — да
        self.assertEqual(self.outcome(c, c.run()), "healthy")
        self.emu.regs = [0, 0, 100, 2000, 2, 7, 4, 0x0102]     # «другой» прибор: свой диапазон и адрес
        c2 = self.conductor(["yes"])                           # «заработало?» — да
        sid = c2.run()
        self.assertEqual(self.outcome(c2, sid), "repaired")
        self.assertEqual(self.emu.regs, [0, 0, 0, 4000, 2, 7, 4, 0x0102])
        self.assertEqual(sorted({r for r, _ in self.emu.writes}), [2, 3])


if __name__ == "__main__":
    unittest.main()
