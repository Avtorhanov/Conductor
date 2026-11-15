"""Автотесты интерфейсов: Штурман, конструктор профиля, веб-сервер, текстовый мастер.

Всё на симуляторе, без железа и без сети.   Запуск:  python3 -m unittest tests.test_interface -v
"""
from __future__ import annotations

import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.profiles import Profile
from dirizher.core.ui import AutoUI
from dirizher.interface import builder, guide, web
from dirizher.interface.wizard import Wizard
from dirizher.plugins.simulator import SimulatorPlugin

ROOT = Path(__file__).resolve().parent.parent


class StrictUI(AutoUI):
    """Как AutoUI, но если ответы кончились — ошибка (чтобы мастер не зациклился)."""

    def _next(self, default):
        if not self.answers:
            raise AssertionError("сценарий теста закончился раньше, чем мастер")
        return self.answers.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_ui_"))
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

    def make_reference(self):
        c = self.conductor(["yes"])
        c.plugin("simulator").do_reset()
        self.assertEqual(c.memory.session(c.run())["outcome"], "healthy")


# ======================= Штурман =======================
class GuideTests(Base):
    def test_guide_file_is_valid(self):
        g = guide.load_guide()
        self.assertEqual(guide.validate(yaml.safe_load(guide.GUIDE_PATH.read_text(encoding="utf-8"))), [])
        self.assertIn(g["start"], g["nodes"])
        for lesson in g["lessons"].values():
            self.assertIn(lesson["group"], guide.LESSON_GROUPS)
            for st in lesson["steps"]:
                self.assertIsInstance(st["text"], str)

    def test_validate_catches_mistakes(self):
        bad = {"start": "home", "lessons": {},
               "nodes": {"home": {"ask": "?", "options": [
                   {"label": "a", "next": "nowhere"},
                   {"label": "b", "lesson": "missing"},
                   {"label": "c", "action": "format_chip"},
                   {"label": "d", "action": "run", "params": {"dry_run": "yes"}},
                   {"label": "e", "action": "sim", "params": {"do": "explode"}},
                   {"label": "f", "next": "home", "lesson": "x"}]},
                   "lost": {"ask": "?", "options": [{"label": "z", "next": "home"}]}}}
        errors = "\n".join(guide.validate(bad))
        for part in ("nowhere", "missing", "format_chip", "dry_run", "do —", "ровно один", "lost"):
            self.assertIn(part, errors)

    def test_first_start_suggests_training(self):
        with guide.open_readonly(self.cfg, True) as c:
            labels = [s.get("lesson") for s in guide.suggest(c)]
        self.assertIn("sim_tour", labels)

    def test_real_mode_without_tools_says_so(self):
        self.cfg["plugins"]["flashrom_spi"]["enabled"] = False
        with guide.open_readonly(self.cfg, False) as c:
            items = guide.suggest(c)
        self.assertEqual(items[0]["level"], "important")
        self.assertIn("инструмент", items[0]["label"])
        self.assertTrue(any(i.get("next") == "sim" for i in items))

    def test_pending_feedback_and_rollback_suggestions(self):
        self.make_reference()
        c = self.conductor(["later"])                 # «заработало?» — позже
        c.plugin("simulator").do_corrupt()
        sid = c.run()
        self.assertEqual(c.memory.session(sid)["outcome"], "written_verified")
        with guide.open_readonly(self.cfg, True) as r:
            items = guide.suggest(r)
            picks = guide.pickable(r, "feedback")
        self.assertEqual(items[0]["action"], "feedback")
        self.assertEqual(items[0]["params"], {"session": sid})
        self.assertEqual(picks[0]["id"], sid)
        c.feedback(sid, False)                        # не помогло → предложит откат
        with guide.open_readonly(self.cfg, True) as r:
            items = guide.suggest(r)
        self.assertTrue(any(i.get("action") == "rollback" and i["params"] == {"session": sid} for i in items))
        c.rollback(sid)                               # после отката подсказка исчезает
        with guide.open_readonly(self.cfg, True) as r:
            items = guide.suggest(r)
        self.assertFalse(any(i.get("action") == "rollback" for i in items))

    def test_draft_suggestion_until_profile_exists(self):
        c = self.conductor()
        c.plugin("simulator").JEDEC = "C84018"
        c.detect_only()
        with guide.open_readonly(self.cfg, True) as r:
            items = guide.suggest(r)
        draft = [i for i in items if i.get("action") == "builder"]
        self.assertEqual(len(draft), 1)
        self.assertIn("C84018", draft[0]["label"])

    def test_after_gives_sensible_next_steps(self):
        dry = guide.after("run", {"dry_run": True, "label": "x"}, "dry_run", 5)
        self.assertEqual(dry[0]["action"], "run")
        self.assertEqual(dry[0]["params"], {"label": "x"})           # «по-настоящему» — без dry_run
        self.assertEqual(guide.after("run", {}, "not_repaired", 7)[0],
                         {"label": "Вернуть копию «до»", "level": "important", "action": "rollback",
                          "params": {"session": 7}})
        self.assertEqual(guide.after("run", {}, "written_verified", 3)[0]["action"], "feedback")
        self.assertEqual(guide.after("sim", {"do": "corrupt"}, None, None)[1]["action"], "run")
        g = guide.load_guide()
        for outcome in ("detected", "not_detected", "no_profile", "blocked_voltage", "healthy", "no_action",
                        "blocked", "dry_run", "cancelled", "repaired", "not_repaired", "interrupted"):
            for o in guide.after("run", {}, outcome, 1):
                self.assertEqual(guide.validate({"start": "home", "nodes": g["nodes"], "lessons": g["lessons"]}), [])
                if "lesson" in o:
                    self.assertIn(o["lesson"], g["lessons"])
                if "next" in o:
                    self.assertIn(o["next"], g["nodes"])
                if "action" in o:
                    self.assertIn(o["action"], guide.ACTIONS)

    def test_execute_runs_core_and_sim_is_sim_only(self):
        c = self.conductor(["yes"])
        guide.execute(c, "sim", {"do": "reset"})
        sid = guide.execute(c, "run", {})
        self.assertEqual(guide.outcome_of(c, sid), "healthy")
        real = Conductor(self.cfg, AutoUI(), sim=False)
        self.opened.append(real)
        with self.assertRaises(ValueError):
            guide.execute(real, "sim", {"do": "corrupt"})

    def test_roadmap_reads_status(self):
        docs = self.tmp / "docs"
        docs.mkdir()
        (docs / "STATUS.md").write_text("# С\n## Готово\n- x\n## Следующие шаги (по порядку)\n"
                                        "1. Первый чип.\n2. Профили.\n\n## Другое\n- y\n", encoding="utf-8")
        self.cfg["_path"] = self.tmp / "config.yaml"
        self.assertEqual(guide.roadmap(self.cfg), ["Первый чип.", "Профили."])


