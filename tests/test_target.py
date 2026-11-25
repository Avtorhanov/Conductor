"""0.7.0 S2b: модель цели из профиля (поле tool:), выбор модели перед сеансом, «начать с переходника».

Без железа: подставные программы (tests/fakes/fake_tools.py) в PATH, AutoUI, подменённые /etc и /sys
(временная папка вместо корня). Что проверяется:
  * поле tool: профиля сверяется со схемой лезвия; запрещены свойства переходника;
  * STM32 и ATmega выбираются из списка без правки config; выбор запоминается;
  * detect с выбранным переходником зовёт только его лезвия;
  * проверки подключения отдают понятные статусы;
  * «Подготовка» у ESP — без слов о прищепке.
"""
from __future__ import annotations

import getpass
import shutil
import tempfile
import unittest
from pathlib import Path

from dirizher.core import target as target_mod
from dirizher.core.profiles import Profile
from dirizher.core.registry import discover
from dirizher.core.ui import AutoUI
from dirizher.interface import guide
from dirizher.workshop import catalog, connect
from tests.fakes import fake_tools as ft
from tests.test_integrations import Base

ROOT = Path(__file__).resolve().parent.parent


def profile(**extra) -> Profile:
    d = {"id": "x", "name": "X", "class": "mcu.arm", "size": 65536}
    d.update(extra)
    return Profile.from_dict(d, Path("x.yaml"))


class ToolFieldTests(unittest.TestCase):
    def setUp(self):
        self.classes, _ = discover()

    def test_shipped_profiles_have_valid_tool(self):
        from dirizher.core.profiles import ProfileDB
        db = ProfileDB(ROOT / "profiles")
        for pid in ("stm32f103c8", "atmega328p", "esp32_4mb", "eeprom_24c02"):
            prof = db.get(pid)
            self.assertTrue(prof.tool, pid)
            self.assertEqual(target_mod.check(prof, self.classes), [], pid)

    def test_size_comes_from_profile(self):
        prof = profile(tool={"probe_rs": {"chip": "STM32F103C8"}})
        self.assertEqual(target_mod.overlay(prof, self.classes)["probe_rs"],
                         {"chip": "STM32F103C8", "flash_size": 65536})

    def test_bad_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            profile(tool={"probe_rs": "STM32"})

    def test_schema_errors(self):
        prof = profile(tool={"probe_rs": {"chp": "STM32", "flash_base": "абв"}, "nope": {"a": 1}})
        text = "\n".join(target_mod.check(prof, self.classes))
        self.assertIn("«chp»", text)
        self.assertIn("chip", text)                           # догадка об опечатке
        self.assertIn("flash_base", text)
        self.assertIn("нет такого лезвия", text)

    def test_adapter_properties_forbidden(self):
        prof = profile(tool={"probe_rs": {"chip": "X", "programmer_voltage": 5, "enabled": True},
                             "bp5_i2c": {"psu_voltage": 5}})
        text = "\n".join(target_mod.check(prof, self.classes))
        for key in ("programmer_voltage", "enabled", "psu_voltage"):
            self.assertIn(key, text)

    def test_image_size_must_match_profile(self):
        prof = profile(**{"class": "mcu.avr", "size": 33792},
                       tool={"avrdude": {"part": "m328p", "flash_size": 32768, "eeprom_size": 512}})
        self.assertIn("33792", "\n".join(target_mod.check(prof, self.classes)))

    def test_hood_check_reports_tool_errors(self):
        from dirizher.workshop.hood import Hood
        hood = Hood({"data_dir": tempfile.mkdtemp(prefix="dirizher_hood_")}, ROOT)
        self.addCleanup(shutil.rmtree, hood.store.parent, True)
        text = (ROOT / "profiles" / "mcu" / "stm32f103c8.yaml").read_text(encoding="utf-8")
        res = hood.check("profiles/mcu/stm32f103c8.yaml", text.replace("chip: STM32F103C8", "chipp: STM32F103C8"))
        self.assertTrue(any("chipp" in e for e in res["errors"]), res)


