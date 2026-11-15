"""Приложение на телефоне (PWA): манифест, значки, сервис-воркер, раздача прибором и демо. Без железа."""
from __future__ import annotations

import http.client
import importlib.util
import json
import re
import shutil
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from dirizher.core.config import load_config
from dirizher.interface import web

ROOT = Path(__file__).resolve().parent.parent
STATIC = web.STATIC


def png_size(data: bytes) -> tuple:
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise AssertionError("не PNG")
    return struct.unpack(">II", data[16:24])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))

    def test_fields_needed_for_install(self):
        for key in ("name", "short_name", "start_url", "scope", "display", "icons", "theme_color",
                    "background_color"):
            self.assertIn(key, self.m)
        self.assertEqual(self.m["display"], "standalone")
        # Относительные адреса: демо живёт в подпапке /<репозиторий>/ на GitHub Pages.
        for key in ("start_url", "scope", "id"):
            self.assertFalse(self.m[key].startswith("/"), key)

    def test_icons_exist_and_sizes_match(self):
        sizes = set()
        for icon in self.m["icons"]:
            path = STATIC / icon["src"]
            self.assertTrue(path.exists(), icon["src"])
            self.assertFalse(icon["src"].startswith("/"))
            if icon["type"] == "image/png":
                w, h = png_size(path.read_bytes())
                self.assertEqual(icon["sizes"], f"{w}x{h}", icon["src"])
                sizes.add(w)
        self.assertTrue({192, 512} <= sizes, "Chrome ждёт значки 192 и 512")
        self.assertIn("maskable", {i.get("purpose") for i in self.m["icons"]})

    def test_theme_matches_page(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        color = re.search(r'name="theme-color" content="([^"]+)"', page).group(1)
        self.assertEqual(self.m["theme_color"].lower(), color.lower())

    def test_every_listed_file_is_served(self):
        listed = {"manifest.webmanifest", "sw.js"} | {i["src"] for i in self.m["icons"]}
        self.assertEqual(listed, set(web.APP_FILES))

    def test_icons_rebuild_identically(self):
        spec = importlib.util.spec_from_file_location("make_icons", ROOT / "tools" / "make_icons.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        raw = mod.render(192, False)
        self.assertEqual(mod.png(192, raw), (STATIC / "icon-192.png").read_bytes(),
                         "значок расходится с tools/make_icons.py — перезапустите его")
        self.assertEqual(len(zlib.decompress(mod.png(192, raw)[33 + 8:-12])), 192 * (192 * 4 + 1))


class PageAndWorkerTests(unittest.TestCase):
    def test_page_links_manifest_relatively(self):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<link rel="manifest" href="manifest.webmanifest">', page)
        self.assertIn('<link rel="apple-touch-icon" href="icon-192.png">', page)
        # Воркер только по HTTPS: на приборе (http://) экран не должен брать что-либо из кэша.
        self.assertRegex(page, r'location\.protocol === "https:"\)\s*\{\s*window\.addEventListener\("load"')
        self.assertIn('register("sw.js")', page)

    def test_worker_never_touches_api_or_writes(self):
        sw = (STATIC / "sw.js").read_text(encoding="utf-8")
        self.assertIn('url.pathname.includes("/api/")', sw)
        self.assertIn('req.method !== "GET"', sw)
        self.assertIn("url.origin !== self.location.origin", sw)
        # «Сначала сеть»: кэш — только запасной путь без сети.
        self.assertLess(sw.index("fetch(req)"), sw.index("caches.match(req)"))


class WebServesAppFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_pwa_"))
        cfg = load_config(ROOT / "config.yaml")
        cfg["data_dir"] = self.tmp / "data"
        cfg["mirror_dir"] = None
        cfg["_path"] = ROOT / "config.yaml"
        self.app = web.App(cfg, sim_default=True, pin="246810")
        self.app.trust_local = False          # как с телефона: без кода входа
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=30)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, r.getheader("Content-Type") or "", body

    def test_files_without_login(self):
        for name, ctype in web.APP_FILES.items():
            st, got, body = self.get("/" + name)
            self.assertEqual(st, 200, name)
            self.assertEqual(got, ctype, name)
            self.assertEqual(body, (STATIC / name).read_bytes(), name)

    def test_api_still_needs_login(self):
        st, _, _ = self.get("/api/device")
        self.assertEqual(st, 401)

    def test_no_path_tricks(self):
        for path in ("/../config.yaml", "/static/index.html", "/icon-192.png/..", "/web.py", "/sw.js.bak"):
            st, _, _ = self.get(path)
            self.assertEqual(st, 404, path)


class PagesBuildTests(unittest.TestCase):
    def test_demo_has_app_files(self):
        spec = importlib.util.spec_from_file_location("build_pages", ROOT / "tools" / "build_pages.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = Path(tempfile.mkdtemp(prefix="dirizher_site_"))
        try:
            mod.build_page(out)
            for name in web.APP_FILES:
                self.assertEqual((out / name).read_bytes(), (STATIC / name).read_bytes(), name)
            page = (out / "index.html").read_text(encoding="utf-8")
            self.assertIn('<link rel="manifest" href="manifest.webmanifest">', page)
            self.assertIn('<script src="demo.js"></script>', page)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
