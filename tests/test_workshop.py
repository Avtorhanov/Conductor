"""Мастерская без железа: навыки и сторож кода, капот, самопроверка, «что помогло», справка, веб.

Каждый тест работает на копии проекта во временной папке — настоящие файлы не меняются.
Запуск:  python3 -m unittest tests.test_workshop -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.skills import lint
from dirizher.core.ui import AutoUI
from dirizher.interface import guide, web
from dirizher.interface.wizard import Wizard
from dirizher.plugins.simulator import SimulatorPlugin
from dirizher.workshop import doctor, helpdocs, skillmaker
from dirizher.workshop.hood import Hood, HoodError, classify

ROOT = Path(__file__).resolve().parent.parent

DEMO_SKILL = '''
from dirizher.core.checks import register
from dirizher.core.models import Detection, Finding
from dirizher.core.plugin import Plugin


class DemoPlugin(Plugin):
    name = "demo_tool"
    title = "Демо-лезвие"
    target_class = "device.demo"
    hardware = True

    def available(self):
        return True, "готов, порт " + str(self.settings.get("port"))

    def probe(self):
        return Detection(plugin=self.name, target_class=self.target_class, fingerprint={"name": "DEMO"},
                         confidence=0.9, summary="демо")

    def read(self, det):
        return bytes(8)

    def write(self, det, data):
        pass


@register("demo_check")
def demo_check(ci):
    return Finding("demo_check", "ok", "демо")
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_ws_"))
        self.root = self.tmp / "root"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", "data", "__pycache__", "tests"))
        self.cfg = load_config(self.root / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = self.tmp / "mirror"
        self.hood = Hood(self.cfg)
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conductor(self, answers=None, confirm=True, sim=True, dry_run=False):
        c = Conductor(self.cfg, AutoUI(answers, confirm), sim=sim, dry_run=dry_run)
        self.opened.append(c)
        return c

    def hardware_only(self, **plugins):
        for name in list(self.cfg["plugins"]):
            self.cfg["plugins"][name] = {"enabled": False}
        self.cfg["plugins"].update(plugins)

    def write_skill(self, sid: str, code: str | None = DEMO_SKILL, manifest: str | None = None) -> Path:
        folder = self.root / "skills" / sid
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "skill.yaml").write_text(manifest or f"id: {sid}\ntitle: Демо\nkind: tool\n"
                                           f"settings: {{enabled: true, port: A}}\n", encoding="utf-8")
        if code is not None:
            (folder / "skill.py").write_text(code, encoding="utf-8")
        return folder

    def report(self, c, name):
        return next((r for r in c.plugin_report if r["name"] == name), None)


# ======================= навыки и сторож =======================
class SkillTests(Base):
    def test_lint_rules(self):
        blocked = {
            "import subprocess": "self.run",
            "import os\nos.remove('x')": "удалять",
            "import os\nos.system('ls')": "self.run",
            "import shutil\nshutil.rmtree('x')": "удалять",
            "from pathlib import Path\nPath('x').unlink()": "удалять",
            "eval('1')": "eval",
            "class P:\n    def go(self):\n        return self.c.store": "хранилище",
            "class P(Plugin):\n    def probe(self):\n        self.write(None, b'')": "только",
            "def f(:\n    pass": "синтаксическая",
        }
        for code, text in blocked.items():
            with self.subTest(code=code):
                blocks, _ = lint(code)
                self.assertTrue(blocks, code)
                self.assertIn(text, blocks[0])
        fine = "import tempfile\nx = [1, 2]\nx.remove(1)\nwith tempfile.TemporaryDirectory() as d:\n    pass\n"
        self.assertEqual(lint(fine), ([], []))
        self.assertTrue(lint("import socket")[1])                  # сеть — предупреждение, не запрет

    def test_builtin_code_passes_the_guard(self):
        files = [p for p in (ROOT / "dirizher" / "plugins").glob("*.py") if p.name != "simulator.py"]
        files += list((ROOT / "skills").rglob("*.py")) + list((ROOT / "dirizher" / "helpers").glob("*.py"))
        for p in files:
            with self.subTest(file=p.name):
                self.assertEqual(lint(p.read_text(encoding="utf-8"), str(p))[0], [])

    def test_skill_brings_plugin_check_and_profile(self):
        folder = self.write_skill("demo")
        (folder / "profiles").mkdir()
        (folder / "profiles" / "demo_dev.yaml").write_text(
            "id: demo_dev\nname: Демо-прибор\nclass: device.demo\nmatch: {name: DEMO}\n"
            "params:\n  - {id: speed, title: Скорость, offset: 0, type: u16be}\n", encoding="utf-8")
        self.cfg["plugins"]["demo_tool"] = {"port": "B"}              # config.yaml важнее skill.yaml
        self.hardware_only(demo_tool={"port": "B"})
        c = self.conductor(sim=False)
        row = self.report(c, "demo_tool")
        self.assertTrue(row["active"], row)
        self.assertEqual(row["skill"], "demo")
        self.assertIn("порт B", row["status"])
        from dirizher.core.checks import CHECKS
        self.assertIn("demo_check", CHECKS)
        self.assertIsNotNone(c.profiles.get("demo_dev"))
        self.assertEqual(c.memory.session(c.read_params())["outcome"], "params_read")

    def test_broken_skills_do_not_break_the_rest(self):
        self.write_skill("demo")
        self.write_skill("bad_yaml", None, "id: [")
        self.write_skill("bad_syntax", "def x(:\n")
        self.write_skill("bad_guard", "import subprocess\n")
        self.write_skill("bad_import", "raise RuntimeError('сломан')\n")
        self.write_skill("Wrong", None)
        self.hardware_only()
        c = self.conductor(sim=False)
        self.assertTrue(self.report(c, "demo_tool")["active"])
        errors = " ".join(r["status"] for r in c.plugin_report if r["title"] == "ошибка загрузки")
        self.assertIn("синтаксическая", errors)
        self.assertIn("сторож", errors)
        self.assertIn("сломан", errors)
        broken = {Path(f).name for f, _ in c.skills.errors}
        self.assertEqual(broken, {"bad_yaml", "Wrong"})

    def test_edit_is_picked_up_without_restart(self):
        folder = self.write_skill("demo")
        self.hardware_only()
        self.assertEqual(self.report(self.conductor(sim=False), "demo_tool")["title"], "Демо-лезвие")
        code = folder / "skill.py"
        code.write_text(DEMO_SKILL.replace("Демо-лезвие", "Новое имя"), encoding="utf-8")
        st = code.stat()
        os.utime(code, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
        self.assertEqual(self.report(self.conductor(sim=False), "demo_tool")["title"], "Новое имя")

    def test_builtin_names_cannot_be_taken(self):
        self.write_skill("demo", DEMO_SKILL.replace('name = "demo_tool"', 'name = "flashrom_spi"'))
        c = self.conductor(sim=False)
        self.assertTrue(any("уже есть" in r["status"] for r in c.plugin_report))

    def test_templates_make_working_skills(self):
        made = {k: skillmaker.create_skill(self.hood, k, f"new_{k}", f"Новый: {k}") for k in ("map", "tool", "check")}
        for kind, res in made.items():
            for rel in res["files"]:
                with self.subTest(file=rel):
                    self.assertEqual(self.hood.check(rel, (self.root / rel).read_text(encoding="utf-8"))["errors"], [])
        self.hardware_only()
        c = self.conductor(sim=False)
        self.assertEqual(c.skills.errors, [])
        row = self.report(c, "new_tool")
        self.assertFalse(row["active"])                              # лезвие нового навыка выключено
        self.assertEqual(len(c.profiles.get("new_map").params), 1)
        from dirizher.core.checks import CHECKS
        self.assertIn("new_check", CHECKS)
        info = guide.skills_info(c)
        self.assertEqual({s["id"] for s in info["items"]} >= {"new_map", "new_tool", "new_check", "modbus_rtu"}, True)
        with self.assertRaises(HoodError):
            skillmaker.create_skill(self.hood, "map", "new_map", "ещё раз")
        for sid in ("1bad", "Bad", "x", "a-b"):
            with self.assertRaises(HoodError):
                skillmaker.create_skill(self.hood, "map", sid, "t")


# ======================= капот =======================
class HoodTests(Base):
    PROFILE = "profiles/chips/winbond_w25q64.yaml"

    def test_policy(self):
        cases = {
            "skills/modbus_rtu/skill.py": "edit", "profiles/chips/x.yaml": "edit", "help/hood.md": "edit",
            "dirizher/interface/guide.yaml": "edit", "dirizher/plugins/flashrom_spi.py": "edit",
            "dirizher/core/safety.py": "view", "dirizher/core/pipeline.py": "view", "config.yaml": "view",
            "dirizher/plugins/_common.py": "view", "dirizher/plugins/simulator.py": "view",
            "dirizher/interface/web.py": "view", "dirizher/helpers/bp5_i2c.py": "view",
            "../etc/passwd": None, "/etc/passwd": None, "data/memory.yaml": None, ".git/config": None,
            "skills/../config.yaml": None, "profiles/x.bin": None, "tests/test_x.py": None,
        }
        for rel, mode in cases.items():
            with self.subTest(rel=rel):
                self.assertEqual(classify(rel)[0], mode)

    def test_areas(self):
        work = self.hood.files("work")
        self.assertTrue(work and all(f["mode"] == "view" for f in work))
        device = {f["path"]: f["mode"] for f in self.hood.files("device")}
        self.assertEqual(device["config.yaml"], "view")
        self.assertEqual(device["dirizher/plugins/flashrom_spi.py"], "edit")
        self.assertIn("skills/modbus_rtu/skill.py", {f["path"] for f in self.hood.files("dev")})
        with self.assertRaises(HoodError):
            self.hood.files("nope")

    def test_save_validates_and_never_breaks_the_file(self):
        original = (self.root / self.PROFILE).read_text(encoding="utf-8")
        bad = {
            self.PROFILE: ["id: [", "id: x\nname: y\n", original.replace("winbond_w25q64\n", "macronix_mx25l6406\n")],
            "skills/modbus_rtu/skill.yaml": ["id: Wrong\ntitle: t\n"],
            "skills/modbus_rtu/skill.py": ["def x(:\n", "import subprocess\n", "raise RuntimeError('boom')\n"],
            "help/hood.md": ["---\n[\n---\ntext"],
        }
        for rel, texts in bad.items():
            before = (self.root / rel).read_text(encoding="utf-8")
            for text in texts:
                with self.subTest(rel=rel, text=text[:20]):
                    res = self.hood.save(rel, text, word="ДА")
                    self.assertFalse(res["ok"])
                    self.assertTrue(res["errors"])
                    self.assertEqual((self.root / rel).read_text(encoding="utf-8"), before)
        g = (self.root / "dirizher/interface/guide.yaml").read_text(encoding="utf-8")
        self.assertTrue(self.hood.check("dirizher/interface/guide.yaml", g.replace("next: repair\n", "next: nowhere\n", 1))["errors"])
        self.assertEqual(self.hood.check("dirizher/interface/guide.yaml", g)["errors"], [])
        res = self.hood.save("dirizher/core/safety.py", "x = 1\n", word="ДА")
        self.assertFalse(res["ok"])

    def test_word_history_and_restore(self):
        original = (self.root / self.PROFILE).read_text(encoding="utf-8")
        edited = original.replace("voltage: 3.3", "voltage: 3.6")
        self.assertFalse(self.hood.save(self.PROFILE, edited)["ok"])                  # без слова
        res = self.hood.save(self.PROFILE, edited, word="да", actor="тест")
        self.assertTrue(res["ok"], res)
        self.assertEqual((self.root / self.PROFILE).read_text(encoding="utf-8"), edited)
        hist = self.hood.history(self.PROFILE)
        self.assertEqual([h["note"] for h in hist], ["правка", "до правки"])
        self.assertTrue(self.hood.save(self.PROFILE, edited, word="ДА")["unchanged"])
        self.assertTrue(self.hood.restore(self.PROFILE, hist[1]["vid"], word="YES")["ok"])
        self.assertEqual((self.root / self.PROFILE).read_text(encoding="utf-8"), original)
        self.assertEqual(len(self.hood.history(self.PROFILE)), 3)                     # возврат — тоже версия
        self.assertTrue(self.hood.journal_rows())
        help_res = self.hood.save("help/hood.md", "# Капот\nновый текст\n")            # справке слово не нужно
        self.assertTrue(help_res["ok"], help_res)
        for bad in ("../x", "nope/../../x"):
            with self.assertRaises(HoodError):
                self.hood.version(self.PROFILE, bad)

    def test_heal_returns_last_working_version(self):
        original = (self.root / self.PROFILE).read_text(encoding="utf-8")
        self.assertTrue(self.hood.save(self.PROFILE, original.replace("3.3", "3.4"), word="ДА")["ok"])
        (self.root / self.PROFILE).write_text("id: [\n", encoding="utf-8")               # сломали мимо капота
        self.assertIsNotNone(self.hood.last_good(self.PROFILE))
        self.assertFalse(self.hood.heal(self.PROFILE, word="нет")["ok"])
        self.assertTrue(self.hood.heal(self.PROFILE, require_word=False)["ok"])
        self.assertIn("voltage: 3.4", (self.root / self.PROFILE).read_text(encoding="utf-8"))


# ======================= самопроверка =======================
class DoctorTests(Base):
    def make_reference(self):
        c = self.conductor(["yes"])
        c.plugin("simulator").do_reset()
        self.assertEqual(c.memory.session(c.run())["outcome"], "healthy")
        return c

    def issues(self, **kw):
        return {i["id"]: i for i in doctor.check(self.cfg, sim=True, hood=self.hood, **kw)}

    def test_copy_is_healed_from_mirror_and_bad_file_kept(self):
        c = self.make_reference()
        sha = c.memory.snapshot(1, "before")["sha"]
        primary = c.store.root / sha[:2] / f"{sha}.bin"
        primary.chmod(0o644)
        primary.write_bytes(b"broken")
        found = self.issues()
        self.assertIn(f"copy:{sha}", found)
        self.assertTrue(found[f"copy:{sha}"]["heal"])
        self.assertNotIn(f"copy:{sha}", self.issues(deep=False))                # быстрый режим хеш не считает
        done = doctor.heal(self.cfg, f"copy:{sha}", sim=True, hood=self.hood)
        self.assertTrue(any("карантин" in d for d in done))
        self.assertEqual(c.store.health(sha), {"primary": "ok", "mirror": "ok"})
        self.assertEqual(primary.read_bytes(), SimulatorPlugin.healthy())
        quarantine = list((c.data_dir / "quarantine").glob("*.bin"))
        self.assertEqual([q.read_bytes() for q in quarantine], [b"broken"])
        self.assertNotIn(f"copy:{sha}", self.issues())

    def test_lost_mirror_copy_is_restored_and_hopeless_is_reported(self):
        c = self.make_reference()
        sha = c.memory.snapshot(1, "before")["sha"]
        mirror = c.store.mirror / sha[:2] / f"{sha}.bin"
        mirror.chmod(0o644)
        mirror.unlink()                                      # «флешку вынимали» — копия пропала
        doctor.heal(self.cfg, f"copy:{sha}", sim=True, hood=self.hood)
        self.assertEqual(c.store.health(sha)["mirror"], "ok")
        for p in (c.store.root / sha[:2] / f"{sha}.bin", mirror):
            p.chmod(0o644)
            p.write_bytes(b"x")
        bad = self.issues()[f"copy:{sha}"]
        self.assertEqual(bad["level"], "bad")
        self.assertIsNone(bad["heal"])
        with self.assertRaises(Exception):
            doctor.heal(self.cfg, f"copy:{sha}", sim=True, hood=self.hood)

    def test_broken_edits_are_found_and_healed(self):
        rel = "profiles/chips/macronix_mx25l6406.yaml"
        text = (self.root / rel).read_text(encoding="utf-8")
        self.assertTrue(self.hood.save(rel, text.replace("3.3", "3.4"), word="ДА")["ok"])
        (self.root / rel).write_text("id: [\n", encoding="utf-8")
        res = skillmaker.create_skill(self.hood, "tool", "my_proto", "Протокол")
        code = self.root / "skills/my_proto/skill.py"
        code.write_text("def broken(:\n", encoding="utf-8")
        found = self.issues()
        self.assertTrue(found[f"file:{rel}"]["heal"])
        self.assertTrue(found["file:skills/my_proto/skill.py"]["heal"])
        self.assertIn("skills/my_proto/skill.py", res["files"])
        for iid in (f"file:{rel}", "file:skills/my_proto/skill.py"):
            doctor.heal(self.cfg, iid, sim=True, hood=self.hood, require_word=False)
        after = self.issues()
        self.assertFalse([i for i in after if i.startswith("file:")], after)

    def test_cli(self):
        self.make_reference()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(doctor.main(self.cfg, sim=True), 0)
        self.assertIn("Самопроверка", out.getvalue())


# ======================= обучение: «что помогло» =======================
class LearningTests(Base):
    def failed_session(self):
        c = self.conductor()
        sim = c.plugin("simulator")
        sim.do_reset()
        sim.do_flaky()
        sid = c.run()
        self.assertEqual(c.memory.session(sid)["outcome"], "blocked")
        sim.do_steady()
        return c, sid

    def test_fix_is_remembered_and_suggested_next_time(self):
        c, sid = self.failed_session()
        self.assertIsNotNone(c.learning.signature(sid))
        with self.assertRaises(ValueError):
            c.learning.remember_fix(sid, "  ")
        fid = c.learning.remember_fix(sid, "переставил прищепку")
        c.learning.remember_fix(sid, "короче провода")
        c.learning.confirm_fix(fid, sid)
        c2, sid2 = self.failed_session()
        hints = c2.learning.hints(sid2)
        self.assertEqual([h["text"] for h in hints], ["переставил прищепку", "короче провода"])
        self.assertEqual(hints[0]["helped"], 2)
        options = guide.after("run", {}, "blocked", sid2, True, hints)
        self.assertEqual(options[0]["action"], "fix")
        self.assertEqual(options[0]["params"], {"session": sid2, "fix": fid})
        self.assertTrue(any(o["label"] == "Записать, что помогло" for o in options))

    def test_success_has_nothing_to_learn(self):
        c = self.conductor(["yes"])
        c.plugin("simulator").do_reset()
        sid = c.run()
        self.assertIsNone(c.learning.signature(sid))
        with self.assertRaises(ValueError):
            c.learning.remember_fix(sid, "что-то")

    def test_suggest_asks_what_helped_after_recovery(self):
        c, sid = self.failed_session()
        c2 = self.conductor(dry_run=True)
        c2.run()                                               # следующий сеанс прошёл
        prompts = [o for o in guide.suggest(c2) if o.get("action") == "fix"]
        self.assertEqual(prompts[0]["params"], {"session": sid})
        c2.learning.remember_fix(sid, "переставил прищепку")
        self.assertFalse([o for o in guide.suggest(c2) if o.get("action") == "fix"])


# ======================= справка =======================
class HelpTests(Base):
    def test_builtin_articles_are_valid(self):
        items = helpdocs.articles(self.cfg)
        arts = [i for i in items if i["kind"] == "article"]
        self.assertGreaterEqual(len(arts), 9)
        for it in arts:
            with self.subTest(id=it["id"]):
                self.assertIn(it["section"], skillmaker.SECTIONS)
                self.assertTrue(it["summary"])
        self.assertTrue(any(i["kind"] == "lesson" for i in items))
        self.assertIn("skill.modbus_rtu", {i["id"] for i in arts})

    def test_search_puts_section_first(self):
        items = helpdocs.articles(self.cfg)
        self.assertEqual(helpdocs.search(items, "диапазон термодатчика")[0]["id"], "example_thermo")
        self.assertEqual(helpdocs.search(items, "", "device")[0]["section"], "device")
        self.assertEqual(helpdocs.search(items, "абракадабра-нет-такого"), [])
        self.assertIn("Капот", helpdocs.article(self.cfg, "hood")["text"])
        self.assertIsNone(helpdocs.article(self.cfg, "nope"))

    def test_new_article(self):
        rel = skillmaker.create_article(self.hood, "router_x1", "Роутер X1: прищепка", "work")
        art = helpdocs.article(self.cfg, "router_x1")
        self.assertEqual((art["title"], art["section"], art["path"]), ("Роутер X1: прищепка", "work", rel))
        with self.assertRaises(HoodError):
            skillmaker.create_article(self.hood, "router_x1", "ещё", "work")
        with self.assertRaises(HoodError):
            skillmaker.create_article(self.hood, "ok_name", "t", "nowhere")


# ======================= веб =======================
class WebWorkshopTests(Base):
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
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        conn.close()
        return r.status, (json.loads(payload) if "json" in (r.getheader("Content-Type") or "") else payload)

    def run_job(self, action, params, answers):
        st, data = self.req("POST", "/api/job", {"action": action, "params": params, "sim": True})
        self.assertEqual(st, 200, data)
        answers, since, job_id, end = list(answers), 0, None, time.time() + 60
        while time.time() < end:
            st, data = self.req("GET", f"/api/job?wait=1&since={since}&job={job_id or ''}")
            job = data["job"]
            job_id, since = job["id"], job["seq"]
            if job["done"]:
                return job
            if job["question"]:
                st, res = self.req("POST", "/api/answer", {"job": job["id"], "qid": job["question"]["qid"],
                                                           "value": answers.pop(0)})
                self.assertEqual(st, 200, res)
        self.fail("задание не завершилось")

    def test_page_has_help_and_hood_buttons(self):
        st, body = self.req("GET", "/")
        self.assertIn(b'id="helpBtn"', body)
        self.assertIn(b'id="hoodBtn"', body)

    def test_hood_closed_until_unlocked_on_device_screen(self):
        rel = "profiles/chips/winbond_w25q64.yaml"
        st, f = self.req("GET", f"/api/hood/file?path={rel}")
        self.assertEqual((st, f["mode"], f["open"]), (200, "edit", False))
        text = f["text"].replace("voltage: 3.3", "voltage: 3.4")
        st, res = self.req("POST", "/api/hood/save", {"path": rel, "text": text, "word": "ДА"})
        self.assertEqual(st, 400)
        self.assertIn("капот закрыт", res["error"])
        orig = web.Handler.is_local
        web.Handler.is_local = lambda h: False                       # «телефон»: открыть нельзя
        try:
            self.app.trust_local = False
            self.app.token = "t"
            st, _ = self.req("POST", "/api/hood/unlock", {"open": True})
            self.assertEqual(st, 401)
        finally:
            web.Handler.is_local = orig
            self.app.trust_local = True
        st, state = self.req("POST", "/api/hood/unlock", {"open": True})
        self.assertTrue(state["open"])
        st, res = self.req("POST", "/api/hood/save", {"path": rel, "text": text})
        self.assertFalse(res["ok"])                                  # нет слова
        st, res = self.req("POST", "/api/hood/save", {"path": rel, "text": text, "word": "ДА"})
        self.assertTrue(res["ok"], res)
        self.assertIn("3.4", (self.root / rel).read_text(encoding="utf-8"))
        st, hist = self.req("GET", f"/api/hood/history?path={rel}")
        st, ver = self.req("GET", f"/api/hood/version?path={rel}&v={hist['items'][-1]['vid']}")
        self.assertIn("voltage: 3.3", ver["text"])
        st, res = self.req("POST", "/api/hood/restore", {"path": rel, "v": hist["items"][-1]["vid"], "word": "ДА"})
        self.assertTrue(res["ok"])
        st, res = self.req("GET", "/api/hood/file?path=../config.yaml")
        self.assertEqual(st, 400)
        st, res = self.req("GET", "/api/hood?area=work")
        self.assertTrue(all(f["mode"] == "view" for f in res["files"]))

    def test_remote_unlock_is_refused(self):
        orig = web.Handler.is_local
        web.Handler.is_local = lambda h: False
        try:
            self.app.trust_local = True                              # вошли по коду, но не с экрана прибора
            self.app.token = "tok"
            conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
            conn.request("POST", "/api/hood/unlock", body=b"{}", headers={"X-Dirizher": "1", "Cookie": "drz=tok",
                                                                         "Content-Type": "application/json"})
            r = conn.getresponse()
            self.assertEqual(r.status, 403)
            conn.close()
        finally:
            web.Handler.is_local = orig
        self.assertFalse(self.app.hood_open)

    def test_skills_and_help_over_http(self):
        st, info = self.req("GET", "/api/skills?sim=1")
        self.assertIn("modbus_rtu", {s["id"] for s in info["items"]})
        self.assertIn("device.modbus", info["classes"])
        st, res = self.req("POST", "/api/skills/new", {"kind": "map", "id": "thermo_trm", "title": "ТРМ"})
        self.assertEqual(st, 400)                                     # капот закрыт
        self.req("POST", "/api/hood/unlock", {"open": True})
        st, res = self.req("POST", "/api/skills/new", {"kind": "map", "id": "thermo_trm", "title": "ТРМ",
                                                       "target_class": "device.modbus"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["open"], "skills/thermo_trm/profiles/thermo_trm.yaml")
        st, info = self.req("GET", "/api/skills?sim=1")
        self.assertIn("thermo_trm", {s["id"] for s in info["items"]})
        st, found = self.req("GET", "/api/help?q=%D0%BA%D0%B0%D0%BF%D0%BE%D1%82")         # «капот»
        self.assertEqual(found["items"][0]["id"], "hood")
        st, art = self.req("GET", "/api/help/article?id=hood")
        self.assertTrue(art["editable"])
        st, _ = self.req("GET", "/api/help/article?id=nope")
        self.assertEqual(st, 404)
        st, res = self.req("POST", "/api/help/new", {"id": "router_x1", "title": "Роутер X1", "section": "work"})
        self.assertEqual(res["open"], "help/router_x1.md")

    def test_tune_and_what_helped_over_http(self):
        job = self.run_job("sim", {"do": "reset"}, [])
        job = self.run_job("params", {}, [])
        self.assertEqual(job["result"]["outcome"], "params_read")
        self.assertEqual(job["next"][0]["action"], "tune_form")
        st, view = self.req("GET", "/api/params?sim=1")
        self.assertEqual({p["id"]: p["value"] for p in view["params"]}["boot_delay"], 255)
        st, res = self.req("POST", "/api/job", {"action": "tune", "params": {}, "sim": True})
        self.assertEqual(st, 400)
        st, res = self.req("POST", "/api/job", {"action": "tune", "params": {"set": {"Bad-Key": 1}}, "sim": True})
        self.assertEqual(st, 400)
        job = self.run_job("tune", {"set": {"boot_delay": 4}}, [True, "ДА"])
        self.assertEqual(job["result"]["outcome"], "tuned")
        st, view = self.req("GET", "/api/params?sim=1")
        self.assertEqual({p["id"]: p["value"] for p in view["params"]}["boot_delay"], 4)
        # неудача → «что помогло» → подсказка в следующий раз
        self.run_job("sim", {"do": "flaky"}, [])
        job = self.run_job("run", {"dry_run": True}, [True])
        self.assertEqual(job["result"]["outcome"], "blocked")
        sid = job["result"]["session"]
        self.assertTrue(any(o.get("action") == "fix" for o in job["next"]))
        st, res = self.req("POST", "/api/fix", {"session": sid, "text": "прижал прищепку", "sim": True})
        self.assertTrue(res["ok"])
        job = self.run_job("run", {"dry_run": True}, [True])
        self.assertTrue(job["next"][0]["label"].startswith("Раньше помогало: прижал прищепку"))
        st, res = self.req("POST", "/api/fix", {"session": job["result"]["session"], "fix": res["id"], "sim": True})
        st, fx = self.req("GET", f"/api/fixes?session={job['result']['session']}&sim=1")
        self.assertEqual(fx["hints"][0]["helped"], 2)
        st, doc = self.req("GET", "/api/doctor?sim=1")
        self.assertIsInstance(doc["items"], list)


# ======================= текстовый мастер =======================
class WizardWorkshopTests(Base):
    def test_tune_and_fix_in_terminal(self):
        c = self.conductor()
        c.plugin("simulator").do_reset()
        c.read_params()
        answers = ["7", "", ""]                                       # boot_delay=7, остальное оставить
        w = Wizard(self.cfg, sim=True, ui=AutoUI(), ask_text=lambda prompt: answers.pop(0))
        opt = w.tune_form()
        self.assertEqual(opt["params"], {"set": {"boot_delay": "7"}})
        w.ui = AutoUI([None])                                         # «что дальше?» — назад
        w.action(opt["action"], opt["params"])
        self.assertEqual(c.plugin("simulator").image.read_bytes()[0x7FE000], 7)
        self.assertIsNone(w.help("hood"))


if __name__ == "__main__":
    unittest.main()
