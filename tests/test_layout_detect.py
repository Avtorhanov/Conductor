"""Определители разметки (core/layout_detect.py) и черновик профиля платы. Без железа.

Образы синтетические, по документации форматов (ESP-IDF, Intel IFD). Испорченная таблица — находка, а не падение.
Запуск:  python3 -m unittest tests.test_layout_detect -v
"""
from __future__ import annotations

import random
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import yaml

from dirizher.core import layout_detect as ld
from dirizher.core.config import load_config
from dirizher.core.learning import binwalk_signatures, content_signature
from dirizher.core.pipeline import Conductor
from dirizher.core.profiles import Profile, ProfileDB
from dirizher.core.ui import AutoUI
from dirizher.interface import guide
from tests.fakes import fake_tools as ft
from tests.test_integrations import Base

ROOT = Path(__file__).resolve().parent.parent
MB = 1024 * 1024

# ---------- синтетический дескриптор Intel ----------
GBE = (0x1000, 0x3000)
ME = (0x3000, 0x500000)
BIOS = (0x500000, 0x800000)


def flreg(lo: int, hi: int) -> int:
    """Регистр FLREG: база и предел (включительно) в блоках по 4 КБ."""
    return ((hi - 1) >> 12) << 16 | (lo >> 12)


def ifd_image(size: int = 8 * MB, regions=None, frba: int = 0x40, mac: bytes = b"\x00\x1b\x21\x11\x22\x33") -> bytes:
    rnd = random.Random(8086)
    img = bytearray(b"\xff" * size)
    img[0x10:0x14] = ld.IFD_SIGNATURE
    struct.pack_into("<II", img, 0x14, 0x03 | (frba >> 4) << 16 | 0x04 << 24, 0x08)   # FCBA=0x30, FMBA=0x80
    if regions is None:
        regions = [flreg(0, 0x1000), flreg(*BIOS), flreg(*ME), flreg(*GBE), 0x00007FFF]
    for i in range(16):
        struct.pack_into("<I", img, frba + 4 * i, regions[i] if i < len(regions) else 0x00007FFF)
    img[GBE[0]:GBE[0] + 6] = mac
    img[ME[0]:ME[0] + 0x4000] = rnd.randbytes(0x4000)
    img[BIOS[0]:BIOS[0] + 0x20000] = rnd.randbytes(0x20000)
    return bytes(img)


def esp_image(parts=ft.ESP_PARTITIONS, md5: bool = True) -> bytes:
    img = bytearray(ft.healthy_esp())
    img[0x8000:0x8C00] = b"\xff" * 0xC00
    table = ft.esp_partition_table(parts, md5)
    img[0x8000:0x8000 + len(table)] = table
    return bytes(img)


# =====================================================================
class EspPartitionTests(unittest.TestCase):
    def test_healthy_table(self):
        parts = [("nvs", 1, 0x02, 0x9000, 0x4000), ("otadata", 1, 0x00, 0xD000, 0x2000),
                 ("phy_init", 1, 0x01, 0xF000, 0x1000), ("ota_0", 0, 0x10, 0x10000, 0x180000),
                 ("ota_1", 0, 0x11, 0x190000, 0x180000), ("spiffs", 1, 0x82, 0x310000, 0xF0000)]
        res = ld.detect(esp_image(parts), ["esp_partitions"], 4 * MB)
        self.assertTrue(res.ok, res.problems)
        by = {r["id"]: r for r in res.rows}
        self.assertEqual(list(by), ["bootloader", "partition_table", "nvs", "otadata", "phy_init",
                                    "ota_0", "ota_1", "spiffs"])
        self.assertEqual((by["bootloader"]["offset"], by["bootloader"]["end"]), (0x1000, 0x8000))
        self.assertTrue(by["nvs"]["preserve"] and by["phy_init"]["preserve"])      # уникальное — с чипа
        self.assertEqual((by["ota_0"]["kind"], by["ota_0"]["restore"]), ("app", True))
        self.assertEqual((by["otadata"]["kind"], by["otadata"]["restore"]), ("config", False))
        self.assertFalse(by["spiffs"]["restore"])                                  # файлы владельца
        self.assertEqual(res.notes, [])

    def test_no_signature_is_not_ours(self):
        self.assertIsNone(ld.detect(b"\xff" * (4 * MB), ["esp_partitions"]))
        self.assertIsNone(ld.detect(b"", ["esp_partitions", "intel_ifd"]))

    def test_without_md5_is_a_note(self):
        res = ld.detect(esp_image(md5=False), ["esp_partitions"], 4 * MB)
        self.assertTrue(res.ok)
        self.assertIn("MD5 нет", res.notes[0])

    def test_damaged_tables_are_explained(self):
        good = esp_image()
        bad_md5 = bytearray(good)
        bad_md5[0x8000 + 4] ^= 0x01                         # смещение nvs испорчено — MD5 не сойдётся
        garbage = bytearray(good)
        garbage[0x8020:0x8022] = b"\x12\x34"                # вторая запись без подписи
        outside = esp_image([("factory", 0, 0, 0x10000, 0x800000)])
        overlap = esp_image([("a", 1, 2, 0x9000, 0x6000), ("b", 1, 1, 0xC000, 0x1000)])
        cases = {"MD5": bytes(bad_md5), "нет подписи AA 50": bytes(garbage), "вне флеша": outside,
                 "противоречива": overlap}
        for needle, img in cases.items():
            with self.subTest(needle):
                res = ld.detect(img, ["esp_partitions"], 4 * MB)
                self.assertFalse(res.ok)
                self.assertIn(needle, "; ".join(res.problems))

    def test_old_fixture_magic_only_is_damaged(self):
        img = bytearray(b"\xff" * (4 * MB))
        img[0x8000:0x8002] = b"\xAA\x50"
        res = ld.detect(bytes(img), ["esp_partitions"])
        self.assertFalse(res.ok)
        self.assertTrue(res.problems)


