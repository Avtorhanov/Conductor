"""0.7.0, S3: Plugin.inspect() и замер напряжения — без железа.

Подставные программы (tests/fakes/fake_tools.py) и эмулятор Bus Pirate 5 на псевдотерминале.
Что проверяется:
  * замер напряжения только добавляет проверку: 1,8 В при профиле 3,3 В — стоп; прежние запреты и
    вопросы остаются (перебор всех сочетаний);
  * находки inspect видны по-русски и пишутся в журнал; «block» запрещает запись;
  * фьюзы ATmega расшифрованы по профилю; защита записи flashrom; регистр ID probe-rs; защита чтения
    OpenOCD; eFuse ESP; контакт в панельке minipro; VOUT Bus Pirate;
  * программы зовутся только на чтение (журнал вызовов подставных программ);
  * у симулятора и лезвий без inspect сценарий прежний.
"""
from __future__ import annotations

import itertools
import json
import os
import sys
import unittest

from dirizher.core import fuses as fuses_mod
from dirizher.core import target as target_mod
from dirizher.core.models import Finding, Inspection
from dirizher.core.pipeline import Conductor
from dirizher.core.profiles import Profile
from dirizher.core.ui import AutoUI
from tests.fakes import fake_tools as ft
from tests.fakes.bp5_emulator import BP5Emulator
from tests.test_integrations import FAKE_TOOLS, ROOT, Base

EXTRA_SHIMS = ["flashrom", "espefuse", "espefuse.py"]
EXTRA_FLAGS = ["FAKE_TARGET_VOLTAGE", "FAKE_DEV_ID", "FAKE_WP", "FAKE_BAD_PIN", "FAKE_ESP_ENCRYPTED",
               "FAKE_ESP_SECURE", "FAKE_FLASHROM_V"]


class InspectBase(Base):
    def setUp(self):
        super().setUp()
        for name in EXTRA_SHIMS:
            p = self.bin / name
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_TOOLS}" "{name}" "$@"\n')
            p.chmod(0o755)
        self._env.update({k: os.environ.get(k) for k in EXTRA_FLAGS})
        for k in EXTRA_FLAGS:
            os.environ.pop(k, None)

    def events(self, c, sid) -> list:
        return [e["message"] for e in c.memory.events(sid)]