# ======================= конструктор профиля =======================
class BuilderTests(Base):
    GOOD = {"id": "gigadevice_gd25q64", "name": "GigaDevice GD25Q64", "jedec_id": "c8 40 17",
            "voltage": "3,3", "size": "64 Мбит", "allow_write": "да"}

    def test_parse_size(self):
        for text, want in (("8 МБ", 8 << 20), ("64 Мбит", 8 << 20), ("0x800000", 8 << 20),
                           ("8388608", 8 << 20), ("512K", 512 << 10), ("", None), (None, None)):
            self.assertEqual(builder.parse_size(text), want, text)
        for bad in ("восемь", "-1", "1.5 байт", "8 попугаев"):
            with self.assertRaises(builder.BuilderError):
                builder.parse_size(bad)

    def test_build_produces_loadable_profile(self):
        res = builder.build(self.GOOD)
        self.assertEqual(res["errors"], [])
        d = yaml.safe_load(res["yaml"])
        prof = Profile.from_dict(d, Path("x.yaml"))
        self.assertEqual(prof.match, {"jedec_id": "C84017"})
        self.assertEqual(prof.voltage, 3.3)
        self.assertEqual(prof.size, 8 << 20)
        self.assertTrue(prof.recipes)
        self.assertTrue(any("GigaDevice" in h for h in res["hints"]))

    def test_build_reports_errors_and_warnings(self):
        res = builder.build({"id": "Bad Id", "jedec_id": "XYZ", "voltage": "12"})
        text = " ".join(res["errors"])
        for part in ("Короткое имя", "Название", "JEDEC", "1.0–5.5"):
            self.assertIn(part, text)
        res = builder.build(dict(self.GOOD, size="4 МБ"))
        self.assertTrue(any("не совпадает" in w for w in res["warnings"]))

    def test_draft_never_overwrites_and_enable_needs_confirmation(self):
        drafts = self.tmp / "drafts"
        p1 = builder.save_draft(drafts, self.GOOD)
        p2 = builder.save_draft(drafts, dict(self.GOOD, notes="другое"))
        self.assertNotEqual(p1, p2)
        self.assertTrue(p1.exists() and p2.exists())
        profiles = self.tmp / "profiles"
        with self.assertRaises(builder.BuilderError):
            builder.enable(profiles, self.GOOD, confirmed=False)
        with self.assertRaises(builder.BuilderError):
            builder.enable(profiles, dict(self.GOOD, voltage=""), confirmed=True)
        path = builder.enable(profiles, self.GOOD, confirmed=True)
        self.assertEqual(path, profiles / "chips" / "gigadevice_gd25q64.yaml")
        with self.assertRaises(builder.BuilderError):              # второй раз — не перезаписывает
            builder.enable(profiles, dict(self.GOOD, name="Другое"), confirmed=True)
        self.assertIn("GigaDevice GD25Q64", path.read_text(encoding="utf-8"))

    def test_enabled_profile_is_used_by_core(self):
        self.cfg["profiles_dir"] = self.tmp / "profiles"
        shutil.copytree(ROOT / "profiles", self.cfg["profiles_dir"])
        builder.enable(self.cfg["profiles_dir"], dict(self.GOOD, jedec_id="C84018", id="gd_test",
                                                      size="16 МБ"), confirmed=True)
        c = self.conductor()
        c.plugin("simulator").JEDEC = "C84018"
        det = c.detect()
        self.assertEqual(det.profile.id, "gd_test")

    def test_fields_from_auto_draft(self):
        c = self.conductor()
        c.plugin("simulator").JEDEC = "C84018"
        c.detect_only()
        items = builder.list_drafts([c.data_dir / "drafts"])
        self.assertEqual(items[0]["fields"]["jedec_id"], "C84018")
        self.assertEqual(items[0]["fields"]["id"], "c84018")
        self.assertEqual(items[0]["fields"]["size"], "8 МБ")


