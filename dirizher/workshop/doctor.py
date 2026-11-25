"""Самопроверка и самоисцеление.

Проверяет то, от чего зависят сохранность данных и работа прибора, и чинит только то, что можно
починить без догадок:
  * копии дампов — каждая сверяется по SHA-256 в основном хранилище и во втором месте (mirror_dir);
    испорченная или пропавшая восстанавливается из исправного места, испорченный файл — в карантин;
  * правки из капота, после которых профиль, навык, лезвие или Штурман перестали загружаться, —
    возврат последней рабочей версии из истории капота (это тоже правка с историей).
Только сообщает: память SQLite (integrity_check), место на диске, недоступные инструменты,
опечатки и неверные типы в настройках лезвий (по их схемам), probe_rs и openocd включены вместе.

Запуск:  python3 -m dirizher doctor          — проверить
         python3 -m dirizher doctor --heal   — проверить и исправить то, что исправляется
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..core.pipeline import Conductor
from ..core.ui import ConsoleUI
from .hood import Hood, HoodError

MIN_FREE = 512 * 1024 * 1024


class _Quiet(ConsoleUI):
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


def _issue(iid: str, level: str, title: str, detail: str = "", heal: str | None = None) -> dict:
    return {"id": iid, "level": level, "title": title, "detail": detail, "heal": heal}


def _rel(hood: Hood, path) -> str | None:
    try:
        return Path(path).resolve().relative_to(hood.root.resolve()).as_posix()
    except ValueError:
        return None


def _file_issue(hood: Hood, rel: str | None, title: str, detail: str) -> dict:
    heal = None
    if rel:
        try:
            vid = hood.last_good(rel)
        except Exception:
            vid = None
        if vid:
            heal = f"вернуть рабочую версию {rel} из истории капота"
        else:
            detail += f" · исправьте в капоте или верните через git: git checkout -- {rel}"
    return _issue(f"file:{rel}" if rel else f"note:{title}", "bad", title, detail, heal)


def check(cfg: dict, sim: bool = False, deep: bool = True, hood: Hood | None = None) -> list:
    """Список находок. deep=False — без пересчёта SHA-256 копий (быстро, для подсказок)."""
    hood = hood or Hood(cfg)
    issues = []
    c = Conductor(cfg, _Quiet(), sim=sim)
    try:
        # 1. память
        try:
            res = c.memory.db.execute("PRAGMA integrity_check").fetchone()[0]
        except Exception as e:
            res = str(e)
        if res != "ok":
            issues.append(_issue("memory", "bad", "Память (SQLite) повреждена", str(res)[:300]
                                 + " · остановитесь и сделайте копию папки data/ — восстановление вручную"))
        # 2. копии
        shas = {r["sha"] for r in c.memory.db.execute("SELECT sha FROM snapshots UNION SELECT sha FROM refs")}
        for sha in sorted(s for s in shas if s):
            if deep:
                h = c.store.health(sha)
            else:
                h = {"primary": "ok" if c.store.has(sha) else "missing", "mirror": "off"}
            if h["primary"] == "ok" and h["mirror"] in ("ok", "off"):
                continue
            can = "ok" in (h["primary"], h["mirror"])
            where = {"missing": "нет", "bad": "испорчена"}
            detail = f"основная: {where.get(h['primary'], h['primary'])}"
            if h["mirror"] != "off":
                detail += f", второе место: {where.get(h['mirror'], h['mirror'])}"
            issues.append(_issue(f"copy:{sha}", "warn" if can else "bad", f"Копия {sha[:12]}…", detail,
                                 "восстановить из исправного места (испорченное — в карантин)" if can else None))
        # 3. место на диске
        try:
            free = shutil.disk_usage(c.data_dir).free
            if free < MIN_FREE:
                issues.append(_issue("disk", "warn", "Мало места для копий",
                                     f"свободно {free // (1024 * 1024)} МБ в {c.data_dir}"))
        except OSError:
            pass
        # 4. профили
        for path, err in c.profiles.errors:
            issues.append(_file_issue(hood, _rel(hood, path), f"Профиль {Path(path).name} не загружен", err))
        # 5. навыки и лезвия
        for folder, err in c.skills.errors:
            issues.append(_file_issue(hood, _rel(hood, Path(folder) / "skill.yaml"),
                                      f"Навык {Path(folder).name} не загружен", err))
        for row in c.plugin_report:
            if row["title"] == "ошибка загрузки":
                name = row["name"]
                if name.startswith("навык "):
                    sid = name.split(" ", 1)[1]
                    rel = f"skills/{sid}/skill.py"
                    title = f"Код навыка {sid} не загружен"
                else:
                    rel, title = f"dirizher/plugins/{name}.py", f"Лезвие {name} не загружено"
                issues.append(_file_issue(hood, rel if (hood.root / rel).is_file() else None, title, row["status"]))
            elif row["status"].startswith("недоступен"):
                issues.append(_issue(f"tool:{row['name']}", "warn", f"{row['title']}: инструмент недоступен",
                                     row["status"]))
            if row.get("warnings"):
                issues.append(_issue(f"settings:{row['name']}", "warn", f"Настройки лезвия {row['name']}",
                                     "; ".join(row["warnings"]) + " · исправьте: «Прибор» → «Инструменты» → "
                                     f"«Настроить» или python3 -m dirizher tool {row['name']}"))
        if cfg.get("_tools_error"):
            issues.append(_file_issue(hood, "config.tools.yaml", "Настройки инструментов не загружены",
                                      cfg["_tools_error"]))
        plugins = cfg.get("plugins") or {}
        both = [n for n in ("probe_rs", "openocd") if (plugins.get(n) or {}).get("enabled")]
        if len(both) == 2:
            issues.append(_issue("conflict:probe_rs+openocd", "warn", "Включены и probe_rs, и openocd",
                                 "оба берут один отладчик и один МК — оставьте одно лезвие "
                                 "(«Прибор» → «Инструменты» → «Настроить» → «Включено»)"))
        # 6. Штурман
        from ..interface import guide
        try:
            guide.load_guide()
        except Exception as e:
            issues.append(_file_issue(hood, "dirizher/interface/guide.yaml", "Штурман не загружается", str(e)[:400]))
        from ..interface import playground
        try:
            playground.load()
        except Exception as e:
            issues.append(_file_issue(hood, "dirizher/interface/playground.yaml", "Учебная площадка не загружается",
                                      str(e)[:400]))
        # 7. справочник: карточки движков и переходников
        from . import catalog
        cards, card_errors = catalog.load(cfg)
        for rel, err in card_errors:
            issues.append(_file_issue(hood, rel, f"Карточка {Path(rel).name} не загружена", err))
        for card in cards:
            if card["warnings"]:
                issues.append(_issue(f"card:{card['id']}", "info", f"Карточка «{card['title']}»: есть что дополнить",
                                     "; ".join(card["warnings"]) + f" · {card['path']}"))
        # 8. второе место
        if not cfg.get("mirror_dir"):
            issues.append(_issue("mirror", "info", "Второе место для копий выключено",
                                 "mirror_dir в config.yaml — флешка или SSD: без него испорченную копию не из чего "
                                 "восстановить"))
    finally:
        c.close()
    order = {"bad": 0, "warn": 1, "info": 2}
    issues.sort(key=lambda i: order.get(i["level"], 3))
    return issues


def heal(cfg: dict, iid: str, sim: bool = False, hood: Hood | None = None, word=None,
         require_word: bool = True) -> list:
    """Исправить одну находку. Возвращает строки «что сделано»."""
    hood = hood or Hood(cfg)
    if iid.startswith("copy:"):
        c = Conductor(cfg, _Quiet(), sim=sim)
        try:
            return c.store.repair(iid[5:], c.data_dir / "quarantine")
        finally:
            c.close()
    if iid.startswith("file:"):
        rel = iid[5:]
        res = hood.heal(rel, word=word, require_word=require_word)
        if not res.get("ok"):
            raise HoodError("; ".join(res.get("errors") or ["не удалось"]))
        return [f"{rel}: возвращена рабочая версия"]
    raise HoodError("эту находку можно исправить только вручную")


def main(cfg: dict, sim: bool = False, do_heal: bool = False) -> int:
    """Для командной строки: показать находки и (по --heal) исправить исправимое."""
    marks = {"bad": "✖", "warn": "⚠", "info": "·"}
    issues = check(cfg, sim)
    print(f"\n══ Самопроверка{' (учебный режим)' if sim else ''} ══")
    if not issues:
        print(" ✔ всё в порядке")
        return 0
    worst = 0
    for i in issues:
        print(f" {marks.get(i['level'], '·')} {i['title']}" + (f" — {i['detail']}" if i["detail"] else ""))
        if i["heal"]:
            print(f"     исправимо: {i['heal']}")
        worst = max(worst, {"bad": 2, "warn": 1}.get(i["level"], 0))
    if do_heal:
        for i in issues:
            if not i["heal"]:
                continue
            try:
                for line in heal(cfg, i["id"], sim, require_word=False):
                    print(f" ✔ {line}")
            except Exception as e:
                print(f" ✖ {i['title']}: {e}")
        left = [i for i in check(cfg, sim) if i["level"] == "bad"]
        return 1 if left else 0
    return 1 if worst == 2 else 0
