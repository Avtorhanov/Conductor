"""Журнал загрузки по UART (dirizher/extras/uart.py) без железа: эмулятор платы на псевдотерминале.

Проверяется: только приём (в плату ничего не посылается), запись в data/captures/, сеанс «замер»
в журнале, приметы загрузки, подбор скорости по доле печатных символов, Штурман.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.extras import uart
from dirizher.interface import guide
from tests.fakes.uart_emulator import BROKEN_LOG, UBOOT_LOG, UartEmulator

ROOT = Path(__file__).resolve().parent.parent


class ParsingTests(unittest.TestCase):
    def test_printable_ratio(self):
        self.assertEqual(uart.printable_ratio(b""), 0.0)
        self.assertGreater(uart.printable_ratio(UBOOT_LOG.encode()), 0.99)
        self.assertGreater(uart.printable_ratio("Загрузка…\r\n".encode()), 0.99)   # UTF-8 — не мусор
        self.assertLess(uart.printable_ratio(bytes(range(0x80, 0x100)) * 2), 0.1)

    def test_pick_baud(self):
        samples = {9600: bytes(range(0x80, 0xC0)), 115200: UBOOT_LOG.encode(), 57600: b"\xff\x00" * 40,
                   38400: b"ab"}                       # слишком мало — не в счёт
        best, scores = uart.pick_baud(samples)
        self.assertEqual(best, 115200)
        self.assertNotIn(38400, scores)
        self.assertIsNone(uart.pick_baud({9600: bytes(range(0x80, 0xC0))})[0])

    def test_markers_and_text(self):
        found = uart.markers(uart.to_text(UBOOT_LOG.replace("\n", "\r\n").encode()))
        texts = [m for _, m in found]
        self.assertTrue(texts[0].startswith("загрузчик U-Boot: U-Boot 2016.11"))
        self.assertIn("загрузчик передал управление ядру", texts)
        self.assertTrue(any(t.startswith("ядро запустилось: Linux version 4.14.90") for t in texts))
        self.assertFalse(any(s == "warn" for s, _ in found))
        broken = uart.markers(BROKEN_LOG)
        self.assertTrue(any(s == "warn" and "Bad Data CRC" in m for s, m in broken))
        self.assertNotIn("\r", uart.to_text(b"a\r\nb\x1b[1mc\x07"))


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_uart_"))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        self.emus, self.opened = [], []

    def tearDown(self):
        for c in self.opened:
            c.close()
        for e in self.emus:
            e.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def emu(self, **kw) -> UartEmulator:
        e = UartEmulator(**kw)
        self.emus.append(e)
        self.cfg["uart"] = {"port": e.port, "baud": 115200, "time_s": 1.0, "sample_s": 0.4,
                            "bauds": [9600, 57600, 115200]}
        return e

    def conductor(self, sim=False) -> Conductor:
        c = Conductor(self.cfg, AutoUI(), sim=sim)
        self.opened.append(c)
        return c

    def text(self, c):
        return "\n".join(c.ui.lines)

    def test_capture_boot_log_receive_only(self):
        e = self.emu()
        c = self.conductor()
        sid = uart.capture(c, self.cfg)
        self.assertEqual(c.memory.session(sid)["outcome"], "captured", self.text(c))
        self.assertEqual(c.memory.session(sid)["kind"], "capture")
        txt = c.data_dir / "captures" / f"session_{sid:05d}_uart.txt"
        self.assertTrue(txt.exists())
        self.assertTrue((c.data_dir / "captures" / f"session_{sid:05d}_uart.bin").exists())
        self.assertIn("U-Boot 2016.11", txt.read_text("utf-8"))
        self.assertIn("загрузчик U-Boot", self.text(c))
        self.assertEqual(bytes(e.received), b"")                 # в плату не послано ни байта
        log = (c.data_dir / "logs" / f"session_{sid:05d}.log").read_text("utf-8")
        self.assertIn("115200 бод", log)

    def test_auto_baud(self):
        e = self.emu(baud=57600)
        self.cfg["uart"]["baud"] = "auto"
        c = self.conductor()
        sid = uart.capture(c, self.cfg)
        self.assertEqual(c.memory.session(sid)["outcome"], "captured", self.text(c))
        self.assertIn("скорость 57600", self.text(c))
        self.assertEqual(bytes(e.received), b"")

    def test_wrong_speed_warns(self):
        self.emu(baud=57600)                                      # настроено 115200 — будет мусор
        c = self.conductor()
        sid = uart.capture(c, self.cfg)
        self.assertEqual(c.memory.session(sid)["outcome"], "captured")
        self.assertIn("похоже на неверную скорость", self.text(c))

    def test_silent_board(self):
        self.emu(silent=True)
        c = self.conductor()
        sid = uart.capture(c, self.cfg)
        self.assertEqual(c.memory.session(sid)["outcome"], "capture_failed")
        self.assertIn("ничего не принято", self.text(c))

    def test_missing_port(self):
        self.cfg["uart"] = {"port": str(self.tmp / "ttyUSB9"), "time_s": 0.1}
        c = self.conductor()
        sid = uart.capture(c, self.cfg)
        self.assertEqual(c.memory.session(sid)["outcome"], "capture_failed")
        self.assertIn("порта", self.text(c))

    def test_broken_image_marker(self):
        self.emu(log=BROKEN_LOG)
        c = self.conductor()
        uart.capture(c, self.cfg)
        self.assertIn("испорченный образ", self.text(c))

    def test_through_guide(self):
        self.emu()
        c = self.conductor()
        sid = guide.execute(c, "uart", {})
        self.assertEqual(guide.outcome_of(c, sid), "captured")
        opts = guide.after("uart", {}, "captured", sid)
        self.assertIn("show", [o.get("action") for o in opts])
        fail = guide.after("uart", {}, "capture_failed", sid)
        self.assertIn("tool_uart", [o.get("lesson") for o in fail])
        sid2 = guide.execute(self.conductor(), "uart", {"baud": "auto"})
        self.assertEqual(guide.outcome_of(c, sid2), "captured")

    def test_web_baud_param(self):
        from dirizher.interface import web
        app = web.App(self.cfg, pin="123456")
        self.assertEqual(app.clean_params("uart", {"baud": "AUTO", "junk": 1}), {"baud": "auto"})
        self.assertEqual(app.clean_params("uart", {"baud": 115200}), {"baud": "115200"})
        with self.assertRaises(ValueError):
            app.clean_params("uart", {"baud": "fast; rm"})
        self.assertEqual(guide.job_title("uart", {"baud": "auto"}), "Журнал загрузки по UART (подбор скорости)")

    def test_refused_in_training_mode(self):
        with self.assertRaises(ValueError):
            guide.execute(self.conductor(sim=True), "uart", {})


if __name__ == "__main__":
    unittest.main()