class IntelIfdTests(unittest.TestCase):
    def test_healthy_descriptor(self):
        res = ld.detect(ifd_image(), ["intel_ifd"], 8 * MB)
        self.assertTrue(res.ok, res.problems)
        by = {r["id"]: r for r in res.rows}
        self.assertEqual(list(by), ["descriptor", "gbe", "me", "bios"])           # по смещению; PDR не задан
        self.assertEqual((by["bios"]["offset"], by["bios"]["end"]), BIOS)
        self.assertEqual((by["me"]["offset"], by["me"]["end"]), ME)
        self.assertTrue(by["gbe"]["preserve"] and not by["gbe"]["restore"])      # MAC — с чипа
        self.assertTrue(by["descriptor"]["restore"])

    def test_damaged_descriptor_is_explained(self):
        cases = {
            "выходит за размер": ifd_image(regions=[flreg(0, 0x1000), flreg(0x500000, 0x1000000)]),
            "дескриптора не описан": ifd_image(regions=[0x00007FFF, flreg(*BIOS)]),
            "противоречива": ifd_image(regions=[flreg(0, 0x1000), flreg(0x4000, 0x800000), flreg(*ME)]),
            "FLMAP0": ifd_image()[:0x14] + struct.pack("<I", 0x03 | 0x01 << 16) + ifd_image()[0x18:],
        }
        for needle, img in cases.items():
            with self.subTest(needle):
                res = ld.detect(img, ["intel_ifd"], 8 * MB)
                self.assertFalse(res.ok)
                self.assertIn(needle, "; ".join(res.problems))

    def test_short_dump(self):
        res = ld.detect(ifd_image()[:0x800], ["intel_ifd"])
        self.assertIn("короче 4 КБ", res.problems[0])


class RobustnessTests(unittest.TestCase):
    def test_random_garbage_never_crashes(self):
        rnd = random.Random(4)
        for _ in range(300):
            n = rnd.choice([0x20, 0x900, 0x1000, 0x8020, 0x9000, 0x10000])
            img = bytearray(rnd.randbytes(n))
            if n > 0x8002:
                img[0x8000:0x8002] = ld.ESP_MAGIC
            if n > 0x14:
                img[0x10:0x14] = ld.IFD_SIGNATURE
            for name, fn in ld.DETECTORS.items():
                res = fn(bytes(img))                       # сам разборщик, без страховки detect()
                planted = n > 0x8002 if name == "esp_partitions" else n > 0x14
                self.assertEqual(res is not None, planted, name)
            res = ld.detect(bytes(img), list(ld.DETECTORS))
            self.assertTrue(res is None or isinstance(res.problems, list))

    def test_detector_exception_becomes_problem(self):
        ld.DETECTORS["_boom"], ld.TITLES["_boom"] = (lambda data: 1 / 0), "проба"
        try:
            res = ld.detect(b"x", ["_boom"])
        finally:
            ld.DETECTORS.pop("_boom"), ld.TITLES.pop("_boom")
        self.assertIn("разбор не удался", res.problems[0])


class ProfileFieldTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(ld.normalize_names("all"), list(ld.DETECTORS))
        self.assertEqual(ld.normalize_names(["intel_ifd", "intel_ifd"]), ["intel_ifd"])
        self.assertEqual(ld.normalize_names(None), [])
        with self.assertRaises(ValueError) as e:
            ld.normalize_names(["uefi_magic"])
        self.assertIn("неизвестный определитель", str(e.exception))

    def test_regions_recipe_with_auto_layout(self):
        p = Profile.from_dict({"id": "y", "name": "Y", "class": "mcu.esp", "layout_auto": ["esp_partitions"],
                               "recipes": [{"id": "r", "action": "write_reference_regions"}]}, Path("y.yaml"))
        self.assertEqual(p.layout_auto, ["esp_partitions"])

    def test_shipped_profiles(self):
        cfg = load_config(ROOT / "config.yaml")
        db = ProfileDB(cfg["profiles_dir"])
        self.assertEqual(db.errors, [])
        self.assertEqual(db.get("esp32_4mb").layout_auto, ["esp_partitions"])
        self.assertEqual(db.get("generic_spi_nor").layout_auto, list(ld.DETECTORS))
        self.assertNotIn("добавьте preserve", db.get("esp32_4mb").notes)


class HelperTests(unittest.TestCase):
    def test_signature_skips_blank_and_unique(self):
        data = b"\xff" * 0x40 + b"MAC=0011223344556677" + b"\x00" * 12 + b"FIRMWARE-V1.2.3!" + b"\xff" * 64
        self.assertEqual(content_signature(data), {"offset": "0x40", "hex": b"MAC=001122334455".hex().upper()})
        sig = content_signature(data, [{"offset": 0x40, "length": 0x20}])
        self.assertEqual(sig["offset"], "0x60")
        self.assertIsNone(content_signature(b"\xff" * 4096))

    def test_binwalk_parsing_v2_and_v3(self):
        v2 = ("\nDECIMAL       HEXADECIMAL     DESCRIPTION\n" + "-" * 80 + "\n"
              "0             0x0             uImage header, header size: 64 bytes\n"
              "2097152       0x200000        Squashfs filesystem, little endian, version 4.0\n"
              "3000000       0x2DC6C0        Squashfs filesystem, little endian\n")
        self.assertEqual(binwalk_signatures(v2), ["uImage header", "Squashfs filesystem"])
        v3 = "  64     0x40     LZMA compressed data, properties: 0x5D\n"
        self.assertEqual(binwalk_signatures(v3), ["LZMA compressed data"])


