"""Учебная площадка: пошаговый тренажёр «что и как можно сделать тем, что есть».

Маршруты (ноутбук, ТВ, телефон, машина, связки…) — данные в playground.yaml, как вопросы Штурмана.
Один источник для всех экранов:
  * страница static/playground.html — на приборе (/playground.html), в демо на GitHub Pages
    и отдельным файлом (`python3 -m dirizher playground --export trainer.html`), который открывается без сети;
  * текстовый режим — мастер в терминале и `python3 -m dirizher playground [маршрут]`.

Тренажёр ничего не записывает и не трогает железо: только объясняет, спрашивает «что сделаете?»
и показывает протоколы настоящего ядра на учебной микросхеме.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

PLAYGROUND_PATH = Path(__file__).with_name("playground.yaml")
PAGE_PATH = Path(__file__).with_name("static") / "playground.html"
DATA_MARKER = "/*PLAYGROUND_DATA*/"
TRACK_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
TERM = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
BOLD = re.compile(r"\*\*(.+?)\*\*")
CODE = re.compile(r"`([^`]+)`")
# Сценарии, по которым тест сверяет протоколы «сеанса вживую» с настоящим ядром (tests/test_playground.py).
CONSOLE_CHECKS = ("healthy_new", "corrupt_ref", "flaky", "voltage", "dry", "blank")
TRACK_KEYS = {"id", "icon", "title", "tagline", "level", "minutes", "needs", "learn", "next", "links", "steps"}
STEP_KEYS = {"title", "text", "why", "dirizher", "code", "warn", "verify", "tick", "quiz", "console"}
TEXT_FIELDS = ("text", "why", "dirizher", "warn", "verify")


class PlaygroundError(ValueError):
    pass


# ======================= файл маршрутов =======================
def load(path: Path = PLAYGROUND_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise PlaygroundError(f"{Path(path).name}: YAML: {getattr(e, 'problem', None) or e}"
                              + (f" (строка {mark.line + 1})" if mark else "")) from None
    errors = validate(data)
    if errors:
        raise PlaygroundError(f"ошибки в {Path(path).name}:\n  " + "\n  ".join(errors))
    return data


def _str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _str_list(v) -> bool:
    return isinstance(v, list) and all(_str(x) for x in v)


def terms_in(text: str) -> list:
    return [m.group(1).strip() for m in TERM.finditer(text or "")]


def _texts(track: dict):
    """Все тексты маршрута, где могут встретиться [[термины]]: (где, текст)."""
    yield f"«{track.get('title')}»", str(track.get("tagline") or "")
    for item in (track.get("needs") or []) + (track.get("learn") or []):
        yield f"«{track.get('title')}»", str(item)
    for i, st in enumerate(track.get("steps") or [], 1):
        if not isinstance(st, dict):
            continue
        where = f"«{track.get('title')}», шаг {i}"
        for key in TEXT_FIELDS:
            yield where, str(st.get(key) or "")
        for item in st.get("tick") or []:
            yield where, str(item)
        quiz = st.get("quiz") or {}
        if isinstance(quiz, dict):
            yield where, str(quiz.get("ask") or "")
            for o in quiz.get("options") or []:
                if isinstance(o, dict):
                    yield where, str(o.get("text") or "")
                    yield where, str(o.get("say") or "")
        con = st.get("console") or {}
        if isinstance(con, dict):
            for case in con.get("cases") or []:
                if isinstance(case, dict):
                    yield where, str(case.get("result") or "")


def _check_quiz(q, where: str, errors: list) -> None:
    if not isinstance(q, dict) or not _str(q.get("ask")):
        errors.append(f"{where}: у вопроса нет ask")
        return
    opts = q.get("options")
    if not isinstance(opts, list) or len(opts) < 2:
        errors.append(f"{where}: у вопроса нужно не меньше двух вариантов")
        return
    for j, o in enumerate(opts, 1):
        if not isinstance(o, dict) or not _str(o.get("text")):
            errors.append(f"{where}: у варианта {j} нет text")
        elif "ok" in o and not isinstance(o["ok"], bool):
            errors.append(f"{where}: ok у варианта {j} — true или false")
        elif "say" in o and not _str(o["say"]):
            errors.append(f"{where}: say у варианта {j} пустой")
    if not any(isinstance(o, dict) and o.get("ok") is True for o in opts):
        errors.append(f"{where}: ни один вариант не отмечен ok: true")


def _check_console(con, where: str, errors: list) -> None:
    if not isinstance(con, dict) or not _str(con.get("ask")):
        errors.append(f"{where}: у сеанса вживую нет ask")
        return
    cases = con.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append(f"{where}: у сеанса вживую нет cases")
        return
    for j, case in enumerate(cases, 1):
        if not isinstance(case, dict) or not _str(case.get("label")):
            errors.append(f"{where}: у ситуации {j} нет label")
            continue
        if not _str_list(case.get("lines")) or not case["lines"]:
            errors.append(f"{where}: у ситуации «{case['label']}» нет строк (lines)")
        if not _str(case.get("result")):
            errors.append(f"{where}: у ситуации «{case['label']}» нет итога (result)")
        if case.get("check") is not None and case["check"] not in CONSOLE_CHECKS:
            errors.append(f"{where}: check — одно из {', '.join(CONSOLE_CHECKS)}")


def validate(d) -> list:
    """Проверить playground.yaml: поля, вопросы, ссылки между маршрутами, термины словарика."""
    if not isinstance(d, dict):
        return ["файл не содержит описания площадки"]
    errors = []
    if d.get("version") != 1:
        errors.append("version: 1")
    for key in ("title", "say"):
        if not _str(d.get(key)):
            errors.append(f"не заполнено «{key}»")
    gloss = d.get("glossary")
    if not isinstance(gloss, dict) or not gloss:
        errors.append("нет словарика (glossary)")
        gloss = {}
    for term, text in gloss.items():
        if not _str(str(term)) or not _str(text):
            errors.append(f"словарик: у «{term}» нет объяснения")
    tracks = d.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        errors.append("нет маршрутов (tracks)")
        return errors
    ids = []
    for i, t in enumerate(tracks, 1):
        if not isinstance(t, dict):
            errors.append(f"маршрут {i}: ожидался словарь")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not TRACK_ID.match(tid):
            errors.append(f"маршрут {i}: id — латиница, цифры и _ (например laptop)")
        elif tid in ids:
            errors.append(f"маршрут {i}: id «{tid}» уже занят")
        ids.append(tid)
        where = f"маршрут «{t.get('title') or tid}»"
        extra = set(t) - TRACK_KEYS
        if extra:
            errors.append(f"{where}: лишние поля {sorted(extra)}")
        for key in ("title", "tagline"):
            if not _str(t.get(key)):
                errors.append(f"{where}: не заполнено «{key}»")
        if not isinstance(t.get("level"), int) or isinstance(t.get("level"), bool) or not 0 <= t["level"] <= 5:
            errors.append(f"{where}: level — число от 0 до 5")
        if "minutes" in t and (not isinstance(t["minutes"], int) or t["minutes"] <= 0):
            errors.append(f"{where}: minutes — положительное число")
        for key in ("needs", "learn", "next"):
            if key in t and not (isinstance(t[key], list) and all(_str(x) for x in t[key])):
                errors.append(f"{where}: {key} — список строк")
        for link in t.get("links") or []:
            if not isinstance(link, dict) or not _str(link.get("title")) \
                    or not str(link.get("url", "")).startswith("https://"):
                errors.append(f"{where}: ссылка — title и url, начинающийся с https://")
        steps = t.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"{where}: нет шагов")
            continue
        for j, st in enumerate(steps, 1):
            sw = f"{where}, шаг {j}"
            if not isinstance(st, dict):
                errors.append(f"{sw}: ожидался словарь")
                continue
            extra = set(st) - STEP_KEYS
            if extra:
                errors.append(f"{sw}: лишние поля {sorted(extra)}")
            if not _str(st.get("title")):
                errors.append(f"{sw}: нет title")
            if not _str(st.get("text")):
                errors.append(f"{sw}: нет text")
            for key in ("why", "dirizher", "code", "warn", "verify"):
                if key in st and not _str(st[key]):
                    errors.append(f"{sw}: {key} — текст")
            if "tick" in st and (not _str_list(st["tick"]) or not st["tick"]):
                errors.append(f"{sw}: tick — список строк")
            if "quiz" in st:
                _check_quiz(st["quiz"], sw, errors)
            if "console" in st:
                _check_console(st["console"], sw, errors)
        for where_t, text in _texts(t):
            for term in terms_in(text):
                if term not in gloss:
                    errors.append(f"{where_t}: термина «{term}» нет в словарике")
    known = {i for i in ids if isinstance(i, str)}
    for t in tracks:
        if isinstance(t, dict):
            for n in t.get("next") or []:
                if n not in known:
                    errors.append(f"маршрут «{t.get('id')}»: next — нет маршрута «{n}»")
                elif n == t.get("id"):
                    errors.append(f"маршрут «{n}»: next не может вести на себя")
    return errors


def track(data: dict, tid: str):
    return next((t for t in data["tracks"] if t["id"] == tid), None)


def to_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def standalone(data: dict | None = None) -> str:
    """Страница тренажёра одним файлом: данные внутри, без сети и без прибора (для диска, почты, флешки)."""
    page = PAGE_PATH.read_text(encoding="utf-8")
    if page.count(DATA_MARKER) != 1:
        raise PlaygroundError("в playground.html не найдено место для данных")
    blob = to_json(data or load()).replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return page.replace(DATA_MARKER, f"window.PLAYGROUND = {blob}; window.PLAYGROUND_STANDALONE = true;", 1)


# ======================= текстовый режим =======================
def plain(text: str) -> str:
    """Убрать разметку для терминала: [[термин|как]] → как, **жирный** → жирный, `код` → код."""
    text = TERM.sub(lambda m: (m.group(2) or m.group(1)).strip(), text or "")
    return CODE.sub(r"\1", BOLD.sub(r"\1", text))


def _say_lines(ui, text: str, indent: str = "   ") -> None:
    for line in plain(text).rstrip().splitlines():
        ui.out(indent + line if line.strip() else "")


def console_line(line: str) -> str:
    """Строка «сеанса вживую» как на экране: без пометки «~» (пример, а не запись ядра)."""
    return line[1:] if line.startswith("~") else line


def run_text(ui, data: dict | None = None, track_id: str | None = None) -> None:
    """Тренажёр в терминале: выбор маршрута → шаги → вопросы с объяснением ответа."""
    data = data or load()
    while True:
        if track_id is None:
            ui.header(data["title"])
            _say_lines(ui, data["say"])
            idx = ui.choose("Маршрут (Enter — назад)",
                            [f"{t['title']} — {t['tagline']}" for t in data["tracks"]])
            if idx is None:
                return
            tr = data["tracks"][idx]
        else:
            tr = track(data, track_id)
            if tr is None:
                ui.warn(f"маршрута «{track_id}» нет")
                return
        _text_track(ui, data, tr)
        if track_id is not None:
            return


def _text_track(ui, data: dict, tr: dict) -> None:
    gloss, seen = data["glossary"], set()
    steps = tr["steps"]
    ui.header(f"{tr['title']} · уровень {tr['level']}")
    _say_lines(ui, tr["tagline"])
    if tr.get("needs"):
        ui.info("Нужно: " + "; ".join(plain(x) for x in tr["needs"]))
    if tr.get("learn"):
        ui.info("Научитесь: " + "; ".join(plain(x) for x in tr["learn"]))
    for i, st in enumerate(steps, 1):
        ui.out()
        ui.out(f" Шаг {i} из {len(steps)}. {plain(st['title'])}")
        _say_lines(ui, st["text"])
        if st.get("why"):
            ui.info("Зачем: " + plain(st["why"]))
        if st.get("code"):
            for line in st["code"].rstrip().splitlines():
                ui.out("      " + line)
        if st.get("dirizher"):
            ui.info("В Дирижёре: " + plain(st["dirizher"]))
        if st.get("warn"):
            ui.warn("Осторожно: " + plain(st["warn"]))
        if st.get("verify"):
            ui.warn("ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: " + plain(st["verify"]))
        for item in st.get("tick") or []:
            ui.out("   ☐ " + plain(item))
        terms = []
        for key in TEXT_FIELDS:
            terms += [t for t in terms_in(st.get(key) or "") if t not in seen and t not in terms]
        for t in terms:
            seen.add(t)
            ui.info(f"· {t} — {gloss[t]}")
        con = st.get("console")
        if con:
            idx = ui.choose(plain(con["ask"]) + " (Enter — пропустить)", [plain(c["label"]) for c in con["cases"]])
            if idx is not None:
                case = con["cases"][idx]
                for line in case["lines"]:
                    ui.out("      " + console_line(line))
                ui.ok("Итог: " + plain(case["result"]))
        quiz = st.get("quiz")
        if quiz:
            opts = quiz["options"]
            idx = ui.choose(plain(quiz["ask"]) + " (Enter — пропустить)", [plain(o["text"]) for o in opts])
            if idx is not None:
                o = opts[idx]
                (ui.ok if o.get("ok") else ui.warn)(("Верно. " if o.get("ok") else "Не совсем. ") + plain(o.get("say") or ""))
                if not o.get("ok"):
                    right = [plain(x["text"]) for x in opts if x.get("ok")]
                    ui.info("Правильно: " + "; ".join(right))
        if i < len(steps):
            if ui.choose("Дальше?", [f"Шаг {i + 1}: {plain(steps[i]['title'])}"]) is None:
                return
    ui.out()
    ui.ok(f"Маршрут «{tr['title']}» пройден.")
    nxt = [track(data, n) for n in tr.get("next") or []]
    if nxt:
        ui.info("Дальше: " + ", ".join(t["title"] for t in nxt if t))
