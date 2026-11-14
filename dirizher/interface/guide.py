"""Штурман: спрашивает, чего хочет человек, и подсказывает следующий шаг.

Общий «мозг» для всех экранов — браузера (ПК, телефон, экран прибора) и текстового
мастера (терминал, SSH, консоль через USB-UART). Экраны только показывают то, что
даёт этот модуль, поэтому вопросы и подсказки везде одинаковые.

Штурман сам ничего не читает с микросхемы и не пишет на неё: он смотрит память
(журнал сеансов), черновики и настройки и предлагает действия. Выполняет их ядро —
Conductor, со всеми копиями, сверками и подтверждением словом.

Вопросы и уроки лежат в guide.yaml — их можно дополнять без программирования.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from pathlib import Path

import yaml

from ..core.params import read_all as param_rows
from ..core.pipeline import OUTCOMES, Conductor
from ..core.ui import ConsoleUI

GUIDE_PATH = Path(__file__).with_name("guide.yaml")

SIM_ACTIONS = ("status", "reset", "corrupt", "erase", "flaky", "steady")
SNAPSHOT_KINDS = ("before", "after", "written")
KIND_RU = {"run": "ремонт", "detect": "опознание", "rollback": "откат", "capture": "замер",
           "params": "параметры", "tune": "настройка"}
HOOD_AREAS = ("guide", "work", "log", "dev", "device", "help")
SET_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SNAP_RU = {"before": "копия «до»", "after": "копия «после»", "written": "что записывали"}

# Действия, которые Штурман умеет предлагать.
#   job    — выполняется ядром (Conductor) и может задавать вопросы;
#   params — допустимые параметры;
#   pick   — сеанс выбирается из журнала (какие сеансы подходят — см. pickable()).
ACTIONS = {
    "run": {"job": True, "params": {"dry_run", "label", "image"}, "title": "Ремонт"},
    "detect": {"job": True, "params": set(), "title": "Опознание"},
    "rollback": {"job": True, "params": {"dry_run", "session"}, "pick": "rollback", "title": "Откат"},
    "feedback": {"job": True, "params": {"session", "ok"}, "pick": "feedback", "title": "Отзыв"},
    "reference": {"job": True, "params": {"session", "kind", "label"}, "pick": "reference",
                  "title": "Эталон из сеанса"},
    "sim": {"job": True, "params": {"do"}, "title": "Учебная микросхема"},
    "capture": {"job": True, "params": {"scan"}, "title": "Замер сигналов"},
    "export": {"job": False, "params": {"session", "kind"}, "pick": "export", "title": "Дамп файлом"},
    "show": {"job": False, "params": {"session"}, "title": "Сеанс"},
    "history": {"job": False, "params": set(), "title": "Журнал"},
    "plugins": {"job": False, "params": set(), "title": "Инструменты"},
    "profiles": {"job": False, "params": set(), "title": "Профили"},
    "roadmap": {"job": False, "params": set(), "title": "План развития"},
    "builder": {"job": False, "params": {"draft"}, "title": "Конструктор профиля"},
    # 0.4.0 — мастерская
    "params": {"job": True, "params": set(), "title": "Параметры прибора"},
    "tune": {"job": True, "params": {"set", "dry_run"}, "title": "Настройка параметров"},
    "tune_form": {"job": False, "params": {"session"}, "title": "Изменить параметры"},
    "skills": {"job": False, "params": set(), "title": "Навыки"},
    "help": {"job": False, "params": {"article"}, "title": "Справка"},
    "hood": {"job": False, "params": {"area"}, "title": "Капот"},
    "doctor": {"job": False, "params": set(), "title": "Самопроверка"},
    "fix": {"job": False, "params": {"session", "fix"}, "title": "Что помогло"},
}
PICK_PURPOSES = ("rollback", "feedback", "reference", "export")
LESSON_GROUPS = ("work", "dev")
LEVELS = {"important": 0, "tip": 1, "info": 2}
FAIL_OUTCOMES = ("not_detected", "read_failed", "blocked", "blocked_voltage", "no_action", "write_failed",
                 "not_repaired", "rollback_failed", "tune_failed", "capture_failed")


class GuideError(ValueError):
    pass


# ======================= файл вопросов и уроков =======================
def load_guide(path: Path = GUIDE_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        g = yaml.safe_load(f) or {}
    errors = validate(g)
    if errors:
        raise GuideError(f"ошибки в {Path(path).name}:\n  " + "\n  ".join(errors))
    for lesson in g["lessons"].values():
        lesson.setdefault("group", "work")
        lesson["steps"] = [s if isinstance(s, dict) else {"text": str(s)} for s in lesson["steps"]]
    return g


def _check_option(opt, where: str, g: dict, errors: list) -> None:
    if not isinstance(opt, dict) or not opt.get("label"):
        errors.append(f"{where}: у варианта нет label")
        return
    targets = [k for k in ("next", "action", "lesson") if k in opt]
    if len(targets) != 1:
        errors.append(f"{where} «{opt['label']}»: нужен ровно один из next / action / lesson")
        return
    if "next" in opt and opt["next"] not in g.get("nodes", {}):
        errors.append(f"{where} «{opt['label']}»: нет вопроса «{opt['next']}»")
    if "lesson" in opt and opt["lesson"] not in g.get("lessons", {}):
        errors.append(f"{where} «{opt['label']}»: нет урока «{opt['lesson']}»")
    if "action" in opt:
        meta = ACTIONS.get(opt["action"])
        if meta is None:
            errors.append(f"{where} «{opt['label']}»: неизвестное действие «{opt['action']}»")
            return
        params = opt.get("params") or {}
        if not isinstance(params, dict):
            errors.append(f"{where} «{opt['label']}»: params должен быть словарём")
            return
        extra = set(params) - meta["params"]
        if extra:
            errors.append(f"{where} «{opt['label']}»: лишние параметры {sorted(extra)}")
        if "dry_run" in params and not isinstance(params["dry_run"], bool):
            errors.append(f"{where} «{opt['label']}»: dry_run — true или false")
        if params.get("image") not in (None, "ask"):
            errors.append(f"{where} «{opt['label']}»: image в Штурмане может быть только ask")
        if opt["action"] == "sim" and params.get("do") not in SIM_ACTIONS:
            errors.append(f"{where} «{opt['label']}»: do — одно из {', '.join(SIM_ACTIONS)}")
        if "article" in params and not isinstance(params["article"], str):
            errors.append(f"{where} «{opt['label']}»: article — имя статьи справки")
        if params.get("area") not in (None, *HOOD_AREAS):
            errors.append(f"{where} «{opt['label']}»: area — одно из {', '.join(HOOD_AREAS)}")
        if "set" in params and not isinstance(params["set"], dict):
            errors.append(f"{where} «{opt['label']}»: set — словарь «параметр: значение»")


def validate(g) -> list:
    """Проверить guide.yaml: ссылки, действия, параметры, достижимость вопросов."""
    errors = []
    if not isinstance(g, dict):
        return ["файл пуст или испорчен"]
    nodes, lessons = g.get("nodes"), g.get("lessons")
    if not isinstance(nodes, dict) or not nodes:
        return ["нет раздела nodes"]
    if not isinstance(lessons, dict):
        return ["нет раздела lessons"]
    if g.get("start") not in nodes:
        errors.append(f"start указывает на несуществующий вопрос «{g.get('start')}»")
    for nid, node in nodes.items():
        if not isinstance(node, dict) or not node.get("ask"):
            errors.append(f"вопрос {nid}: нет ask")
            continue
        if node.get("mode") not in (None, "sim"):
            errors.append(f"вопрос {nid}: mode может быть только sim")
        options = node.get("options")
        if not isinstance(options, list) or not options:
            errors.append(f"вопрос {nid}: нет вариантов")
            continue
        for opt in options:
            _check_option(opt, f"вопрос {nid}", g, errors)
    for lid, lesson in lessons.items():
        if not isinstance(lesson, dict) or not lesson.get("title"):
            errors.append(f"урок {lid}: нет title")
            continue
        if lesson.get("group", "work") not in LESSON_GROUPS:
            errors.append(f"урок {lid}: group — work или dev")
        steps = lesson.get("steps")
        if not isinstance(steps, list) or not steps:
            errors.append(f"урок {lid}: нет шагов")
        else:
            for i, st in enumerate(steps, 1):
                if isinstance(st, str):
                    continue
                if not isinstance(st, dict) or not isinstance(st.get("text"), str):
                    errors.append(f"урок {lid}, шаг {i}: нужен text")
                elif "code" in st and not isinstance(st["code"], str):
                    errors.append(f"урок {lid}, шаг {i}: code должен быть текстом")
        for opt in lesson.get("related") or []:
            _check_option(opt, f"урок {lid}", g, errors)
    # все вопросы должны быть достижимы с первого экрана
    if g.get("start") in nodes:
        seen, todo = set(), [g["start"]]
        sources = {lid: les.get("related") or [] for lid, les in lessons.items() if isinstance(les, dict)}
        while todo:
            nid = todo.pop()
            if nid in seen or nid not in nodes:
                continue
            seen.add(nid)
            opts = list(nodes[nid].get("options") or [])
            for opt in list(opts):
                if isinstance(opt, dict) and opt.get("lesson") in sources:
                    opts += sources[opt["lesson"]]
            todo += [o["next"] for o in opts if isinstance(o, dict) and "next" in o]
        for nid in nodes:
            if nid not in seen:
                errors.append(f"вопрос {nid} недостижим с первого экрана")
    return errors


# ======================= доступ к ядру =======================
class QuietUI(ConsoleUI):
    """Для справочных запросов: ничего не печатает, на вопросы отвечает безопасно."""

    def __init__(self):
        super().__init__(color=False)

    def out(self, text: str = "") -> None:
        pass

    def pause(self, text: str) -> None:
        pass

    def yes_no(self, question: str, allow_later: bool = False) -> str:
        return "later" if allow_later else "no"

    def choose(self, question: str, options: list):
        return None

    def confirm_word(self, text: str, word: str) -> bool:
        return False


@contextmanager
def open_readonly(cfg: dict, sim: bool):
    """Ядро для справок (журнал, подсказки). Железо не опрашивается: только available()."""
    c = Conductor(cfg, QuietUI(), sim=sim)
    try:
        yield c
    finally:
        c.close()


def execute(c, action: str, params: dict | None = None):
    """Выполнить действие ядром. Возвращает номер сеанса (или None)."""
    p = params or {}
    if action == "run":
        return c.run(image_path=p.get("image") or None, label=p.get("label") or None)
    if action == "detect":
        return c.detect_only()
    if action == "rollback":
        return c.rollback(int(p["session"]))
    if action == "feedback":
        sid = int(p["session"])
        ok = p.get("ok")
        if ok is None:
            answer = c.ui.yes_no(f"Сеанс №{sid}: устройство заработало?", allow_later=True)
            if answer == "later":
                c.ui.info("хорошо, спрошу позже")
                return sid
            ok = answer == "yes"
        c.feedback(sid, bool(ok))
        return sid
    if action == "reference":
        sid = int(p["session"])
        c.mark_reference(sid, p.get("kind") or None, p.get("label") or None)
        return sid
    if action == "sim":
        if not c.sim:
            raise ValueError("учебные поломки работают только в учебном режиме")
        do = p.get("do")
        if do not in SIM_ACTIONS:
            raise ValueError(f"неизвестная учебная команда «{do}»")
        c.ui.ok(getattr(c.plugin("simulator"), f"do_{do}")())
        return None
    if action == "params":
        return c.read_params()
    if action == "tune":
        return c.tune(clean_set(p.get("set")))
    if action == "capture":
        # Логический анализатор только принимает сигналы — в устройство ничего не пишется.
        if c.sim:
            raise ValueError("замер работает только с настоящим логическим анализатором — выключите учебный режим")
        from ..extras import sigrok
        return sigrok.scan(c) if p.get("scan") else sigrok.capture(c, c.cfg)
    raise ValueError(f"действие «{action}» не выполняется ядром")


def clean_set(raw) -> dict:
    """Новые значения параметров от человека: {id: число или текст}, не больше 64."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError("не задано ни одного нового значения")
    if len(raw) > 64:
        raise ValueError("слишком много параметров за раз")
    out = {}
    for k, v in raw.items():
        if not SET_KEY.match(str(k)):
            raise ValueError(f"недопустимое имя параметра «{k}»")
        if isinstance(v, bool) or not isinstance(v, (int, float, str)) or len(str(v)) > 32:
            raise ValueError(f"«{k}»: значение — число")
        out[str(k)] = v
    return out


