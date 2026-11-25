"""Журнал → тест (0.7.0, S1): полный вывод команд, версии программ, пакет журнала, фикстуры с железа.

1. RealOutputTests — каждая фикстура tests/fixtures/real/*.json прогоняется через настоящий плагин
   с подменённым Runner (записанный вывод по команде) и сравнивается с ожидаемым опознанием.
   Пока фикстур с железа нет, там лежит одна синтетическая (помечена "synthetic": true).
2. Остальные — механизм целиком без железа: подставные flashrom/esptool/minipro/probe-rs в PATH.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path

import yaml

from dirizher import __main__ as cli
from dirizher import __version__
from dirizher.core import journal
from dirizher.core.runner import Runner, RunResult
from tests.fakes import fake_tools as ft
from tests.test_integrations import FAKE_TOOLS, Base as FakeBase

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures" / "real"

_spec = importlib.util.spec_from_file_location("log_to_test", ROOT / "tools" / "log_to_test.py")
log_to_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(log_to_test)


# ======================= фикстуры с железа =======================
class RealOutputTests(unittest.TestCase):
    def fixtures(self) -> list:
        return sorted(FIXTURES.glob("*.json"))

    def test_there_is_something_to_check(self):
        self.assertTrue(self.fixtures(), "в tests/fixtures/real/ нет ни одной фикстуры")

    def test_each_fixture_parses_as_expected(self):
        for path in self.fixtures():
            with self.subTest(fixture=path.name):
                fx = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(fx["format"], journal.FIXTURE_FORMAT)
                self.assertIn("synthetic", fx, "у фикстуры должна быть пометка synthetic: true/false")
                det, runner = journal.replay_probe(fx, FIXTURES / "_no_data_dir", ROOT / "skills")
                self.assertEqual(journal.compare(fx["expect"], det), [])
                self.assertTrue(runner.used, "плагин не вызвал ни одной записанной команды")

    def test_synthetic_fixtures_are_marked(self):
        for path in self.fixtures():
            fx = json.loads(path.read_text(encoding="utf-8"))
            if "synthetic" in path.name:
                self.assertIs(fx["synthetic"], True, path.name)
                self.assertIn("не с железа", fx["note"])


# ======================= очистка и повтор =======================
class ScrubTests(unittest.TestCase):
    def test_mac_serials_host_home(self):
        text = ("MAC: 24:6F:28:AA:BB:CC again 24-6f-28-aa-bb-cc other 10:20:30:40:50:60\n"
                "Serial code: HSSCVO9LARFMOYKYOMVE5123\nSerial number: 12345678\nSN=DRZ-000123\n"
                "[0]: STLink V2 -- 0483:3748:48FF6E066772574857351967 (ST-LINK)\n"
                "port /dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A50285BI-if00-port0\n"
                "Serial port /dev/ttyUSB0\nuser@workbench-pi: /home/alice/dirizher/data and /root/x\n"
                "C:\\Users\\Bob\\file 12:34:56 0xEF4017")
        clean, counts = journal.scrub({"t": text, "list": [text]}, hostname="workbench-pi")
        out = clean["t"]
        for secret in ("24:6F:28", "24-6f-28", "10:20:30", "HSSCVO9", "12345678", "DRZ-000123",
                       "48FF6E06", "A50285BI", "workbench-pi", "alice", "Bob"):
            self.assertNotIn(secret, out)
        self.assertEqual(out.count("02:00:00:00:00:01"), 2, "один MAC — одна заглушка")
        self.assertIn("02:00:00:00:00:02", out)
        self.assertIn("0483:3748:SERIAL-", out)
        self.assertIn("Serial port /dev/ttyUSB0", out, "«Serial port» — не серийный номер")
        self.assertIn("~/dirizher/data", out)
        self.assertIn("12:34:56 0xEF4017", out, "время и JEDEC ID не трогаются")
        self.assertEqual(clean["list"][0], out)
        self.assertGreaterEqual(counts["mac"], 3)
        self.assertGreaterEqual(counts["serial"], 4)
        self.assertEqual(counts["host"], 2)
        self.assertGreaterEqual(counts["home"], 3)

    def test_replay_runner(self):
        r = journal.ReplayRunner([{"argv": ["x", "-r", "/tmp/tmpab12/read.bin"], "code": 0, "stdout": "ok",
                                   "stderr": ""}])
        res = r.run(["x", "-r", "/tmp/tmpzz99/read.bin"])
        self.assertEqual((res.code, res.out), (0, "ok"))
        with self.assertRaises(LookupError):
            r.run(["x", "-r", "/tmp/tmpzz99/read.bin"])            # каждый ответ — один раз
        with self.assertRaises(LookupError):
            r.run(["y"])

    def test_runner_keeps_full_output_but_not_chip_data(self):
        saved, logged = [], []

        def keep(res, keep_output):
            saved.append((res, keep_output))
            return {"file": "x"}

        run = Runner(lambda m, lvl="info", data=None: logged.append(data), keep)
        long = "строка\n" * 2000
        run.run([sys.executable, "-c", f"print({long!r})"])
        self.assertTrue(saved[0][1])
        self.assertEqual(saved[0][0].out.strip(), long.strip())
        self.assertEqual(logged[-1]["file"], "x")
        self.assertLessEqual(len(logged[-1]["stdout"]), 3000)
        run.run([sys.executable, "-c", "print('DUMP')"], keep_output=False)
        self.assertFalse(saved[1][1])
        self.assertNotIn("DUMP", logged[-1]["stdout"])

    def test_save_command_limits_and_never_overwrites(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            res = RunResult(["p"], 0, "я" * (journal.LIMIT), "e", 0.1)        # 2 МБ в UTF-8
            a = journal.save_command(Path(td), 7, res)
            b = journal.save_command(Path(td), 7, RunResult(["q"], 1, "DATA", "", 0.0), keep_output=False)
            self.assertEqual((a, b), ("session_00007/cmd_001.json", "session_00007/cmd_002.json"))
            rows = journal.commands(Path(td), 7)
            self.assertTrue(rows[0]["truncated"]["stdout"])
            self.assertLessEqual(len(rows[0]["stdout"].encode()), journal.LIMIT)
            self.assertEqual((rows[1]["stdout"], rows[1]["kept"]), ("", False))


# ======================= весь путь на подставных программах =======================
class JournalFlowTests(FakeBase):
    def setUp(self):
        super().setUp()
        p = self.bin / "flashrom"
        p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS}" "flashrom" "$@"\n')
        p.chmod(0o755)
        self.spi = self.fake / ft.SPI
        self.spi.write_bytes(ft.healthy_w25q64())

    def detect(self, plugin="flashrom_spi"):
        self.use(plugin)
        c = self.conductor()
        sid = c.detect_only()
        return c, sid

    def test_full_output_version_and_fingerprint(self):
        c, sid = self.detect()
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        rows = journal.commands(c.memory.logs_dir, sid)
        probe = rows[0]
        self.assertEqual(probe["argv"], ["flashrom", "-p", "ch341a_spi", "-V"])
        self.assertIn("probe_spi_rdid_generic: id1 0xef, id2 0x4017", probe["stdout"])
        self.assertIn('Found Winbond flash chip', probe["stdout"])
        self.assertGreater(len(probe["stdout"]), 3000, "вывод длиннее хвоста события")
        ev = [e for e in c.memory.events(sid) if e["level"] == "cmd" and e["data"]]
        tail = json.loads(ev[0]["data"])["stdout"]
        self.assertNotIn("probe_spi_rdid_generic", tail, "в хвосте события RDID уже не виден")
        self.assertEqual(rows[1]["argv"], ["flashrom", "--version"])
        msgs = [e["message"] for e in c.memory.events(sid)]
        self.assertIn("инструмент: SPI-память через flashrom (flashrom_spi), версия 1.3.0", msgs)
        self.assertEqual(sum("инструмент:" in m for m in msgs), 1, "версия — один раз за сеанс")
        fp = json.loads(c.memory.session(sid)["fingerprint"])
        self.assertEqual(fp["jedec_id"], "EF4017")
        self.assertNotIn("version", json.dumps(fp), "версия не входит в отпечаток")
        log = (c.memory.logs_dir / f"session_{sid:05d}.log").read_text(encoding="utf-8")
        self.assertIn(f"file: session_{sid:05d}/cmd_001.json", log)

    def test_version_once_per_session_in_run(self):
        self.use("flashrom_spi")
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        msgs = [e["message"] for e in c.memory.events(sid)]
        self.assertEqual(sum(m.startswith("инструмент:") for m in msgs), 1)
        versions = [r for r in journal.commands(c.memory.logs_dir, sid) if r["argv"][-1] == "--version"]
        self.assertEqual(len(versions), 1)
        backups = c.data_dir / "logs" / f"session_{sid:05d}"
        for f in backups.glob("cmd_*.json"):
            self.assertNotIn("hsqs", f.read_text(encoding="utf-8"), "дамп в журнал не попадает")

    def test_bundle_fixture_roundtrip(self):
        c, sid = self.detect()
        b = journal.bundle(c, sid)
        self.assertEqual((b["format"], b["dirizher"]), (journal.FORMAT, __version__))
        self.assertEqual(b["tools"][0]["version"], "1.3.0")
        self.assertEqual(b["tools"][0]["settings"]["enabled"], True)
        self.assertEqual(b["session"]["fingerprint"]["jedec_id"], "EF4017")
        self.assertEqual(b["session"]["outcome"], "detected")
        self.assertTrue(all("stdout" not in (e.get("data") or {}) for e in b["events"]))
        fx = log_to_test.make_fixture(b)
        self.assertEqual((fx["program"], fx["program_version"], fx["plugin"]), ("flashrom", "1.3.0", "flashrom_spi"))
        self.assertEqual(log_to_test.check(fx), [])
        self.assertEqual(log_to_test.fixture_path(self.tmp, fx).name, "flashrom_1.3.0_1.json")
        wrong = log_to_test.make_fixture(b, expect_over={"jedec_id": "C22017"})
        self.assertEqual(len(log_to_test.check(wrong)), 1)
        # файл → инструмент командной строки → фикстура, которую принимает общий тест
        src = self.tmp / "bundle.json"
        src.write_text(json.dumps(b, ensure_ascii=False), encoding="utf-8")
        out = self.tmp / "fx"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(log_to_test.main([str(src), "--out", str(out)]), 0)
            self.assertEqual(log_to_test.main([str(src), "--out", str(out), "--expect", "size=1"]), 2)
        made = json.loads((out / "flashrom_1.3.0_1.json").read_text(encoding="utf-8"))
        self.assertIs(made["synthetic"], False)
        det, _ = journal.replay_probe(made, self.tmp, ROOT / "skills")
        self.assertEqual(journal.compare(made["expect"], det), [])
        self.assertTrue((out / "flashrom_1.3.0_2.json").exists(), "прежняя фикстура не переписана")

    def test_not_detected_becomes_fixture_too(self):
        self.spi.unlink()
        c, sid = self.detect()
        self.assertEqual(self.outcome(c, sid), "not_detected")
        fx = log_to_test.make_fixture(journal.bundle(c, sid), plugin="flashrom_spi")
        self.assertEqual(fx["expect"], {"detected": False})
        self.assertEqual(log_to_test.check(fx), [])

    def test_bundle_scrubs_mac_and_serial(self):
        self.fake.joinpath(ft.ESP).write_bytes(ft.healthy_esp())
        c, sid = self.detect("esptool")
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        raw = json.dumps([dict(e) for e in c.memory.events(sid)], ensure_ascii=False)
        self.assertIn("24:6f:28:aa:bb:cc", raw, "в самой памяти MAC остаётся (откат по отпечатку)")
        b = journal.bundle(c, sid)
        text = json.dumps(b, ensure_ascii=False)
        self.assertNotIn("24:6f:28", text)
        self.assertEqual(b["session"]["fingerprint"]["mac"], "02:00:00:00:00:01")
        self.assertEqual(b["tools"][0]["version"], "4.8.1")
        fx = log_to_test.make_fixture(b)
        self.assertEqual(log_to_test.check(fx), [], "заглушка MAC разбирается так же")

        self.fake.joinpath(ft.MINIPRO).write_bytes(b"\xff" * 8 * 1024 * 1024)
        self.cfg["plugins"]["esptool"] = {"enabled": False}
        c2, sid2 = self.detect("minipro")
        text = json.dumps(journal.bundle(c2, sid2), ensure_ascii=False)
        self.assertNotIn("HSSCVO9LARFMOYKYOMVE5123", text)
        self.assertIn("Serial code: SERIAL-1", text)
        self.assertIn('"version": "0.7.2"', text)

    def test_probe_rs_dump_is_not_kept(self):
        self.fake.joinpath(ft.MCU).write_bytes(ft.healthy_stm32())
        self.use("probe_rs")
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        rows = journal.commands(c.memory.logs_dir, sid)
        # 0.7.0 (S3): `read b32` регистра ID (inspect) — не дамп, его вывод нужен для сверки формата на железе
        reads = [r for r in rows if r["argv"][1] == "read" and "b8" in r["argv"]]
        self.assertTrue(reads)
        self.assertTrue(all(r["kept"] is False and r["stdout"] == "" for r in reads))
        self.assertIn({"plugin": "probe_rs", "version": "0.24.0"},
                      [{k: t[k] for k in ("plugin", "version")} for t in journal.bundle(c, sid)["tools"]])

    def test_versions_of_other_blades(self):
        for k in ("openocd", "avrdude"):
            self.use(k)
        c = self.conductor()
        self.assertEqual(c.plugin("openocd").version(), "0.12.0")
        self.assertEqual(c.plugin("avrdude").version(), "7.1")
        os.environ["FAKE_AVRDUDE_V8"] = "1"
        self.assertEqual(c.plugin("avrdude").version(), "8.0")

    def test_cli_journal_to_file(self):
        self.use("flashrom_spi")
        conf = self.tmp / "config.yaml"
        conf.write_text(yaml.safe_dump({
            "data_dir": str(self.tmp / "data"), "profiles_dir": str(ROOT / "profiles"),
            "skills_dir": str(ROOT / "skills"), "help_dir": str(ROOT / "help"),
            "plugins": {"flashrom_spi": {"enabled": True}}}, allow_unicode=True), encoding="utf-8")
        base = ["--config", str(conf)]
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.assertEqual(cli.main(base + ["detect"]), 0)
        dest = self.tmp / "out" / "s1.json"
        self.assertEqual(cli.main(base + ["journal", "1", "--to", str(dest)]), 0)
        b = json.loads(dest.read_text(encoding="utf-8"))
        self.assertEqual(b["session"]["id"], 1)
        self.assertEqual(cli.main(base + ["journal", "1", "--to", str(dest)]), 1, "файл не переписывается")
        self.assertEqual(cli.main(base + ["journal", "1"]), 0)
        self.assertEqual(cli.main(base + ["journal", "99"]), 1)


# ======================= браузер: «Журнал» → сеанс → «Скачать журнал для разработки» =======================
class WebJournalTests(unittest.TestCase):
    def setUp(self):
        import shutil
        import tempfile
        import threading

        from dirizher.core.config import load_config
        from dirizher.interface import web

        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_wj_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        self.app = web.App(self.cfg, sim_default=True, pin="246810")
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()

    def get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        conn.request("GET", path)
        r = conn.getresponse()
        body, headers = r.read(), dict(r.getheaders())
        conn.close()
        return r.status, body, headers

    def test_download_journal(self):
        from dirizher.core.pipeline import Conductor
        from dirizher.core.ui import AutoUI
        c = Conductor(self.cfg, AutoUI(), sim=True)
        sid = c.detect_only()
        c.close()
        st, body, hdr = self.get(f"/api/journal?sid={sid}&sim=1")
        self.assertEqual(st, 200)
        self.assertIn("attachment", hdr["Content-Disposition"])
        self.assertIn(f"dirizher_journal_s{sid}_sim.json", hdr["Content-Disposition"])
        b = json.loads(body)
        self.assertEqual(b["format"], journal.FORMAT)
        self.assertTrue(b["sim"])
        self.assertEqual(b["tools"][0]["version"], f"встроенный {__version__}")
        st, body, _ = self.get("/api/journal?sid=999&sim=1")
        self.assertEqual(st, 404)

    def test_button_in_session_card(self):
        html = (ROOT / "dirizher" / "interface" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Скачать журнал для разработки", html)
        self.assertIn('"/api/journal?sid=" + sid', html)


if __name__ == "__main__":
    unittest.main()
