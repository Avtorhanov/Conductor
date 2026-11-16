"""Командная строка «Дирижёра».   Запуск:  python3 -m dirizher <команда>

Без команды в терминале открывается пошаговый мастер (Штурман).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core.config import load_config
from .core.pipeline import OUTCOMES, Conductor
from .core.ui import ConsoleUI

ROOT = Path(__file__).resolve().parent.parent
KIND_RU = {"run": "ремонт", "detect": "опознание", "rollback": "откат", "params": "параметры",
           "tune": "настройка", "capture": "замер"}


def cmd_plugins(c, a):
    c.ui.header("Плагины («лезвия»)")
    for row in c.plugin_report:
        mark = "✔" if row["active"] else "·"
        c.ui.out(f" {mark} {row['name']:<14} {row['title']}")
        c.ui.out(f"      {row['status']}")
    if c.sim:
        c.ui.info("(режим --sim: работают только виртуальные плагины)")


def cmd_profiles(c, a):
    c.ui.header(f"Профили ({c.profiles.root})")
    for p in sorted(c.profiles.profiles, key=lambda p: (p.target_class, -p.priority, p.id)):
        kind = "плата" if p.content_match else ("общий" if p.is_generic else "чип")
        volt = f"{p.voltage} В" if p.voltage else "напряжение не указано"
        c.ui.out(f" • {p.id:<24} [{kind}] {p.name} — {volt}")
    for path, err in c.profiles.errors:
        c.ui.warn(f"{path}: {err}")


def cmd_detect(c, a):
    c.detect_only()


def cmd_run(c, a):
    c.run(image_path=a.image, label=a.label)


def cmd_history(c, a):
    c.ui.header("История сеансов")
    rows = c.memory.sessions(a.n)
    if not rows:
        c.ui.info("пока пусто")
    for s in rows:
        outcome = OUTCOMES.get(s["outcome"], s["outcome"] or "не завершён")
        c.ui.out(f" №{s['id']:<4} {s['started']}  {KIND_RU.get(s['kind'], s['kind']):<10} "
                 f"{s['profile_id'] or '—':<22} {s['recipe_id'] or '':<18} {outcome}")


def cmd_show(c, a):
    s = c.memory.session(a.session)
    if s is None:
        c.ui.error("нет такого сеанса")
        return 1
    c.ui.header(f"Сеанс №{s['id']} ({KIND_RU.get(s['kind'], s['kind'])})")
    for key in ("started", "finished", "plugin", "profile_id", "recipe_id", "outcome", "parent_session",
                "fingerprint"):
        if s[key] is not None:
            c.ui.info(f"{key}: {s[key]}")
    for sn in c.memory.snapshots(s["id"]):
        c.ui.info(f"снимок «{sn['kind']}»: {sn['sha'][:16]}… {sn['size']} байт, "
                  f"подтверждён: {'да' if sn['verified'] else 'нет'}")
    c.ui.out()
    for e in c.memory.events(s["id"]):
        c.ui.out(f"   {e['ts']} [{e['level']}] {e['message']}")


def cmd_feedback(c, a):
    c.feedback(a.session, a.ok)


def cmd_reference(c, a):
    c.mark_reference(a.session, a.kind, a.label)


def cmd_rollback(c, a):
    c.rollback(a.session)


def cmd_export(c, a):
    c.export(a.session, a.kind, a.to)


def cmd_params(c, a):
    c.read_params()


def cmd_tune(c, a):
    changes = {}
    for item in a.set or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            c.ui.error(f"«{item}»: пишите параметр=значение, например range_high=600")
            return 1
        changes[key.strip()] = value.strip()
    if not changes:
        c.ui.error("укажите хотя бы одно --set параметр=значение (список — команда params)")
        return 1
    c.tune(changes)


def cmd_skills(c, a):
    from .interface.guide import skills_info
    info = skills_info(c)
    c.ui.header(f"Навыки ({c.skills.root})")
    if not info["items"] and not info["broken"]:
        c.ui.info("пока нет. Новый: python3 -m dirizher web → «Навыки» → «+», или скопируйте skills/modbus_rtu")
    for sk in info["items"]:
        mark = "✖" if sk["errors"] else ("✔" if sk["enabled"] else "·")
        c.ui.out(f" {mark} {sk['id']:<16} {sk['title']} [{sk['kind']}]")
        for pl in sk["plugins"]:
            c.ui.info(f"лезвие {pl['name']}: {pl['status']}")
        if sk["profiles"]:
            c.ui.info("профили: " + ", ".join(sk["profiles"]))
        for e in sk["errors"]:
            c.ui.error(e)
        for w in sk["warnings"]:
            c.ui.warn(w)
    for b in info["broken"]:
        c.ui.error(f"{b['path']}: {b['error']}")


def cmd_sim(c, a):
    sim = c.plugin("simulator")
    c.ui.ok(getattr(sim, f"do_{a.action}")())


COMMANDS = {
    "plugins": cmd_plugins, "profiles": cmd_profiles, "detect": cmd_detect, "run": cmd_run,
    "history": cmd_history, "show": cmd_show, "feedback": cmd_feedback, "reference": cmd_reference,
    "rollback": cmd_rollback, "export": cmd_export, "sim": cmd_sim,
    "params": cmd_params, "tune": cmd_tune, "skills": cmd_skills,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m dirizher",
                                description="Дирижёр — опознание, диагностика и восстановление прошивок")
    p.add_argument("--config", default=str(ROOT / "config.yaml"), help="путь к config.yaml")
    p.add_argument("--sim", action="store_true", help="учебный режим: виртуальная микросхема вместо железа")
    p.add_argument("--version", action="version", version=f"Дирижёр {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="команда")

    w = sub.add_parser("web", help="интерфейс в браузере: ПК, телефон, сенсорный экран прибора")
    w.add_argument("--host", help="адрес (по умолчанию из config.yaml → web.host)")
    w.add_argument("--port", type=int, help="порт (по умолчанию из config.yaml → web.port)")
    sub.add_parser("wizard", help="пошаговый мастер в терминале (SSH, консоль, маленький экран)")

    sub.add_parser("plugins", help="какие инструменты подключены")
    sub.add_parser("profiles", help="какие профили загружены")
    sub.add_parser("detect", help="только опознать, что подключено")

    r = sub.add_parser("run", help="полный цикл: опознание → копия → диагностика → ремонт")
    r.add_argument("--dry-run", action="store_true", help="показать план, ничего не записывая")
    r.add_argument("--image", help="официальный образ прошивки для записи")
    r.add_argument("--label", help="метка эталона (например, модель платы)")

    h = sub.add_parser("history", help="история сеансов")
    h.add_argument("-n", type=int, default=20, help="сколько последних показать")

    s = sub.add_parser("show", help="подробности сеанса")
    s.add_argument("session", type=int)

    f = sub.add_parser("feedback", help="отзыв после проверки устройства (обучение)")
    f.add_argument("session", type=int)
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--ok", action="store_true", help="устройство заработало")
    g.add_argument("--fail", action="store_true", help="не помогло")

    ref = sub.add_parser("reference", help="сохранить снимок сеанса как эталон")
    ref.add_argument("session", type=int)
    ref.add_argument("--kind", choices=["before", "after"], help="какой снимок (по умолчанию «после», иначе «до»)")
    ref.add_argument("--label", help="метка эталона")

    rb = sub.add_parser("rollback", help="вернуть на чип копию «до» из сеанса")
    rb.add_argument("session", type=int)
    rb.add_argument("--dry-run", action="store_true", help="только проверить условия")

    ex = sub.add_parser("export", help="выгрузить снимок в файл")
    ex.add_argument("session", type=int)
    ex.add_argument("--kind", default="before", choices=["before", "after", "written"])
    ex.add_argument("--to", required=True, help="куда сохранить, например ~/dump.bin")

    sub.add_parser("params", help="прочитать параметры прибора по карте профиля (только чтение)")
    tu = sub.add_parser("tune", help="изменить параметры прибора: копия «до», план, запись после «ДА»")
    tu.add_argument("--set", action="append", metavar="ПАРАМЕТР=ЗНАЧЕНИЕ", help="например range_high=600")
    tu.add_argument("--dry-run", action="store_true", help="показать план, ничего не записывая")
    sub.add_parser("skills", help="навыки: что загружено и что сломано")
    dr = sub.add_parser("doctor", help="самопроверка: копии, память, профили, навыки, Штурман")
    dr.add_argument("--heal", action="store_true", help="исправить то, что исправляется без догадок")
    ca = sub.add_parser("catalog", help="справочник: движки (авторы, как работают) и переходники (как подключить)")
    ca.add_argument("card", nargs="?", help="id карточки, например ch341a или flashrom")
    ca.add_argument("--kind", choices=["engine", "adapter", "diy"], help="только движки, переходники или «собрать»")
    pg = sub.add_parser("playground", help="учебная площадка: пошаговый тренажёр — ноутбук, ТВ, телефон, машина, связки")
    pg.add_argument("track", nargs="?", help="маршрут, например laptop, car, first_chip (без него — список)")
    pg.add_argument("--export", metavar="ФАЙЛ",
                    help="сохранить тренажёр одним HTML-файлом: открывается в браузере без сети и без прибора")
    hp = sub.add_parser("help", help="справка: статьи и уроки (без слов — список)")
    hp.add_argument("words", nargs="*", help="поиск или имя статьи")
    sm = sub.add_parser("sim", help="управление виртуальной микросхемой (учебные поломки)")
    sm.add_argument("action", choices=["status", "reset", "corrupt", "erase", "flaky", "steady"])
    return p


def cmd_help(cfg, words: list) -> int:
    from .workshop import helpdocs
    ui = ConsoleUI()
    query = " ".join(words)
    art = helpdocs.article(cfg, query) if len(words) == 1 else None
    if art is not None:
        ui.header(art["title"])
        ui.out(art["text"].rstrip())
        return 0
    items = helpdocs.search(helpdocs.articles(cfg), query)
    ui.header("Справка" + (f": «{query}»" if query else ""))
    if not items:
        ui.info("ничего не нашлось")
    for it in items[:40]:
        name = {"article": it["id"], "card": f"catalog {it.get('card')}"}.get(it["kind"]) or f"(урок) {it.get('lesson')}"
        ui.out(f" • {it['title']}  [{name}]")
    ui.info("статья целиком: python3 -m dirizher help <имя>")
    return 0


def cmd_playground(track_id, export) -> int:
    from .interface import playground
    ui = ConsoleUI()
    try:
        data = playground.load()
    except playground.PlaygroundError as e:
        ui.error(str(e))
        return 1
    if export:
        dest = Path(export).expanduser()
        dest.write_text(playground.standalone(data), encoding="utf-8")
        ui.ok(f"тренажёр сохранён одним файлом: {dest} ({dest.stat().st_size // 1024} КБ) — "
              f"откройте в любом браузере, сеть и прибор не нужны")
        return 0
    if track_id and playground.track(data, track_id) is None:
        ui.error(f"маршрута «{track_id}» нет: " + ", ".join(t["id"] for t in data["tracks"]))
        return 1
    try:
        playground.run_text(ui, data, track_id)
    except KeyboardInterrupt:
        ui.out()
    return 0


def cmd_catalog(cfg, card_id, kind) -> int:
    from .workshop import catalog
    ui = ConsoleUI()
    cards, errors = catalog.load(cfg)
    if card_id:
        card = catalog.get(cards, card_id)
        if card is None:
            ui.error(f"карточки «{card_id}» нет — список: python3 -m dirizher catalog")
            return 1
        ui.header(f"{card['title']} · {card['kind_title'].lower()}")
        for line in catalog.text(card, cards):
            ui.out(line)
        return 0
    ui.header("Справочник: движки и переходники")
    for c in catalog.search(cards, "", kind or ""):
        ui.out(f" • {c['id']:<15} {c['title']} · {c['group_title'].lower()} — {c['tagline']}")
    for path, err in errors:
        ui.warn(f"{path}: {err}")
    ui.info("карточка целиком: python3 -m dirizher catalog <id>; с рисунками — в браузере, раздел «Справочник»")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "sim":
        args.sim = True
    cfg = load_config(Path(args.config))
    if args.cmd is None and not sys.stdin.isatty():
        build_parser().print_help()
        return 0
    if args.cmd in (None, "wizard"):
        from .interface.wizard import Wizard
        try:
            return Wizard(cfg, sim=args.sim).run()
        except KeyboardInterrupt:
            print("\n   до встречи! Все сделанные копии сохранены.")
            return 130
    if args.cmd == "web":
        from .interface.web import serve
        return serve(cfg, host=args.host, port=args.port, sim=args.sim)
    if args.cmd == "doctor":
        from .workshop import doctor
        return doctor.main(cfg, sim=args.sim, do_heal=args.heal)
    if args.cmd == "help":
        return cmd_help(cfg, args.words)
    if args.cmd == "catalog":
        return cmd_catalog(cfg, args.card, args.kind)
    if args.cmd == "playground":
        return cmd_playground(args.track, args.export)
    ui = ConsoleUI()
    c = Conductor(cfg, ui, sim=args.sim, dry_run=getattr(args, "dry_run", False))
    try:
        result = COMMANDS[args.cmd](c, args)
        return result if result in (0, 1) else 0
    except KeyboardInterrupt:
        c.interrupted()
        ui.warn("прервано. Все сделанные копии сохранены.")
        return 130
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
