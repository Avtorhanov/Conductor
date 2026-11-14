"""Штурман и инструменты 0.3.x: раздел «Подключить инструмент», уроки, действие «замер» (sigrok).

Без железа: подставной sigrok-cli из tests/fakes/fake_tools.py.
Запуск:  python3 -m unittest tests.test_guide_tools -v
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.interface import guide, web

ROOT = Path(__file__).resolve().parent.parent
FAKE_TOOLS = ROOT / "tests" / "fakes" / "fake_tools.py"
TOOL_LESSONS = ["tool_bp5", "tool_arm", "tool_minipro", "tool_avr", "tool_esp", "tool_sigrok", "tool_cards",
                "dev_binwalk_expect"]


class GuideToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_gt_"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.tmp / "fake").mkdir()
        shim = self.bin / "sigrok-cli"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS}" sigrok-cli "$@"\n')
        shim.chmod(0o755)
        self._env = {k: os.environ.get(k) for k in ("PATH", "FAKE_DIR", "FAKE_ABSENT")}
        os.environ["PATH"] = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        os.environ["FAKE_DIR"] = str(self.tmp / "fake")
        os.environ.pop("FAKE_ABSENT", None)
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

    def conductor(self, sim=False) -> Conductor:
        c = Conductor(self.cfg, AutoUI(), sim=sim)
        self.opened.append(c)
        return c

    # ---------- guide.yaml ----------
    def test_tools_section_reachable_and_lessons_exist(self):
        g = guide.load_guide()
        self.assertEqual(guide.validate(g), [])
        self.assertIn("tools", [o.get("next") for o in g["nodes"]["home"]["options"]])
        for lid in TOOL_LESSONS:
            self.assertIn(lid, g["lessons"])
        targets = {o.get("lesson") for o in g["nodes"]["tools"]["options"]}
        self.assertTrue({"tool_bp5", "tool_arm", "tool_minipro", "tool_avr", "tool_esp", "tool_sigrok"} <= targets)

    def test_lesson_configs_are_valid_yaml_for_plugins(self):
        """Куски config.yaml в уроках — правильный YAML, и плагины с такими именами существуют."""
        import yaml
        from dirizher.core.registry import discover
        found, _ = discover()
        g = guide.load_guide()
        for lid in TOOL_LESSONS:
            for st in g["lessons"][lid]["steps"]:
                code = st.get("code", "") if isinstance(st, dict) else ""
                if code.lstrip().startswith("plugins:"):
                    data = yaml.safe_load(code)
                    for name in data["plugins"]:
                        self.assertIn(name, found, f"урок {lid}: нет плагина {name}")

    # ---------- действие «замер» ----------
    def test_capture_and_scan_through_guide(self):
        c = self.conductor()
        sid = guide.execute(c, "capture", {})
        self.assertEqual(guide.outcome_of(c, sid), "captured")
        self.assertTrue((c.data_dir / "captures" / f"session_{sid:05d}.sr").exists())
        c2 = self.conductor()
        sid2 = guide.execute(c2, "capture", {"scan": True})
        self.assertEqual(guide.outcome_of(c2, sid2), "captured")
        self.assertIn("fx2lafw", "\n".join(c2.ui.lines))

    def test_capture_refused_in_training_mode(self):
        with self.assertRaises(ValueError):
            guide.execute(self.conductor(sim=True), "capture", {})

    def test_capture_without_analyzer_suggests_lesson(self):
        os.environ["FAKE_ABSENT"] = "1"
        c = self.conductor()
        sid = guide.execute(c, "capture", {})
        outcome = guide.outcome_of(c, sid)
        self.assertEqual(outcome, "capture_failed")
        opts = guide.after("capture", {}, outcome, sid)
        self.assertIn("tool_sigrok", [o.get("lesson") for o in opts])
        ok = guide.after("capture", {"scan": True}, "captured", 5)
        self.assertEqual(ok[0]["action"], "capture")

    def test_web_accepts_scan_param(self):
        app = web.App(self.cfg, pin="123456")
        self.assertEqual(app.clean_params("capture", {"scan": 1, "junk": 2}), {"scan": True})
        self.assertEqual(app.clean_params("capture", {}), {})
        self.assertEqual(guide.job_title("capture", {"scan": True}), "Поиск анализатора")

    def test_no_tools_suggests_tools_section(self):
        with guide.open_readonly(self.cfg, False) as c:
            items = guide.suggest(c)
        self.assertIn("tools", [i.get("next") for i in items])


if __name__ == "__main__":
    unittest.main()