def outcome_of(c, sid) -> str | None:
    if sid is None:
        return None
    s = c.memory.session(sid)
    return s["outcome"] if s is not None else None


def job_title(action: str, params: dict, dry_run: bool = False) -> str:
    p = params or {}
    if action == "run":
        title = "План ремонта (без записи)" if dry_run else "Ремонт"
        if p.get("label"):
            title += f" · «{p['label']}»"
        return title
    if action == "sim":
        return f"Учебная микросхема: {p.get('do', '')}"
    if action == "capture":
        return "Поиск анализатора" if p.get("scan") else "Замер сигналов"
    if action == "tune":
        return "Настройка параметров" + (" (без записи)" if dry_run else "")
    title = ACTIONS.get(action, {}).get("title", action)
    if p.get("session"):
        title += f" · сеанс №{p['session']}"
    if dry_run and action == "rollback":
        title += " (проверка условий)"
    return title


# ======================= журнал =======================
def _fmt_time(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def describe(s) -> str:
    outcome = OUTCOMES.get(s["outcome"], s["outcome"] or "не завершён")
    prof = s["profile_id"] or "—"
    return f"№{s['id']} · {_fmt_time(s['started'])} · {KIND_RU.get(s['kind'], s['kind'])} · {prof} · {outcome}"


def _snapshots(c, sid: int) -> dict:
    return {sn["kind"]: sn for sn in c.memory.snapshots(sid)}


def history_rows(c, n: int = 30) -> list:
    rows = []
    for s in c.memory.sessions(n):
        rows.append({"id": s["id"], "started": _fmt_time(s["started"]), "kind": s["kind"],
                     "kind_ru": KIND_RU.get(s["kind"], s["kind"]), "profile": s["profile_id"],
                     "recipe": s["recipe_id"], "outcome": s["outcome"],
                     "outcome_text": OUTCOMES.get(s["outcome"], s["outcome"] or "не завершён"),
                     "text": describe(s)})
    return rows


def pickable(c, purpose: str, limit: int = 30) -> list:
    """Сеансы, подходящие для действия: откат, отзыв, эталон, выгрузка."""
    if purpose not in PICK_PURPOSES:
        raise ValueError(f"неизвестная цель выбора «{purpose}»")
    found, pending = [], []
    for s in c.memory.sessions(300):
        snaps = _snapshots(c, s["id"])
        if purpose == "rollback":
            ok = "before" in snaps and s["kind"] in ("run", "rollback", "tune")
        elif purpose == "feedback":
            ok = bool(s["recipe_id"] and s["profile_id"])
        elif purpose == "reference":
            ok = bool(s["profile_id"]) and any(sn["verified"] for k, sn in snaps.items() if k != "written")
        else:
            ok = bool(snaps)
        if not ok:
            continue
        item = {"id": s["id"], "text": describe(s), "outcome": s["outcome"], "snapshots": sorted(snaps)}
        (pending if purpose == "feedback" and s["outcome"] == "written_verified" else found).append(item)
    return (pending + found)[:limit]


def session_detail(c, sid: int) -> dict | None:
    s = c.memory.session(sid)
    if s is None:
        return None
    info = {k: s[k] for k in s.keys()}
    info["kind_ru"] = KIND_RU.get(s["kind"], s["kind"])
    info["outcome_text"] = OUTCOMES.get(s["outcome"], s["outcome"] or "не завершён")
    snaps = [{"kind": sn["kind"], "kind_ru": SNAP_RU.get(sn["kind"], sn["kind"]), "sha": sn["sha"],
              "size": sn["size"], "verified": bool(sn["verified"]), "ts": _fmt_time(sn["ts"])}
             for sn in c.memory.snapshots(sid)]
    events = [{"ts": e["ts"][11:19], "level": e["level"], "message": e["message"]} for e in c.memory.events(sid)]
    actions = []
    kinds = {sn["kind"] for sn in snaps}
    if s["recipe_id"] and s["profile_id"]:
        actions.append({"label": "Сообщить, помог ли ремонт", "action": "feedback", "params": {"session": sid}})
    if "before" in kinds and s["kind"] in ("run", "rollback", "tune"):
        actions.append({"label": "Вернуть копию «до» на чип", "action": "rollback", "params": {"session": sid}})
    prof = c.profiles.get(s["profile_id"]) if s["profile_id"] else None
    if prof is not None and prof.params and kinds & {"before", "after"}:
        actions.append({"label": "Изменить параметры", "action": "tune_form", "params": {"session": sid}})
    if c.learning.signature(sid):
        actions.append({"label": "Записать, что помогло", "action": "fix", "params": {"session": sid}})
    if s["profile_id"] and any(sn["verified"] for sn in snaps if sn["kind"] != "written"):
        actions.append({"label": "Сделать эталоном", "action": "reference",
                        "params": {"session": sid, "label": "ask"}})
    return {"session": info, "snapshots": snaps, "events": events, "actions": actions}


def params_view(c, sid: int | None = None) -> dict | None:
    """Значения параметров из последнего снимка сеанса (или самого свежего подходящего сеанса)."""
    for s in c.memory.sessions(300):
        if sid is not None and s["id"] != sid:
            continue
        prof = c.profiles.get(s["profile_id"]) if s["profile_id"] else None
        if prof is None or not prof.params:
            if sid is not None:
                return None
            continue
        after = c.memory.snapshot(s["id"], "after")
        snap = after if after is not None and after["verified"] else c.memory.snapshot(s["id"], "before")
        if snap is None:
            if sid is not None:
                return None
            continue
        return {"session": s["id"], "profile": prof.id, "profile_name": prof.name, "ts": _fmt_time(snap["ts"]),
                "verified": bool(snap["verified"]), "snapshot": snap["kind"],
                "params": param_rows(c.store.get(snap["sha"]), prof.params)}
    return None


def skills_info(c) -> dict:
    """Навыки для вкладки «Навыки»: что загружено, что сломано, что даёт каждый."""
    from ..core.registry import discover
    found, _ = discover(c.skills.active)
    items = []
    for sk in c.skills.skills:
        plugins = [r for r in c.plugin_report if r.get("skill") == sk.id]
        errors = list(sk.errors) + [r["status"] for r in c.plugin_report
                                    if r["title"] == "ошибка загрузки" and r["name"] == f"навык {sk.id}"]
        root = Path(c.cfg.get("_path") or "config.yaml").resolve().parent
        try:
            rel = sk.path.resolve().relative_to(root).as_posix()
        except ValueError:
            rel = str(sk.path)
        items.append({"id": sk.id, "title": sk.title, "kind": sk.kind, "summary": sk.summary,
                      "enabled": sk.enabled, "path": rel, "plugins": plugins, "errors": errors,
                      "warnings": list(dict.fromkeys(sk.warnings)),
                      "profiles": [p.id for p in c.profiles.profiles
                                   if Path(p.path).resolve().is_relative_to(sk.path.resolve())],
                      "help": f"skill.{sk.id}" if (sk.path / "help.md").is_file() else None})
    broken = [{"path": Path(folder).name, "error": err} for folder, err in c.skills.errors]
    classes = sorted({cls.target_class for cls, _ in found.values() if cls.hardware})
    return {"items": items, "broken": broken, "classes": classes}


# ======================= прибор =======================
def device_info(c) -> dict:
    profiles = []
    for p in sorted(c.profiles.profiles, key=lambda p: (p.target_class, -p.priority, p.id)):
        kind = "плата" if p.content_match else ("общий" if p.is_generic else "чип")
        profiles.append({"id": p.id, "name": p.name, "kind": kind, "class": p.target_class,
                         "voltage": p.voltage, "size": p.size, "allow_write": p.allow_write})
    safety = c.cfg["safety"]
    return {
        "sim": c.sim,
        "plugins": c.plugin_report,
        "profiles": profiles,
        "profile_errors": [{"file": Path(path).name, "error": err} for path, err in c.profiles.errors],
        "safety": {"programmer_voltage": safety.get("programmer_voltage"),
                   "confirm_word": safety.get("confirm_word"), "read_passes": safety.get("read_passes"),
                   "confidence_threshold": safety.get("confidence_threshold")},
        "data_dir": str(c.data_dir),
        "mirror_dir": str(c.cfg["mirror_dir"]) if c.cfg.get("mirror_dir") else None,
    }


def roadmap(cfg: dict) -> list:
    """Следующие шаги из docs/STATUS.md (раздел «Следующие шаги»)."""
    path = Path(cfg.get("_path") or "config.yaml").parent / "docs" / "STATUS.md"
    if not path.is_file():
        return []
    items, inside = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower().startswith("следующие шаги")
            continue
        if inside:
            m = re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.*)$", line)
            if m:
                items.append(m.group(1).strip())
    return items


