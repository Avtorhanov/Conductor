"""«Подключить инструмент» начинается с переходника: проверки подключения и выбор модели перед сеансом.

Человек начинает с того, что у него в руках, — с переходника (карточка справочника `catalog/adapters/`).
По карточке собирается список проверок, у каждой — статус и совет:

  * программа установлена (shutil.which);
  * правило udev на месте (/etc/udev/rules.d/99-dirizher*.rules, и в нём есть VID:PID карточки);
  * пользователь в нужной группе (plugdev — USB, dialout — последовательный порт, spi — SPI самого Pi);
  * устройство видно по USB (VID:PID карточки в /sys/bus/usb/devices/*/idVendor|idProduct — только stdlib);
  * лезвие включено и настроено (схема настроек S2a) → «Настроить» → «Проверить подключение».

Всё только читает: файлы /etc и /sys, PATH и настройки. `root` подменяется в тестах.

Здесь же — выбор перед сеансом (`prepare`): лезвия переходника (опрашиваются только они), строки
«Подготовки» из карточки (поле preflight:) и модель цели — профиль с полем tool:. Модель спрашивается
списком профилей нужного класса, если лезвие переходника не узнаёт её по ID (Plugin.model_setting).
Последний выбор запоминается как удобство: строкой в data/choices.jsonl (файл только дописывается).
"""
from __future__ import annotations

import getpass
import json
import os
import shutil
import time
from pathlib import Path

from ..core import settings as S
from ..core import target as target_mod
from ..core.registry import discover
from . import catalog

STATUS_RU = {"ok": "готово", "warn": "проверьте", "fail": "нужно сделать", "info": "к сведению"}
SERIAL_GROUP = "dialout"
USB_GROUP = "plugdev"
CHOICES_FILE = "choices.jsonl"


# ======================= переходник → лезвия =======================
def adapter_plugins(cards: list, card: dict) -> list:
    """Имена лезвий переходника: из его движков (engines → карточка движка → plugin)."""
    by_id = {c["id"]: c for c in cards}
    out = []
    for eid in card.get("engines") or []:
        eng = by_id.get(eid)
        if eng and eng["kind"] == "engine" and eng.get("plugin") and eng["plugin"] not in out:
            out.append(eng["plugin"])
    return out


def adapters(cards: list) -> list:
    """Переходники, с которыми работает хотя бы одно лезвие (их можно проверить и выбрать).
    Принадлежности (прищепка, панелька — их «нужно» другим карточкам, своего USB нет) не показываются."""
    needed = {n for c in cards for n in c.get("needs") or []}
    return [c for c in cards if c["kind"] == "adapter" and adapter_plugins(cards, c)
            and not (c["id"] in needed and not c.get("usb"))]


def find_adapter(cfg: dict, adapter_id: str) -> tuple:
    """(карточка, все карточки). ValueError — нет такого переходника."""
    cards, _ = catalog.load(cfg)
    card = catalog.get(cards, adapter_id)
    if card is None or card["kind"] != "adapter":
        known = ", ".join(c["id"] for c in adapters(cards))
        raise ValueError(f"нет переходника «{adapter_id}». Есть: {known}")
    return card, cards


def _groups(card: dict) -> list:
    if card.get("groups"):
        return list(card["groups"])
    return [USB_GROUP] if card.get("usb") else []


def _usb_ids(card: dict) -> list:
    return [x.lower() for x in str(card.get("usb") or "").replace(",", " ").split() if ":" in x]


# ======================= проверки =======================
def _row(cid: str, title: str, status: str, text: str, advice: str = "", code: str = "", action=None) -> dict:
    return {"id": cid, "title": title, "status": status, "status_title": STATUS_RU[status], "text": text,
            "advice": advice, "code": code, "action": action}


