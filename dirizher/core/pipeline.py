"""Дирижёр: единый сценарий для любого «лезвия».

  1 подготовка → 2 опознание → 3 напряжение → 4 копия «до» → 5 диагностика
  → 6 план ремонта → 7 подтверждение, запись, сверка, копия «после» → отзыв (обучение)

Второй сценарий — «Настройка» (tune): изменить именованные параметры прибора по карте
из профиля (core/params.py). Защита та же: копия «до», сверка, слово «ДА», копия «после».

Ядро не знает ничего о конкретных чипах: «как подключиться» знают плагины,
«что это и как лечить» — профили.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from .checks import CHECKS, DEFAULT_CHECKS, CheckInput, diff_sectors, is_fill, masked, region_diff
from . import layout as layout_mod
from . import layout_detect
from .learning import Learning
from .memory import Memory, now
from .models import Finding, Inspection, chip_key
from .params import apply_changes, read_all as param_rows
from .profiles import ProfileDB, to_int
from .registry import load_plugins
from . import journal
from .runner import Runner
from .safety import Safety
from .skills import SkillDB
from .storage import BackupStore, sha256
from . import target as target_mod

STEPS = 7

PREFLIGHT = [
    "Устройство обесточено и отключено от сети, конденсаторы разряжены.",
    "Адаптер работает на {pv} В (или на programmer_voltage из настроек плагина) — маркировка "
    "чипа сверена с даташитом (чипам на 1.8 В нужен переходник).",
    "Прищепка стоит ровно: красный провод = вывод 1 (точка или скос на корпусе чипа).",
    "Запись будет только после резервной копии и вашего подтверждения.",
]

TUNE_PREFLIGHT = [
    "Новые значения сверены с паспортом (руководством) прибора.",
    "Прибор можно ненадолго отвлечь: на время записи он может перезапуститься или пересчитать настройки.",
    "Запись будет только после копии текущих настроек и вашего подтверждения.",
]

NOT_FOUND_HINTS = [
    "Проверьте ориентацию прищепки (вывод 1) и контакт всех зубьев.",
    "Чип на плате может отдавать питание всей схеме: попробуйте удерживать процессор "
    "платы в сбросе или выпаять чип.",
    "Проверьте, что адаптер виден системе: lsusb (CH341A = 1a86:5512).",
    "Для учёбы без железа запустите с флагом --sim.",
]

OUTCOMES = {
    "detected": "опознано",
    "not_detected": "ничего не опознано",
    "no_profile": "нет профиля — создан черновик",
    "blocked_voltage": "остановлено: несовместимое или непроверенное напряжение",
    "read_failed": "не удалось прочитать чип",
    "healthy": "неисправностей не найдено",
    "no_action": "есть замечания, но нечем лечить (нужен эталон или образ)",
    "blocked": "запись заблокирована правилами безопасности",
    "dry_run": "пробный режим, ничего не записано",
    "cancelled": "отменено, чип не тронут",
    "written_verified": "записано и сверено, ждёт отзыва",
    "write_failed": "запись не прошла сверку",
    "repaired": "отремонтировано",
    "not_repaired": "записано, но не помогло",
    "already_same": "откат не нужен — на чипе уже эта копия",
    "rolled_back": "откат выполнен",
    "rollback_failed": "откат не удался",
    "interrupted": "прервано",
    "params_read": "параметры прочитаны",
    "no_params": "у профиля нет карты параметров",
    "no_change": "значения уже такие — запись не нужна",
    "tuned": "параметры записаны и сверены",
    "tune_failed": "параметры не записались (сверка не совпала)",
}


class Conductor:
    def __init__(self, cfg: dict, ui, sim: bool = False, dry_run: bool = False):
        self.cfg, self.ui, self.sim, self.dry_run = cfg, ui, sim, dry_run
        self.data_dir = Path(cfg["data_dir"])
        if sim:
            # у симулятора своя память, чтобы учебные эталоны не смешались с настоящими
            self.data_dir = self.data_dir / "simulator"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.session = None
        self.memory = Memory(self.data_dir / "memory.sqlite", self.data_dir / "logs")
        mirror = Path(cfg["mirror_dir"]) / ("simulator" if sim else "") if cfg.get("mirror_dir") else None
        self.store = BackupStore(self.data_dir / "backups", mirror)
        self.skills = SkillDB(cfg.get("skills_dir"))
        self.profiles = ProfileDB(cfg["profiles_dir"], self.skills.profile_dirs())
        self.safety = Safety(cfg["safety"])
        self.learning = Learning(self)
        self.runner = Runner(self.log, self._keep_output)
        self._versions = set()           # (сеанс, лезвие): версия уже записана
        self.target = None               # профиль модели, выбранный человеком (select)
        self.inspected = []              # находки Plugin.inspect текущего сеанса
        self._measured = {}              # лезвие → замеренное напряжение цели, В (текущий сеанс)
        self.only = None                 # опрашивать только эти лезвия (выбран переходник)
        self.preflight = None            # строки «Подготовки» из карточки переходника
        self.plugins, self.plugin_report = load_plugins(self, cfg, sim, self.skills.active)
        for path, err in self.profiles.errors:
            ui.warn(f"профиль {Path(path).name} пропущен: {err}")

    def select(self, target: str | None = None, plugins=None, preflight=None, source: str = "") -> None:
        """Выбор перед сеансом: модель (id профиля) и/или лезвия переходника. Только настройки — железо
        не опрашивается (кроме available()).

        target — настройки лезвий берутся ещё и из profile.tool (core/target.py): config.yaml <
        config.tools.yaml < профиль. plugins — опрашивать только эти лезвия (source — чьи: название
        переходника). preflight — строки шага «Подготовка» (из карточки переходника)."""
        from .registry import discover
        overlay = None
        if target:
            prof = self.profiles.get(target)
            if prof is None:
                raise ValueError(f"нет профиля «{target}» — список: python3 -m dirizher profiles")
            classes, _ = discover(self.skills.active)
            errors = target_mod.check(prof, classes)
            if errors:
                raise ValueError(f"в профиле «{target}» ошибки в поле tool: " + "; ".join(errors))
            overlay = target_mod.overlay(prof, classes)
            self.target = prof
        self.only = list(plugins) if plugins is not None else None
        self.preflight = list(preflight) if preflight else None
        why = f"выбран переходник «{source}»" if source else "выбран другой инструмент"
        self.plugins, self.plugin_report = load_plugins(self, self.cfg, self.sim, self.skills.active,
                                                        overlay=overlay, only=self.only, only_why=why)

    def _selection_note(self) -> None:
        """В журнал и человеку — что выбрано перед сеансом."""
        if self.only is not None:
            names = ", ".join(self.only) or "—"
            self.log(f"опрашиваются только лезвия переходника: {names}", data={"only": self.only})
            if not self.plugins:
                self.ui.warn(f"у выбранного переходника нет готовых лезвий ({names}): "
                             f"«Прибор» → «Инструменты» → включить и настроить")
        if self.target is not None:
            self.log(f"модель выбрана человеком: {self.target.id}", data={"target": self.target.id})

    # ================= служебное =================
    def log(self, message: str, level: str = "info", data: dict | None = None) -> None:
        if self.session is not None:
            self.memory.log(self.session, level, message, data)

    def _keep_output(self, res, keep_output: bool = True) -> dict:
        """Полный вывод команды — отдельным файлом рядом с журналом сеанса (для разработки).

        Дампы сюда не попадают: плагины читают чип во временные файлы, а вывод с данными
        чипа помечают keep_output=False. Файлы только добавляются, ничего не переписывается.
        """
        if self.session is None:
            return {}
        rel = journal.save_command(self.memory.logs_dir, self.session, res, keep_output)
        return {"file": rel}

    def _log_version(self, plugin) -> None:
        """Версия программы-движка — один раз за сеанс, только в журнал (не в отпечаток)."""
        key = (self.session, plugin.name)
        if self.session is None or key in self._versions:
            return
        self._versions.add(key)
        try:
            ver = plugin.version()
        except Exception as e:                       # версия — сведение, сеанс не роняет
            ver, err = None, str(e)
        else:
            err = None
        text = f"версия {ver}" if ver else "версия не определена"
        self.log(f"инструмент: {plugin.title} ({plugin.name}), {text}",
                 data={"tool": plugin.name, "version": ver, "settings": dict(plugin.settings or {}),
                       **({"error": err} if err else {})})

    def _start(self, kind: str, parent: int | None = None) -> int:
        self.session = self.memory.start_session(kind, parent)
        self.inspected, self._measured = [], {}
        mode = (" — пробный режим" if self.dry_run else "") + (" — симулятор" if self.sim else "")
        self.log(f"начат сеанс «{kind}»{mode}")
        return self.session

    def _finish(self, outcome: str) -> int:
        self.memory.update_session(self.session, finished=now(), outcome=outcome)
        self.log(f"итог: {outcome}")
        log_path = self.memory.logs_dir / f"session_{self.session:05d}.log"
        self.ui.info(f"Сеанс №{self.session}: {OUTCOMES.get(outcome, outcome)}. Журнал: {log_path}")
        return self.session

    def close(self) -> None:
        self.memory.close()

    def interrupted(self) -> None:
        if self.session is not None:
            s = self.memory.session(self.session)
            if s is not None and not s["outcome"]:
                self._finish("interrupted")

    def plugin(self, name: str):
        for p in self.plugins:
            if p.name == name:
                return p
        raise KeyError(f"плагин «{name}» не активен")

    def _cli(self, args: str) -> str:
        """Готовая команда для подсказки (в учебном режиме — с --sim)."""
        return "python3 -m dirizher " + ("--sim " if self.sim else "") + args

    @staticmethod
    def _recipe_line(r: dict, s: int, f: int) -> str:
        if s + f == 0:
            return f"{r['title']} — ещё не применялся"
        return f"{r['title']} — проверено: успешно {s}, неудачно {f}"

    # ================= шаги =================
    def detect(self):
        """Опросить все активные плагины (только чтение) и выбрать лучшее совпадение."""
        dets = []
        for p in self.plugins:
            try:
                d = p.probe()
            except Exception as e:
                self.ui.warn(f"{p.title}: ошибка опознания — {e}")
                self.log(f"{p.name}: ошибка опознания: {e}", "error")
                continue
            if d is None:
                self.log(f"{p.name}: ничего не найдено")
                continue
            d.profile, d.profile_score = self.profiles.best_for(d)
            self.log(f"{p.name}: {d.summary}; профиль: {d.profile.id if d.profile else '—'}; "
                     f"уверенность {d.score}", data={"fingerprint": d.fingerprint})
            dets.append(d)
        if not dets:
            return None
        dets.sort(key=lambda d: d.score, reverse=True)
        best = dets[0]
        threshold = float(self.cfg["safety"]["confidence_threshold"])
        ambiguous = len(dets) > 1 and dets[1].score >= best.score * 0.8
        if ambiguous or best.confidence < threshold:
            idx = self.ui.choose("Опознание неуверенное. Что из этого подключено?",
                                 [f"{d.summary} — {d.plugin}, уверенность {d.score}" for d in dets])
            if idx is None:
                return None
            best = dets[idx]
            self.log(f"выбрано человеком: {best.summary}")
        return best

    def _record_detection(self, det) -> None:
        if self.target is not None:
            self.ui.info(f"модель выбрана вами: {self.target.name} — по проводам её не узнать, "
                         f"выбор проверьте по маркировке чипа")
            if det.profile is not None and det.profile.id != self.target.id \
                    and det.profile.target_class == self.target.target_class:
                self.ui.warn(f"по отпечатку подошёл другой профиль: {det.profile.name}")
        self.memory.update_session(self.session, plugin=det.plugin, target_class=det.target_class,
                                   fingerprint=det.fingerprint,
                                   profile_id=det.profile.id if det.profile else None)
        try:
            self._log_version(self.plugin(det.plugin))
        except KeyError:
            pass

    def inspect(self, plugin, det) -> list:
        """Plugin.inspect: что ещё программа узнаёт только чтением (защита, фьюзы, ID, замер напряжения).

        Находки показываются и пишутся в журнал; «block» потом запрещает запись (write_blockers).
        Замер напряжения запоминается для шага «Напряжение». Ошибка здесь сеанс не роняет."""
        try:
            res = plugin.inspect(det)
        except Exception as e:                       # сведения — дополнение, сеанс продолжается
            self.log(f"{plugin.name}: дополнительная проверка не удалась: {e}", "error")
            res = [Finding("inspect", "info", f"дополнительные сведения от программы не получены: {e}")]
        if res is None:
            res = Inspection()
        elif isinstance(res, (list, tuple)):
            res = Inspection(findings=list(res))
        found = [f for f in res.findings if isinstance(f, Finding)]
        for f in found:
            self.ui.finding(f)
            self.log(f"[{f.status}] {f.check}: {f.message}", data={"inspect": plugin.name})
        if res.measured_voltage is not None:
            try:
                volts = float(res.measured_voltage)
            except (TypeError, ValueError):
                self.log(f"{plugin.name}: замер напряжения не разобран: {res.measured_voltage!r}", "error")
            else:
                self._measured[plugin.name] = volts
                self.log(f"замер напряжения цели: {volts:.3f} В ({plugin.title})",
                         data={"measured_voltage": volts, "tool": plugin.name})
        self.inspected = found
        return found

    def adapter_voltage(self, plugin=None) -> float:
        """Напряжение адаптера: своё у плагина (programmer_voltage в его настройках) или общее."""
        v = plugin.settings.get("programmer_voltage") if plugin is not None else None
        return float(v) if v is not None else self.safety.programmer_voltage

    def _voltage_gate(self, profile, plugin=None) -> bool:
        if plugin is not None and not getattr(plugin, "voltage_check", True):
            note = plugin.voltage_note or "проверка напряжения логики для этого инструмента не нужна"
            self.log(f"напряжение: не проверяется: {note}")
            self.ui.ok(note)
            return True
        pv = self.adapter_voltage(plugin)
        status, msg = self.safety.voltage_check(profile, pv)
        self.log(f"напряжение: {status}: {msg}")
        if status == "block":                        # прежний запрет — всегда запрет, замер его не снимает
            self.ui.error(msg)
            return False
        # Замер (Plugin.inspect) только добавляет: может остановить, но не отменяет вопрос и запрет выше.
        measured = self._measured.get(plugin.name) if plugin is not None else None
        m_msg = None
        if measured is not None:
            m_status, m_msg = self.safety.measured_check(profile, pv, measured)
            self.log(f"замер напряжения: {m_status}: {m_msg}", data={"measured_voltage": measured})
            if m_status == "block":
                self.ui.error(m_msg)
                return False
        if status == "warn":
            self.ui.warn(msg)
            answer = self.ui.yes_no(f"Вы проверили по даташиту, что чип рассчитан на {pv} В?")
            if answer != "yes":
                return False
        else:
            self.ui.ok(msg)
        if m_msg:
            self.ui.ok(m_msg)
        return True

    def backup(self, plugin, det, kind: str = "before") -> tuple:
        """Прочитать минимум дважды и сверить. Копия сохраняется в любом случае,
        но без совпадения чтений запись будет запрещена."""
        passes = max(2, int(self.cfg["safety"]["read_passes"]))
        first, hashes = None, []
        for i in range(passes):
            self.ui.info(f"чтение {i + 1} из {passes}…")
            data = plugin.read(det)
            hashes.append(sha256(data))
            if first is None:
                first = data
        verified = len(set(hashes)) == 1
        if not verified:
            self.ui.warn("чтения не совпали — пробую ещё раз…")
            hashes.append(sha256(plugin.read(det)))
        sha = self.store.put(first)
        self.memory.add_snapshot(self.session, kind, sha, len(first), verified)
        self.log(f"снимок «{kind}»: {sha[:16]}…, {len(first)} байт, подтверждён: {'да' if verified else 'НЕТ'}",
                 data={"hashes": hashes})
        if self.store.mirror_error:
            self.ui.warn(self.store.mirror_error)
        return first, sha, verified

    def find_reference(self, det, label: str | None = None):
        """Эталон для сравнения: сначала этого профиля, затем — с такого же чипа."""
        prof = det.profile
        rows = self.memory.references(det.chip_key())
        if label:
            rows = [r for r in rows if r["label"] == label]
        latest = {}
        for r in rows:                                  # свежие идут первыми
            latest.setdefault((r["profile_id"], r["label"]), r)
        options = list(latest.values())
        if not options:
            return None
        same = [r for r in options if r["profile_id"] == prof.id]
        if len(same) == 1:
            chosen = same[0]
        elif len(options) == 1:
            chosen = options[0]
        else:
            idx = self.ui.choose("Найдено несколько эталонов. С каким сравнивать?",
                                 [f"«{r['label']}» (профиль {r['profile_id']}, {r['ts']})" for r in options])
            if idx is None:
                return None
            chosen = options[idx]
        if chosen["profile_id"] != prof.id:
            answer = self.ui.yes_no(f"Есть эталон «{chosen['label']}» с такого же чипа (профиль "
                                    f"{chosen['profile_id']}). Это эталон от такой же модели устройства?")
            if answer != "yes":
                return None
        ref_prof = self.profiles.get(chosen["profile_id"])
        preserve = list(prof.preserve)
        if ref_prof is not None:
            preserve += [reg for reg in ref_prof.preserve if reg not in preserve]
        self.log(f"эталон: «{chosen['label']}» ({chosen['sha'][:16]}…)")
        return {"data": self.store.get(chosen["sha"]), "label": chosen["label"],
                "sha": chosen["sha"], "profile_id": chosen["profile_id"], "preserve": preserve}

    def auto_layout(self, det, data: bytes) -> list:
        """Карта разделов по содержимому копии «до» (core/layout_detect.py), если в профиле её нет.

        Находка: «ok» — разметка найдена и подставлена в профиль этого сеанса (preserve дополняется);
        «fail» — подпись есть, а таблица испорчена (разделы не определены, работа как без карты).
        Профиль в базе не меняется: сеанс получает копию с картой."""
        prof = det.profile
        if prof is None or prof.layout or not prof.layout_auto:
            return []
        res = layout_detect.detect(data, prof.layout_auto, prof.size or len(data))
        if res is None:
            return []
        if not res.ok:
            f = Finding("layout_auto", "fail",
                        f"{res.title}: подпись есть, но разметка испорчена — " + "; ".join(res.problems)
                        + ". Разделы не определены, работаю без карты.",
                        ["restore_reference", "restore_image"])
            self.ui.finding(f)
            self.log(f"[{f.status}] {f.check}: {f.message}")
            return [f]
        preserve = list(prof.preserve)
        have = {(to_int(r["offset"]), to_int(r["length"])) for r in preserve}
        for reg in layout_mod.preserve_regions(res.rows):
            if (reg["offset"], reg["length"]) not in have:
                preserve.append(reg)
        det.profile = dataclasses.replace(prof, layout=res.rows, preserve=preserve)
        kept = [r["title"] for r in res.rows if r["preserve"]]
        msg = (f"разметка по содержимому ({res.title}): разделов {len(res.rows)} — "
               + ", ".join(f"«{r['title']}»" for r in res.rows)
               + (f". Сохраняются с чипа: {', '.join(kept)}" if kept else "")
               + "".join(f". {n[0].upper()}{n[1:]}" for n in res.notes))
        f = Finding("layout_auto", "ok", msg)
        self.ui.finding(f)
        self.log(f"[{f.status}] {f.check}: {f.message}",
                 data={"layout_auto": {"detector": res.detector, "regions": [
                     {k: r[k] for k in ("id", "title", "kind", "offset", "end", "preserve", "restore")}
                     for r in res.rows]}})
        return [f]

    def diagnose(self, det, data: bytes, verified: bool, ref) -> list:
        findings = []
        specs = det.profile.check_specs(DEFAULT_CHECKS)
        if det.profile.layout and not any(sp.get("type") == "layout" for sp in specs):
            specs.append({"type": "layout"})        # есть карта разделов — проверка по ней всегда
        for spec in specs:
            name = str(spec.get("type"))
            fn = CHECKS.get(name)
            if fn is None:
                f = Finding(name, "warn", "неизвестная проверка в профиле — пропущена")
            else:
                try:
                    f = fn(CheckInput(data, verified, det.profile, ref, spec, self))
                except Exception as e:
                    f = Finding(name, "warn", f"проверка завершилась ошибкой: {e}")
            findings.append(f)
            self.ui.finding(f)
            self.log(f"[{f.status}] {f.check}: {f.message}")
        if det.profile.layout and ref is not None:
            rows = region_diff(CheckInput(data, verified, det.profile, ref, {}, self))
            if rows:                                # для подробностей сеанса (веб, мастер)
                self.log("разделы с отличиями от эталона: " + layout_mod.describe(rows),
                         data={"regions": [{k: r[k] for k in ("id", "title", "kind", "sectors", "restore")}
                                           for r in rows]})
        return findings

    def plan(self, det, findings: list, ref, image_path) -> tuple:
        wanted = []
        for f in findings:
            for rid in f.suggest:
                if rid not in wanted:
                    wanted.append(rid)
        if image_path and "restore_image" not in wanted:
            wanted.append("restore_image")
        candidates, hints = [], []
        for rid in wanted:
            r = det.profile.recipe(rid)
            if r is None:
                continue
            if r["action"] in ("write_reference", "write_reference_regions") and ref is None:
                hints.append("восстановить из эталона нельзя — эталона для этого устройства ещё нет")
                continue
            if r["action"] == "write_reference_regions" and not det.profile.layout:
                hints.append("восстановить по разделам нельзя — карта разделов не определилась")
                continue
            if r["action"] == "write_image" and not image_path:
                hints.append("можно записать официальный образ: запустите с --image путь/к/файлу.bin")
                continue
            candidates.append(r)
        return self.learning.rank(det.profile.id, candidates), hints

    def build_image(self, recipe: dict, det, current: bytes, ref, image_path) -> tuple:
        """Собрать образ для записи, сохранив уникальные области платы (MAC, серийник...)."""
        action = recipe["action"]
        if action == "write_reference":
            if ref is None:
                raise ValueError("эталона нет")
            image, source, preserve = bytearray(ref["data"]), f"эталон «{ref['label']}»", ref["preserve"]
        elif action == "write_reference_regions":
            return self._build_regions(det, current, ref)
        elif action == "write_image":
            path = Path(image_path).expanduser()
            if not path.is_file():
                raise ValueError(f"файл образа не найден: {path}")
            image, source, preserve = bytearray(path.read_bytes()), f"образ {path.name}", det.profile.preserve
        else:
            raise ValueError(f"неизвестное действие {action}")
        notes, warnings = [], []
        if len(image) != len(current):
            warnings.append(f"размер источника ({len(image)} байт) не равен размеру чипа ({len(current)} байт)")
            return bytes(image), source, notes, warnings
        for reg in preserve:
            off, ln = to_int(reg["offset"]), to_int(reg["length"])
            why = reg.get("why", "уникальные данные платы")
            cur = current[off:off + ln]
            if is_fill(cur, 0xFF) or is_fill(cur, 0x00):
                warnings.append(f"область 0x{off:X}+0x{ln:X} ({why}) на чипе пустая — будет взята из источника. "
                                f"Если источник с другой платы, эти данные окажутся чужими: поправьте их вручную.")
            else:
                image[off:off + ln] = cur
                notes.append(f"сохраняю с текущего чипа: {why} (0x{off:X}+0x{ln:X})")
        return bytes(image), source, notes, warnings

    def _build_regions(self, det, current: bytes, ref) -> tuple:
        """Частичное восстановление: текущий чип, а отличающиеся разделы restore: true — из эталона.
        Уникальное (preserve), настройки и место вне карты остаются с чипа."""
        if ref is None:
            raise ValueError("эталона нет")
        if not det.profile.layout:
            raise ValueError("в профиле нет карты разделов layout")
        preserve = ref["preserve"]
        # отличия считаются без уникальных областей: они остаются с чипа в любом случае
        _, taken, left = layout_mod.build(masked(current, preserve), masked(ref["data"], preserve),
                                          det.profile.layout)
        image = bytearray(current)
        for row in taken:
            image[row["offset"]:row["end"]] = ref["data"][row["offset"]:row["end"]]
        for reg in preserve:                        # старое поле preserve внутри раздела — тоже с чипа
            off, ln = to_int(reg["offset"]), to_int(reg["length"])
            image[off:off + ln] = current[off:off + ln]
        notes = [f"беру из эталона: «{r['title']}» ({layout_mod.span(r)}), отличалось "
                 f"{layout_mod.sectors_text(r['sectors'])}" for r in taken]
        warnings = [(f"не трогаю «{r['title']}» (отличается {layout_mod.sectors_text(r['sectors'])}): "
                     "раздел не берётся из эталона (restore: false)") if r["id"] else
                    (f"не трогаю место вне карты разделов (отличается {layout_mod.sectors_text(r['sectors'])}): "
                     "оно не описано в профиле") for r in left]
        if not taken:
            notes.append("отличий в восстанавливаемых разделах нет")
        source = (f"разделы из эталона «{ref['label']}»: " + ", ".join(f"«{r['title']}»" for r in taken)
                  if taken else f"эталон «{ref['label']}» (разделы не отличаются)")
        return bytes(image), source, notes, warnings

    def apply(self, plugin, det, image: bytes, source: str) -> bool:
        """Записать, прочитать обратно, сверить, сохранить копию «после»."""
        sha_img = self.store.put(image)
        self.memory.add_snapshot(self.session, "written", sha_img, len(image), True)
        self.log(f"запись: {source}, sha {sha_img[:16]}…")
        self.ui.info("запись… не отключайте питание и не прерывайте")
        try:
            plugin.write(det, image)
        except Exception as e:
            self.ui.error(f"запись не удалась: {e}")
            self.log(f"ошибка записи: {e}", "error")
            return False
        self.ui.info("сверка: читаю обратно…")
        try:
            after = plugin.read(det)
        except Exception as e:
            self.ui.error(f"не удалось прочитать для сверки: {e}")
            self.log(f"ошибка чтения при сверке: {e}", "error")
            return False
        ok = after == image
        sha_after = self.store.put(after)
        self.memory.add_snapshot(self.session, "after", sha_after, len(after), ok)
        if ok:
            self.ui.ok(f"записано и сверено, копия «после»: {sha_after[:12]}…")
        else:
            self.ui.error(f"сверка НЕ совпала: отличается {len(diff_sectors(after, image))} секторов")
        self.log(f"сверка: {'совпало' if ok else 'НЕ совпало'}; снимок «после» {sha_after[:16]}…")
        return ok

    def preflight_lines(self) -> list:
        """Строки «Подготовки»: из карточки переходника, иначе — общие у всех активных лезвий
        (Plugin.preflight), иначе — общий текст ядра (PREFLIGHT, про прищепку SPI-флешки)."""
        if self.preflight:
            return list(self.preflight)
        own = {tuple(p.preflight) if p.preflight else None for p in self.plugins}
        if len(own) == 1:
            only = next(iter(own))
            if only:
                return list(only)
        return list(PREFLIGHT)

    # ================= сценарии =================
    def run(self, image_path: str | None = None, label: str | None = None, target: str | None = None) -> int:
        if target:
            self.select(target=target, plugins=self.only, preflight=self.preflight)
        ui = self.ui
        sid = self._start("run")
        ui.header(f"Сеанс №{sid}" + (" — ПРОБНЫЙ РЕЖИМ, запись отключена" if self.dry_run else "")
                  + (" — симулятор" if self.sim else ""))

        ui.step(1, STEPS, "Подготовка: безопасность прежде всего")
        self._selection_note()
        for line in self.preflight_lines():
            ui.info("• " + line.format(pv=self.safety.programmer_voltage))
        ui.pause("Проверили? Нажмите Enter…")

        ui.step(2, STEPS, "Опознание (только чтение идентификаторов)")
        det = self.detect()
        if det is None:
            ui.error("ничего не опознано")
            for hint in NOT_FOUND_HINTS:
                ui.info("• " + hint)
            return self._finish("not_detected")
        self._record_detection(det)
        plugin = self.plugin(det.plugin)
        ui.ok(det.summary)
        ui.info(f"инструмент: {plugin.title}")
        if det.profile is None:
            draft = self.learning.record_unknown(det)
            ui.warn(f"профиля для такого устройства нет. Создан черновик: {draft}")
            ui.info("заполните его по даташиту и перенесите в profiles/ — тогда система сможет с ним работать")
            return self._finish("no_profile")
        if det.profile.is_generic:
            draft = self.learning.record_unknown(det)
            ui.warn(f"точного профиля нет — работаю по общему: «{det.profile.name}»")
            ui.info(f"черновик точного профиля (для обучения): {draft}")
        else:
            ui.ok(f"профиль: {det.profile.name}")
        inspected = self.inspect(plugin, det)

        ui.step(3, STEPS, "Совместимость по напряжению")
        if not self._voltage_gate(det.profile, plugin):
            return self._finish("blocked_voltage")

        ui.step(4, STEPS, "Резервная копия «до» (чтение с повторной сверкой)")
        try:
            data, before_sha, verified = self.backup(plugin, det, "before")
        except Exception as e:
            ui.error(f"не удалось прочитать чип: {e}")
            self.log(f"ошибка чтения: {e}", "error")
            return self._finish("read_failed")
        (ui.ok if verified else ui.warn)(f"копия сохранена: {before_sha[:12]}… ({len(data)} байт)"
                                         + ("" if verified else " — НЕ подтверждена"))
        board = self.profiles.refine(det, data)
        if board is not None and board.id != det.profile.id:
            previous = det.profile
            det.profile, det.profile_score = board, 1.0
            self.memory.update_session(sid, profile_id=board.id)
            ui.ok(f"по содержимому узнана плата: {board.name}")
            if board.voltage != previous.voltage and not self._voltage_gate(board, plugin):
                return self._finish("blocked_voltage")

        ui.step(5, STEPS, "Диагностика")
        auto = self.auto_layout(det, data)       # до эталона: preserve из найденной карты нужен и ему
        ref = self.find_reference(det, label)
        diag = auto + self.diagnose(det, data, verified, ref)
        findings = inspected + diag            # находки inspect — как диагностика: «block» запрещает запись

        ui.step(6, STEPS, "План ремонта")
        ranked, hints = self.plan(det, findings, ref, image_path)
        if not ranked:
            for hint in hints:
                ui.info("• " + hint)
            if any(f.status == "block" for f in findings):
                ui.error("есть блокирующие проблемы — устраните их и запустите снова")
                return self._finish("blocked")
            # замечания inspect (защита записи, шифрование…) — сведения об устройстве, а не о прошивке
            if any(f.status in ("warn", "fail") for f in diag):
                ui.warn("есть замечания, но подходящего способа ремонта сейчас нет")
                return self._finish("no_action")
            ui.ok("неисправностей в прошивке не найдено")
            self._offer_reference(det, before_sha, len(data), ref, label)
            return self._finish("healthy")

        if len(ranked) == 1:
            choice = ranked[0]
        else:
            idx = ui.choose("Что сделать? (выше — способы, которые чаще помогали)",
                            [self._recipe_line(*x) for x in ranked])
            if idx is None:
                return self._finish("cancelled")
            choice = ranked[idx]
        recipe = choice[0]
        ui.info(f"способ: {self._recipe_line(*choice)}")
        try:
            image, source, notes, warnings = self.build_image(recipe, det, data, ref, image_path)
        except Exception as e:
            ui.error(f"не удалось подготовить образ: {e}")
            return self._finish("blocked")
        changed = diff_sectors(data, image)
        ui.info(f"источник: {source}")
        ui.info(f"изменится секторов по 4 КБ: {len(changed)} из {(len(data) + 4095) // 4096}")
        for n in notes:
            ui.info("• " + n)
        for w in warnings:
            ui.warn(w)
        blockers = self.safety.write_blockers(verified=verified, findings=findings, profile=det.profile,
                                              image=image, chip_size=len(data), dry_run=self.dry_run)
        if blockers:
            only_dry = self.dry_run and len(blockers) == 1
            for b in blockers:
                (ui.info if only_dry else ui.error)("• " + b)
            self.log("запись не выполняется: " + "; ".join(blockers))
            if only_dry:
                ui.ok("пробный режим: план показан, ничего не записано. Уберите --dry-run, чтобы выполнить.")
                return self._finish("dry_run")
            return self._finish("blocked")
        if not changed:
            ui.ok("на чипе уже этот образ — запись не нужна")
            return self._finish("healthy")

        ui.step(7, STEPS, "Подтверждение, запись и сверка")
        if not self.safety.confirm(ui, f"Записать {source} в «{det.summary}»? "
                                       f"Копия «до» сохранена: {before_sha[:12]}…"):
            ui.info("отменено, чип не тронут")
            return self._finish("cancelled")
        self.memory.update_session(sid, recipe_id=recipe["id"])
        if not self.apply(plugin, det, image, source):
            self.learning.result(det.profile.id, recipe["id"], ok=False)
            if ui.yes_no("Вернуть на чип копию «до»?") == "yes":
                self.apply(plugin, det, data, "копия «до» (возврат)")
            return self._finish("write_failed")
        ui.ok("готово. Теперь проверьте устройство вживую.")
        answer = ui.yes_no("Устройство заработало?", allow_later=True)
        if answer == "later":
            ui.info(f"когда проверите: {self._cli(f'feedback {sid} --ok')}   (или --fail)")
            return self._finish("written_verified")
        self.feedback(sid, answer == "yes")
        return sid

    def _offer_reference(self, det, sha: str, size: int, ref, label: str | None) -> None:
        if ref is not None and (label is None or ref["label"] == label):
            return
        lbl = label or det.profile.id
        answer = self.ui.yes_no(f"Эталона «{lbl}» ещё нет. Устройство сейчас исправно работает? "
                                f"Если да — сохраню этот дамп как эталон.", allow_later=True)
        if answer == "yes":
            self.memory.add_reference(det.profile.id, lbl, det.chip_key(), sha, size, self.session,
                                      "исправное устройство")
            self.log(f"сохранён эталон «{lbl}»: {sha[:16]}…")
            self.ui.ok(f"эталон «{lbl}» сохранён — с ним будут сравниваться такие же устройства")
            self._board_draft(det.profile, det.target_class, sha)
        elif answer == "later":
            self.ui.info(f"сохранить позже: {self._cli(f'reference {self.session} --kind before')}")

    def feedback(self, sid: int, ok: bool) -> None:
        """Отзыв после проверки устройства вживую — это и есть обучение."""
        s = self.memory.session(sid)
        if s is None:
            self.ui.error(f"сеанса №{sid} нет")
            return
        if not s["recipe_id"] or not s["profile_id"]:
            self.ui.error("в этом сеансе ремонта не было — отзыв не нужен")
            return
        self.learning.result(s["profile_id"], s["recipe_id"], ok)
        outcome = "repaired" if ok else "not_repaired"
        self.memory.update_session(sid, outcome=outcome, finished=s["finished"] or now())
        self.memory.log(sid, "info", f"отзыв: {'заработало' if ok else 'не помогло'}")
        if ok:
            self.ui.ok("записал успех — этот способ будет предлагаться первым")
            fp = json.loads(s["fingerprint"] or "{}")
            key = chip_key(s["target_class"], fp)
            has_ref = bool(self.memory.references(key))
            prof = self.profiles.get(s["profile_id"])
            recipe = prof.recipe(s["recipe_id"]) if prof else None
            after = self.memory.snapshot(sid, "after")
            new_firmware = recipe is not None and recipe["action"] == "write_image"
            if after is not None and after["verified"] and (not has_ref or new_firmware):
                if self.ui.yes_no("Сохранить записанную прошивку как эталон для будущих ремонтов?") == "yes":
                    self.memory.add_reference(s["profile_id"], s["profile_id"], key, after["sha"],
                                              after["size"], sid, "после успешного ремонта")
                    self.ui.ok("эталон сохранён")
                    self._board_draft(prof, s["target_class"], after["sha"])
        else:
            self.ui.warn("записал неудачу — этот способ теперь будет ниже в списке")
            self.ui.info(f"вернуть копию «до»: {self._cli(f'rollback {sid}')}")
        self.ui.info(f"Сеанс №{sid}: {OUTCOMES[outcome]}")

    def rollback(self, target_sid: int):
        """Вернуть на чип копию «до» из указанного сеанса. Сам откат тоже можно отменить."""
        ui = self.ui
        src = self.memory.session(target_sid)
        before = self.memory.snapshot(target_sid, "before")
        if src is None or before is None:
            ui.error(f"у сеанса №{target_sid} нет копии «до»")
            return None
        data_before = self.store.get(before["sha"])
        sid = self._start("rollback", parent=target_sid)
        ui.header(f"Сеанс №{sid}: откат к копии «до» из сеанса №{target_sid}"
                  + (" — ПРОБНЫЙ РЕЖИМ" if self.dry_run else ""))

        ui.step(1, 5, "Опознание")
        det = self.detect()
        if det is None:
            ui.error("ничего не опознано")
            return self._finish("not_detected")
        self._record_detection(det)
        now_fp = json.dumps(det.fingerprint, ensure_ascii=False, sort_keys=True)
        if now_fp != (src["fingerprint"] or ""):
            ui.error("подключено не то устройство, что было в том сеансе — откат запрещён")
            ui.info(f"было:  {src['fingerprint']}")
            ui.info(f"сейчас: {now_fp}")
            return self._finish("blocked")
        plugin = self.plugin(det.plugin)
        inspected = self.inspect(plugin, det)

        ui.step(2, 5, "Совместимость по напряжению")
        if det.profile is None or not self._voltage_gate(det.profile, plugin):
            return self._finish("blocked_voltage")

        ui.step(3, 5, "Копия текущего состояния (чтобы откат тоже можно было отменить)")
        data, sha, verified = self.backup(plugin, det, "before")
        if sha == before["sha"]:
            ui.ok("на чипе уже эта копия")
            return self._finish("already_same")

        ui.step(4, 5, "Проверка условий записи")
        blockers = self.safety.write_blockers(verified=verified, findings=inspected, profile=None,
                                              image=data_before, chip_size=len(data), dry_run=self.dry_run)
        if blockers:
            for b in blockers:
                ui.error("• " + b)
            return self._finish("dry_run" if self.dry_run and len(blockers) == 1 else "blocked")
        ui.ok("условия выполнены")

        ui.step(5, 5, "Подтверждение, запись и сверка")
        if not self.safety.confirm(ui, f"Вернуть на чип копию «до» из сеанса №{target_sid} "
                                       f"({before['sha'][:12]}…)?"):
            ui.info("отменено, чип не тронут")
            return self._finish("cancelled")
        ok = self.apply(plugin, det, data_before, f"копия «до» из сеанса №{target_sid}")
        return self._finish("rolled_back" if ok else "rollback_failed")

    # ================= параметры и настройка =================
    def _params_target(self, first_step: int, total: int) -> tuple:
        """Опознание и напряжение для «Параметров» и «Настройки».

        Возвращает (плагин, опознание, None) или (None, None, итог остановки)."""
        ui = self.ui
        self._selection_note()
        ui.step(first_step, total, "Опознание (только чтение идентификаторов)")
        det = self.detect()
        if det is None:
            ui.error("ничего не опознано")
            for hint in NOT_FOUND_HINTS:
                ui.info("• " + hint)
            return None, None, "not_detected"
        self._record_detection(det)
        plugin = self.plugin(det.plugin)
        ui.ok(det.summary)
        ui.info(f"инструмент: {plugin.title}")
        if det.profile is None:
            draft = self.learning.record_unknown(det)
            ui.warn(f"профиля для такого устройства нет. Создан черновик: {draft}")
        # карта может быть у профиля чипа или у профиля платы (узнаётся после чтения)
        if not any(p.params for p in self.profiles.profiles if p.match_score(det) > 0):
            ui.warn("для этого устройства нет карты параметров. Добавьте её: «Навыки» → «+» → "
                    "«Карта параметров» (справка «Параметры прибора»)")
            return None, None, "no_params"
        if det.profile is not None:
            ui.ok(f"профиль: {det.profile.name}")
        self.inspect(plugin, det)
        ui.step(first_step + 1, total, "Совместимость по напряжению")
        if not self._voltage_gate(det.profile, plugin):
            return None, None, "blocked_voltage"
        return plugin, det, None

    def _refine_for_params(self, det, data: bytes, plugin) -> str | None:
        """Уточнить профиль платы по содержимому. Возвращает итог остановки или None."""
        board = self.profiles.refine(det, data)
        if board is not None and board.id != det.profile.id:
            previous = det.profile
            det.profile, det.profile_score = board, 1.0
            self.memory.update_session(self.session, profile_id=board.id)
            self.ui.ok(f"по содержимому узнана плата: {board.name}")
            if board.voltage != previous.voltage and not self._voltage_gate(board, plugin):
                return "blocked_voltage"
        if not det.profile.params:
            self.ui.warn(f"у профиля «{det.profile.name}» нет карты параметров")
            return "no_params"
        return None

    def show_params(self, profile, data: bytes) -> None:
        for row in param_rows(data, profile.params):
            self.ui.info(f"{row['title']}: {row['text']}" + ("" if row["writable"] else "  (только чтение)"))

    def read_params(self, target: str | None = None) -> int:
        """Прочитать параметры прибора и показать их. Только чтение."""
        if target:
            self.select(target=target, plugins=self.only, preflight=self.preflight)
        ui = self.ui
        sid = self._start("params")
        ui.header(f"Сеанс №{sid}: параметры прибора (только чтение)" + (" — симулятор" if self.sim else ""))
        plugin, det, stop = self._params_target(1, 3)
        if stop:
            return self._finish(stop)
        ui.step(3, 3, "Чтение параметров (копия с повторной сверкой)")
        try:
            data, sha, verified = self.backup(plugin, det, "before")
        except Exception as e:
            ui.error(f"не удалось прочитать: {e}")
            self.log(f"ошибка чтения: {e}", "error")
            return self._finish("read_failed")
        stop = self._refine_for_params(det, data, plugin)
        if stop:
            return self._finish(stop)
        self.show_params(det.profile, data)
        if not verified:
            ui.warn("повторные чтения не совпали — значения могут быть неточными, запись будет заблокирована")
        return self._finish("params_read")

    def tune(self, changes: dict, target: str | None = None) -> int:
        """Изменить параметры прибора: копия «до» → план «было → стало» → «ДА» → запись и сверка."""
        if not changes:
            raise ValueError("не задано ни одного нового значения")
        if target:
            self.select(target=target, plugins=self.only, preflight=self.preflight)
        ui = self.ui
        total = 6
        sid = self._start("tune")
        ui.header(f"Сеанс №{sid}: настройка параметров" + (" — ПРОБНЫЙ РЕЖИМ, запись отключена" if self.dry_run else "")
                  + (" — симулятор" if self.sim else ""))
        self.log("запрошены значения", data={"changes": {k: str(v) for k, v in changes.items()}})

        ui.step(1, total, "Подготовка")
        for line in TUNE_PREFLIGHT:
            ui.info("• " + line)
        ui.pause("Проверили? Нажмите Enter…")

        plugin, det, stop = self._params_target(2, total)
        if stop:
            return self._finish(stop)

        ui.step(4, total, "Копия текущих настроек «до» (чтение с повторной сверкой)")
        try:
            data, before_sha, verified = self.backup(plugin, det, "before")
        except Exception as e:
            ui.error(f"не удалось прочитать: {e}")
            self.log(f"ошибка чтения: {e}", "error")
            return self._finish("read_failed")
        (ui.ok if verified else ui.warn)(f"копия сохранена: {before_sha[:12]}… ({len(data)} байт)"
                                         + ("" if verified else " — НЕ подтверждена"))
        stop = self._refine_for_params(det, data, plugin)
        if stop:
            return self._finish(stop)

        ui.step(5, total, "План: что изменится")
        image, lines, errors = apply_changes(data, det.profile.params, changes)
        if errors:
            for e in errors:
                ui.error("• " + e)
            self.log("значения отклонены: " + "; ".join(errors))
            return self._finish("blocked")
        if image == data:
            ui.ok("на приборе уже эти значения — запись не нужна")
            return self._finish("no_change")
        for line in lines:
            ui.info("• " + line)
        blockers = self.safety.write_blockers(verified=verified, findings=self.inspected, profile=det.profile,
                                              image=image, chip_size=len(data), dry_run=self.dry_run)
        if blockers:
            only_dry = self.dry_run and len(blockers) == 1
            for b in blockers:
                (ui.info if only_dry else ui.error)("• " + b)
            self.log("запись не выполняется: " + "; ".join(blockers))
            if only_dry:
                ui.ok("пробный режим: план показан, ничего не записано.")
                return self._finish("dry_run")
            return self._finish("blocked")

        ui.step(6, total, "Подтверждение, запись и сверка")
        if not self.safety.confirm(ui, f"Записать в «{det.summary}»: " + "; ".join(lines)
                                   + f". Копия «до» сохранена: {before_sha[:12]}…"):
            ui.info("отменено, прибор не тронут")
            return self._finish("cancelled")
        self.log("изменения: " + "; ".join(lines))
        if not self.apply(plugin, det, image, "параметры: " + ", ".join(changes)):
            if ui.yes_no("Вернуть прежние настройки (копию «до»)?") == "yes":
                self.apply(plugin, det, data, "копия «до» (возврат)")
            return self._finish("tune_failed")
        ui.ok("новые значения записаны и сверены")
        self.show_params(det.profile, image)
        return self._finish("tuned")

    def detect_only(self, target: str | None = None) -> int:
        if target:
            self.select(target=target, plugins=self.only, preflight=self.preflight)
        self._start("detect")
        self.ui.header(f"Сеанс №{self.session}: опознание (только чтение)")
        self._selection_note()
        det = self.detect()
        if det is None:
            self.ui.error("ничего не опознано")
            for hint in NOT_FOUND_HINTS:
                self.ui.info("• " + hint)
            return self._finish("not_detected")
        self._record_detection(det)
        self.ui.ok(det.summary)
        for k, v in det.fingerprint.items():
            self.ui.info(f"{k}: {v}")
        if det.profile is not None:
            self.ui.info(f"профиль: {det.profile.name} ({det.profile.id}), точность совпадения {det.profile_score}")
        if det.profile is None or det.profile.is_generic:
            draft = self.learning.record_unknown(det)
            self.ui.warn(f"точного профиля нет. Черновик: {draft}")
        return self._finish("detected")

    def mark_reference(self, sid: int, kind: str | None = None, label: str | None = None) -> None:
        s = self.memory.session(sid)
        if s is None or not s["profile_id"]:
            self.ui.error(f"в сеансе №{sid} нет опознанного устройства")
            return
        snap = (self.memory.snapshot(sid, kind) if kind
                else self.memory.snapshot(sid, "after") or self.memory.snapshot(sid, "before"))
        if snap is None:
            self.ui.error("в сеансе нет такого снимка")
            return
        if not snap["verified"]:
            self.ui.error("снимок не подтверждён повторным чтением — эталоном быть не может")
            return
        fp = json.loads(s["fingerprint"] or "{}")
        lbl = label or s["profile_id"]
        self.memory.add_reference(s["profile_id"], lbl, chip_key(s["target_class"], fp), snap["sha"],
                                  snap["size"], sid, f"вручную из снимка «{snap['kind']}»")
        self.ui.ok(f"эталон «{lbl}» сохранён из снимка «{snap['kind']}» сеанса №{sid}")
        self._board_draft(self.profiles.get(s["profile_id"]), s["target_class"], snap["sha"])

    def _board_draft(self, profile, target_class: str, sha: str) -> None:
        """После сохранения эталона: черновик профиля ПЛАТЫ в data/drafts/ (подпись, разметка, binwalk).
        Ошибка черновика не мешает эталону — он уже сохранён."""
        if profile is None or profile.content_match:
            return                                  # плата уже узнаётся своим профилем
        try:
            path = self.learning.record_board(profile, target_class, self.store.get(sha), sha)
        except Exception as e:
            self.log(f"черновик профиля платы не создан: {e}", "warn")
            return
        if path is not None:
            self.ui.info(f"черновик профиля платы (проверьте и перенесите в profiles/boards/): {path}")

    def export(self, sid: int, kind: str, dest: str) -> None:
        snap = self.memory.snapshot(sid, kind)
        if snap is None:
            self.ui.error(f"в сеансе №{sid} нет снимка «{kind}»")
            return
        path = self.store.export(snap["sha"], Path(dest).expanduser())
        self.ui.ok(f"сохранено: {path} ({snap['size']} байт, sha256 {snap['sha']})")