# ======================= замер напряжения =======================
class MeasuredVoltageTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.chip = self.fake / ft.MCU
        self.chip.write_bytes(ft.healthy_stm32())
        self.use("openocd")

    def test_measured_18_with_profile_33_stops(self):
        os.environ["FAKE_TARGET_VOLTAGE"] = "1.8"
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "blocked_voltage", self.text(c))
        self.assertIn("замерено 1,8 В, а профиль на 3,3 В", self.text(c))
        self.assertFalse(any("dump_image" in " ".join(a) for a in self.calls("openocd")), "чип не читался")
        self.assertTrue(any("замер напряжения цели: 1.800 В" in m for m in self.events(c, sid)))
        self.assertNotIn("1.8", c.memory.session(sid)["fingerprint"], "замер не входит в отпечаток")

    def test_measured_matches(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("замер совпал: на цели 3,24 В", self.text(c))

    def test_no_power_on_target(self):
        os.environ["FAKE_TARGET_VOLTAGE"] = "0.0"
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "blocked_voltage")
        self.assertIn("на цели нет питания", self.text(c))

    def test_measurement_never_lifts_old_block(self):
        self.use("openocd", programmer_voltage=5.0)        # прежний запрет: чип 3,3 В, адаптер 5 В
        c = self.conductor()                                # замер 3,24 В совпадает с профилем
        self.assertEqual(self.outcome(c, c.run()), "blocked_voltage")
        self.assertIn("адаптер — на 5.0 В", self.text(c))

    def test_measure_voltage_off_keeps_old_check(self):
        os.environ["FAKE_TARGET_VOLTAGE"] = "1.8"
        self.use("openocd", measure_voltage=False)
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("сверка замера выключена", self.text(c))
        self.use("openocd", measure_voltage=False, programmer_voltage=5.0)
        c2 = self.conductor()
        self.assertEqual(self.outcome(c2, c2.run()), "blocked_voltage", "прежняя проверка осталась")

    def test_measure_voltage_not_allowed_in_profile(self):
        prof = Profile.from_dict({"id": "x", "name": "x", "class": "mcu.arm", "size": 65536,
                                  "tool": {"openocd": {"measure_voltage": False}}}, ROOT / "x.yaml")
        from dirizher.core.registry import discover
        errors = target_mod.check(prof, discover()[0])
        self.assertTrue(any("measure_voltage" in e for e in errors), errors)

    def test_rollback_also_checks_measurement(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy")
        os.environ["FAKE_TARGET_VOLTAGE"] = "1.8"
        c2 = self.conductor()
        sid2 = c2.rollback(sid)
        self.assertEqual(self.outcome(c2, sid2), "blocked_voltage", self.text(c2))


class StubPlugin:
    """Лезвие-заглушка для перебора шага «Напряжение» без программ."""
    name = "stub"
    title = "заглушка"
    voltage_check = True
    voltage_note = ""

    def __init__(self, pv):
        self.settings = {"programmer_voltage": pv}


class StubProfile:
    def __init__(self, v):
        self.voltage = v


class VoltageGateMatrixTests(InspectBase):
    """Замер только добавляет: ни в одном сочетании он не превращает «стоп» в «можно»."""

    def gate(self, profile_v, pv, measured, answer):
        c = Conductor(self.cfg, AutoUI([answer] * 3), sim=True)
        self.opened.append(c)
        c._start("run")
        plugin = StubPlugin(pv)
        if measured is not None:
            c._measured[plugin.name] = measured
        ok = c._voltage_gate(StubProfile(profile_v), plugin)
        return ok, list(c.ui.lines)

    def test_matrix(self):
        for profile_v, pv, measured, answer in itertools.product(
                (3.3, 1.8, 5.0, None), (3.3, 1.8, 5.0), (3.3, 3.24, 1.8, 0.0, 5.0), ("yes", "no")):
            with self.subTest(profile_v=profile_v, pv=pv, measured=measured, answer=answer):
                base, base_lines = self.gate(profile_v, pv, None, answer)
                with_m, lines = self.gate(profile_v, pv, measured, answer)
                if not base:
                    self.assertFalse(with_m, "замер снял прежний запрет")
                if with_m:
                    self.assertTrue(base)
                    self.assertTrue(abs(measured - pv) <= 0.3)
                    if profile_v is not None:
                        self.assertTrue(abs(measured - profile_v) <= 0.3)
                if base and profile_v is None and abs(measured - pv) <= 0.3:
                    # вопрос «проверили по даташиту?» не пропадает из-за совпавшего замера
                    self.assertTrue(any("?" in ln for ln in lines) or with_m == base, lines)

    def test_question_still_asked_when_profile_has_no_voltage(self):
        ok, lines = self.gate(None, 3.3, 3.3, "no")
        self.assertFalse(ok, "человек ответил «нет» — замер ответ не заменяет")
        ok, lines = self.gate(None, 3.3, 3.3, "yes")
        self.assertTrue(ok)
        self.assertTrue(any("замер совпал" in ln for ln in lines), lines)

    def test_no_measure_no_change(self):
        for profile_v, pv in itertools.product((3.3, 1.8, None), (3.3, 5.0)):
            ok, lines = self.gate(profile_v, pv, None, "yes")
            self.assertFalse(any("замер" in ln for ln in lines), lines)


# ======================= находки лезвий =======================
class AvrFusesTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.use("avrdude")
        self.flash, self.eeprom = self.fake / ft.AVR_FLASH, self.fake / ft.AVR_EEPROM
        f, e = ft.healthy_avr()
        self.flash.write_bytes(f)
        self.eeprom.write_bytes(e)

    def set_fuses(self, **values):
        (self.fake / "avr_fuses.json").write_text(json.dumps(values), encoding="utf-8")

    def test_fuses_shown_in_russian(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        t = self.text(c)
        self.assertIn("fuses lfuse: 0xFF: Источник тактирования (CKSEL) — внешний кварц 8–16 МГц", t)
        self.assertIn("Делитель частоты на 8 (CKDIV8) — выключен", t)
        self.assertIn("fuses efuse: 0xFD: Контроль питания (BOD) — 2,7 В", t)
        self.assertIn("Размер загрузчика (BOOTSZ) — 1024 слова (2 КБ)", t)
        self.assertIn("Защита памяти (LB) — нет защиты", t)
        self.assertTrue(any(m.startswith("[info] fuses hfuse: 0xDA") for m in self.events(c, sid)))
        fuse_calls = [a for a in self.calls("avrdude") if "lfuse:r:-:h" in a]
        self.assertEqual(len(fuse_calls), 1)
        self.assertIn("-n", fuse_calls[0])
        self.assertFalse((self.fake / "fuses_written").exists())
        self.assertNotIn("lfuse", c.memory.session(sid)["fingerprint"])

    def test_internal_rc_and_ckdiv8(self):
        self.set_fuses(lfuse=0x62)                         # заводские: RC 8 МГц / 8
        c = self.conductor()
        c.run()
        t = self.text(c)
        self.assertIn("внутренний RC-генератор 8 МГц", t)
        self.assertIn("включён — МК работает в 8 раз медленнее кварца", t)

    def test_read_lock_blocks_write(self):
        c = self.conductor()
        self.assertEqual(self.outcome(c, c.run()), "healthy")          # эталон сохранён
        f, _ = ft.healthy_avr()
        self.flash.write_bytes(ft.corrupt(f, 0x100, 0x400))
        self.set_fuses(lock=0xFC)                                       # LB = 00: чтение и запись запрещены
        broken = self.flash.read_bytes()
        c2 = self.conductor()
        sid = c2.run()
        self.assertEqual(self.outcome(c2, sid), "blocked", self.text(c2))
        self.assertIn("запрещены запись и чтение", self.text(c2))
        self.assertIn("блокирующая проблема", self.text(c2))
        self.assertEqual(self.flash.read_bytes(), broken, "чип не тронут")
        self.assertFalse(any(":w:" in x for a in self.calls("avrdude") for x in a))

    def test_generic_profile_without_fuses_reads_nothing(self):
        self.use("avrdude", part="m8")                     # подпись та же, но профиль — по подписи
        from dirizher.plugins.avrdude import AvrdudePlugin
        p = AvrdudePlugin(self.conductor(), {"part": "m328p"})

        class Det:
            profile = None
        self.assertIsNone(p.inspect(Det()))

    def test_fuse_command_only_reads(self):
        from dirizher.plugins.avrdude import AvrdudePlugin
        p = AvrdudePlugin(self.conductor(), {"part": "m328p"})
        cmd = p.fuse_command(["lfuse", "lock"])
        self.assertIn("-n", cmd)
        self.assertEqual([cmd[i + 1] for i, a in enumerate(cmd) if a == "-U"], ["lfuse:r:-:h", "lock:r:-:h"])
        with self.assertRaises(RuntimeError):
            p.fuse_command(["flash"])


class FlashromWriteProtectTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.cfg["plugins"]["flashrom_spi"] = {"enabled": True}
        self.spi = self.fake / ft.SPI
        self.spi.write_bytes(ft.healthy_w25q64())

    def test_wp_enabled_visible(self):
        os.environ["FAKE_WP"] = "1"
        c = self.conductor()
        sid = c.run()
        t = self.text(c)
        self.assertIn("включена защита записи: область 0x0+0x800000 — весь чип", t)
        self.assertIn("аппаратная", t)
        self.assertIn("сверка покажет «не совпало»", t)
        self.assertEqual(self.outcome(c, sid), "healthy", "замечание об устройстве, а не о прошивке")
        self.assertTrue(any(a[-1] == "--wp-status" for a in self.calls("flashrom")))
        self.assertFalse((self.fake / "wp_changed").exists(), "защиту не трогали")
        versions = [a for a in self.calls("flashrom") if a == ["--version"]]
        self.assertEqual(len(versions), 1, "версия спрашивается один раз")

    def test_wp_disabled(self):
        c = self.conductor()
        c.run()
        self.assertIn("write_protect: защита записи выключена", self.text(c))

    def test_old_flashrom_skips_wp(self):
        os.environ["FAKE_FLASHROM_V"] = "1.2"
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy")
        self.assertFalse(any("--wp-status" in a for a in self.calls("flashrom")))
        self.assertNotIn("write_protect", self.text(c))
        self.assertTrue(any("--wp-status есть с 1.3" in m for m in self.events(c, sid)))


class ProbeRsIdTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.use("probe_rs")
        self.chip = self.fake / ft.MCU
        self.chip.write_bytes(ft.healthy_stm32())

    def test_id_confirms_model(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("DBGMCU_IDCODE (DEV_ID) = 0x410 — модель STM32F103C8 подтверждена", self.text(c))
        reads = [a for a in self.calls("probe-rs") if a[:1] == ["read"] and "b32" in a]
        self.assertEqual(len(reads), 1)
        self.assertIn("0xE0042000", reads[0])

    def test_id_mismatch_blocks(self):
        os.environ["FAKE_DEV_ID"] = "0x20036412"            # STM32F1 low-density
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "blocked", self.text(c))
        self.assertIn("= 0x412, а у STM32F103C8 должно быть 0x410", self.text(c))

    def test_parse_word(self):
        from dirizher.plugins.probe_rs import parse_word
        self.assertEqual(parse_word("WARN x: y\n0xe0042000: 20036410\n"), 0x20036410)
        self.assertEqual(parse_word("0x20036410"), 0x20036410)
        self.assertIsNone(parse_word("Error: something"))


class OpenOcdProtectionTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.cfg["plugins"]["openocd"] = {"enabled": True, "interface": "interface/stlink.cfg"}
        self.chip = self.fake / ft.MCU
        self.chip.write_bytes(ft.healthy_stm32())

    def test_rdp_off_from_profile_command(self):
        c = self.conductor()
        sid = c.run(target="stm32f103c8")
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("read_protection: защита чтения выключена", self.text(c))
        cmds = [x for a in self.calls("openocd") for x in a]
        self.assertIn("stm32f1x options_read 0", cmds)

    def test_rdp_on_blocks(self):
        os.environ["FAKE_PROTECTED"] = "1"
        c = self.conductor()
        sid = c.run(target="stm32f103c8")
        self.assertIn("включена защита чтения (on)", self.text(c))
        self.assertIn(self.outcome(c, sid), ("read_failed", "blocked"))
        self.assertEqual(self.chip.read_bytes(), ft.healthy_stm32())

    def test_not_read_command_refused(self):
        self.cfg["plugins"]["openocd"] = {**self.cfg["plugins"]["openocd"], "rdp_cmd": "stm32f1x options_write 0"}
        c = self.conductor()
        c.run(target="stm32f103c8")              # профиль задаёт свою команду, а настройки — нет
        self.cfg["plugins"]["openocd"] = {"enabled": True, "interface": "interface/stlink.cfg", "mcu": "STM32F103C8",
                                          "target": "target/stm32f1x.cfg", "flash_size": 65536,
                                          "rdp_cmd": "reset run"}
        c2 = self.conductor()
        c2.run()
        self.assertIn("не похожа на чтение — не запускаю", self.text(c2))
        self.assertFalse(any("reset run" in a for a in self.calls("openocd")))


class EsptoolEfuseTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.use("esptool")
        self.chip = self.fake / ft.ESP
        self.chip.write_bytes(ft.healthy_esp())

    def test_mac_id_and_efuse_off(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        t = self.text(c)
        self.assertIn("MAC 24:6f:28:aa:bb:cc, ID флеша 0x204016", t)
        self.assertIn("шифрование флеша выключено", t)
        self.assertIn("безопасная загрузка (secure boot) выключена", t)
        self.assertFalse((self.fake / "efuse_written").exists())
        self.assertTrue(any(a[-1] == "summary" for a in self.calls("espefuse")))

    def test_encryption_and_secure_boot_on(self):
        os.environ["FAKE_ESP_ENCRYPTED"] = "1"
        os.environ["FAKE_ESP_SECURE"] = "1"
        c = self.conductor()
        c.run()
        t = self.text(c)
        self.assertIn("включено шифрование флеша: копия — шифротекст", t)
        self.assertIn("включена безопасная загрузка (secure boot)", t)

    def test_efuse_command_only_summary(self):
        from dirizher.plugins.esptool_flash import EsptoolPlugin
        p = EsptoolPlugin(self.conductor(), {"port": "/dev/ttyUSB0"})
        self.assertEqual(p.efuse_command("espefuse")[-1], "summary")
        p2 = EsptoolPlugin(self.conductor(), {"port": "/dev/burn_efuse"})
        with self.assertRaises(RuntimeError):
            p2.efuse_command("espefuse")
        self.assertIs(EsptoolPlugin._flag("= 0 R/W (0b0000000)".lstrip("= ")), False)
        self.assertIs(EsptoolPlugin._flag("1 R/W (0b0000001)"), True)
        self.assertIs(EsptoolPlugin._flag("3 R/W (0b0000011)"), False)
        self.assertIs(EsptoolPlugin._flag("Enable R/W (0b001)"), True)


class MiniproPinTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.use("minipro", device="AT24C02")
        self.chip = self.fake / ft.MINIPRO
        self.chip.write_bytes(ft.healthy_24c02())

    def test_pins_ok(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("контакт всех выводов в панельке хороший", self.text(c))
        self.assertTrue(any("-z" in a for a in self.calls("minipro")))

    def test_bad_pin_blocks(self):
        os.environ["FAKE_BAD_PIN"] = "4"
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "blocked", self.text(c))
        self.assertIn("плохой контакт: вывод 4", self.text(c))
        self.assertFalse(any("-w" in a for a in self.calls("minipro")))


# ======================= Bus Pirate 5: VOUT =======================
class BusPirateVoutTests(InspectBase):
    def setUp(self):
        super().setUp()
        self.emu = BP5Emulator(ft.healthy_24c02(), "24C02")
        self.cfg["plugins"]["bp5_i2c"] = {"enabled": True, "port": self.emu.port, "eeprom": "24C02",
                                          "address": 0x50, "psu_voltage": 3.3, "pullups": True}

    def tearDown(self):
        self.emu.close()
        super().tearDown()

    def test_vout_matches(self):
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("замер совпал: на цели 3,3 В", self.text(c))

    def test_vout_sagged_stops(self):
        self.emu.vout = 2.1                                  # питание просело (КЗ или слабый источник)
        c = self.conductor()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "blocked_voltage", self.text(c))
        self.assertIn("замерено 2,1 В, а адаптер на 3,3 В", self.text(c))     # у 24C02 в профиле напряжения нет


# ======================= ядро =======================
class CoreInspectTests(InspectBase):
    def sim(self) -> Conductor:
        self.cfg["plugins"]["simulator"] = {"enabled": True}
        c = Conductor(self.cfg, AutoUI(), sim=True)
        self.opened.append(c)
        return c

    def test_simulator_unchanged(self):
        c = self.sim()
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertFalse(any("замер" in ln for ln in c.ui.lines))
        self.assertEqual(c.inspected, [])

    def test_inspect_error_does_not_break_session(self):
        c = self.sim()
        plugin = c.plugins[0]
        plugin.inspect = lambda det: (_ for _ in ()).throw(RuntimeError("порт занят"))
        sid = c.run()
        self.assertEqual(self.outcome(c, sid), "healthy", self.text(c))
        self.assertIn("дополнительные сведения от программы не получены: порт занят", self.text(c))

    def test_block_finding_forbids_tune(self):
        c = self.sim()
        plugin = c.plugins[0]
        plugin.inspect = lambda det: Inspection(findings=[Finding("x", "block", "проверка-заглушка")])
        sid = c.tune({"boot_delay": 3})
        self.assertEqual(self.outcome(c, sid), "blocked", self.text(c))
        self.assertIn("блокирующая проблема: проверка-заглушка", self.text(c))
        self.assertTrue(any(m == "[block] x: проверка-заглушка" for m in self.events(c, sid)))

    def test_measured_ignored_for_isolated_bus(self):
        c = self.sim()
        plugin = c.plugins[0]
        plugin.voltage_check = False
        plugin.inspect = lambda det: Inspection(measured_voltage=1.0)
        sid = c.run()
        self.assertNotEqual(self.outcome(c, sid), "blocked_voltage", self.text(c))

    def test_fuses_spec_validation(self):
        with self.assertRaises(ValueError):
            fuses_mod.normalize({"lfuse": [{"name": "x"}]})
        with self.assertRaises(ValueError):
            fuses_mod.normalize({"lfuse": [{"name": "x", "mask": 0x100}]})
        with self.assertRaises(ValueError):
            fuses_mod.normalize({"lock": [{"name": "x", "mask": 3, "status": {0: "danger"}}]})
        with self.assertRaises(ValueError):
            fuses_mod.normalize_id_register({"address": 0xE0042000})
        spec = fuses_mod.normalize({"lock": [{"name": "LB", "mask": 3, "values": {3: "нет"}}]})
        self.assertEqual(fuses_mod.decode(spec, {"lock": 0xFE})[0][2][0][1], "значение 0x02 (нет в профиле)")


if __name__ == "__main__":
    unittest.main()