class SelectTests(Base):
    def setUp(self):
        super().setUp()
        (self.fake / ft.MCU).write_bytes(ft.healthy_stm32())
        f, e = ft.healthy_avr()
        (self.fake / ft.AVR_FLASH).write_bytes(f)
        (self.fake / ft.AVR_EEPROM).write_bytes(e)
        (self.fake / ft.ESP).write_bytes(ft.healthy_esp())

    def job(self, action, params, answers=None):
        c = self.conductor(answers)
        p = dict(params)
        sid = guide.execute(c, action, p)
        return c, sid, p

    def test_stm32_from_list_without_config(self):
        self.cfg["plugins"]["probe_rs"] = {"enabled": True}        # ни chip, ни flash_size в настройках
        c0 = self.conductor()
        row = next(r for r in c0.plugin_report if r["name"] == "probe_rs")
        self.assertIn("укажите", row["status"])
        c, sid, p = self.job("detect", {"adapter": "st_link_v2"}, answers=[0])
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        self.assertEqual(c.memory.session(sid)["profile_id"], "stm32f103c8")
        self.assertEqual(p["target"], "stm32f103c8")
        self.assertIn("Какая модель подключена", self.text(c))
        self.assertIn("модель выбрана вами", self.text(c))
        reads = [a for a in self.calls("probe-rs") if a and a[0] == "info"]
        self.assertTrue(reads)
        # выбор запомнился; «что дальше» ведёт с тем же переходником и моделью
        self.assertEqual(connect.last_target(self.cfg, "st_link_v2"), "stm32f103c8")
        nxt = guide.after("detect", p, "detected", sid)
        self.assertEqual(nxt[0]["params"], {"dry_run": True, "adapter": "st_link_v2", "target": "stm32f103c8"})

    def test_stm32_full_run_with_target(self):
        self.cfg["plugins"]["probe_rs"] = {"enabled": True}
        c, sid, _ = self.job("run", {"target": "stm32f103c8"})
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertTrue(any("--chip" in a and "STM32F103C8" in a and "65536" in a
                            for a in self.calls("probe-rs")), self.calls("probe-rs"))

    def test_atmega_from_list_and_remembered(self):
        self.cfg["plugins"]["avrdude"] = {"enabled": True, "programmer_voltage": 5.0}
        c, sid, _ = self.job("run", {"adapter": "usbasp"}, answers=[0])
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertEqual(c.memory.session(sid)["profile_id"], "atmega328p")
        self.assertTrue(any("m328p" in a for a in self.calls("avrdude")))
        self.assertEqual(connect.last_target(self.cfg, "usbasp"), "atmega328p")
        c2 = self.conductor([0])
        seen = []
        ask = c2.ui.choose
        c2.ui.choose = lambda q, opts: (seen.append(list(opts)), ask(q, opts))[1]
        sid2 = guide.execute(c2, "detect", {"adapter": "usbasp"})
        self.assertIn("в прошлый раз", seen[0][0])
        self.assertIn("как в настройках", seen[0][-1])
        self.assertEqual(self.outcome(c2, sid2), "detected")

    def test_other_model_keeps_tool_settings(self):
        self.use("probe_rs")
        c, sid, p = self.job("detect", {"adapter": "st_link_v2"}, answers=[1])  # «Другая — как в настройках»
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        self.assertNotIn("target", p)

    def test_cancel_choice_starts_nothing(self):
        self.cfg["plugins"]["probe_rs"] = {"enabled": True}
        c = self.conductor([None])
        with self.assertRaises(ValueError):
            guide.execute(c, "detect", {"adapter": "st_link_v2"})
        self.assertEqual(self.calls("probe-rs"), [])

    def test_unknown_target(self):
        c = self.conductor()
        with self.assertRaises(ValueError):
            c.select(target="no_such_profile")

    def test_adapter_polls_only_its_plugins(self):
        self.use("probe_rs")
        self.use("avrdude")
        self.use("esptool")
        c = self.conductor()
        c.detect_only()
        tools = {x["tool"] for x in ft.calls(self.fake)}
        self.assertTrue({"probe-rs", "avrdude", "esptool"} <= tools, tools)   # без переходника — все
        self.reset_calls()
        c, sid, _ = self.job("detect", {"adapter": "st_link_v2", "target": "stm32f103c8"})
        self.assertEqual(self.outcome(c, sid), "detected", self.text(c))
        tools = {x["tool"] for x in ft.calls(self.fake)}
        self.assertEqual(tools, {"probe-rs"}, tools)
        row = next(r for r in c.plugin_report if r["name"] == "avrdude")
        self.assertIn("не опрашивается", row["status"])
        self.assertIn("ST-Link", row["status"])

    def test_adapter_without_ready_plugins_explains(self):
        self.use("avrdude")
        c, sid, _ = self.job("detect", {"adapter": "st_link_v2", "target": "stm32f103c8"})
        self.assertEqual(self.outcome(c, sid), "not_detected")
        self.assertIn("нет готовых лезвий", self.text(c))

    def test_esp_preflight_without_clip(self):
        self.use("esptool")
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "healthy", self.text(c))
        step1 = self.text(c).split("Опознание")[0]
        self.assertNotIn("рищепк", step1)
        self.assertIn("GPIO0", step1)
        c2, sid2, _ = self.job("run", {"adapter": "usb_uart"})
        step1 = self.text(c2).split("Опознание")[0]
        self.assertNotIn("рищепк", step1)
        self.assertIn("3,3 В", step1)                             # строка из карточки переходника

    def test_flashrom_keeps_common_preflight(self):
        self.use("probe_rs")
        self.use("avrdude")
        c = self.conductor()
        self.assertEqual(c.preflight_lines()[2][:9], "Прищепка ")   # два разных лезвия — общий текст

    def test_sim_ignores_adapter(self):
        from dirizher.core.pipeline import Conductor
        cs = Conductor(self.cfg, AutoUI(), sim=True)
        self.opened.append(cs)
        cs.plugin("simulator").do_reset()
        sid = guide.execute(cs, "detect", {"adapter": "st_link_v2"})
        self.assertEqual(cs.memory.session(sid)["outcome"], "detected")
        self.assertIn("учебный режим", "\n".join(cs.ui.lines))