def usb_devices(root="/") -> set:
    """{"vvvv:pppp"} — что сейчас видно на шине USB (из /sys, только чтение)."""
    base = Path(root) / "sys" / "bus" / "usb" / "devices"
    found = set()
    if not base.is_dir():
        return found
    for dev in base.iterdir():
        try:
            vid = (dev / "idVendor").read_text().strip().lower()
            pid = (dev / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        found.add(f"{vid}:{pid}")
    return found


def user_groups(root="/", user: str | None = None) -> set:
    """Группы пользователя по /etc/group (+ текущие группы процесса, если root — настоящий «/»)."""
    user = user or getpass.getuser()
    out = set()
    try:
        lines = (Path(root) / "etc" / "group").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    gids = set(os.getgroups()) | {os.getgid()} if str(root) == "/" else set()
    for line in lines:
        parts = line.split(":")
        if len(parts) < 4:
            continue
        members = [m.strip() for m in parts[3].split(",") if m.strip()]
        try:
            gid = int(parts[2])
        except ValueError:
            gid = None
        if user in members or (gid is not None and gid in gids):
            out.add(parts[0])
    return out


def _program_checks(card: dict, cards: list) -> list:
    rows = []
    by_id = {c["id"]: c for c in cards}
    for eid in card.get("engines") or []:
        eng = by_id.get(eid)
        if not eng or eng["kind"] != "engine":
            continue
        prog = eng.get("program")
        if not prog:
            if eng.get("plugin"):
                rows.append(_row(f"program_{eid}", f"Программа: {eng['title']}", "ok",
                                 "встроена в Дирижёр — ставить ничего не нужно"))
            continue
        found = shutil.which(prog) or shutil.which(prog + ".py")
        if found:
            rows.append(_row(f"program_{eid}", f"Программа {prog}", "ok", f"установлена: {found}"))
        else:
            rows.append(_row(f"program_{eid}", f"Программа {prog}", "fail", "не установлена",
                             "Установите по карточке движка (раздел «Установка»).",
                             code=eng.get("install") or "",
                             action={"action": "catalog", "params": {"card": eid}}))
    return rows


def _udev_check(card: dict, root) -> dict | None:
    ids = _usb_ids(card)
    if not ids or USB_GROUP not in _groups(card):
        return None
    rules_dir = Path(root) / "etc" / "udev" / "rules.d"
    files = sorted(rules_dir.glob("99-dirizher*.rules")) if rules_dir.is_dir() else []
    install = ("sudo cp system/99-dirizher*.rules /etc/udev/rules.d/\n"
               "sudo udevadm control --reload-rules && sudo udevadm trigger")
    if not files:
        return _row("udev", "Права на USB без sudo (правило udev)", "fail",
                    "правил Дирижёра в /etc/udev/rules.d/ нет — программа увидит устройство только под sudo",
                    "Скопируйте правила из комплекта (один раз).", code=install)
    text = "\n".join(f.read_text(encoding="utf-8", errors="replace").lower() for f in files)
    known = [i for i in ids if i.split(":")[0] in text and i.split(":")[1] in text]
    names = ", ".join(f.name for f in files)
    if known:
        return _row("udev", "Права на USB без sudo (правило udev)", "ok", f"есть: {names}")
    return _row("udev", "Права на USB без sudo (правило udev)", "warn",
                f"правила есть ({names}), но VID:PID {', '.join(ids)} в них не упомянут",
                "Обновите правила из комплекта или добавьте строку для своего устройства (VID:PID — из lsusb).",
                code=install)


def _group_checks(card: dict, root) -> list:
    rows = []
    have = user_groups(root)
    user = getpass.getuser()
    for g in _groups(card):
        why = {USB_GROUP: "доступ к USB-программатору", SERIAL_GROUP: "доступ к последовательному порту",
               "spi": "доступ к SPI самого Raspberry Pi", "i2c": "доступ к I2C самого Raspberry Pi"}.get(g, "доступ к устройству")
        if user == "root" and str(root) == "/":
            rows.append(_row(f"group_{g}", f"Группа {g}", "info", "работаете под root — группа не нужна, "
                             "но для прибора лучше обычный пользователь"))
        elif g in have:
            rows.append(_row(f"group_{g}", f"Группа {g}", "ok", f"пользователь {user} в группе {g} ({why})"))
        else:
            rows.append(_row(f"group_{g}", f"Группа {g}", "fail", f"пользователь {user} не в группе {g} ({why})",
                             "Добавьте и перелогиньтесь (выйдите из системы и войдите снова).",
                             code=f"sudo usermod -aG {g} $USER"))
    return rows


def _usb_check(card: dict, root) -> dict:
    ids = _usb_ids(card)
    if not ids:
        return _row("usb", "Устройство на шине USB", "info",
                    "у карточки нет VID:PID — проверьте сами, что переходник виден системе", code="lsusb")
    seen = usb_devices(root)
    found = [i for i in ids if i in seen]
    if found:
        return _row("usb", "Устройство на шине USB", "ok", f"видно: {', '.join(found)}")
    return _row("usb", "Устройство на шине USB", "warn", f"не видно ({', '.join(ids)})",
                "Проверьте кабель и разъём (лучше напрямую, без хаба). У клонов VID:PID бывает другим — "
                "сверьте с lsusb.", code="lsusb")


def _plugin_checks(cfg: dict, card: dict, cards: list, report: list, profiles=None) -> list:
    rows = []
    by_name = {r["name"]: r for r in report or []}
    classes, _ = discover()
    for name in adapter_plugins(cards, card):
        r = by_name.get(name)
        setup = {"action": "tool", "params": {"name": name}}
        title = f"Лезвие {name}"
        if r is None:
            rows.append(_row(f"plugin_{name}", title, "warn", "лезвие не загружено (навык выключен?)",
                             "«Мастерская» → «Навыки»: включите навык, который его приносит."))
            continue
        if r["active"]:
            rows.append(_row(f"plugin_{name}", title, "ok", f"включено: {r['status']}", action=setup))
            continue
        status = r["status"]
        if status == "выключен":
            rows.append(_row(f"plugin_{name}", title, "fail", "выключено",
                             "Включите и настройте: «Настроить» (сохраняется в config.tools.yaml).", action=setup))
            continue
        cls = classes.get(name, (None,))[0]
        if cls is not None and profiles is not None and status.startswith("недоступен: укажите"):
            # не хватает модели? — её даст профиль, выбранный перед сеансом
            settings = dict((cfg.get("plugins") or {}).get(name) or {})
            for prof in target_mod.candidates(profiles.profiles, [name]):
                merged = dict(settings, **target_mod.settings_for(prof, cls))
                if not S.validate(cls.schema(), merged)["missing"]:
                    rows.append(_row(f"plugin_{name}", title, "ok",
                                     "включено; модель (и размер) — из профиля, выберете перед сеансом",
                                     action=setup))
                    break
            else:
                rows.append(_row(f"plugin_{name}", title, "fail", status,
                                 "Заполните недостающее: «Настроить».", action=setup))
            continue
        rows.append(_row(f"plugin_{name}", title, "fail" if status.startswith("недоступен") else "warn", status,
                         "Откройте «Настроить» — там видно, чего не хватает.", action=setup))
    return rows


def checks(cfg: dict, card: dict, cards: list, report: list, root="/", profiles=None) -> list:
    """Проверки подключения переходника. Только чтение."""
    rows = _program_checks(card, cards)
    udev = _udev_check(card, root)
    if udev:
        rows.append(udev)
    rows += _group_checks(card, root)
    rows.append(_usb_check(card, root))
    rows += _plugin_checks(cfg, card, cards, report, profiles)
    return rows


def view(cfg: dict, adapter_id: str | None, report: list, root="/", profiles=None, sim: bool = False) -> dict:
    """Всё для экрана «Подключить инструмент»: без id — список переходников; с id — проверки и действия."""
    cards, _ = catalog.load(cfg)
    if not adapter_id:
        return {"adapters": [{"id": c["id"], "title": c["title"], "tagline": c["tagline"],
                              "plugins": adapter_plugins(cards, c), "level": c["level"]}
                             for c in adapters(cards)]}
    card = catalog.get(cards, adapter_id)
    if card is None or card["kind"] != "adapter":
        raise ValueError(f"нет переходника «{adapter_id}»")
    rows = checks(cfg, card, cards, report, root, profiles)
    plugins = adapter_plugins(cards, card)
    ready = any(r["id"].startswith("plugin_") and r["status"] == "ok" for r in rows)
    actions = [{"label": "Проверить подключение", "explain": "опознание только лезвиями этого переходника",
                "level": "important" if ready else "tip", "action": "detect", "params": {"adapter": adapter_id}},
               {"label": "Копия и план ремонта (без записи)", "action": "run",
                "params": {"adapter": adapter_id, "dry_run": True}}]
    actions += [{"label": f"Настроить {n}", "action": "tool", "params": {"name": n}} for n in plugins]
    actions.append({"label": "Карточка переходника — рисунки и шаги", "action": "catalog",
                    "params": {"card": adapter_id}})
    summary = {s: sum(1 for r in rows if r["status"] == s) for s in STATUS_RU}
    return {"card": {"id": card["id"], "title": card["title"], "tagline": card["tagline"],
                     "lesson": card.get("lesson") or ""},
            "checks": rows, "actions": actions, "summary": summary, "plugins": plugins,
            "note": "учебный режим: проверки — про настоящее железо" if sim else ""}


def text(v: dict) -> list:
    """Строки для терминала."""
    if "adapters" in v:
        return [f" • {a['id']:<16} {a['title']} — {a['tagline']}" for a in v["adapters"]]
    mark = {"ok": "✔", "warn": "!", "fail": "✖", "info": "·"}
    out = [f"{v['card']['title']} — {v['card']['tagline']}"]
    if v.get("note"):
        out.append(v["note"])
    for r in v["checks"]:
        out.append(f" {mark[r['status']]} {r['title']}: {r['text']}")
        if r["advice"] and r["status"] != "ok":
            out.append(f"     → {r['advice']}")
        for line in (r["code"] or "").splitlines() if r["status"] != "ok" else []:
            out.append(f"       $ {line}")
    return out


# ======================= выбор перед сеансом =======================
def _choices_path(cfg: dict) -> Path:
    return Path(cfg["data_dir"]) / CHOICES_FILE


def last_target(cfg: dict, key: str) -> str | None:
    """Последний выбор модели для ключа (переходник или «*»)."""
    path = _choices_path(cfg)
    last = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("key") == key and row.get("target"):
                last = str(row["target"])
    except OSError:
        return None
    return last


def remember(cfg: dict, key: str, target: str) -> None:
    """Дописать выбор (файл только дописывается). Ошибка записи — не повод останавливать сеанс."""
    path = _choices_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "key": key, "target": target},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass


