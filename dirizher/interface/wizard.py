"""Текстовый мастер: тот же Штурман, но в терминале.

Для SSH, консоли через переходник USB-UART, маленьких экранов без графики
и всего, где нет браузера. Вопросы, подсказки и уроки — те же, что в браузере
(dirizher/interface/guide.yaml).

Запуск:   python3 -m dirizher wizard      или просто   python3 -m dirizher
Ответ — номер варианта. Enter — назад, на первом экране — выход.
"""
from __future__ import annotations

from pathlib import Path

from ..core.pipeline import Conductor
from ..core.ui import ConsoleUI
from . import builder, guide

MARK = {"important": "★ ", "tip": "• ", "info": "· "}


def _input_text(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


class Wizard:
    def __init__(self, cfg: dict, sim: bool = False, ui=None, ask_text=None, guide_data=None):
        self.cfg = cfg
        self.sim = sim
        self.ui = ui or ConsoleUI()
        self.ask_text = ask_text or _input_text
        self.g = guide_data or guide.load_guide()

    # ---------- главный цикл ----------
    def run(self) -> int:
        ui, start = self.ui, self.g["start"]
        node, stack = start, []
        ui.header("Дирижёр — Штурман")
        ui.info("Отвечайте номером варианта. Enter — назад, на первом экране — выход.")
        while True:
            n = self.g["nodes"][node]
            if n.get("mode") == "sim" and not self.sim:
                self.sim = True
                ui.warn("включён учебный режим: виртуальная микросхема, железо не используется")
            items = []
            if node == start:
                items += self.suggestions()
            items += n["options"]
            if node == start:
                items.append({"label": "Режим: " + ("учебный → переключить на железо" if self.sim
                                                    else "железо → переключить на учебный"), "_toggle": True})
            ui.out()
            if n.get("say"):
                ui.info(n["say"])
            mode = "учебный режим" if self.sim else "железо"
            idx = ui.choose(f"{n['ask']}  [{mode}]", [self.label(o) for o in items])
            if idx is None:
                if stack:
                    node = stack.pop()
                    continue
                ui.info("до встречи!")
                return 0
            pending = items[idx]
            if pending.get("_toggle"):
                self.sim = not self.sim
                continue
            while pending:
                if "next" in pending:
                    if pending["next"] != node:
                        stack.append(node)
                    node = pending["next"]
                    if node == start:
                        stack.clear()
                    break
                if "lesson" in pending:
                    pending = self.lesson(pending["lesson"])
                else:
                    pending = self.action(pending["action"], pending.get("params") or {})

    @staticmethod
    def label(o: dict) -> str:
        text = MARK.get(o.get("level"), "") + o["label"]
        if o.get("explain"):
            text += "\n        " + o["explain"]
        return text

    def suggestions(self) -> list:
        try:
            with guide.open_readonly(self.cfg, self.sim) as c:
                return guide.suggest(c)
        except Exception as e:                      # подсказки не должны мешать работе
            self.ui.warn(f"подсказки недоступны: {e}")
            return []

    def then(self, options: list, question: str = "Что дальше?"):
        options = [o for o in options if o]
        if not options:
            return None
        idx = self.ui.choose(question, [self.label(o) for o in options])
        return None if idx is None else options[idx]

    # ---------- уроки ----------
    def lesson(self, lid: str):
        ui, les = self.ui, self.g["lessons"][lid]
        ui.header(les["title"])
        if les.get("why"):
            ui.info(les["why"])
        for i, st in enumerate(les["steps"], 1):
            ui.out()
            ui.out(f" {i}. {st['text']}")
            for line in (st.get("code") or "").rstrip().splitlines():
                ui.out(f"      {line}")
        if les.get("check"):
            ui.out()
            ui.ok("Как проверить: " + les["check"])
        related = les.get("related") or []
        if not related:
            ui.pause("Enter — назад…")
            return None
        return self.then(related, "Дальше (Enter — назад):")

    # ---------- действия ----------
    def action(self, name: str, params: dict):
        ui, params = self.ui, dict(params)
        if name == "history":
            return self.history()
        if name == "show":
            return self.show(int(params["session"]))
        if name in ("plugins", "profiles"):
            return self.device()
        if name == "roadmap":
            return self.roadmap()
        if name == "builder":
            return self.builder(params.get("draft"))
        meta = guide.ACTIONS[name]
        if meta.get("pick") and "session" not in params:
            sid = self.pick(meta["pick"])
            if sid is None:
                return None
            params["session"] = sid
        if name == "export":
            return self.export(int(params["session"]), params.get("kind"))
        if params.get("label") == "ask":
            params["label"] = self.ask_text("   Метка (например, роутер-X1-rev2; Enter — без метки): ") or None
        if params.get("image") == "ask":
            path = self.ask_text("   Путь к файлу полного образа (Enter — отмена): ")
            if not path:
                return None
            params["image"] = str(Path(path).expanduser())
        sim = True if name == "sim" else self.sim
        dry_run = bool(params.get("dry_run"))
        c = Conductor(self.cfg, ui, sim=sim, dry_run=dry_run)
        sid = outcome = None
        try:
            sid = guide.execute(c, name, params)
            outcome = guide.outcome_of(c, sid)
        except KeyboardInterrupt:
            c.interrupted()
            ui.warn("прервано. Все сделанные копии сохранены.")
            sid, outcome = c.session, "interrupted"
        except ValueError as e:
            ui.error(str(e))
            return None
        finally:
            c.close()
        return self.then(guide.after(name, params, outcome, sid, sim))

    def pick(self, purpose: str):
        with guide.open_readonly(self.cfg, self.sim) as c:
            rows = guide.pickable(c, purpose)
        if not rows:
            self.ui.warn("подходящих сеансов нет" + (" (учебный режим)" if self.sim else ""))
            return None
        idx = self.ui.choose("Какой сеанс? (Enter — отмена)", [r["text"] for r in rows])
        return None if idx is None else rows[idx]["id"]

    def history(self):
        with guide.open_readonly(self.cfg, self.sim) as c:
            rows = guide.history_rows(c, 20)
        if not rows:
            self.ui.info("журнал пока пуст")
            return None
        idx = self.ui.choose("Журнал (номер — подробности, Enter — назад)", [r["text"] for r in rows])
        return None if idx is None else self.show(rows[idx]["id"])

    def show(self, sid: int):
        ui = self.ui
        with guide.open_readonly(self.cfg, self.sim) as c:
            d = guide.session_detail(c, sid)
        if d is None:
            ui.error(f"сеанса №{sid} нет")
            return None
        s = d["session"]
        ui.header(f"Сеанс №{sid} — {s['kind_ru']}: {s['outcome_text']}")
        for sn in d["snapshots"]:
            ui.info(f"{sn['kind_ru']}: {sn['sha'][:16]}… {sn['size']} байт, "
                    f"подтверждён: {'да' if sn['verified'] else 'нет'}")
        for e in d["events"][-40:]:
            ui.out(f"   {e['ts']} [{e['level']}] {e['message']}")
        actions = d["actions"] + ([{"label": "Сохранить снимок в файл", "action": "export",
                                    "params": {"session": sid}}] if d["snapshots"] else [])
        return self.then(actions, "Действия (Enter — назад):")

    def export(self, sid: int, kind: str | None = None):
        with guide.open_readonly(self.cfg, self.sim) as c:
            kinds = [sn["kind"] for sn in c.memory.snapshots(sid)]
        kinds = list(dict.fromkeys(kinds))
        if not kinds:
            self.ui.warn("в этом сеансе нет снимков")
            return None
        if kind not in kinds:
            idx = self.ui.choose("Какой снимок?", [guide.SNAP_RU.get(k, k) for k in kinds])
            if idx is None:
                return None
            kind = kinds[idx]
        dest = self.ask_text(f"   Куда сохранить (например, ~/dump_s{sid}_{kind}.bin; Enter — отмена): ")
        if not dest:
            return None
        c = Conductor(self.cfg, self.ui, sim=self.sim)
        try:
            c.export(sid, kind, dest)
        finally:
            c.close()
        return None

    def device(self):
        ui = self.ui
        with guide.open_readonly(self.cfg, self.sim) as c:
            info = guide.device_info(c)
        ui.header("Прибор" + (" — учебный режим" if self.sim else ""))
        for p in info["plugins"]:
            ui.out(f" {'✔' if p['active'] else '·'} {p['name']}: {p['title']} — {p['status']}")
        ui.out()
        for p in info["profiles"]:
            volt = f"{p['voltage']} В" if p["voltage"] else "напряжение не указано"
            ui.out(f" • {p['id']} [{p['kind']}] {p['name']} — {volt}")
        for e in info["profile_errors"]:
            ui.warn(f"{e['file']}: {e['error']}")
        s = info["safety"]
        ui.out()
        ui.info(f"адаптер: {s['programmer_voltage']} В; слово подтверждения: {s['confirm_word']}; "
                f"зеркало копий: {info['mirror_dir'] or 'выключено'}")
        ui.pause("Enter — назад…")
        return None

    def roadmap(self):
        items = guide.roadmap(self.cfg)
        if not items:
            self.ui.info("docs/STATUS.md не найден или в нём нет раздела «Следующие шаги»")
            return None
        self.ui.header("Что делать дальше по плану (docs/STATUS.md)")
        for i, item in enumerate(items, 1):
            self.ui.out(f" {i}. {item}")
        return self.then([{"label": "Сессия разработки с Claude — как вести", "lesson": "dev_session"},
                          {"label": "Как устроен Дирижёр", "lesson": "dev_architecture"}],
                         "Дальше (Enter — назад):")

    # ---------- конструктор профиля ----------
    def builder(self, draft: str | None = None):
        ui = self.ui
        fields = {}
        base = Path(self.cfg["data_dir"])
        if draft:
            for folder in (base / "drafts", base / "simulator" / "drafts"):
                if (folder / Path(draft).name).is_file():
                    fields = builder.fields_from_file(folder / Path(draft).name)
                    break
        ui.header("Конструктор профиля чипа")
        ui.info("Заполните по даташиту. Enter — оставить как есть.")
        with guide.open_readonly(self.cfg, False) as c:
            profiles = list(c.profiles.profiles)
        while True:
            for f in builder.FIELDS:
                cur = fields.get(f["name"], f.get("default", ""))
                if f.get("type") == "bool":
                    cur = "да" if builder._as_bool(cur, True) else "нет"
                ui.out()
                ui.out(f" {f['label']} — {f['help']}")
                shown = cur if cur not in (None, "") else f"пример: {f.get('example', '')}"
                value = self.ask_text(f"   [{shown}]: ")
                if value:
                    fields[f["name"]] = value
                elif f["name"] not in fields and "default" in f:
                    fields[f["name"]] = f["default"]
            res = builder.build(fields, profiles)
            ui.out()
            for line in res["yaml"].splitlines():
                ui.out("   " + line)
            for h in res["hints"]:
                ui.info("подсказка: " + h)
            for w in res["warnings"]:
                ui.warn(w)
            for e in res["errors"]:
                ui.error(e)
            options = ["Исправить поля"]
            if not res["errors"]:
                options += ["Сохранить черновик (data/drafts/)",
                            "Включить профиль — я сверил напряжение и JEDEC ID с даташитом"]
            idx = ui.choose("Что сделать? (Enter — выйти без сохранения)", options)
            if idx is None:
                return None
            if idx == 0:
                continue
            try:
                if idx == 1:
                    path = builder.save_draft(base / "drafts", fields, profiles)
                    ui.ok(f"черновик сохранён: {path}")
                else:
                    if ui.yes_no("Напряжение и JEDEC ID сверены с даташитом?") != "yes":
                        continue
                    path = builder.enable(Path(self.cfg["profiles_dir"]), fields, True, profiles)
                    ui.ok(f"профиль включён: {path}")
                    return self.then([{"label": "Проверить: узнать, что за микросхема", "action": "detect"}])
            except builder.BuilderError as e:
                ui.error(str(e))
                continue
            return None