# =====================================================================
class SimulatorIfdTests(unittest.TestCase):
    """BIOS ноутбука на учебной микросхеме W25Q64: профиль чипа + layout_auto: all."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_ld_"))
        self.cfg = load_config(ROOT / "config.yaml")
        self.cfg["data_dir"] = self.tmp / "data"
        self.cfg["mirror_dir"] = None
        self.opened = []

    def tearDown(self):
        for c in self.opened:
            c.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def conductor(self, answers=None, dry_run=False):
        c = Conductor(self.cfg, AutoUI(answers, True), sim=True, dry_run=dry_run)
        self.opened.append(c)
        return c

    def put(self, c, data: bytes) -> None:
        sim = c.plugin("simulator")
        sim.do_reset()
        sim.write(None, data)

    def test_laptop_bios_cycle_keeps_mac(self):
        healthy = ifd_image()
        c = self.conductor(["yes"])
        self.put(c, healthy)
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertEqual(c.memory.session(sid)["outcome"], "healthy", text)
        self.assertIn("layout_auto: разметка по содержимому (дескриптор Intel (IFD)): разделов 4", text)
        self.assertIn("Сохраняются с чипа: GbE — сетевая карта (MAC)", text)
        detail = guide.session_detail(c, sid)
        self.assertEqual(detail["auto_layout"]["detector"], "intel_ifd")
        self.assertEqual(detail["auto_layout"]["regions"][1]["span"], "0x1000…0x3000")
        # черновик профиля платы — из эталона
        drafts = list((c.data_dir / "drafts").glob("board_*.yaml"))
        self.assertEqual(len(drafts), 1, text)
        self.assertIn("черновик профиля платы", text)
        raw = yaml.safe_load(drafts[0].read_text(encoding="utf-8"))
        prof = Profile.from_dict(raw, drafts[0])                      # черновик — рабочий профиль
        self.assertEqual(raw["content_match"][0]["offset"], "0x10")
        self.assertEqual([r["id"] for r in prof.layout], ["descriptor", "gbe", "me", "bios"])
        self.assertIn("restore_regions", [r["id"] for r in prof.recipes])
        self.assertTrue(prof.content_matches(healthy))

        # другая плата той же модели (свой MAC), BIOS испорчен → ремонт из эталона, MAC — свой
        broken = bytearray(ifd_image(mac=b"\x00\x1b\x21\xAA\xBB\xCC"))
        broken[BIOS[0]:BIOS[0] + 0x8000] = b"\x00" * 0x8000
        c = self.conductor(["yes"])
        self.put(c, bytes(broken))
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertEqual(c.memory.session(sid)["outcome"], "repaired", text)
        self.assertIn("По разделам: «BIOS / UEFI» — 8 секторов.", text)
        self.assertNotIn("«GbE — сетевая карта (MAC)» —", text)
        self.assertIn("сохраняю с текущего чипа: GbE — сетевая карта (MAC)", text)
        flash = c.plugin("simulator").image.read_bytes()
        self.assertEqual(flash[BIOS[0]:BIOS[1]], healthy[BIOS[0]:BIOS[1]])
        self.assertEqual(flash[GBE[0]:GBE[0] + 6], b"\x00\x1b\x21\xAA\xBB\xCC")

    def test_damaged_descriptor_is_a_finding(self):
        c = self.conductor()
        self.put(c, ifd_image(regions=[flreg(0, 0x1000), flreg(0x500000, 0x1000000)]))
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertIn("✖ layout_auto: дескриптор Intel (IFD): подпись есть, но разметка испорчена — регион "
                      "«BIOS / UEFI» (0x500000…0x1000000) выходит за размер дампа 0x800000. "
                      "Разделы не определены, работаю без карты.", text)
        self.assertEqual(c.memory.session(sid)["outcome"], "no_action")
        self.assertEqual(list((c.data_dir / "drafts").glob("board_*.yaml")), [])

    def test_plain_dump_unchanged(self):
        """Учебная плата и дамп без подписей — как раньше: ни находки, ни черновика платы."""
        c = self.conductor(["yes"])
        c.plugin("simulator").do_reset()
        c.run()
        text = "\n".join(c.ui.lines)
        self.assertNotIn("layout_auto", text)
        self.assertEqual(list((c.data_dir / "drafts").glob("board_*.yaml")), [])


# =====================================================================
class EsptoolLayoutTests(Base):
    def setUp(self):
        super().setUp()
        self.use("esptool")
        self.chip = self.fake / ft.ESP
        self.chip.write_bytes(ft.healthy_esp())

    def test_nvs_kept_app_restored_by_regions(self):
        c = self.conductor()
        c.run()                                                  # эталон
        broken = bytearray(ft.healthy_esp())
        broken[0x9000:0x9010] = b"NVS:wifi=office7"              # свои настройки Wi-Fi
        broken[0x20000:0x30000] = b"\x00" * 0x10000              # испорченное приложение
        self.chip.write_bytes(bytes(broken))
        c = self.conductor([1, "yes"])                           # второй способ — только разделы
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertEqual(c.memory.session(sid)["recipe_id"], "restore_regions", text)
        self.assertEqual(c.memory.session(sid)["outcome"], "repaired", text)
        self.assertIn("Сохраняются с чипа: nvs — настройки NVS (Wi-Fi, ключи, счётчики), phy_init — "
                      "калибровка радио", text)
        self.assertIn("беру из эталона: «factory — приложение (factory)»", text)
        flash = self.chip.read_bytes()
        self.assertEqual(flash[0x9000:0x9010], b"NVS:wifi=office7")
        self.assertEqual(flash[0x20000:0x30000], ft.healthy_esp()[0x20000:0x30000])

    def test_broken_table_is_a_finding(self):
        img = bytearray(ft.healthy_esp())
        img[0x8004] ^= 0x10                                       # MD5 таблицы не сойдётся
        self.chip.write_bytes(bytes(img))
        c = self.conductor()
        sid = c.run()
        text = "\n".join(c.ui.lines)
        self.assertIn("✖ layout_auto: таблица разделов ESP32: подпись есть, но разметка испорчена — "
                      "контрольная сумма MD5 таблицы не совпала", text)
        self.assertNotEqual(c.memory.session(sid)["outcome"], "healthy")

    def test_board_draft_with_binwalk(self):
        c = self.conductor()
        data = ft.healthy_w25q64()
        path = c.learning.record_board(c.profiles.get("generic_spi_nor"), "memory.spi_nor", data, "ab" * 32)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        expect = [x for x in raw["checks"] if isinstance(x, dict) and x["type"] == "binwalk_expect"]
        self.assertEqual(expect[0]["expect"], ["uImage header", "Squashfs filesystem"])
        self.assertEqual(raw["content_match"], [{"offset": "0x0", "hex": data[:16].hex().upper()}])
        self.assertNotIn("layout", raw)
        Profile.from_dict(raw, path)
        again = c.learning.record_board(c.profiles.get("generic_spi_nor"), "memory.spi_nor", data, "ab" * 32)
        self.assertEqual(again, path)                            # черновик не переписывается


if __name__ == "__main__":
    unittest.main()