class ConnectTests(Base):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="dirizher_root_"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.cards, _ = catalog.load(self.cfg)
        self.user = getpass.getuser()

    def fs(self, rules=True, group=True, usb=True):
        if rules:
            d = self.root / "etc" / "udev" / "rules.d"
            d.mkdir(parents=True, exist_ok=True)
            (d / "99-dirizher-tools.rules").write_text(
                'SUBSYSTEM=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3748", GROUP="plugdev"\n')
        (self.root / "etc").mkdir(parents=True, exist_ok=True)
        (self.root / "etc" / "group").write_text(
            f"root:x:0:\nplugdev:x:46:{self.user if group else 'someone'}\ndialout:x:20:\n")
        if usb:
            dev = self.root / "sys" / "bus" / "usb" / "devices" / "1-1"
            dev.mkdir(parents=True)
            (dev / "idVendor").write_text("0483\n")
            (dev / "idProduct").write_text("3748\n")

    def view(self, adapter="st_link_v2"):
        c = self.conductor()
        return connect.view(self.cfg, adapter, c.plugin_report, root=self.root, profiles=c.profiles)

    def status(self, v) -> dict:
        return {r["id"]: r["status"] for r in v["checks"]}

    def test_all_good(self):
        self.fs()
        self.cfg["plugins"]["probe_rs"] = {"enabled": True}          # модель — из профиля
        self.use("openocd")
        st = self.status(self.view())
        self.assertEqual(st["program_probe_rs"], "ok")
        self.assertEqual(st["program_openocd"], "ok")
        self.assertEqual(st["udev"], "ok")
        self.assertEqual(st["group_plugdev"], "ok")
        self.assertEqual(st["usb"], "ok")
        self.assertEqual(st["plugin_probe_rs"], "ok")
        self.assertEqual(st["plugin_openocd"], "ok")

    def test_problems_have_advice(self):
        self.fs(rules=False, group=False, usb=False)
        shutil.rmtree(self.bin)                                    # программ нет
        self.bin.mkdir()
        v = self.view()
        st = self.status(v)
        self.assertEqual(st["program_probe_rs"], "fail")
        self.assertEqual(st["udev"], "fail")
        self.assertEqual(st["group_plugdev"], "fail")
        self.assertEqual(st["usb"], "warn")
        self.assertEqual(st["plugin_probe_rs"], "fail")
        rows = {r["id"]: r for r in v["checks"]}
        self.assertIn("usermod -aG plugdev", rows["group_plugdev"]["code"])
        self.assertIn("99-dirizher", rows["udev"]["code"])
        self.assertEqual(rows["plugin_probe_rs"]["action"], {"action": "tool", "params": {"name": "probe_rs"}})
        self.assertIn("Включите", rows["plugin_probe_rs"]["advice"])
        self.assertTrue(any(a["action"] == "detect" and a["params"] == {"adapter": "st_link_v2"}
                            for a in v["actions"]))
        lines = "\n".join(connect.text(v))
        self.assertIn("✖ Группа plugdev", lines)

    def test_rules_without_this_device(self):
        self.fs()
        v = self.view("t48")
        self.assertEqual(self.status(v)["udev"], "warn")

    def test_serial_adapter_needs_dialout_not_udev(self):
        self.fs()
        st = self.status(self.view("usb_uart"))
        self.assertNotIn("udev", st)
        self.assertEqual(st["group_dialout"], "fail")
        self.assertEqual(st["usb"], "warn")

    def test_adapter_list_and_plugins(self):
        v = connect.view(self.cfg, None, [])
        ids = [a["id"] for a in v["adapters"]]
        self.assertIn("st_link_v2", ids)
        self.assertNotIn("soic8_clip", ids)                       # принадлежность, не переходник
        st = catalog.get(self.cards, "st_link_v2")
        self.assertEqual(connect.adapter_plugins(self.cards, st), ["probe_rs", "openocd"])
        with self.assertRaises(ValueError):
            connect.view(self.cfg, "no_such", [])

    def test_web_params(self):
        from dirizher.interface.web import App
        app = App(self.cfg)
        try:
            self.assertEqual(app.clean_params("detect", {"adapter": "st_link_v2", "target": "stm32f103c8"}),
                             {"adapter": "st_link_v2", "target": "stm32f103c8"})
            with self.assertRaises(ValueError):
                app.clean_params("run", {"target": "../etc"})
        finally:
            app.shutdown()

    def test_guide_has_connect_first(self):
        g = guide.load_guide()
        first = g["nodes"]["tools"]["options"][0]
        self.assertEqual(first["action"], "connect")


if __name__ == "__main__":
    unittest.main()
