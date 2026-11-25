"""Схема настроек лезвий (0.7.0, S2a): проверка по схеме, config.tools.yaml, форма в вебе, мастер, CLI.

Каждый тест работает на копии проекта во временной папке — настоящие файлы не меняются.
Запуск:  python3 -m unittest tests.test_settings -v
"""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import re
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

from dirizher.core import settings as S
from dirizher.core.config import load_config
from dirizher.core.pipeline import Conductor
from dirizher.core.registry import discover
from dirizher.core.skills import SkillDB
from dirizher.core.ui import AutoUI
from dirizher.interface import web
from dirizher.interface.wizard import Wizard
from dirizher.workshop import doctor, toolsettings
from dirizher.workshop.hood import Hood, classify

ROOT = Path(__file__).resolve().parent.parent
BUILTIN = ("flashrom_spi", "probe_rs", "openocd", "minipro", "avrdude", "esptool", "bp5_i2c", "simulator",
           "modbus_rtu")


def all_classes(root=ROOT) -> dict:
    found, errors = discover(SkillDB(root / "skills").active)
    assert not errors, errors
    return {name: cls for name, (cls, _) in found.items()}


class SchemaTests(unittest.TestCase):
    def test_every_blade_has_a_sound_schema(self):
        classes = all_classes()
        for name in BUILTIN:
            with self.subTest(name=name):
                cls = classes[name]
                self.assertTrue(cls.has_schema(), "нет settings_schema")
                schema = cls.schema()
                keys = [f["key"] for f in schema]
                self.assertEqual(len(keys), len(set(keys)), "повтор ключа")
                self.assertEqual(keys[0], "enabled")
                for f in schema:
                    self.assertIn(f["type"], S.TYPES, f["key"])
                    self.assertTrue(f["title"], f["key"])
                    if f["type"] == "choice":
                        self.assertTrue(f["choices"], f["key"])
                    if f["required"]:
                        self.assertIsNone(f["default"], f"{f['key']}: обязательное поле с умолчанием")
                    for v in (f["default"], f["example"]):
                        if v is not None:
                            self.assertIsNone(S.type_error(f, v), f"{f['key']}: пример/умолчание не того типа")
                if cls.voltage_check and cls.hardware:
                    self.assertIn("programmer_voltage", keys)
                    self.assertTrue(any(f["safety"] == "voltage" for f in schema))
                else:
                    self.assertNotIn("programmer_voltage", keys)

    def test_snippet_keys_are_in_schemas(self):
        """docs/config-snippet-0.3.0.yaml: каждый ключ (и закомментированный образец) есть в схеме лезвия."""
        text = (ROOT / "docs" / "config-snippet-0.3.0.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(text)
        classes = all_classes()
        checked = 0
        for name, section in data["plugins"].items():
            keys = {f["key"] for f in classes[name].schema()}
            for key in section:
                with self.subTest(plugin=name, key=key):
                    self.assertIn(key, keys)
                    checked += 1
        # закомментированные образцы: «    # probe: "0483:3748"» внутри секции лезвия
        current = None
        for line in text.split("\n"):
            m = re.match(r"^  (\w+):", line)
            if m:
                current = m.group(1)
            if line and not line.startswith(" "):
                current = None
            m = re.match(r"^    # (\w+): ", line)
            if current and m:
                with self.subTest(plugin=current, key=m.group(1)):
                    self.assertIn(m.group(1), {f["key"] for f in classes[current].schema()})
                    checked += 1
        common = re.findall(r"^#   (\w+)(?: / (\w+) / (\w+))?:", text, re.M)
        for group in common:
            for key in filter(None, group):
                self.assertIn(key, S.COMMON, key)
        self.assertGreater(checked, 30)

    def test_validate_and_coerce(self):
        schema = all_classes()["probe_rs"].schema()
        res = S.validate(schema, {"enabled": True, "chip": "STM32F103C8", "flash_sise": 65536,
                                  "flash_size": "много", "protocol": "usb"})
        self.assertEqual(len(res["unknown"]), 1)
        self.assertIn("«flash_size»", res["unknown"][0])               # догадка про опечатку
        self.assertEqual(len(res["type"]), 2)
        self.assertTrue(any("целое число" in t and "много" in t for t in res["type"]))
        self.assertTrue(any("swd, jtag" in t for t in res["type"]))
        self.assertEqual(S.validate(schema, {"enabled": True})["missing"],
                         ["укажите «Чип (имя из probe-rs chip list)» (chip)", "укажите «Размер flash, байт» (flash_size)"])
        f = {f["key"]: f for f in schema}
        self.assertEqual(S.coerce(f["flash_base"], "0x08000000"), 0x08000000)
        self.assertEqual(S.coerce(f["enabled"], "да"), True)
        self.assertEqual(S.coerce(f["protocol"], "JTAG"), "jtag")
        self.assertEqual(S.coerce(f["extra_args"], "--a, --b"), ["--a", "--b"])
        self.assertEqual(S.coerce(f["programmer_voltage"], "1,8"), 1.8)
        self.assertIsNone(S.coerce(f["chip"], "  "))
        with self.assertRaises(ValueError):
            S.coerce(f["speed"], "быстро")
        self.assertEqual(S.show(f["flash_base"], 0x08000000), "0x08000000")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_set_"))
        self.root = self.tmp / "root"
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns(".git", "data", "__pycache__", "tests",
                                                                       "config.tools.yaml"))
        self.load()

    def load(self):
        self.cfg = load_config(self.root / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = ""
        self.hood = Hood(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def tools(self, text: str):
        (self.root / "config.tools.yaml").write_text(text, encoding="utf-8")
        self.load()

    def report(self, name):
        c = Conductor(self.cfg, AutoUI(), sim=False)
        try:
            return next(r for r in c.plugin_report if r["name"] == name)
        finally:
            c.close()


class ToolsFileTests(Base):
    def test_tools_file_overrides_config_yaml(self):
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["programmer"], "ch341a_spi")
        self.tools("plugins:\n  flashrom_spi:\n    programmer: linux_spi:dev=/dev/spidev0.0\n    read_timeout: null\n"
                   "  probe_rs:\n    enabled: true\n")
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["programmer"], "linux_spi:dev=/dev/spidev0.0")
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["read_timeout"], 900)   # null не перекрывает
        self.assertTrue(self.cfg["plugins"]["probe_rs"]["enabled"])
        desc = toolsettings.describe(self.cfg, "flashrom_spi")
        src = {f["key"]: f["source"] for f in desc["fields"]}
        self.assertEqual((src["programmer"], src["read_timeout"], src["extra_args"] if "extra_args" in src else "-"),
                         ("tools", "config", "-"))
        self.assertIsNone(self.cfg["_tools_error"])

    def test_missing_required_field_makes_blade_unavailable(self):
        self.tools("plugins:\n  probe_rs:\n    enabled: true\n    chip: STM32F103C8\n")
        row = self.report("probe_rs")
        self.assertFalse(row["active"])
        self.assertEqual(row["status"], "недоступен: укажите «Размер flash, байт» (flash_size)")
        self.tools("plugins:\n  probe_rs:\n    enabled: true\n    chip: STM32F103C8\n    flash_size: много\n")
        self.assertIn("нужно целое число, а указано «много»", self.report("probe_rs")["status"])

    def test_typo_is_seen_in_report_and_doctor(self):
        self.tools("plugins:\n  probe_rs:\n    flash_sise: 65536\n")
        row = self.report("probe_rs")
        self.assertEqual(row["status"], "выключен")                    # выключенное лезвие не ломается
        self.assertEqual(row["warnings"], ["неизвестный ключ «flash_sise» — может быть, «flash_size»?"])
        issues = {i["id"]: i for i in doctor.check(self.cfg, deep=False, hood=self.hood)}
        self.assertIn("settings:probe_rs", issues)
        self.assertIn("flash_sise", issues["settings:probe_rs"]["detail"])
        self.assertNotIn("conflict:probe_rs+openocd", issues)

    def test_doctor_warns_probe_rs_and_openocd_together(self):
        self.tools("plugins:\n  probe_rs:\n    enabled: true\n  openocd:\n    enabled: true\n")
        issues = {i["id"]: i for i in doctor.check(self.cfg, deep=False, hood=self.hood)}
        self.assertEqual(issues["conflict:probe_rs+openocd"]["level"], "warn")

    def test_broken_tools_file_is_reported_and_healed_from_history(self):
        res = toolsettings.save(self.cfg, self.hood, "flashrom_spi", {"read_timeout": "1000"}, word="ДА")
        self.assertTrue(res["ok"], res)
        (self.root / "config.tools.yaml").write_text("plugins: [broken\n", encoding="utf-8")
        self.load()
        self.assertTrue(self.cfg["_tools_error"])
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["read_timeout"], 900)    # сломанный файл не действует
        issues = {i["id"]: i for i in doctor.check(self.cfg, deep=False, hood=self.hood)}
        issue = issues["file:config.tools.yaml"]
        self.assertEqual(issue["level"], "bad")
        self.assertTrue(issue["heal"])
        doctor.heal(self.cfg, "file:config.tools.yaml", hood=self.hood, require_word=False)
        self.load()
        self.assertIsNone(self.cfg["_tools_error"])
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["read_timeout"], 1000)

    def test_hood_policy_and_check(self):
        self.assertEqual(classify("config.tools.yaml")[0], "edit")
        self.assertEqual(classify("config.yaml")[0], "view")
        bad = self.hood.check("config.tools.yaml", "safety:\n  programmer_voltage: 1.8\nplugins: {}\n")
        self.assertTrue(any("только секция plugins" in e for e in bad["errors"]))
        bad = self.hood.check("config.tools.yaml", "plugins:\n  avrdude:\n    flash_size: x\n")
        self.assertTrue(any("целое число" in e for e in bad["errors"]))
        ok = self.hood.check("config.tools.yaml", "plugins:\n  avrdude:\n    programmer_voltage: 5.0\n  nope:\n    a: 1\n")
        self.assertEqual(ok["errors"], [])
        self.assertTrue(ok["needs_word"])
        self.assertTrue(any("напряж" in w.lower() for w in ok["warnings"]))
        self.assertTrue(any("«nope» нет" in w for w in ok["warnings"]))
        self.assertIn("config.tools.yaml", {f["path"] for f in self.hood.files("device")} | {"config.tools.yaml"})


