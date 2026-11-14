"""Командная строка «Дирижёра».   Запуск:  python3 -m dirizher <команда>"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .core.config import load_config
from .core.pipeline import OUTCOMES, Conductor
from .core.ui import ConsoleUI

ROOT = Path(__file__).resolve().parent.parent
KIND_RU = {"run": "ремонт", "detect": "опознание", "rollback": "откат"}


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


def cmd_sim(c, a):
    sim = c.plugin("simulator")
    c.ui.ok(getattr(sim, f"do_{a.action}")())


COMMANDS = {
    "plugins": cmd_plugins, "profiles": cmd_profiles, "detect": cmd_detect, "run": cmd_run,
    "history": cmd_history, "show": cmd_show, "feedback": cmd_feedback, "reference": cmd_reference,
    "rollback": cmd_rollback, "export": cmd_export, "sim": cmd_sim,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m dirizher",
                                description="Дирижёр — опознание, диагностика и восстановление прошивок")
    p.add_argument("--config", default=str(ROOT / "config.yaml"), help="путь к config.yaml")
    p.add_argument("--sim", action="store_true", help="учебный режим: виртуальная микросхема вместо железа")
    p.add_argument("--version", action="version", version=f"Дирижёр {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="команда")

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

    sm = sub.add_parser("sim", help="управление виртуальной микросхемой (учебные поломки)")
    sm.add_argument("action", choices=["status", "reset", "corrupt", "erase", "flaky", "steady"])
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "sim":
        args.sim = True
    cfg = load_config(Path(args.config))
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