def drafts(c) -> list:
    """Черновики профилей, для которых ещё нет настоящего профиля."""
    out = []
    folder = c.data_dir / "drafts"
    if not folder.is_dir():
        return out
    for path in sorted(folder.glob("*.yaml")):
        try:
            d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        match = d.get("match") or {}
        if _covered(c.profiles.profiles, d.get("id"), match):
            continue
        out.append({"file": path.name, "id": d.get("id"), "name": d.get("name"), "match": match})
    return out


def _covered(profiles, draft_id, match: dict) -> bool:
    for p in profiles:
        if p.id == draft_id:
            return True
        if not p.match or not match:
            continue
        for key, value in match.items():
            expected = p.match.get(key)
            if expected is None or value is None:
                continue
            options = expected if isinstance(expected, list) else [expected]
            if str(value).upper() in {str(o).upper() for o in options}:
                return True
    return False


# ======================= подсказки =======================
def _opt(label: str, level: str = "tip", explain: str = "", **target) -> dict:
    d = {"label": label, "level": level}
    if explain:
        d["explain"] = explain
    d.update(target)
    return d


def suggest(c, limit: int = 6) -> list:
    """Что важно прямо сейчас — по журналу, черновикам и настройкам."""
    items = []
    sessions = list(c.memory.sessions(50))

    for s in [s for s in sessions if s["outcome"] == "written_verified"][:3]:
        items.append(_opt(f"Сеанс №{s['id']}: заработало ли устройство?", "important",
                          "запись прошла и сверена — сообщите результат, это обучает Дирижёр",
                          action="feedback", params={"session": s["id"]}))

    undone = {s["parent_session"] for s in sessions
              if s["kind"] == "rollback" and s["outcome"] in ("rolled_back", "already_same")}
    for s in [s for s in sessions if s["outcome"] in ("not_repaired", "write_failed")
              and s["id"] not in undone][:2]:
        items.append(_opt(f"Сеанс №{s['id']} не помог — вернуть копию «до»?", "important",
                          "перед откатом тоже снимется копия, так что и откат можно отменить",
                          action="rollback", params={"session": s["id"]}))

    if sessions:
        last = sessions[0]
        tips = {
            "dry_run": _opt(f"План сеанса №{last['id']} готов — выполнить ремонт?", "tip",
                            "запись только после вашего слова «ДА»", action="run"),
            "blocked_voltage": _opt("Прошлый раз остановилось из-за напряжения", "tip",
                                    "переходник 1.8 В и programmer_voltage", lesson="voltage"),
            "not_detected": _opt("Прошлый раз микросхема не нашлась", "tip",
                                 "прищепка, вывод 1, права на USB", lesson="not_detected"),
            "read_failed": _opt("Прошлый раз не удалось прочитать чип", "tip",
                                "контакт и питание", lesson="not_detected"),
            "blocked": _opt(f"Сеанс №{last['id']}: запись была заблокирована", "tip",
                            "откройте журнал — там причина", action="show", params={"session": last["id"]}),
            "no_action": _opt("Нечем лечить: нужен эталон или полный образ", "tip",
                              "эталон снимают с такого же исправного устройства", lesson="reference"),
        }
        if last["outcome"] in tips:
            items.append(tips[last["outcome"]])

    for d in drafts(c)[:2]:
        ident = (d.get("match") or {}).get("jedec_id") or d.get("name") or d["file"]
        items.append(_opt(f"Новый чип {ident}: заполнить профиль", "tip",
                          "Дирижёр встретил чип без профиля и сохранил черновик — допишите по даташиту",
                          action="builder", params={"draft": d["file"]}))

    for path, err in c.profiles.errors[:2]:
        items.append(_opt(f"Профиль {Path(path).name} не загружен", "important", err, action="doctor"))

    broken = [r for r in c.plugin_report if r["title"] == "ошибка загрузки"]
    broken += [{"name": f"навык {Path(f).name}", "status": e} for f, e in c.skills.errors]
    for r in broken[:2]:
        items.append(_opt(f"{r['name'].capitalize()} не загружается", "important",
                          f"{r['status'][:160]} — самопроверка вернёт рабочую версию", action="doctor"))

    # обучение на неудачах: после неудачи следующий такой же сеанс прошёл — что помогло?
    known = {r["session_id"] for r in c.memory.db.execute("SELECT session_id FROM fixes")}
    good = {"detected", "healthy", "repaired", "rolled_back", "params_read", "tuned", "dry_run",
            "written_verified", "captured", "already_same", "no_change"}
    for i, s in enumerate(sessions[1:12], 1):
        if s["id"] in known or s["outcome"] not in c.learning.FAIL_OUTCOMES or s["outcome"] == "interrupted":
            continue
        later = [x for x in sessions[:i] if x["outcome"] in good and (x["plugin"] == s["plugin"] or not s["plugin"])]
        if later:
            items.append(_opt(f"Сеанс №{s['id']} не удался, а №{later[-1]['id']} прошёл — что помогло?", "tip",
                              "одна фраза — в следующий раз Дирижёр подскажет это первым",
                              action="fix", params={"session": s["id"]}))
            break

    if not c.sim:
        hardware_ready = any(p.hardware for p in c.plugins)
        if not hardware_ready:
            why = "; ".join(f"{r['name']}: {r['status']}" for r in c.plugin_report) or "нет плагинов"
            items.append(_opt("Нет подключённых инструментов", "important", why, lesson="first_chip"))
            items.append(_opt("Какой инструмент подключить", "tip",
                              "flashrom, Bus Pirate 5, ST-Link, T48, USBasp, ESP — что у вас есть", next="tools"))
            items.append(_opt("Потренироваться без железа", "tip",
                              "виртуальная микросхема — весь цикл без риска", next="sim"))
        elif not sessions:
            items.append(_opt("Первый настоящий чип — с чего начать", "tip",
                              "донор, подключение, первый эталон", lesson="first_chip"))
        if not c.cfg.get("mirror_dir"):
            items.append(_opt("Включите второе место для копий", "info",
                              "mirror_dir в config.yaml — флешка или SSD", lesson="mirror"))
    elif not sessions:
        items.append(_opt("Начните с учебного ремонта", "tip", "5 шагов — от эталона до починки",
                          lesson="sim_tour"))

    if sessions and not c.memory.db.execute("SELECT 1 FROM refs LIMIT 1").fetchone():
        items.append(_opt("Эталонов пока нет", "tip",
                          "снимите копию с исправного устройства — по ней чинят такие же",
                          next="backup_good"))

    items.sort(key=lambda o: LEVELS.get(o["level"], 9))
    return items[:limit]