def ask_target(c, plugins: list, classes: list, key: str) -> str | None:
    """Спросить модель списком профилей. None — «как в настройках инструмента». ValueError — отмена."""
    found, _ = discover(c.skills.active)
    need = [n for n in plugins if n in found and getattr(found[n][0], "model_setting", None)]
    if not need:
        return None
    options = target_mod.candidates(c.profiles.profiles, need, classes)
    if not options:
        return None
    last = last_target(c.cfg, key)
    options.sort(key=lambda p: p.id != last)
    labels = [p.name + (" — в прошлый раз" if p.id == last else "") for p in options]
    labels.append("Другая — как в настройках инструмента")
    idx = c.ui.choose("Какая модель подключена? По проводам её не узнать — выберите по маркировке на корпусе.",
                      labels)
    if idx is None:
        raise ValueError("модель не выбрана — сеанс не начат")
    if idx >= len(options):
        return None
    chosen = options[idx].id
    remember(c.cfg, key, chosen)
    return chosen


def prepare(c, params: dict) -> None:
    """Перед сеансом: переходник (только его лезвия, его «Подготовка») и модель цели.
    params может получить target — выбранную модель (чтобы «что дальше» не спрашивало снова)."""
    adapter, chosen = params.get("adapter"), params.get("target")
    if c.sim and (adapter or chosen):
        c.ui.info("учебный режим: переходник и модель не используются — работает учебная микросхема")
        return
    plugins = preflight = None
    source = ""
    if adapter:
        card, cards = find_adapter(c.cfg, adapter)
        plugins = adapter_plugins(cards, card)
        preflight = card.get("preflight") or None
        source = card["title"]
        if not chosen:
            chosen = ask_target(c, plugins, card.get("targets") or [], adapter)
            if chosen:
                params["target"] = chosen
    if adapter or chosen:
        c.select(target=chosen or None, plugins=plugins, preflight=preflight, source=source)
