"""Автотесты на симуляторе: весь цикл без железа.

Запуск из корня проекта:   python3 -m unittest -v
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.plugins.simulator import SimulatorPlugin

ROOT = Path(__file__).resolve().parent.parent


class SimulatorScenarios(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_test_"))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = self.tmp / "mirror"
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- помощники ----------
    def conductor(self, answers=None, confirm=True, dry_run=False):
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=True, dry_run=dry_run)
        self.opened.append(c)
        return c

    def sim(self, c) -> SimulatorPlugin:
        return c.plugin("simulator")

    def flash(self, c) -> bytes:
        return self.sim(c).image.read_bytes()

    def outcome(self, c, sid) -> str:
        return c.memory.session(sid)["outcome"]

    def make_reference(self):
        c = self.conductor(["yes"])
        self.sim(c).do_reset()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy")
        return c

    # ---------- сценарии ----------
    def test_healthy_creates_reference(self):
        c = self.make_reference()
        refs = c.memory.references("memory.spi_nor:EF4017")
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["profile_id"], "example_sim_board")

    def test_repair_corrupted_preserves_unique_data(self):
        self.make_reference()
        c = self.conductor([0, "yes"])                        # способ — первый (весь эталон); «заработало?» — да
        self.sim(c).do_corrupt()
        broken = self.flash(c)
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "repaired")
        self.assertEqual(self.flash(c), SimulatorPlugin.healthy())
        # копия «до» хранит именно испорченное состояние
        before = c.memory.snapshot(sid, "before")
        self.assertEqual(c.store.get(before["sha"]), broken)
        # обучение: успех рецепта учтён
        self.assertEqual(c.memory.recipe_stats("example_sim_board", "restore_reference"), (1, 0))

    def test_unique_area_kept_from_current_chip(self):
        self.make_reference()
        c = self.conductor([0, "yes"])                        # способ — первый (весь эталон)
        sim = self.sim(c)
        sim.do_corrupt()
        buf = bytearray(self.flash(c))
        other = b"MAC=02:00:00:AB:CD:EF;SN=DRZ-999999"   # «другая плата» с тем же софтом
        off = SimulatorPlugin.UNIQUE_OFFSET
        buf[off:off + len(other)] = other
        sim.write(None, bytes(buf))
        c.run()
        result = self.flash(c)
        self.assertEqual(result[off:off + len(other)], other)          # свой MAC сохранён
        self.assertEqual(result[:off], SimulatorPlugin.healthy()[:off])  # остальное — из эталона

    def test_erased_chip_restored_from_same_chip_reference(self):
        self.make_reference()
        c = self.conductor(["yes", "yes"])   # «эталон от такой же модели?» — да; «заработало?» — да
        self.sim(c).do_erase()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "repaired")
        self.assertEqual(self.flash(c), SimulatorPlugin.healthy())
        self.assertTrue(any("пустая" in line for line in c.ui.lines))  # предупреждение про MAC

    def test_flaky_contact_blocks_writing(self):
        self.make_reference()
        c = self.conductor()
        sim = self.sim(c)
        sim.do_corrupt()
        broken = self.flash(c)
        sim.do_flaky()
        sid = c.run()
        self.assertIn(self.outcome(c, sid), ("blocked", "no_action"))
        self.assertEqual(self.flash(c), broken)                       # чип не тронут
        self.assertFalse(c.memory.snapshot(sid, "before")["verified"])

    def test_dry_run_writes_nothing(self):
        self.make_reference()
        c = self.conductor(dry_run=True)
        self.sim(c).do_corrupt()
        broken = self.flash(c)
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "dry_run")
        self.assertEqual(self.flash(c), broken)

    def test_refused_confirmation_writes_nothing(self):
        self.make_reference()
        c = self.conductor(confirm=False)
        self.sim(c).do_corrupt()
        broken = self.flash(c)
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "cancelled")
        self.assertEqual(self.flash(c), broken)

    def test_rollback_restores_before_and_is_itself_reversible(self):
        self.make_reference()
        c = self.conductor([0, "yes"])
        self.sim(c).do_corrupt()
        broken = self.flash(c)
        repair_sid = c.run()
        self.assertEqual(self.flash(c), SimulatorPlugin.healthy())
        c2 = self.conductor()
        rb_sid = c2.rollback(repair_sid)
        self.assertEqual(self.outcome(c2, rb_sid), "rolled_back")
        self.assertEqual(self.flash(c2), broken)
        # перед откатом сохранено исправное состояние — откат тоже можно отменить
        before_rb = c2.memory.snapshot(rb_sid, "before")
        self.assertEqual(c2.store.get(before_rb["sha"]), SimulatorPlugin.healthy())

    def test_failed_feedback_lowers_recipe(self):
        self.make_reference()
        c = self.conductor([0, "no"])                         # способ — первый; «заработало?» — нет
        self.sim(c).do_corrupt()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "not_repaired")
        self.assertEqual(c.memory.recipe_stats("example_sim_board", "restore_reference"), (0, 1))

    def test_backups_are_mirrored_and_integrity_checked(self):
        c = self.make_reference()
        sha = c.memory.snapshot(1, "before")["sha"]
        primary = c.store._path(c.store.root, sha)
        primary.chmod(0o644)
        primary.write_bytes(b"broken")                         # порча основной копии
        self.assertEqual(c.store.get(sha), SimulatorPlugin.healthy())  # достали из зеркала

    def test_voltage_mismatch_blocks_everything(self):
        self.cfg["safety"]["programmer_voltage"] = 1.8         # «переходник на 1.8 В» на чипе 3.3 В
        c = self.conductor()
        self.sim(c).do_reset()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "blocked_voltage")
        self.assertIsNone(c.memory.snapshot(sid, "before"))   # даже чтения не было

    def test_unknown_chip_creates_draft_profile(self):
        c = self.conductor()
        sim = self.sim(c)
        sim.JEDEC = "C84018"                                   # чип, которого нет в профилях
        c.detect_only()
        drafts = list((c.data_dir / "drafts").glob("*.yaml"))
        self.assertEqual(len(drafts), 1)
        self.assertIn("C84018", drafts[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
