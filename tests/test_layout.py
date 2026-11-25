"""Карта разделов (core/layout.py) и частичное восстановление restore_regions. Без железа.

Запуск:  python3 -m unittest tests.test_layout -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from dirizher.core import layout
from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.profiles import Profile
from dirizher.core.ui import AutoUI
from dirizher.interface import guide
from dirizher.plugins.simulator import SimulatorPlugin

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = 0x7FE000          # раздел «Параметры» учебной платы (restore: false)
UNIQUE = SimulatorPlugin.UNIQUE_OFFSET


class NormalizeTests(unittest.TestCase):
    def test_defaults_and_order(self):
        rows = layout.normalize([
            {"id": "mac", "title": "MAC", "offset": 0x3000, "length": 0x1000, "kind": "unique"},
            {"id": "boot", "title": "Загрузчик", "offset": 0, "end": 0x1000, "kind": "boot"},
            {"id": "cfg", "title": "Настройки", "offset": "0x1000", "length": 0x1000, "kind": "config"},
            {"id": "cal", "title": "Калибровки", "offset": 0x2000, "length": 0x1000, "kind": "calibration",
             "restore": True},
        ], size=0x4000)
        self.assertEqual([r["id"] for r in rows], ["boot", "cfg", "cal", "mac"])
        by = {r["id"]: r for r in rows}
        self.assertTrue(by["boot"]["restore"] and not by["boot"]["preserve"])
        self.assertFalse(by["cfg"]["restore"])                  # настройки из эталона — только явно
        self.assertTrue(by["cal"]["restore"])                   # явно разрешено
        self.assertTrue(by["mac"]["preserve"] and not by["mac"]["restore"])
        self.assertEqual(layout.preserve_regions(rows), [{"offset": 0x3000, "length": 0x1000, "why": "MAC"}])

    def test_mistakes_are_explained(self):
        base = {"id": "a", "title": "A", "offset": 0, "length": 0x100}
        cases = {
            "пересекается": [base, dict(base, id="b", offset=0x80)],
            "выходит за размер": [dict(base, length=0x2000)],
            "ровно один": [dict(base, end=0x100)],
            "kind": [dict(base, kind="bios")],
            "повторяется": [base, dict(base, offset=0x200)],
            "preserve и restore": [dict(base, preserve=True, restore=True)],
            "лишние поля": [dict(base, lenght=1)],
            "нет title": [dict(base, title="")],
        }
        for needle, raw in cases.items():
            with self.subTest(needle):
                with self.assertRaises(ValueError) as e:
                    layout.normalize(raw, size=0x1000)
                self.assertIn(needle, str(e.exception))

    def test_profile_merges_preserve_and_needs_layout_for_recipe(self):
        d = {"id": "x", "name": "X", "class": "memory.spi_nor", "size": 0x2000,
             "preserve": [{"offset": 0x1000, "length": 0x800, "why": "старое поле"}],
             "layout": [{"id": "u", "title": "Серийник", "offset": 0x1000, "length": 0x800, "kind": "unique"},
                        {"id": "c", "title": "Калибровки", "offset": 0x1800, "length": 0x800, "kind": "calibration",
                         "preserve": True}]}
        p = Profile.from_dict(d, Path("x.yaml"))
        self.assertEqual([(r["offset"], r["length"]) for r in p.preserve], [(0x1000, 0x800), (0x1800, 0x800)])
        with self.assertRaises(ValueError) as e:
            Profile.from_dict({"id": "y", "name": "Y", "class": "memory.spi_nor",
                               "recipes": [{"id": "r", "action": "write_reference_regions"}]}, Path("y.yaml"))
        self.assertIn("layout", str(e.exception))

    def test_old_profiles_have_no_layout(self):
        c_cfg = load_config(ROOT / "config.yaml")
        from dirizher.core.profiles import ProfileDB
        db = ProfileDB(c_cfg["profiles_dir"])
        self.assertEqual(db.errors, [])
        self.assertEqual(db.get("winbond_w25q64").layout, [])
        self.assertEqual(len(db.get("example_sim_board").layout), 5)


class SimBoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_layout_"))
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

    def reference(self):
        c = self.conductor(["yes"])
        c.plugin("simulator").do_reset()
        c.run()

    def broken(self, c, settings: bool = True) -> bytes:
        """Учебная порча (прошивка) + изменённый параметр (раздел без restore) + свой MAC."""
        sim = c.plugin("simulator")
        sim.do_corrupt()
        buf = bytearray(sim.image.read_bytes())
        if settings:
            buf[SETTINGS] = 7                                   # «Задержка загрузки» = 7 с — настройка этой платы
        other = b"MAC=02:00:00:AB:CD:EF;SN=DRZ-999999"
        buf[UNIQUE:UNIQUE + len(other)] = other
        sim.write(None, bytes(buf))
        return bytes(buf)

    def test_corruption_names_the_region(self):
        self.reference()
        c = self.conductor(dry_run=True)
        self.broken(c)
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertIn("По разделам: «Прошивка» — 157 секторов, «Параметры» — 1 сектор.", text)
        self.assertNotIn("«MAC и серийник» —", text)           # уникальное не считается отличием
        self.assertIn("layout: разделов в карте: 5, загрузчик на месте", text)
        detail = guide.session_detail(c, sid)
        self.assertEqual([(r["title"], r["sectors"], r["restore"]) for r in detail["regions"]],
                         [("Прошивка", 157, True), ("Параметры", 1, False)])

    def test_restore_regions_repairs_only_damaged(self):
        self.reference()
        c = self.conductor([1, "yes"])                          # способ — второй: только разделы
        broken = self.broken(c)
        sid = c.run()
        self.assertEqual(c.memory.session(sid)["outcome"], "repaired")
        self.assertEqual(c.memory.session(sid)["recipe_id"], "restore_regions")
        flash = c.plugin("simulator").image.read_bytes()
        healthy = SimulatorPlugin.healthy()
        self.assertEqual(flash[:SETTINGS], healthy[:SETTINGS])          # прошивка — из эталона
        self.assertEqual(flash[SETTINGS:], broken[SETTINGS:])            # параметры и MAC — свои
        self.assertEqual(flash[SETTINGS], 7)
        text = "\n".join(c.ui.lines)
        self.assertIn("беру из эталона: «Прошивка» (0x1000…0x301000), отличалось 157 секторов", text)
        self.assertIn("не трогаю «Параметры» (отличается 1 сектор)", text)
        # защита та же: копия «до», план, слово, сверка всего чипа, копия «после»
        kinds = [s["kind"] for s in c.memory.snapshots(sid)]
        self.assertEqual(kinds[0], "before")
        self.assertIn("written", kinds)
        self.assertIn("after", kinds)
        self.assertTrue(c.memory.snapshot(sid, "after")["verified"])
        self.assertTrue(any(x.startswith(" ! Записать разделы из эталона") for x in c.ui.lines))
        self.assertEqual(c.store.get(c.memory.snapshot(sid, "before")["sha"]), broken)

    def test_restore_regions_needs_the_word(self):
        self.reference()
        c = self.conductor([1], confirm=False)
        broken = self.broken(c)
        sid = c.run()
        self.assertEqual(c.memory.session(sid)["outcome"], "cancelled")
        self.assertEqual(c.plugin("simulator").image.read_bytes(), broken)

    def test_restore_regions_dry_run_and_flaky_blocked(self):
        self.reference()
        c = self.conductor([1], dry_run=True)
        broken = self.broken(c)
        sid = c.run()
        self.assertEqual(c.memory.session(sid)["outcome"], "dry_run")
        self.assertEqual(c.plugin("simulator").image.read_bytes(), broken)
        c = self.conductor([1])
        c.plugin("simulator").do_flaky()
        sid = c.run()
        self.assertIn(c.memory.session(sid)["outcome"], ("blocked", "no_action"))
        self.assertEqual(c.plugin("simulator").image.read_bytes(), broken)

    def test_only_non_restorable_differs(self):
        """Испорчены только «Параметры» — restore_regions не предлагается, полный эталон — да."""
        self.reference()
        c = self.conductor(dry_run=True)
        sim = c.plugin("simulator")
        sim.do_reset()
        buf = bytearray(sim.image.read_bytes())
        buf[SETTINGS] = 3
        sim.write(None, bytes(buf))
        c.run()
        text = "\n".join(c.ui.lines)
        self.assertIn("По разделам: «Параметры» — 1 сектор.", text)
        self.assertNotIn("Что сделать?", text)
        self.assertIn("способ: Записать эталонную прошивку", text)

    def test_erased_boot_is_a_finding(self):
        c = self.conductor()
        sim = c.plugin("simulator")
        sim.do_reset()
        buf = bytearray(sim.image.read_bytes())
        buf[0x10:0x1000] = b"\xff" * (0x1000 - 0x10)     # подпись FWIM цела (плата узнаётся), остальное стёрто
        buf[0x4:0x10] = b"\xff" * 0xC
        sim.write(None, bytes(buf))
        c.run()
        text = "\n".join(c.ui.lines)
        self.assertNotIn("раздел загрузчика пустой", text)    # подпись на месте — раздел не пуст
        from dirizher.core.checks import layout_check, CheckInput
        prof = c.profiles.get("example_sim_board")
        f = layout_check(CheckInput(b"\xff" * prof.size, True, prof, None, {}, c))
        self.assertEqual(f.status, "fail")
        self.assertIn("«Заголовок прошивки» (0x0…0x1000)", f.message)
        self.assertIn("restore_regions", f.suggest)

    def test_outside_map_is_left_alone(self):
        rows = layout.normalize([{"id": "a", "title": "A", "offset": 0, "length": 0x1000, "kind": "data"}], 0x3000)
        cur = bytearray(b"\x11" * 0x3000)
        ref = bytearray(b"\x22" * 0x3000)
        image, taken, left = layout.build(bytes(cur), bytes(ref), rows)
        self.assertEqual(image[:0x1000], bytes(ref[:0x1000]))
        self.assertEqual(image[0x1000:], bytes(cur[0x1000:]))
        self.assertEqual([r["title"] for r in taken], ["A"])
        self.assertEqual([(r["title"], r["sectors"]) for r in left], [("вне карты", 2)])

    def test_board_yaml_layout_is_valid(self):
        d = yaml.safe_load((ROOT / "profiles/boards/example_sim_board.yaml").read_text(encoding="utf-8"))
        self.assertIn("restore_regions", [r["id"] for r in d["recipes"]])
        self.assertEqual(d["recipes"][0]["id"], "restore_reference")   # прежний способ — первым


if __name__ == "__main__":
    unittest.main()
