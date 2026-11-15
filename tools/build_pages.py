"""Собрать демо интерфейса для GitHub Pages — страница работает в браузере без прибора и без сервера.

Как устроено:
  * берётся та же страница, что на приборе (dirizher/interface/static/index.html), плюс подмена связи
    tools/pages_demo.js: запросы /api/… не уходят в сеть, а отвечаются из записанных данных;
  * данные записываются здесь же, НАСТОЯЩИМ ядром: сборщик запускает веб-сервер Дирижёра в учебном режиме
    на временной копии проекта и проходит сценарии на виртуальной микросхеме;
  * для заданий записывается дерево ходов: для каждого состояния учебной микросхемы (исправна / повреждена /
    стёрта, плохой контакт, есть ли эталон) и каждого ответа на вопросы («ДА» и неверное слово, «да / нет /
    позже», выбор варианта). Поэтому в демо ответы меняют ход так же, как на приборе.
Сохранение, капот и запись «что помогло» в демо не работают — об этом говорит сама страница.

Запуск из корня проекта:
    python3 tools/build_pages.py               → папка _site/ (её публикует .github/workflows/pages.yml)
    python3 tools/build_pages.py --out /tmp/s  → в другую папку
Проверить локально:  python3 -m http.server -d _site 8000   и открыть http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import http.client
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dirizher import __version__  # noqa: E402
from dirizher.core.config import load_config  # noqa: E402
from dirizher.interface import web  # noqa: E402
from dirizher.plugins.simulator import SimulatorPlugin  # noqa: E402
from dirizher.workshop import helpdocs  # noqa: E402
from dirizher.workshop.hood import AREAS  # noqa: E402

SIM_DO = ("reset", "corrupt", "erase", "flaky", "steady", "status")
JOBS = [("run", {}), ("run", {"dry_run": True}), ("detect", {}), ("params", {})] + [("sim", {"do": d}) for d in SIM_DO]
MAX_LEAVES = 12          # ветвей одного задания
MAX_STATES = 16          # состояний учебной микросхемы
DEMO_TEXT = ("Демо: железа нет — задания проигрываются по записи настоящего ядра на учебной микросхеме. "
             "Сохранение и капот — только на приборе.")
IGNORE = shutil.ignore_patterns(".git", "data", "__pycache__", "tests", "_site", "*.pyc", "dirizher_code.txt")


def job_key(action: str, params: dict) -> str:
    key = action
    if params.get("dry_run"):
        key += ":dry"
    if action == "sim":
        key += ":" + str(params.get("do"))
    return key


class Recorder:
    def __init__(self, out: Path, quick: bool = False):
        self.out = out
        self.quick = quick
        self.tmp = Path(tempfile.mkdtemp(prefix="dirizher_pages_"))
        self.root = self.tmp / "root"
        shutil.copytree(ROOT, self.root, ignore=IGNORE)
        self.data = self.tmp / "data"
        self.cfg = load_config(self.root / "config.yaml")
        self.cfg["data_dir"] = self.data
        self.cfg["mirror_dir"] = None
        self.app = web.App(self.cfg, sim_default=True, pin="000000")
        self.httpd = web.make_server(self.app, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.states_dir = self.tmp / "states"
        self.states_dir.mkdir()
        self.runs = 0

    def close(self) -> None:
        self.app.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------- связь с настоящим сервером ----------
    def req(self, method: str, path: str, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=120)
        hdrs = {"X-Dirizher": "1"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        raw = r.read()
        conn.close()
        payload = json.loads(raw) if "json" in (r.getheader("Content-Type") or "") else None
        if r.status != 200:
            raise RuntimeError(f"{method} {path}: {r.status} {payload}")
        return self.clean(payload)

    def clean(self, obj):
        """Убрать из записи пути временной папки сборки."""
        text = json.dumps(obj, ensure_ascii=False)
        for real, shown in ((str(self.data), "data"), (str(self.root), "~/dirizher"), (str(self.tmp), "~")):
            text = text.replace(real.replace("\\", "\\\\"), shown)
        return json.loads(text)

    # ---------- состояние учебной микросхемы ----------
    def world(self) -> str:
        sim = self.data / "simulator"
        flash = sim / "sim" / "flash.bin"
        if not flash.exists():
            content = "healthy"
        else:
            data = flash.read_bytes()
            content = "healthy" if data == SimulatorPlugin.healthy() else (
                "erased" if data.count(0xFF) == len(data) else "corrupt")
        flaky = (sim / "sim" / "flaky").exists()
        refs = 0
        db = sim / "memory.sqlite"
        if db.exists():
            con = sqlite3.connect(str(db))
            try:
                refs = con.execute("SELECT COUNT(*) FROM refs").fetchone()[0]
            except sqlite3.Error:
                refs = 0
            con.close()
        return f"{content}{'-flaky' if flaky else ''}{'-ref' if refs else ''}"

    def snapshot(self, key: str) -> Path:
        dest = self.states_dir / key
        if not dest.exists():
            shutil.copytree(self.data, dest) if self.data.exists() else dest.mkdir()
        return dest

    def restore(self, snap: Path) -> None:
        shutil.rmtree(self.data, ignore_errors=True)
        shutil.copytree(snap, self.data)

    # ---------- одно задание по заданным ответам ----------
    @staticmethod
    def answer_for(q: dict, key: str):
        t = q["type"]
        if t == "pause":
            return True
        if t == "yes_no":
            return key
        if t == "choose":
            return int(key)
        if t == "confirm_word":
            return q.get("word", "ДА") if key == "ok" else "нет"
        return None

    @staticmethod
    def default_key(q: dict) -> str:
        return {"pause": "ok", "yes_no": "yes", "choose": "0", "confirm_word": "ok"}.get(q["type"], "ok")

    @staticmethod
    def alternatives(q: dict) -> list:
        t = q["type"]
        if t == "yes_no":
            return [o["value"] for o in q.get("options", [])]
        if t == "choose":
            return [str(i) for i in range(min(len(q.get("options", [])), 3))]
        if t == "confirm_word":
            return ["ok", "wrong"]
        return ["ok"]

    def run_once(self, action: str, params: dict, prefix: list) -> tuple:
        self.runs += 1
        st = self.req("POST", "/api/job", {"action": action, "params": params, "sim": True})["job"]
        job_id, since, title = st["id"], 0, st["title"]
        steps, events, keys = [], [], list(prefix)
        deadline = time.time() + 120
        while time.time() < deadline:
            job = self.req("GET", f"/api/job?wait=1&since={since}&job={job_id}")["job"]
            since = job["seq"]
            events += [{k: v for k, v in e.items() if k != "seq"} for e in job["events"]]
            if job["done"]:
                steps.append((events, None, None))
                return title, steps, {"result": job["result"], "next": job["next"]}
            q = job["question"]
            if q:
                i = len(steps)
                key = keys[i] if i < len(keys) else self.default_key(q)
                if i >= len(keys):
                    keys.append(key)
                qq = {k: v for k, v in q.items() if k != "qid"}
                steps.append((events, qq, key))
                events = []
                self.req("POST", "/api/answer", {"job": job_id, "qid": q["qid"], "value": self.answer_for(q, key)})
        raise RuntimeError(f"задание {action} не завершилось")

    def record_tree(self, snap: Path, action: str, params: dict, found_states: dict) -> dict:
        tree, queue, seen, leaves = {"title": "", "root": {}}, [[]], set(), 0
        while queue and leaves < MAX_LEAVES:
            prefix = queue.pop(0)
            if tuple(prefix) in seen:
                continue
            seen.add(tuple(prefix))
            self.restore(snap)
            title, steps, final = self.run_once(action, params, prefix)
            leaves += 1
            end = self.world()
            if end not in found_states and len(found_states) < MAX_STATES:
                found_states[end] = self.snapshot(end)
            tree["title"] = tree["title"] or title
            node = tree["root"]
            for i, (evs, q, key) in enumerate(steps):
                node.setdefault("ev", evs)
                if q is None:
                    node["end"] = dict(final, state=end)
                    break
                node.setdefault("q", q)
                branches = node.setdefault("br", {})
                if i >= len(prefix):
                    taken = [k for _, qq, k in steps[:i] if qq is not None]
                    for alt in self.alternatives(q):
                        if alt != key and alt not in branches and not self.quick:
                            queue.append(taken + [alt])
                node = branches.setdefault(key, {})
        return tree

    # ---------- всё вместе ----------
    def record(self) -> dict:
        found = {}
        start = self.world()
        found[start] = self.snapshot(start)
        jobs, suggest, done = {}, {}, set()
        while True:
            todo = [k for k in found if k not in done]
            if not todo:
                break
            for key in todo:
                done.add(key)
                self.restore(found[key])
                suggest[key] = self.req("GET", "/api/suggest?sim=1")
                jobs[key] = {}
                for action, params in JOBS:
                    jobs[key][job_key(action, params)] = self.record_tree(found[key], action, params, found)
                print(f"   состояние {key}: записано заданий {len(jobs[key])}, прогонов всего {self.runs}", flush=True)
        return {"start": start, "jobs": jobs, "suggest": suggest}

    def story(self) -> dict:
        """Журнал для демо: эталон с исправной → поломка → ремонт → параметры."""
        shutil.rmtree(self.data, ignore_errors=True)
        plan = [("sim", {"do": "reset"}, []), ("run", {}, ["ok", "yes"]), ("sim", {"do": "corrupt"}, []),
                ("run", {"dry_run": True}, ["ok"]), ("run", {}, ["ok", "ok", "yes"]), ("params", {}, [])]
        for action, params, answers in plan:
            self.run_once(action, params, answers)
        hist = self.req("GET", "/api/history?n=100&sim=1")
        sessions, fixes = {}, {}
        for s in hist["items"]:
            sessions[str(s["id"])] = self.req("GET", f"/api/session?id={s['id']}&sim=1")
            fixes[str(s["id"])] = self.req("GET", f"/api/fixes?session={s['id']}&sim=1")
        picks = {p: self.req("GET", f"/api/pick?purpose={p}&sim=1") for p in ("rollback", "feedback", "reference", "export")}
        try:
            params = self.req("GET", "/api/params?sim=1")
        except RuntimeError:
            params = None
        return {"history": hist, "sessions": sessions, "fixes": fixes, "pick": picks, "params": params,
                "doctor": self.req("GET", "/api/doctor?sim=1"), "device": self.req("GET", "/api/device?sim=1"),
                "skills": self.req("GET", "/api/skills?sim=1")}

    def static(self) -> dict:
        cat = self.req("GET", "/api/catalog?sim=1")
        cards = {c["id"]: self.req("GET", f"/api/catalog/card?id={c['id']}&sim=1") for c in cat["items"]}
        items = [dict(it, _text=it["_text"][:4000]) for it in helpdocs.articles(self.cfg)]
        articles = {}
        for it in items:
            if it["kind"] == "article":
                articles[it["id"]] = self.req("GET", f"/api/help/article?id={it['id']}")
        hello = self.req("GET", "/api/hello")
        hello.update({"authed": True, "local": False, "demo": True, "demo_text": DEMO_TEXT, "sim_default": True,
                      "connect": {"urls": [], "pin": None, "need_pin": False}})
        return {"version": __version__, "hello": hello, "guide": self.req("GET", "/api/guide"),
                "roadmap": self.req("GET", "/api/roadmap"), "catalog": cat, "cards": cards,
                "help_items": self.clean(items), "articles": articles,
                "builder_fields": self.req("GET", "/api/builder/fields"),
                "builder_drafts": self.req("GET", "/api/builder/drafts")}

    def hood(self) -> dict:
        areas, files = {}, {}
        for area in AREAS:
            listing = self.req("GET", f"/api/hood?area={area}")
            areas[area] = listing
            for f in listing["files"]:
                if f["path"] not in files:
                    files[f["path"]] = self.req("GET", f"/api/hood/file?path={f['path']}")
        return {"areas": areas, "files": files}


def build_page(out: Path) -> None:
    page = (ROOT / "dirizher" / "interface" / "static" / "index.html").read_text(encoding="utf-8")
    marker = '<script>\n"use strict";'
    if page.count(marker) != 1:
        raise SystemExit("не нашёл начало сценария страницы — проверьте index.html")
    page = page.replace(marker, '<script src="demo.js"></script>\n' + marker, 1)
    page = page.replace("<title>Дирижёр</title>", "<title>Дирижёр — демо интерфейса</title>", 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    shutil.copyfile(ROOT / "tools" / "pages_demo.js", out / "demo.js")
    (out / ".nojekyll").write_text("", encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Собрать демо интерфейса «Дирижёра» для GitHub Pages")
    ap.add_argument("--out", default=str(ROOT / "_site"), help="куда положить сайт (по умолчанию _site/)")
    ap.add_argument("--quick", action="store_true", help="только основной ход заданий, без запасных ветвей")
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "demo").mkdir(parents=True)
    t0 = time.time()
    rec = Recorder(out, quick=args.quick)
    try:
        print("1/4 справочник, справка, Штурман…", flush=True)
        core = rec.static()
        print("2/4 капот (только просмотр)…", flush=True)
        hood = rec.hood()
        print("3/4 задания на учебной микросхеме…", flush=True)
        jobs = rec.record()
        print("4/4 журнал примера…", flush=True)
        core.update(rec.story())
        runs = rec.runs
    finally:
        rec.close()
    core["suggest"] = jobs.pop("suggest")
    for name, data in (("core", core), ("jobs", jobs), ("hood", hood)):
        (out / "demo" / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                                                   encoding="utf-8")
    build_page(out)
    sizes = ", ".join(f"{p.name} {p.stat().st_size // 1024} КБ" for p in sorted((out / "demo").glob("*.json")))
    print(f"готово: {out} — состояний {len(jobs['jobs'])}, прогонов ядра {runs}, {sizes}, "
          f"{time.time() - t0:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
