"""Справочник движков и переходников и демо для GitHub Pages — без железа.

Каждый тест работает на копии проекта во временной папке — настоящие файлы не меняются.
Запуск:  python3 -m unittest tests.test_catalog -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from dirizher import __main__ as cli
from dirizher.core.config import load_config
from dirizher.core.ui import AutoUI
from dirizher.interface import guide, web
from dirizher.interface.wizard import Wizard
from dirizher.workshop import catalog, doctor, helpdocs
from dirizher.workshop.hood import Hood, HoodError, classify, needs_word

ROOT = Path(__file__).resolve().parent.parent

GOOD_ADAPTER = {
    "kind": "adapter", "id": "swd_10to4", "title": "Переходник SWD 10→4", "tagline": "ST-Link к разъёму Cortex Debug",
    "group": "diy", "engines": ["probe_rs"], "steps": "Отключите питание\nПодключите по рисунку",
    "a_title": "ST-Link", "b_title": "Разъём 10",
    "wires": [{"a": "SWDIO", "b": "2 · SWDIO", "color": "green"}, {"a": "GND", "b": "3 · GND", "color": "black",
                                                                       "note": "первым"}],
    "caution": "Только 3,3 В", "verify": "Распиновку разъёма",
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_cat_"))
        self.root = self.tmp / "root"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", "data", "__pycache__", "tests", "_site"))
        self.cfg = load_config(self.root / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        self.hood = Hood(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cards(self):
        return catalog.load(self.cfg)

    def write_card(self, rel: str, data) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return p


# ======================= карточки =======================
class CardTests(Base):
    def test_builtin_cards_load_clean(self):
        cards, errors = self.cards()
        self.assertEqual(errors, [])
        ids = {c["id"] for c in cards}
        for must in ("flashrom", "probe_rs", "openocd", "minipro", "avrdude", "esptool", "binwalk", "sigrok",
                     "buspirate", "modbus_rtu", "ch341a", "rpi_spi", "st_link_v2", "pico_probe", "usbasp",
                     "arduino_isp", "bus_pirate5", "usb_uart", "usb_rs485", "t48", "logic_analyzer"):
            self.assertIn(must, ids)
        for c in cards:
            with self.subTest(card=c["id"]):
                self.assertEqual(c["warnings"], [], "ссылки, авторы и шаги на месте")
                if c["kind"] == "engine":
                    self.assertTrue(c["authors"] and c["license"], "у движка указаны авторы и лицензия")
                    self.assertTrue(c["what"] and c["strengths"] and c["how"])
                    if c["group"] == "tool":
                        self.assertTrue(c["in_dirizher"], "как работает внутри Дирижёра")
                else:
                    self.assertTrue(c["steps"] and c["drawings"], "у переходника шаги и хотя бы один рисунок")
                for link in c["links"]:
                    self.assertTrue(link["url"].startswith("https://"))

    def test_levels_go_from_simple_to_complex(self):
        """Справочник по ступеням: от первой флешки (1) до капота (5); внутри — купить, собрать, движки, основа."""
        cards, _ = self.cards()
        self.assertEqual(list(catalog.LEVELS), [1, 2, 3, 4, 5])
        for c in cards:
            with self.subTest(card=c["id"]):
                raw = yaml.safe_load((self.root / c["path"]).read_text(encoding="utf-8"))
                self.assertIn(raw.get("level"), catalog.LEVELS, "у каждой встроенной карточки ступень указана явно")
                self.assertEqual(c["level_title"], catalog.LEVELS[c["level"]]["title"])
        levels = [c["level"] for c in cards]
        self.assertEqual(levels, sorted(levels), "карточки идут по ступеням")
        self.assertEqual(cards[0]["id"], "ch341a", "первым — самый доступный программатор")
        lv = {c["id"]: c["level"] for c in cards}
        self.assertEqual({k for k, v in lv.items() if v == 1}, {"ch341a", "soic8_clip", "soic8_socket", "flashrom"})
        self.assertEqual({k for k, v in lv.items() if v == 5}, {"python", "pyyaml", "sqlite"})
        self.assertLess(lv["adapter_18v"], lv["usbasp"])
        self.assertLess(lv["st_link_v2"], lv["usb_rs485"])
        groups = [catalog.GROUP_ORDER[c["group"]] for c in cards if c["level"] == 3]
        self.assertEqual(groups, sorted(groups), "внутри ступени: купить → собрать → движки")
        # «Как всё связано» — в том же порядке, переходники в строке тоже от простого к сложному
        rows = catalog.overview(cards)
        self.assertEqual([r["level"] for r in rows], sorted(r["level"] for r in rows))
        self.assertEqual(rows[0]["target"], "memory.spi_nor")
        spi = next(r for r in rows if r["engine"] == "flashrom")
        ids = [a["id"] for a in spi["adapters"]]
        self.assertLess(ids.index("ch341a"), ids.index("adapter_18v"))
        self.assertEqual([x["n"] for x in catalog.levels_public()], [1, 2, 3, 4, 5])
        self.assertTrue(all(x["short"] and x["about"] for x in catalog.levels_public()))

    def test_level_field_checked_and_defaulted(self):
        base = {"id": "ok_card", "kind": "adapter", "title": "т", "tagline": "т"}
        for bad in (0, 6, "2", True, 2.5):
            with self.subTest(level=bad):
                self.assertTrue(any("level" in e for e in catalog.validate(dict(base, level=bad))))
        self.assertEqual(catalog.validate(dict(base, level=4)), [])
        self.assertEqual(catalog.level_of(dict(base, level=4)), 4)
        self.assertEqual(catalog.level_of(dict(base, targets=["mcu.arm", "memory.i2c_eeprom"])), 2,
                         "без level — по самому простому виду микросхем")
        self.assertEqual(catalog.level_of(dict(base, kind="engine", group="base")), 5)
        self.assertEqual(catalog.level_of(base), catalog.DEFAULT_LEVEL)
        self.write_card("catalog/adapters/no_level.yaml", dict(base, id="no_level", targets=["device.modbus"],
                                                                steps=["x"]))
        self.assertEqual(catalog.get(self.cards()[0], "no_level")["level"], 4)

    def test_engine_plugins_exist(self):
        cards, _ = self.cards()
        from dirizher.core.registry import discover
        classes, _ = discover()
        names = set(classes) | {"modbus_rtu"}                 # лезвие навыка
        for c in cards:
            if c["kind"] == "engine" and c["plugin"]:
                with self.subTest(card=c["id"]):
                    self.assertIn(c["plugin"], names)

    def test_validation_catches_mistakes(self):
        base = {"id": "ok_card", "kind": "adapter", "title": "т", "tagline": "т"}
        self.assertEqual(catalog.validate(base), [])
        cases = {
            "id": dict(base, id="Bad-Id"), "kind": dict(base, kind="tool"), "tagline": dict(base, tagline=""),
            "ожидалась строка": dict(base, caution=[{"тогда echo": "true"}]),
            "https": dict(base, links=[{"title": "x", "url": "http://x.org"}]),
            "usb": dict(base, usb="1A86-5512"), "цвет": dict(base, drawings=[{"type": "wiring", "wires": [
                {"a": "A", "b": "B", "color": "pink"}]}]),
            "pins": dict(base, drawings=[{"type": "package", "pins": ["1", "2", "3"]}]),
            "вывод": dict(base, drawings=[{"type": "header", "count": 10, "rows": 2, "pins": {12: {"label": "x"}}}]),
            "targets": dict(base, targets=["memory.nand"]), "group": dict(base, group="tool"),
        }
        for word, data in cases.items():
            with self.subTest(word=word):
                errs = catalog.validate(data)
                self.assertTrue(errs and any(word in e for e in errs), errs)

    def test_broken_card_does_not_break_others_and_duplicates_refused(self):
        self.write_card("catalog/adapters/broken.yaml", "id: broken\nkind: adapter\ntitle: [x\n")
        self.write_card("catalog/adapters/dup.yaml", {"id": "ch341a", "kind": "adapter", "title": "x", "tagline": "y"})
        cards, errors = self.cards()
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("YAML" in e for _, e in errors))
        self.assertTrue(any("уже занят" in e for _, e in errors))
        self.assertIsNotNone(catalog.get(cards, "flashrom"))

    def test_skill_brings_its_card(self):
        cards, _ = self.cards()
        card = catalog.get(cards, "modbus_rtu")
        self.assertEqual(card["source"], "навык modbus_rtu")
        self.assertEqual(card["path"], "skills/modbus_rtu/catalog/modbus_rtu.yaml")
        manifest = self.root / "skills" / "modbus_rtu" / "skill.yaml"
        manifest.write_text(manifest.read_text(encoding="utf-8").replace("\nenabled: true", "\nenabled: false"),
                            encoding="utf-8")
        self.assertIsNone(catalog.get(self.cards()[0], "modbus_rtu"), "выключенный навык не приносит карточку")

    def test_related_and_overview_go_both_ways(self):
        cards, _ = self.cards()
        rel = catalog.related(cards, catalog.get(cards, "ch341a"))
        self.assertIn("flashrom", [r["id"] for r in rel["items"]])
        self.assertIn("soic8_clip", [r["id"] for r in rel["needs"]])
        rel = catalog.related(cards, catalog.get(cards, "probe_rs"))
        self.assertEqual({r["id"] for r in rel["items"]}, {"st_link_v2", "pico_probe"})
        spi = [o for o in catalog.overview(cards) if o["target"] == "memory.spi_nor" and o["engine"] == "flashrom"]
        self.assertEqual(spi[0]["plugin"], "flashrom_spi")
        self.assertIn("rpi_spi", [a["id"] for a in spi[0]["adapters"]])

    def test_status_shows_what_is_ready(self):
        cards, _ = self.cards()
        report = [{"name": "flashrom_spi", "title": "x", "active": True, "status": "программатор: ch341a_spi"},
                  {"name": "probe_rs", "title": "x", "active": False, "status": "выключен"},
                  {"name": "avrdude", "title": "x", "active": False, "status": "недоступен: avrdude не установлен"}]
        st = {c["id"]: (c["status"], c["status_level"]) for c in catalog.with_status(cards, report)}
        self.assertEqual(st["flashrom"][1], "ok")
        self.assertIn("config.yaml", st["probe_rs"][0])
        self.assertEqual(st["avrdude"][1], "warn")
        self.assertEqual(st["sqlite"][1], "ok")
        sim = {c["id"]: c["status"] for c in catalog.with_status(cards, report, sim=True)}
        self.assertIn("учебный режим", sim["probe_rs"])

    def test_text_draws_pictures_as_lines(self):
        cards, _ = self.cards()
        lines = "\n".join(catalog.text(catalog.get(cards, "rpi_spi"), cards))
        self.assertIn("24 · CE0", lines)
        self.assertIn("→ 1 · CS", lines)
        self.assertIn("● — вывод 1", lines)
        self.assertIn("вывод 17: 3,3 В", lines)
        self.assertIn("ПРОВЕРИТЬ НА ЖЕЛЕЗЕ", lines)
        lines = "\n".join(catalog.text(catalog.get(cards, "openocd"), cards))
        self.assertIn("Dominic Rath", lines)
        self.assertIn("лицензия: GPL-2.0", lines)
        self.assertIn("ступень 3 из 5 · Микроконтроллеры", lines)

    def test_guide_points_to_existing_cards(self):
        cards, _ = self.cards()
        ids = {c["id"] for c in cards}
        g = guide.load_guide()
        opts = [o for n in g["nodes"].values() for o in n["options"]]
        opts += [o for les in g["lessons"].values() for o in les.get("related") or []]
        found = [o for o in opts if o.get("action") == "catalog"]
        self.assertGreaterEqual(len(found), 8)
        for o in found:
            card = (o.get("params") or {}).get("card")
            if card:
                self.assertIn(card, ids, o["label"])
        for c in cards:
            if c["lesson"]:
                self.assertIn(c["lesson"], g["lessons"], c["id"])
        bad = {"nodes": {"x": {"ask": "?", "options": [{"label": "a", "action": "catalog", "params": {"kind": "nope"}}]}},
               "start": "x", "lessons": {}}
        self.assertTrue(any("kind" in e for e in guide.validate(bad)))


# ======================= пополнение: конструктор, шаблон, капот =======================
class AddTests(Base):
    def test_preview_builds_card_and_reports_problems(self):
        res = catalog.preview(self.cfg, GOOD_ADAPTER)
        self.assertEqual(res["errors"], [])
        card = res["card"]
        self.assertEqual(card["drawings"][0]["wires"][1]["note"], "первым")
        self.assertEqual([s["text"] for s in card["steps"]], ["Отключите питание", "Подключите по рисунку"])
        self.assertIn("kind: adapter", res["yaml"])
        self.assertEqual(yaml.safe_load(res["yaml"])["id"], "swd_10to4")
        bad = catalog.preview(self.cfg, dict(GOOD_ADAPTER, id="ch341a", tagline=""))
        self.assertTrue(any("уже занят" in e for e in bad["errors"]) or any("tagline" in e for e in bad["errors"]))
        self.assertIsNone(bad["card"])
        eng = catalog.preview(self.cfg, {"kind": "engine", "id": "pyocd", "title": "pyOCD", "tagline": "отладка ARM",
                                         "authors": "Команда pyOCD — разработка\nArm", "license": "Apache-2.0",
                                         "examples": "pyocd list # какие отладчики видны", "adapters": ["st_link_v2"]})
        self.assertEqual(eng["errors"], [])
        self.assertEqual(eng["card"]["authors"][0], {"name": "Команда pyOCD", "role": "разработка"})
        self.assertEqual(eng["card"]["examples"][0]["note"], "какие отладчики видны")
        self.assertEqual(catalog.preview(self.cfg, dict(GOOD_ADAPTER, level="3"))["card"]["level"], 3)
        self.assertEqual(yaml.safe_load(catalog.preview(self.cfg, dict(GOOD_ADAPTER, level="3"))["yaml"])["level"], 3)
        self.assertNotIn("level", yaml.safe_load(res["yaml"]), "«авто» — поле не пишется, ступень по видам микросхем")
        self.assertTrue(catalog.preview(self.cfg, dict(GOOD_ADAPTER, level="9"))["errors"])

    def test_create_through_hood_loads_and_never_overwrites(self):
        rel = catalog.create(self.hood, self.cfg, GOOD_ADAPTER, actor="тест")
        self.assertEqual(rel, "catalog/adapters/swd_10to4.yaml")
        cards, errors = self.cards()
        self.assertEqual(errors, [])
        self.assertEqual(catalog.get(cards, "swd_10to4")["group"], "diy")
        self.assertTrue(self.hood.history(rel), "создание записано в историю капота")
        with self.assertRaises(HoodError):
            catalog.create(self.hood, self.cfg, GOOD_ADAPTER)
        with self.assertRaises(HoodError):
            catalog.create(self.hood, self.cfg, dict(GOOD_ADAPTER, id="x_new", title=""))

    def test_template_card(self):
        rel = catalog.create_from_template(self.hood, "engine", "my_tool", "Мой: инструмент")
        data = yaml.safe_load((self.root / rel).read_text(encoding="utf-8"))
        self.assertEqual((data["id"], data["title"], data["kind"]), ("my_tool", "Мой: инструмент", "engine"))
        self.assertIsNotNone(catalog.get(self.cards()[0], "my_tool"))
        with self.assertRaises(HoodError):
            catalog.create_from_template(self.hood, "adapter", "Bad Id", "x")

    def test_hood_edits_cards_without_word_but_with_checks(self):
        rel = "catalog/adapters/ch341a.yaml"
        self.assertEqual(classify(rel)[0], "edit")
        self.assertEqual(classify("skills/modbus_rtu/catalog/modbus_rtu.yaml")[0], "edit")
        self.assertFalse(needs_word(rel), "карточка только описывает — слово «ДА» не нужно")
        self.assertTrue(needs_word("profiles/chips/winbond_w25q64.yaml"), "профили по-прежнему со словом")
        text = (self.root / rel).read_text(encoding="utf-8")
        broken = text.replace("engines: [flashrom]", "engines: [flashrom, nosuch]")
        res = self.hood.check(rel, broken)
        self.assertEqual(res["errors"], [])
        self.assertTrue(any("nosuch" in w for w in res["warnings"]))
        res = self.hood.check(rel, text.replace("id: ch341a", "id: flashrom"))
        self.assertTrue(any("уже занят" in e for e in res["errors"]))
        self.assertTrue(self.hood.check(rel, text + "\ndrawings: [{type: nope}]\n")["errors"])
        saved = self.hood.save(rel, text.replace("Самый доступный", "Самый дешёвый"), None, "тест")
        self.assertFalse(saved.get("errors"))
        self.assertIn("Самый дешёвый", catalog.get(self.cards()[0], "ch341a")["tagline"])
        self.assertIn("catalog/adapters/ch341a.yaml", {f["path"] for f in self.hood.files("catalog")})

    def test_doctor_reports_broken_and_unfinished_cards(self):
        self.write_card("catalog/adapters/broken.yaml", {"id": "broken", "kind": "adapter", "title": "x"})
        self.write_card("catalog/adapters/thin.yaml", {"id": "thin", "kind": "adapter", "title": "x", "tagline": "y",
                                                       "engines": ["nosuch"]})
        items = {i["id"]: i for i in doctor.check(self.cfg, sim=True, deep=False, hood=self.hood)}
        self.assertEqual(items["file:catalog/adapters/broken.yaml"]["level"], "bad")
        self.assertIn("tagline", items["file:catalog/adapters/broken.yaml"]["detail"])
        self.assertEqual(items["card:thin"]["level"], "info")
        self.assertIn("nosuch", items["card:thin"]["detail"])

    def test_help_search_finds_cards(self):
        items = helpdocs.articles(self.cfg)
        found = helpdocs.search(items, "прищепка")
        self.assertIn("card", {i["kind"] for i in found})
        self.assertEqual(helpdocs.search(items, "Dominic Rath")[0]["card"], "openocd")
        first = helpdocs.search(items, "", "catalog")[0]
        self.assertEqual(first["section"], "catalog")
        self.assertIsNone(helpdocs.article(self.cfg, "card.flashrom"), "статьи и карточки не путаются")


# ======================= интерфейсы =======================
class InterfaceTests(Base):
    def setUp(self):
        super().setUp()
        self.app = web.App(self.cfg, sim_default=True, pin="135790")
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=60)
        hdrs = {"X-Dirizher": "1"}
        data = json.dumps(body).encode() if body is not None else None
        if data:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, json.loads(payload) if payload[:1] in (b"{", b"[") else payload

    def test_catalog_over_http(self):
        st, d = self.req("GET", "/api/catalog?sim=1")
        self.assertEqual(st, 200)
        self.assertEqual(d["total"], len(d["items"]))
        self.assertIn("red", d["colors"])
        self.assertTrue(d["overview"])
        self.assertNotIn("_text", d["items"][0])
        self.assertEqual([x["n"] for x in d["levels"]], [1, 2, 3, 4, 5])
        self.assertEqual((d["items"][0]["id"], d["items"][0]["level"]), ("ch341a", 1))
        st, d = self.req("GET", "/api/catalog/card?id=bus_pirate5&sim=1")
        self.assertEqual(st, 200)
        self.assertTrue(d["editable"])
        self.assertEqual(d["related"]["items"][0]["id"], "buspirate")
        self.assertEqual(self.req("GET", "/api/catalog/card?id=nope")[0], 404)
        st, d = self.req("POST", "/api/catalog/preview", {"fields": GOOD_ADAPTER})
        self.assertEqual((st, d["errors"]), (200, []))

    def test_new_card_needs_open_hood(self):
        st, d = self.req("POST", "/api/catalog/new", {"fields": GOOD_ADAPTER})
        self.assertEqual(st, 400)
        self.assertIn("капот закрыт", d["error"])
        self.assertEqual(self.req("POST", "/api/hood/unlock", {"open": True})[0], 200)   # экран прибора
        st, d = self.req("POST", "/api/catalog/new", {"fields": GOOD_ADAPTER})
        self.assertEqual((st, d["id"]), (200, "swd_10to4"))
        st, d = self.req("POST", "/api/catalog/new", {"template": True, "kind": "engine", "id": "my_tool", "title": "x"})
        self.assertEqual((st, d["open"]), (200, "catalog/engines/my_tool.yaml"))
        self.assertEqual(self.req("GET", "/api/catalog/card?id=my_tool")[0], 200)

    def test_page_has_catalog(self):
        st, body = self.req("GET", "/")
        for marker in (b'data-tab="catalog"', b"function renderCatalog", b"function drawWiring", b"function drawPackage",
                       b"function drawHeader", b"function renderCardBuilder", b"function renderCatalogMap"):
            self.assertIn(marker, body)
        self.assertIn(b"repeat(6, minmax(0, 1fr))", body)

    def test_wizard_and_cli(self):
        ui = AutoUI([None])
        Wizard(self.cfg, sim=True, ui=ui, ask_text=lambda p: "").action("catalog", {"card": "usbasp"})
        text = "\n".join(ui.lines)
        self.assertIn("USBasp", text)
        self.assertIn("1 · MOSI", text)
        shown = []

        class Picky(AutoUI):
            def choose(self, question, options):
                shown.extend(options)
                return None

        Wizard(self.cfg, sim=True, ui=Picky(), ask_text=lambda p: "").action("catalog", {"kind": "diy"})
        self.assertTrue(shown and all("собрать" in o.lower() or "доработать" in o.lower() for o in shown), shown)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", str(self.root / "config.yaml"), "catalog", "flashrom"]), 0)
            self.assertEqual(cli.main(["--config", str(self.root / "config.yaml"), "catalog", "--kind", "engine"]), 0)
            self.assertEqual(cli.main(["--config", str(self.root / "config.yaml"), "catalog", "nope"]), 1)
        text = out.getvalue()
        self.assertIn("Carl-Daniel Hailfinger", text)
        self.assertIn("probe_rs", text)
        self.assertTrue(shown[0].startswith("[2] "), "в мастере у каждой карточки — номер ступени")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", str(self.root / "config.yaml"), "catalog"])
        heads = [ln.strip() for ln in out.getvalue().splitlines() if ln.strip()[:2] in ("1.", "2.", "3.", "4.", "5.")]
        self.assertEqual([h[:2] for h in heads], ["1.", "2.", "3.", "4.", "5."], "список в терминале — по ступеням")
        self.assertIn("Первая флешка", heads[0])


# ======================= демо для GitHub Pages =======================
class PagesTests(unittest.TestCase):
    def test_demo_builds_from_real_core(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("build_pages", ROOT / "tools" / "build_pages.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = Path(tempfile.mkdtemp(prefix="dirizher_site_")) / "site"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(mod.main(["--out", str(out), "--quick"]), 0)
            page = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn('<script src="demo.js"></script>', page)
            self.assertTrue((out / ".nojekyll").exists())
            jobs = json.loads((out / "demo" / "jobs.json").read_text(encoding="utf-8"))
            core = json.loads((out / "demo" / "core.json").read_text(encoding="utf-8"))
            self.assertIn(jobs["start"], jobs["jobs"])
            self.assertIn("corrupt-ref", jobs["jobs"], "эталон и поломка записаны")
            repair = jobs["jobs"]["corrupt-ref"]["run"]["root"]
            self.assertEqual(repair["q"]["type"], "pause")
            # у учебной платы два способа (весь эталон или только разделы) — сначала выбор, затем слово «ДА»
            pick = repair["br"]["ok"]
            self.assertEqual(pick["q"]["type"], "choose")
            self.assertEqual(len(pick["q"]["options"]), 2)
            self.assertEqual(pick["br"]["0"]["q"]["type"], "confirm_word", "ремонт доходит до слова «ДА»")
            self.assertTrue(core["hello"]["demo"])
            self.assertEqual(len(core["cards"]), core["catalog"]["total"])
            raw = json.dumps([jobs, core], ensure_ascii=False)
            self.assertNotIn("dirizher_pages_", raw, "в записи нет путей временной папки сборки")
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