# ======================= веб =======================
class WebUITests(unittest.TestCase):
    def ask_in_thread(self, fn):
        box = {}
        t = threading.Thread(target=lambda: box.setdefault("result", fn()))
        t.start()
        return t, box

    def wait_question(self, job):
        for _ in range(200):
            if job.question:
                return job.question
            time.sleep(0.01)
        self.fail("вопрос не появился")

    def test_question_roundtrip_and_validation(self):
        job = web.Job("run", {}, True, False)
        ui = web.WebUI(job)
        t, box = self.ask_in_thread(lambda: ui.choose("Что?", ["а", "б"]))
        q = self.wait_question(job)
        with self.assertRaises(ValueError):
            job.answer(q["qid"], 5)                   # нет такого варианта
        self.assertFalse(job.answer(q["qid"] + 1, 1))  # чужой вопрос
        self.assertTrue(job.answer(q["qid"], 1))
        t.join(2)
        self.assertEqual(box["result"], 1)

    def test_confirm_word_checked_by_server(self):
        job = web.Job("run", {}, True, False)
        ui = web.WebUI(job)
        for typed, want in (("да", True), ("YES", True), ("дa", False), ("ok", False)):   # «дa» — латинская a
            t, box = self.ask_in_thread(lambda: ui.confirm_word("Записать?", "ДА"))
            q = self.wait_question(job)
            job.answer(q["qid"], typed)
            t.join(2)
            self.assertEqual(box["result"], want, typed)

    def test_cancel_gives_safe_answers(self):
        job = web.Job("run", {}, True, False)
        ui = web.WebUI(job)
        cases = [(lambda: ui.yes_no("?"), "no"), (lambda: ui.yes_no("?", allow_later=True), "later"),
                 (lambda: ui.choose("?", ["a"]), None), (lambda: ui.confirm_word("?", "ДА"), False)]
        for fn, want in cases:
            t, box = self.ask_in_thread(fn)
            self.wait_question(job)
            self.assertTrue(job.cancel())
            t.join(2)
            self.assertEqual(box["result"], want)
        errors = []
        t = threading.Thread(target=lambda: errors.append(self._pause_raises(ui)))
        t.start()
        self.wait_question(job)
        job.cancel()
        t.join(2)
        self.assertEqual(errors, [True])

    @staticmethod
    def _pause_raises(ui):
        try:
            ui.pause("Начать?")
        except web.Interrupted:
            return True
        return False