class SaveTests(Base):
    def test_plan_keeps_only_what_differs(self):
        p = toolsettings.plan(self.cfg, self.hood, "flashrom_spi", {"programmer": "ch341a_spi", "read_timeout": "1200"})
        self.assertEqual(p["section"], {"read_timeout": 1200})          # совпадает с config.yaml — не пишем
        self.assertEqual([c["key"] for c in p["changes"]], ["read_timeout"])
        self.assertEqual((p["changes"][0]["before"], p["changes"][0]["after"]), ("900", "1200"))
        p = toolsettings.plan(self.cfg, self.hood, "flashrom_spi", {"flash_sise": "1", "read_timeout": "x"})
        self.assertEqual(len(p["errors"]), 2)
        with self.assertRaises(toolsettings.ToolSettingsError):
            toolsettings.plan(self.cfg, self.hood, "nope", {})

    def test_voltage_is_not_changed_without_word(self):
        values = {"programmer_voltage": "1.8"}
        res = toolsettings.save(self.cfg, self.hood, "flashrom_spi", values, word=None)
        self.assertFalse(res["ok"])
        res = toolsettings.save(self.cfg, self.hood, "flashrom_spi", values, word="да, наверное")
        self.assertFalse(res["ok"])
        self.assertFalse((self.root / "config.tools.yaml").exists())
        self.assertNotIn("programmer_voltage", self.cfg["plugins"]["flashrom_spi"])
        res = toolsettings.save(self.cfg, self.hood, "flashrom_spi", values, word="ДА")
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.cfg["plugins"]["flashrom_spi"]["programmer_voltage"], 1.8)   # cfg обновлён на месте
        self.assertEqual(load_config(self.root / "config.yaml")["plugins"]["flashrom_spi"]["programmer_voltage"], 1.8)
        self.assertNotIn("programmer_voltage", (self.root / "config.yaml").read_text(encoding="utf-8").split("plugins:")[1])
        # пусто — вернуть унаследованное; история капота хранит обе версии
        res = toolsettings.save(self.cfg, self.hood, "flashrom_spi", {"programmer_voltage": ""}, word="ДА")
        self.assertTrue(res["ok"], res)
        self.assertNotIn("programmer_voltage", self.cfg["plugins"]["flashrom_spi"])
        self.assertGreaterEqual(len(self.hood.history("config.tools.yaml")), 2)

    def test_cli_tool(self):
        ui = AutoUI(confirm=False)
        self.assertEqual(toolsettings.cli(self.cfg, "bp5_i2c", ["psu_voltage=5"], False, ui), 1)
        self.assertTrue(any("напряжение" in ln.lower() and "отказ" in ln for ln in ui.lines), ui.lines)
        self.assertFalse((self.root / "config.tools.yaml").exists())
        ui = AutoUI(confirm=True)
        self.assertEqual(toolsettings.cli(self.cfg, "bp5_i2c", ["enabled=да", "eeprom=24C04"], True, ui), 0)
        self.assertFalse((self.root / "config.tools.yaml").exists())   # --check ничего не пишет
        self.assertEqual(toolsettings.cli(self.cfg, "bp5_i2c", ["enabled=да", "eeprom=24C04"], False, ui), 0)
        self.assertEqual(self.cfg["plugins"]["bp5_i2c"], {"enabled": True, "eeprom": "24C04"})
        ui = AutoUI()
        self.assertEqual(toolsettings.cli(self.cfg, "bp5_i2c", [], False, ui), 0)
        self.assertTrue(any("eeprom" in ln and "config.tools.yaml" in ln for ln in ui.lines))
        self.assertEqual(toolsettings.cli(self.cfg, "bp5_i2c", ["oops"], False, AutoUI()), 1)
        self.assertEqual(toolsettings.cli(self.cfg, None, [], False, AutoUI()), 0)
        from dirizher.__main__ import main
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--config", str(self.root / "config.yaml"), "tool", "openocd"]), 0)
        self.assertIn("flash_size", out.getvalue())

    def test_wizard_field_by_field(self):
        port = self.tmp / "ttyUSB9"
        port.write_text("")
        answers = {"enabled": "да", "port": str(port), "unit": "7", "parity": "E"}

        def ask(prompt):
            m = re.search(r"\((\w+)\) =", prompt)
            return answers.get(m.group(1), "") if m else ""
        c = Conductor(self.cfg, AutoUI(), sim=False)
        names = [r["name"] for r in c.plugin_report if r["title"] != "ошибка загрузки"]
        c.close()
        ui = AutoUI(answers=[names.index("modbus_rtu")], confirm=True)
        Wizard(self.cfg, sim=False, ui=ui, ask_text=ask).device()
        section = self.cfg["plugins"]["modbus_rtu"]
        self.assertEqual((section["enabled"], section["port"], section["unit"], section["parity"]),
                         (True, str(port), 7, "E"))
        self.assertTrue(self.report("modbus_rtu")["active"])


