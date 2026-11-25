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
        if name == "tool":
            return self.tool(str(params.get("name") or ""))
        if name == "connect":
            return self.connect(params.get("card"))
        if name == "roadmap":
            return self.roadmap()
        if name == "builder":
            return self.builder(params.get("draft"))
        if name == "skills":
            return self.skills()
        if name == "help":
            return self.help(params.get("article"))
        if name == "hood":
            return self.hood()
        if name == "doctor":
            return self.doctor()
        if name == "tune_form":
            return self.tune_form(params.get("session"))
        if name == "fix":
            return self.fix(params.get("session"), params.get("fix"))
        if name == "catalog":
            return self.catalog(params.get("card"), params.get("kind"))
        if name == "playground":
            return self.playground(params.get("track"))
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
        hints = []
        try:
            sid = guide.execute(c, name, params)
            outcome = guide.outcome_of(c, sid)
            hints = c.learning.hints(sid) if sid else []
        except KeyboardInterrupt:
            c.interrupted()
            ui.warn("прервано. Все сделанные копии сохранены.")
            sid, outcome = c.session, "interrupted"
        except ValueError as e:
            ui.error(str(e))
            return None
        finally:
            c.close()
        return self.then(guide.after(name, params, outcome, sid, sim, hints))

    # ---------- мастерская ----------
    def skills(self):
        with guide.open_readonly(self.cfg, self.sim) as c:
            info = guide.skills_info(c)
        self.ui.header("Навыки")
        for sk in info["items"]:
            self.ui.out(f" {'✖' if sk['errors'] else '✔'} {sk['title']} ({sk['id']}, {sk['kind']}) — {sk['path']}")
            for pl in sk["plugins"]:
                self.ui.info(f"лезвие {pl['name']}: {pl['status']}")
            for e in sk["errors"]:
                self.ui.error(e)
        for b in info["broken"]:
            self.ui.error(f"{b['path']}: {b['error']}")
        self.ui.info("новый навык и правка кода — в браузере («Навыки» → «+», кнопка </>); "
                     "в терминале — редактором, затем python3 -m dirizher doctor")
        return self.then([{"label": "Справка: навыки", "action": "help", "params": {"article": "skills"}},
                          {"label": "Самопроверка", "action": "doctor"}], "Дальше (Enter — назад):")

    def help(self, article: str | None = None):
        from ..workshop import helpdocs
        if article:
            art = helpdocs.article(self.cfg, article)
            if art is None:
                self.ui.warn(f"статьи «{article}» нет")
                return None
            self.ui.header(art["title"])
            for line in art["text"].rstrip().splitlines():
                self.ui.out("   " + line)
            self.ui.pause("Enter — назад…")
            return None
        items = [i for i in helpdocs.search(helpdocs.articles(self.cfg, with_lessons=False, with_cards=False))]
        idx = self.ui.choose("Справка (Enter — назад)", [i["title"] for i in items])
        return None if idx is None else self.help(items[idx]["id"])

    def playground(self, track_id: str | None = None):
        """Учебная площадка в терминале: маршрут → шаги → вопросы с объяснением ответа."""
        from . import playground
        try:
            data = playground.load()
        except playground.PlaygroundError as e:
            self.ui.error(str(e))
            return None
        playground.run_text(self.ui, data, track_id)
        return None

    def catalog(self, card_id: str | None = None, kind: str | None = None):
        """Справочник в терминале: список → карточка с рисунками строками."""
        from ..workshop import catalog
        cards, errors = catalog.load(self.cfg)
        if card_id:
            card = catalog.get(cards, card_id)
            if card is None:
                self.ui.warn(f"карточки «{card_id}» нет")
                return None
            self.ui.header(f"{card['title']} · {card['kind_title'].lower()}")
            for line in catalog.text(card, cards):
                self.ui.out("   " + line if line else "")
            rel = catalog.related(cards, card)["items"]
            opts = [{"label": f"{r['title']} — {r['tagline']}", "action": "catalog", "params": {"card": r["id"]}}
                    for r in rel]
            if card.get("lesson"):
                lesson = (self.g.get("lessons") or {}).get(card["lesson"])
                if lesson:
                    opts.append({"label": f"Урок: {lesson['title']}", "lesson": card["lesson"]})
            return self.then(opts, "Дальше (Enter — назад):") if opts else self.ui.pause("Enter — назад…")
        shown = catalog.search(cards, "", kind or "")
        for path, err in errors:
            self.ui.error(f"{path}: {err}")
        idx = self.ui.choose("Справочник от простого к сложному, [ступень] (Enter — назад)",
                             [f"[{c['level']}] {c['title']} · {c['group_title'].lower()} — {c['tagline']}" for c in shown])
        return None if idx is None else self.catalog(shown[idx]["id"])

    def hood(self):
        self.ui.info("капот (правка кода с проверкой и историей) — в браузере: python3 -m dirizher web, кнопка </>")
        return self.help("hood")

    def doctor(self):
        from ..workshop import doctor
        result = doctor.main(self.cfg, sim=self.sim)
        issues = [i for i in doctor.check(self.cfg, self.sim) if i["heal"]]
        if issues and self.ui.yes_no(f"Исправить исправимое ({len(issues)})?") == "yes":
            doctor.main(self.cfg, sim=self.sim, do_heal=True)
        elif not issues and result == 0:
            self.ui.ok("исправлять нечего")
        return None

    def tune_form(self, sid=None):
        ui = self.ui
        with guide.open_readonly(self.cfg, self.sim) as c:
            view = guide.params_view(c, int(sid) if sid else None)
        if view is None:
            ui.warn("сначала прочитайте параметры")
            return {"label": "Прочитать параметры", "action": "params"}
        ui.header(f"{view['profile_name']} — по сеансу №{view['session']} ({view['ts']})")
        changes = {}
        for row in view["params"]:
            if not row["writable"]:
                ui.info(f"{row['title']}: {row['text']} (только чтение)")
                continue
            limits = ""
            if row["choices"]:
                limits = "; ".join(f"{k} — {v}" for k, v in row["choices"].items())
            elif row["min"] is not None or row["max"] is not None:
                limits = f"{row['min']} … {row['max']} {row['unit']}".strip()
            value = self.ask_text(f"   {row['title']} = {row['text']}" + (f" [{limits}]" if limits else "")
                                  + " — новое (Enter — оставить): ")
            if value:
                changes[row["id"]] = value
        if not changes:
            ui.info("ничего не меняем")
            return None
        return {"label": "Записать", "action": "tune", "params": {"set": changes}}

    def fix(self, sid, fix_id=None):
        if sid is None:
            return None
        with guide.open_readonly(self.cfg, self.sim) as c:
            if fix_id is not None:
                c.learning.confirm_fix(int(fix_id), int(sid))
                self.ui.ok("отмечено: подсказка помогла снова — она поднимется выше")
                return None
            text = self.ask_text(f"   Что помогло после сеанса №{sid}? (одна фраза, Enter — отмена): ")
            if not text:
                return None
            try:
                c.learning.remember_fix(int(sid), text)
            except ValueError as e:
                self.ui.error(str(e))
                return None
        self.ui.ok("записал — в следующий раз подскажу")
        return None

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
        tools = [p for p in info["plugins"] if p["title"] != "ошибка загрузки"]
        idx = ui.choose("Настроить инструмент? (Enter — назад)", [f"{p['name']} — {p['title']}" for p in tools])
        if idx is None or not 0 <= idx < len(tools):
            return None
        return self.tool(tools[idx]["name"])

    def connect(self, card_id=None):
        """Начать с переходника: выбрать карточку → проверки → «Настроить» / «Проверить подключение»."""
        from ..workshop import connect
        ui = self.ui
        with guide.open_readonly(self.cfg, False) as c:
            report, profiles = c.plugin_report, c.profiles
        try:
            v = connect.view(self.cfg, card_id, report, profiles=profiles, sim=self.sim)
        except ValueError as e:
            ui.error(str(e))
            return None
        if not card_id:
            ui.header("Подключить инструмент: какой переходник у вас в руках?")
            opts = [{"label": a["title"], "explain": a["tagline"], "action": "connect", "params": {"card": a["id"]}}
                    for a in v["adapters"]]
            return self.then(opts, "Выберите переходник (Enter — назад):")
        ui.header(f"Подключить: {v['card']['title']}")
        for line in connect.text(v):
            ui.out(line)
        return self.then(v["actions"], "Дальше (Enter — назад):")

    def tool(self, name: str):
        """Настройки лезвия поле за полем; сохранение — config.tools.yaml через капот, со словом."""
        from ..workshop import toolsettings
        from ..workshop.hood import Hood
        try:
            toolsettings.ask_fields(self.cfg, Hood(self.cfg), self.ui, self.ask_text, name)
        except toolsettings.ToolSettingsError as e:
            self.ui.error(str(e))
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