# ======================= что дальше =======================
def after(action: str, params: dict | None, outcome: str | None, sid, sim: bool = False,
          hints: list | None = None) -> list:
    """Варианты после завершения действия — чтобы человек не гадал, что делать дальше.

    В учебном режиме добавляется следующая учебная поломка — чтобы пройти весь цикл.
    hints — что помогало раньше при такой же неудаче (Learning.hints): показываются первыми."""
    p = {k: v for k, v in (params or {}).items() if v is not None}
    same = {"action": action, "params": p}
    if action == "sim":
        do = p.get("do")
        by_do = {
            "reset": [_opt("Снять копию и сохранить эталон", action="run", params={"dry_run": True}),
                      _opt("Устроить сбой обновления", action="sim", params={"do": "corrupt"})],
            "corrupt": [_opt("Показать план ремонта (без записи)", action="run", params={"dry_run": True}),
                        _opt("Чинить", action="run")],
            "erase": [_opt("Чинить", action="run")],
            "flaky": [_opt("Попробовать снять копию — увидите блокировку", action="run",
                           params={"dry_run": True}),
                      _opt("Контакт снова хороший", action="sim", params={"do": "steady"})],
            "steady": [_opt("Снять копию и показать план", action="run", params={"dry_run": True})],
        }
        return by_do.get(do, [])
    if action == "reference":
        return [_opt("К началу", next="home")]
    if action == "capture":
        if outcome == "captured":
            first = (_opt("Записать сигналы", action="capture") if p.get("scan")
                     else _opt("Записать ещё раз", action="capture"))
            return [first, _opt("Открыть журнал сеанса", action="show", params={"session": sid}) if sid
                    else _opt("К началу", next="home"), _opt("Как читать замер", lesson="tool_sigrok")]
        return [_opt("Что проверить", lesson="tool_sigrok"), _opt("Попробовать ещё раз", **same)]
    if outcome is None:
        return []
    real = dict(p)
    real.pop("dry_run", None)
    retry = _opt("Попробовать ещё раз", **same)
    journal = _opt("Открыть журнал сеанса", action="show", params={"session": sid}) if sid else None
    table = {
        "detected": [_opt("Снять копию и показать план", action="run", params={"dry_run": True}),
                     _opt("Снять эталон с исправного устройства", next="backup_good")],
        "not_detected": [_opt("Что проверить", lesson="not_detected"), retry],
        "no_profile": [_opt("Заполнить профиль чипа", action="builder"),
                       _opt("Как устроен профиль", lesson="dev_chip_profile")],
        "blocked_voltage": [_opt("Что делать с напряжением", lesson="voltage")],
        "read_failed": [_opt("Что проверить", lesson="not_detected"), retry],
        "healthy": [_opt("Забрать копию файлом", action="export", params={"session": sid}),
                    _opt("К началу", next="home")],
        "no_action": [_opt("Как получить эталон", lesson="reference"),
                      _opt("Ремонт по образу прошивки", next="repair_image")],
        "blocked": [journal, _opt("Чтения не совпадают — что делать", lesson="unstable_read"), retry],
        "dry_run": [_opt("Выполнить по-настоящему", "important", "запись только после слова «ДА»",
                         action=action, params=real), journal],
        "cancelled": [retry],
        "written_verified": [_opt("Сообщить, заработало ли", "important", action="feedback",
                                  params={"session": sid})],
        "write_failed": [_opt("Вернуть копию «до»", "important", action="rollback", params={"session": sid}),
                         _opt("Почему сверка не совпала", lesson="unstable_read")],
        "repaired": [_opt("Забрать копию «после» файлом", action="export", params={"session": sid}),
                     _opt("К началу", next="home")],
        "not_repaired": [_opt("Вернуть копию «до»", "important", action="rollback", params={"session": sid}),
                         _opt("Что ещё проверить", lesson="after_fail")],
        "already_same": [_opt("К началу", next="home")],
        "rolled_back": [journal, _opt("К началу", next="home")],
        "rollback_failed": [_opt("Почему не удалось", lesson="unstable_read"), retry],
        "interrupted": [retry, journal],
        "params_read": [_opt("Изменить параметры", "important", "копия «до», план, запись после «ДА»",
                             action="tune_form", params={"session": sid}), journal],
        "no_params": [_opt("Добавить карту параметров", "important", "Навыки → + → Карта параметров",
                           action="skills"),
                      _opt("Как устроена карта", action="help", params={"article": "params"})],
        "no_change": [_opt("Изменить другие параметры", action="tune_form", params={"session": sid})],
        "tuned": [_opt("Прочитать ещё раз", action="params"),
                  _opt("Вернуть прежние настройки", action="rollback", params={"session": sid})],
        "tune_failed": [_opt("Вернуть прежние настройки", "important", action="rollback",
                             params={"session": sid}),
                        _opt("Почему сверка не совпала", action="help", params={"article": "params"})],
    }
    if action == "tune" and outcome == "dry_run":
        table["dry_run"][0] = _opt("Выполнить по-настоящему", "important", "запись только после слова «ДА»",
                                   action="tune", params=real)
    if action == "tune" and outcome in ("blocked", "cancelled"):
        table[outcome] = [_opt("Изменить значения", action="tune_form", params={"session": sid}), journal]
    options = [o for o in table.get(outcome, [journal]) if o]
    if sim:
        drill = {
            "healthy": _opt("Учебная поломка: сбой обновления", action="sim", params={"do": "corrupt"}),
            "repaired": _opt("Учебная поломка: плохой контакт", action="sim", params={"do": "flaky"}),
            "blocked": _opt("Контакт снова хороший", action="sim", params={"do": "steady"}),
            "rolled_back": _opt("Начать заново: исправная прошивка", action="sim", params={"do": "reset"}),
        }.get(outcome)
        if drill:
            options.insert(0, drill)
    if outcome in FAIL_OUTCOMES:
        learned = [_opt(f"Раньше помогало: {h['text']}", "important",
                        f"подтверждено {h['helped']} раз(а) — нажмите, если помогло и сейчас",
                        action="fix", params={"session": sid, "fix": h["id"]}) for h in (hints or [])]
        options = learned + options + ([_opt("Записать, что помогло", "info", "одна фраза — для следующего раза",
                                             action="fix", params={"session": sid})] if sid else [])
    return options
