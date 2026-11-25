"""Учебная площадка (тренажёр): данные, сверка «сеансов вживую» с настоящим ядром, страница, Штурман,
мастер, капот, демо и командная строка. Без железа.

Запуск:  python3 -m unittest tests.test_playground -v
"""
from __future__ import annotations

import contextlib
import copy
import http.client
import importlib.util
import io
import json
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from dirizher.__main__ import main as cli_main
from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.ui import AutoUI
from dirizher.interface import guide, playground, web
from dirizher.interface.wizard import Wizard
from dirizher.workshop.hood import Hood, classify, needs_word

ROOT = Path(__file__).resolve().parent.parent
PG_REL = "dirizher/interface/playground.yaml"
TRACKS = {"start", "phone", "ipad", "meter", "solder", "first_chip", "laptop", "car", "tv", "combos", "shop", "limits"}
# числа и шестнадцатеричные куски (номера сеансов, отпечатки, счётчики секторов) — не сравниваем
NUMBERS = re.compile(r"[0-9a-fA-F]*\d[0-9a-fA-F]*")


def skeleton(line: str) -> str:
    return NUMBERS.sub("#", line.strip())


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_pg_"))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ======================= данные =======================
class DataTests(unittest.TestCase):
    def setUp(self):
        self.data = playground.load()
        self.raw = yaml.safe_load((ROOT / PG_REL).read_text(encoding="utf-8"))

    def test_all_tracks_present(self):
        ids = [t["id"] for t in self.data["tracks"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(TRACKS <= set(ids), TRACKS - set(ids))
        for t in self.data["tracks"]:
            self.assertTrue(t["steps"], t["id"])

    def test_every_quiz_explains_every_answer(self):
        for t in self.data["tracks"]:
            for i, st in enumerate(t["steps"], 1):
                for o in (st.get("quiz") or {}).get("options", []):
                    self.assertTrue(o.get("say"), f"{t['id']}, шаг {i}: у ответа «{o['text']}» нет объяснения")

    def test_validation_catches_mistakes(self):
        def broken(fn):
            d = copy.deepcopy(self.raw)
            fn(d)
            return playground.validate(d)
        st = lambda d: d["tracks"][0]["steps"][0]  # noqa: E731
        cases = {
            "термина": lambda d: st(d).update(text="Про [[несуществующий термин]]."),
            "ok: true": lambda d: [o.pop("ok", None) for o in st(d)["quiz"]["options"]],
            "уже занят": lambda d: d["tracks"][1].update(id=d["tracks"][0]["id"]),
            "нет маршрута": lambda d: d["tracks"][0].update(next=["nowhere"]),
            "лишние поля": lambda d: st(d).update(texxt="опечатка"),
            "check": lambda d: d["tracks"][0]["steps"][4]["console"]["cases"][0].update(check="made_up"),
            "level": lambda d: d["tracks"][0].update(level=9),
            "https": lambda d: d["tracks"][6]["links"].append({"title": "x", "url": "http://example.com"}),
        }
        for needle, fn in cases.items():
            with self.subTest(needle):
                self.assertTrue(any(needle in e for e in broken(fn)), broken(fn))
        self.assertEqual(playground.validate(self.raw), [])

    def test_plain_text_for_terminal(self):
        self.assertEqual(playground.plain("**Жирно** и [[дамп]], [[копия «до»|копию]], `run`"),
                         "Жирно и дамп, копию, run")


# ======================= «сеанс вживую» = настоящее ядро =======================
class ConsoleMatchesCoreTests(Tmp):
    """Протоколы в тренажёре сокращены, но каждая строка должна быть такой, как её печатает ядро
    (номера сеансов, отпечатки и счётчики не сравниваются). Строки «~» — примеры, «? ! >» — диалог."""

    def run_core(self, name: str) -> list:
        cfg = copy.deepcopy(self.cfg)
        cfg["data_dir"] = self.tmp / name
        if name == "voltage":
            cfg["safety"]["programmer_voltage"] = 1.8
        opened = []

        def cond(answers=None, dry_run=False):
            c = Conductor(cfg, AutoUI(answers, True), sim=True, dry_run=dry_run)
            opened.append(c)
            return c

        try:
            if name in ("corrupt_ref", "flaky", "dry"):
                c = cond(["yes"])
                c.plugin("simulator").do_reset()
                c.run()                                    # эталон с исправной платы
            # после порчи с эталоном — выбор из двух способов (весь эталон или только разделы): первый
            c = cond([0, "yes"] if name in ("corrupt_ref", "flaky", "dry") else ["yes"], dry_run=(name == "dry"))
            sim = c.plugin("simulator")
            sim.do_reset()
            if name in ("corrupt_ref", "flaky", "dry"):
                sim.do_corrupt()
            if name == "flaky":
                sim.do_flaky()
            if name == "blank":
                sim.do_erase()
            c.run()
            return [skeleton(x) for x in c.ui.lines if x.strip()]
        finally:
            for c in opened:
                c.close()

    def test_console_lines_come_from_core(self):
        cases = [(t["id"], c) for t in playground.load()["tracks"] for st in t["steps"]
                 for c in (st.get("console") or {}).get("cases", []) if c.get("check")]
        self.assertTrue({c["check"] for _, c in cases} >= set(playground.CONSOLE_CHECKS))
        real = {}
        for tid, case in cases:
            if case["check"] not in real:
                real[case["check"]] = self.run_core(case["check"])
            for line in case["lines"]:
                if line.startswith(("~", "?", "!", ">")):
                    continue
                want = skeleton(line[:-2] if line.endswith(" …") else line)
                with self.subTest(track=tid, case=case["label"], line=line):
                    self.assertTrue(any(want in got for got in real[case["check"]]),
                                    f"нет в выводе ядра ({case['check']}): {line}")


# ======================= страница и прибор =======================
class PageTests(unittest.TestCase):
    def setUp(self):
        self.page = playground.PAGE_PATH.read_text(encoding="utf-8")

    def test_page_is_offline(self):
        self.assertEqual(self.page.count(playground.DATA_MARKER), 1)
        self.assertNotRegex(self.page, r"<script[^>]+src=")
        self.assertNotRegex(self.page, r"<link[^>]+stylesheet")
        self.assertNotRegex(self.page, r"@import|url\(\s*['\"]?https?:")
        self.assertIn('fetch("playground.json"', self.page)

    def test_standalone_file(self):
        html = playground.standalone()
        self.assertNotIn(playground.DATA_MARKER, html)
        self.assertIn("window.PLAYGROUND_STANDALONE = true;", html)
        blob = html.split("window.PLAYGROUND = ", 1)[1].split("; window.PLAYGROUND_STANDALONE", 1)[0]
        self.assertEqual(json.loads(blob), playground.load())
        self.assertNotIn("</", blob)                        # данные не закроют <script> раньше времени

    def test_back_everywhere(self):
        # шапка: «назад» на каждом экране, кроме главной; на главной — «← Дирижёр» (кроме отдельного файла)
        self.assertIn('id="upBtn"', self.page)
        self.assertIn('if (hash === "glossary" || !part) return ["#", "← Все маршруты"];', self.page)
        self.assertIn('return ["#" + tid, "← К маршруту"];', self.page)
        self.assertIn('if (up && !(t && part && part !== "done")) view.append(backRow(hash));', self.page)
        self.assertIn('i ? "← Назад" : "← К маршруту"', self.page)        # шаг: внизу

    def test_same_theme_and_progress_store_as_app(self):
        self.assertIn('localStorage.getItem("drz." + k)', self.page)
        self.assertIn('store.get("theme", "auto")', self.page)
        self.assertIn('store.set("playground", P)', self.page)


class WebServesPlaygroundTests(Tmp):
    def setUp(self):
        super().setUp()
        self.cfg["_path"] = ROOT / "config.yaml"
        self.app = web.App(self.cfg, sim_default=True, pin="135790")
        self.app.trust_local = False                        # как с телефона: без кода входа
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, r.getheader("Content-Type") or "", r.getheader("Content-Security-Policy") or "", body

    def test_page_and_data_without_login(self):
        st, ctype, csp, body = self.get("/playground.html")
        self.assertEqual(st, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("default-src 'self'", csp)
        self.assertEqual(body, playground.PAGE_PATH.read_bytes())
        st, ctype, _, body = self.get("/playground.json")
        self.assertEqual(st, 200)
        self.assertIn("application/json", ctype)
        self.assertEqual(json.loads(body), playground.load())

    def test_device_api_still_needs_login(self):
        self.assertEqual(self.get("/api/device")[0], 401)
        self.assertEqual(self.get("/playground.yaml")[0], 404)


# ======================= Штурман, мастер, командная строка =======================
class GuideAndWizardTests(Tmp):
    def test_guide_offers_playground(self):
        g = guide.load_guide()
        opts = [o for n in g["nodes"].values() for o in n["options"] if o.get("action") == "playground"]
        self.assertTrue(any(o in g["nodes"]["home"]["options"] for o in opts), "нет на главной Штурмана")
        ids = {t["id"] for t in playground.load()["tracks"]}
        for o in opts:
            track = (o.get("params") or {}).get("track")
            if track:
                self.assertIn(track, ids)
        bad = {"start": "home", "lessons": {}, "nodes": {"home": {"ask": "?", "options": [
            {"label": "x", "action": "playground", "params": {"track": 5}}]}}}
        self.assertTrue(any("track" in e for e in guide.validate(bad)))

    def test_browser_opens_page(self):
        page = (web.STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('playground: p => { location.href = "playground.html"', page)
        self.assertIn('o.action === "playground"', page)

    def test_track_in_terminal(self):
        ui = AutoUI()                                       # на все вопросы — первый вариант
        playground.run_text(ui, playground.load(), "start")
        text = "\n".join(ui.lines)
        self.assertIn("Шаг 1 из 9. Прошивка — это память устройства", text)
        self.assertIn("Не совсем.", text)                   # первый вариант первого вопроса неверный
        self.assertIn("Правильно: Записать файл, не сняв копию того, что было", text)
        self.assertIn("✔ Winbond W25Q64, 8 МБ SPI NOR (виртуальная)", text)   # сеанс вживую
        self.assertIn("· прошивка — Программа", text)       # словарик под шагом
        self.assertIn("Маршрут «Как думает Дирижёр» пройден.", text)
        self.assertNotIn("[[", text)

    def test_wizard_action(self):
        ui = AutoUI()
        w = Wizard(self.cfg, sim=True, ui=ui, ask_text=lambda p: "")
        self.assertIsNone(w.action("playground", {"track": "limits"}))
        self.assertIn("Маршрут «Границы: чего не делаем» пройден.", "\n".join(ui.lines))

    def test_cli_export_and_unknown_track(self):
        dest = self.tmp / "тренажёр.html"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["playground", "--export", str(dest)]), 0)
            self.assertEqual(cli_main(["playground", "nowhere"]), 1)
        self.assertIn("window.PLAYGROUND_STANDALONE = true;", dest.read_text(encoding="utf-8"))


# ======================= капот и демо =======================
class HoodAndPagesTests(Tmp):
    def test_hood_edits_without_word_and_validates(self):
        self.assertEqual(classify(PG_REL)[0], "edit")
        self.assertFalse(needs_word(PG_REL))
        hood = Hood(self.cfg, root=ROOT)
        good = hood.check(PG_REL, (ROOT / PG_REL).read_text(encoding="utf-8"))
        self.assertEqual(good["errors"], [])
        self.assertTrue(any("маршрутов" in h for h in good["hints"]))
        bad = hood.check(PG_REL, (ROOT / PG_REL).read_text(encoding="utf-8").replace("[[дамп]]", "[[дампп]]", 1))
        self.assertTrue(any("дампп" in e for e in bad["errors"]))

    def test_pages_demo_has_playground(self):
        spec = importlib.util.spec_from_file_location("build_pages", ROOT / "tools" / "build_pages.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = self.tmp / "site"
        out.mkdir()
        mod.build_page(out)
        self.assertEqual((out / "playground.html").read_bytes(), playground.PAGE_PATH.read_bytes())
        self.assertEqual(json.loads((out / "playground.json").read_text(encoding="utf-8")), playground.load())


if __name__ == "__main__":
    unittest.main()