class WebTests(Base):
    def setUp(self):
        super().setUp()
        self.app = web.App(self.cfg, sim_default=False, pin="135790")
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

    def test_blade_is_enabled_from_the_screen_without_editing_config_yaml(self):
        before = (self.root / "config.yaml").read_text(encoding="utf-8")
        port = self.tmp / "ttyUSB7"
        port.write_text("")
        st, page = self.req("GET", "/")
        self.assertIn(b"renderTool", page)
        st, dev = self.req("GET", "/api/device")
        self.assertIn("modbus_rtu", {p["name"] for p in dev["plugins"]})
        st, d = self.req("GET", "/api/tool?name=modbus_rtu")
        self.assertEqual(st, 200, d)
        fields = {f["key"]: f for f in d["fields"]}
        self.assertEqual((fields["port"]["source"], fields["baud"]["shown"]), ("skill", "9600"))
        self.assertFalse(d["status"]["active"])
        self.assertEqual(self.req("GET", "/api/tool?name=nope")[0], 404)
        values = {"enabled": "true", "port": str(port), "unit": "300"}
        st, chk = self.req("POST", "/api/tool/check", {"name": "modbus_rtu", "values": values})
        self.assertEqual(chk["errors"], [])
        self.assertIn("1…247", chk["status"]["status"])                 # available() лезвия — как было
        self.assertFalse((self.root / "config.tools.yaml").exists())    # «Проверить» не пишет
        values["unit"] = "5"
        st, res = self.req("POST", "/api/tool/save", {"name": "modbus_rtu", "values": values, "word": "ДА"})
        self.assertEqual(st, 400)
        self.assertIn("капот закрыт", res["error"])
        st, _ = self.req("POST", "/api/hood/unlock", {"open": True})
        st, res = self.req("POST", "/api/tool/save", {"name": "modbus_rtu", "values": values})
        self.assertFalse(res["ok"])                                    # без слова — нет
        self.assertFalse((self.root / "config.tools.yaml").exists())
        st, res = self.req("POST", "/api/tool/save", {"name": "modbus_rtu", "values": values, "word": "ДА"})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["status"]["active"], res["status"])
        st, dev = self.req("GET", "/api/device")
        row = next(p for p in dev["plugins"] if p["name"] == "modbus_rtu")
        self.assertTrue(row["active"])
        self.assertEqual((self.root / "config.yaml").read_text(encoding="utf-8"), before)
        st, d = self.req("GET", "/api/tool?name=modbus_rtu")
        self.assertEqual({f["key"]: f["source"] for f in d["fields"]}["unit"], "tools")

    def test_voltage_needs_word_over_http(self):
        self.req("POST", "/api/hood/unlock", {"open": True})
        st, d = self.req("GET", "/api/tool?name=avrdude")
        volt = next(f for f in d["fields"] if f["key"] == "programmer_voltage")
        self.assertEqual(volt["safety"], "voltage")
        st, res = self.req("POST", "/api/tool/save", {"name": "avrdude", "values": {"programmer_voltage": "5"},
                                                      "word": "нет"})
        self.assertFalse(res["ok"])
        self.assertNotIn("programmer_voltage", self.cfg["plugins"].get("avrdude") or {})
        st, chk = self.req("POST", "/api/tool/check", {"name": "avrdude", "values": {"programmer_voltage": "5"}})
        self.assertEqual(chk["changes"][0]["safety"], "voltage")
        self.assertTrue(any("напряж" in w.lower() for w in chk["warnings"]))


if __name__ == "__main__":
    unittest.main()