class WebServerTests(Base):
    def setUp(self):
        super().setUp()
        self.cfg["_path"] = ROOT / "config.yaml"
        self.app = web.App(self.cfg, sim_default=True, pin="246810")
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = None

    def tearDown(self):
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, raw=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        hdrs = {"X-Dirizher": "1"}
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        hdrs.update(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        cookie = r.getheader("Set-Cookie")
        conn.close()
        ctype = r.getheader("Content-Type") or ""
        return r.status, (json.loads(payload) if "json" in ctype else payload), cookie

    def wait_job(self, until, timeout=60):
        since, job_id, end = 0, None, time.time() + timeout
        while time.time() < end:
            st, data, _ = self.req("GET", f"/api/job?wait=1&since={since}&job={job_id or ''}")
            job = data["job"]
            job_id, since = job["id"], job["seq"]
            if until(job):
                return job
        self.fail("задание не дошло до нужного состояния")

    def answer(self, job, value):
        st, data, _ = self.req("POST", "/api/answer", {"job": job["id"], "qid": job["question"]["qid"],
                                                       "value": value})
        self.assertEqual(st, 200, data)

    def run_job(self, action, params, answers):
        """Запустить задание и отвечать на вопросы по порядку: {тип вопроса: ответ} или значения."""
        st, data, _ = self.req("POST", "/api/job", {"action": action, "params": params, "sim": True})
        self.assertEqual(st, 200, data)
        answers = list(answers)
        while True:
            job = self.wait_job(lambda j: j["done"] or j["question"])
            if job["done"]:
                return job
            self.answer(job, answers.pop(0))

    def test_index_and_guide(self):
        st, body, _ = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("Дирижёр".encode(), body)
        st, g, _ = self.req("GET", "/api/guide")
        self.assertEqual(st, 200)
        self.assertIn("home", g["nodes"])
        st, hello, _ = self.req("GET", "/api/hello")
        self.assertTrue(hello["authed"] and hello["local"])
        self.assertEqual(hello["connect"]["pin"], "246810")

    def test_full_training_cycle_over_http(self):
        job = self.run_job("sim", {"do": "reset"}, [])
        self.assertEqual(job["next"][0]["action"], "run")
        job = self.run_job("run", {"dry_run": True}, [True, "yes"])       # пауза → «эталон?» да
        self.assertEqual(job["result"]["outcome"], "healthy")
        self.run_job("sim", {"do": "corrupt"}, [])
        job = self.run_job("run", {}, [True, "ДА", "later"])             # пауза → слово → «позже»
        sid = job["result"]["session"]
        self.assertEqual(job["result"]["outcome"], "written_verified")
        self.assertEqual(job["next"][0]["action"], "feedback")
        st, sug, _ = self.req("GET", "/api/suggest?sim=1")
        self.assertEqual(sug["items"][0]["params"], {"session": sid})
        job = self.run_job("feedback", {"session": sid}, ["yes"])        # «заработало?» да
        self.assertEqual(job["result"]["outcome"], "repaired")
        st, data, _ = self.req("GET", f"/api/download?sid={sid}&kind=after&sim=1")
        self.assertEqual(st, 200)
        self.assertEqual(data, SimulatorPlugin.healthy())
        st, hist, _ = self.req("GET", "/api/history?sim=1")
        self.assertEqual(hist["items"][0]["id"], sid)
        st, det, _ = self.req("GET", f"/api/session?id={sid}&sim=1")
        self.assertEqual({s["kind"] for s in det["snapshots"]}, {"before", "written", "after"})

    def test_wrong_word_cancels_and_chip_untouched(self):
        self.run_job("sim", {"do": "reset"}, [])
        self.run_job("run", {"dry_run": True}, [True, "yes"])
        self.run_job("sim", {"do": "corrupt"}, [])
        broken = (self.tmp / "data" / "simulator" / "sim" / "flash.bin").read_bytes()
        job = self.run_job("run", {}, [True, "да, наверное"])
        self.assertEqual(job["result"]["outcome"], "cancelled")
        self.assertEqual((self.tmp / "data" / "simulator" / "sim" / "flash.bin").read_bytes(), broken)

    def test_cancel_at_start_marks_interrupted(self):
        self.run_job("sim", {"do": "reset"}, [])
        self.req("POST", "/api/job", {"action": "run", "params": {}, "sim": True})
        self.wait_job(lambda j: j["question"] and j["question"]["type"] == "pause")
        st, data, _ = self.req("POST", "/api/cancel", {})
        self.assertTrue(data["ok"])
        job = self.wait_job(lambda j: j["done"])
        self.assertEqual(job["result"]["outcome"], "interrupted")

    def test_one_job_at_a_time(self):
        self.req("POST", "/api/job", {"action": "run", "params": {}, "sim": True})
        self.wait_job(lambda j: j["question"])
        st, data, _ = self.req("POST", "/api/job", {"action": "detect", "params": {}, "sim": True})
        self.assertEqual(st, 409)
        self.req("POST", "/api/cancel", {})
        self.wait_job(lambda j: j["done"])

    def test_bad_requests_rejected(self):
        st, _, _ = self.req("POST", "/api/job", {"action": "rm_rf", "sim": True})
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/job", {"action": "rollback", "params": {}, "sim": True})
        self.assertEqual(st, 400)                                        # сеанс не выбран
        st, _, _ = self.req("POST", "/api/job", {"action": "run", "params": {"image": "../../etc/passwd"},
                                                 "sim": True})
        self.assertEqual(st, 400)
        st, _, _ = self.req("POST", "/api/job", {"action": "detect"}, headers={"X-Dirizher": "0"})
        self.assertEqual(st, 403)                                        # без заголовка — чужая страница

    def test_upload_is_sanitized_and_never_overwrites(self):
        st, a, _ = self.req("POST", "/api/upload?name=../../evil.bin", raw=b"\x01" * 100,
                            headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(st, 200)
        self.assertEqual(a["name"], "evil.bin")
        st, b, _ = self.req("POST", "/api/upload?name=evil.bin", raw=b"\x02" * 100)
        self.assertEqual(b["name"], "evil_2.bin")
        st, c, _ = self.req("POST", "/api/upload?name=evil.bin", raw=b"\x01" * 100)
        self.assertEqual(c["name"], "evil.bin")                          # тот же файл — не дублируется
        self.assertEqual((self.tmp / "data" / "images" / "evil.bin").read_bytes(), b"\x01" * 100)
        st, imgs, _ = self.req("GET", "/api/images")
        self.assertEqual({i["name"] for i in imgs["items"]}, {"evil.bin", "evil_2.bin"})

    def test_image_upload_then_repair_with_image(self):
        self.run_job("sim", {"do": "erase"}, [])
        st, up, _ = self.req("POST", "/api/upload?name=full.bin", raw=SimulatorPlugin.healthy())
        self.assertEqual(st, 200)
        job = self.run_job("run", {"image": up["name"]}, [True, "ДА", "yes", "no"])
        self.assertEqual(job["result"]["outcome"], "repaired")
        self.assertEqual((self.tmp / "data" / "simulator" / "sim" / "flash.bin").read_bytes(),
                         SimulatorPlugin.healthy())

    def test_builder_endpoints(self):
        self.cfg["profiles_dir"] = self.tmp / "profiles"
        shutil.copytree(ROOT / "profiles", self.cfg["profiles_dir"])
        fields = dict(BuilderTests.GOOD)
        st, prev, _ = self.req("POST", "/api/builder/preview", {"fields": fields})
        self.assertEqual(prev["errors"], [])
        st, saved, _ = self.req("POST", "/api/builder/save", {"fields": fields})
        self.assertEqual(st, 200)
        st, drafts, _ = self.req("GET", "/api/builder/drafts")
        self.assertEqual(drafts["items"][0]["fields"]["jedec_id"], "C84017")
        st, err, _ = self.req("POST", "/api/builder/enable", {"fields": fields, "confirmed": False})
        self.assertEqual(st, 400)
        st, ok, _ = self.req("POST", "/api/builder/enable", {"fields": fields, "confirmed": True})
        self.assertEqual(st, 200)
        self.assertTrue((self.tmp / "profiles" / "chips" / ok["file"]).exists())

    def test_remote_needs_pin_and_locks_after_guessing(self):
        self.app.trust_local = False                                     # ведём себя как телефон
        st, _, _ = self.req("GET", "/api/guide")
        self.assertEqual(st, 401)
        st, hello, _ = self.req("GET", "/api/hello")
        self.assertFalse(hello["authed"])
        st, _, cookie = self.req("POST", "/api/login", {"pin": "246810"})
        self.assertEqual(st, 200)
        self.cookie = cookie.split(";")[0]
        st, _, _ = self.req("GET", "/api/guide")
        self.assertEqual(st, 200)
        self.cookie = None
        for _ in range(web.LOCK_AFTER):
            st, _, _ = self.req("POST", "/api/login", {"pin": "000000"})
            self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/api/login", {"pin": "246810"})
        self.assertEqual(st, 429)                                        # даже верный код — после паузы


# ======================= текстовый мастер =======================
class WizardTests(Base):
    def index(self, g, node, part, offset=0):
        for i, o in enumerate(g["nodes"][node]["options"]):
            if part in o["label"]:
                return i + offset
        raise KeyError(part)

    def test_training_through_wizard(self):
        g = guide.load_guide()
        with guide.open_readonly(self.cfg, True) as c:
            n_sug = len(guide.suggest(c))
        answers = [
            self.index(g, "home", "Потренироваться", n_sug),
            self.index(g, "sim", "Исправная прошивка"),
            0,          # что дальше → «Снять копию и сохранить эталон» (план без записи)
            "yes",      # устройство исправно → эталон
            None,       # что дальше → назад
            self.index(g, "sim", "Сбой обновления"),
            1,          # что дальше → «Чинить»
            "yes",      # заработало
            None,       # что дальше → назад
            None, None,
        ]
        ui = StrictUI(answers, confirm=True)
        self.assertEqual(Wizard(self.cfg, sim=True, ui=ui, ask_text=lambda p: "").run(), 0)
        text = "\n".join(ui.lines)
        self.assertIn("эталон «example_sim_board» сохранён", text)
        self.assertIn("отремонтировано", text)
        sim_flash = (self.tmp / "data" / "simulator" / "sim" / "flash.bin").read_bytes()
        self.assertEqual(sim_flash, SimulatorPlugin.healthy())

    def test_lesson_and_builder_in_wizard(self):
        g = guide.load_guide()
        with guide.open_readonly(self.cfg, True) as c:
            n_sug = len(guide.suggest(c))
        self.cfg["profiles_dir"] = self.tmp / "profiles"
        shutil.copytree(ROOT / "profiles", self.cfg["profiles_dir"])
        typed = iter(["gd_wizard", "GigaDevice GD25Q128", "C84018", "3.3", "16 МБ", "", ""])
        answers = [
            self.index(g, "home", "Развивать", n_sug),
            self.index(g, "develop", "Новый чип"),     # урок
            0,                                         # дальше → «Открыть конструктор профиля»
            2,                                         # «Включить профиль»
            "yes",                                     # сверено с даташитом
            None,                                      # что дальше → назад
            None, None,
        ]
        ui = StrictUI(answers, confirm=True)
        Wizard(self.cfg, sim=True, ui=ui, ask_text=lambda p: next(typed)).run()
        self.assertTrue((self.tmp / "profiles" / "chips" / "gd_wizard.yaml").exists())
        self.assertIn("Новый чип за 5 минут", "\n".join(ui.lines))


class NavyThemeTests(unittest.TestCase):
    """Тема «Нави» вместо тёмной: переключатель, переменные обеих тем, работа без сети."""

    TOKENS = ("--tab-radius", "--tab-bg", "--tab-on-bg", "--sh-raised", "--sh-on", "--sh-card",
              "--sh-press", "--sh-sunken", "--edge", "--font-head", "--bad-ink")

    def setUp(self):
        self.html = (ROOT / "dirizher" / "interface" / "static" / "index.html").read_text(encoding="utf-8")

    def block(self, selector: str) -> str:
        self.assertIn(selector + " {", self.html)
        return self.html.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_switch_offers_navy_instead_of_dark(self):
        self.assertIn('["navy", "Нави"]', self.html)
        self.assertNotIn('"Тёмное"', self.html)
        self.assertIn('t === "dark" ? "navy" : t', self.html)       # старая настройка открывается как «Нави»
        self.assertIn(':root:not([data-theme="navy"])', self.html)  # системная светлая не перебивает выбор «Нави»

    def test_both_themes_define_shape_and_volume(self):
        navy = self.block(":root")
        light = self.block(':root[data-theme="light"]')
        light_auto = self.block(':root:not([data-theme="navy"])')
        for token in self.TOKENS:
            self.assertIn(token + ":", navy, token)
            self.assertIn(token + ":", light, token)
            self.assertIn(token + ":", light_auto, token)
        self.assertIn("--tab-radius: 14px", navy)       # скруглённые прямоугольники, не «таблетки»
        self.assertIn("--sh-raised: none", light)       # светлая тема осталась плоской

    def test_page_works_offline(self):
        for bad in ("https://", "@import", "url(http", "fonts.googleapis", '<link rel="stylesheet"'):
            self.assertNotIn(bad, self.html)


class MobileLayoutTests(unittest.TestCase):
    """Телефон: шапка без просвета, подписи вкладок внутри кнопок, копирование значком рядом с командой."""

    def setUp(self):
        self.html = (ROOT / "dirizher" / "interface" / "static" / "index.html").read_text(encoding="utf-8")

    def block(self, selector: str) -> str:
        self.assertIn(selector + " {", self.html)
        return self.html.split(selector + " {", 1)[1].split("}", 1)[0]

    def test_header_fill_covers_area_above_screen(self):
        fill = self.block(".top::before")
        self.assertIn("position: fixed", fill)          # только над экраном: полосу демо не закрывает
        self.assertIn("bottom: 100%", fill)
        self.assertIn("var(--top-bg)", fill)
        self.assertIn("pointer-events: none", fill)
        self.assertIn('<meta name="theme-color" content="#08112a">', self.html)   # цвет полосы = цвет шапки
        self.assertIn("--top-bg: #08112a", self.html)
        bottom = self.block(".tabs::after")
        self.assertIn("top: 100%", bottom)
        self.assertIn("var(--tabs-bg)", bottom)

    def test_tab_labels_stay_inside_buttons(self):
        self.assertIn("repeat(6, minmax(0, 1fr))", self.block(".tabs"))   # длинное слово не раздвигает колонку
        self.assertIn("min-width: 0", self.block(".tab"))
        label = self.block(".tab span")
        for rule in ("white-space: nowrap", "overflow: hidden", "text-overflow: ellipsis"):
            self.assertIn(rule, label)
        # на телефоне — короткая подпись, полное имя остаётся для ПК и для экранного диктора
        self.assertIn('<span class="full">Справочник</span><span class="short">Каталог</span>', self.html)
        self.assertIn('aria-label="Справочник"', self.html)
        self.assertIn("@media (max-width: 480px) and (min-height: 521px)", self.html)  # экраны прибора не задеты

    def test_copy_is_icon_beside_code(self):
        box = self.html.split("function codeBox(code) {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('"aria-label": "Копировать команду"', box)
        self.assertIn("copyIcon(false)", box)
        self.assertNotIn('"Копировать")', box)          # не текстовая кнопка поверх команды
        copy = self.block(".code .copy")
        self.assertNotIn("position: absolute", copy)
        self.assertIn("flex: none", copy)
        pre = self.block(".code pre")
        self.assertIn("min-width: 0", pre)
        self.assertIn("white-space: pre-wrap", pre)     # длинная команда видна целиком
        self.assertIn("display: flex", self.block(".code"))


if __name__ == "__main__":
    unittest.main()
