"""Дирижёр: единый сценарий для любого «лезвия».

  1 подготовка → 2 опознание → 3 напряжение → 4 копия «до» → 5 диагностика
  → 6 план ремонта → 7 подтверждение, запись, сверка, копия «после» → отзыв (обучение)

Ядро не знает ничего о конкретных чипах: «как подключиться» знают плагины,
«что это и как лечить» — профили.
"""
from __future__ import annotations

import json
from pathlib import Path

from .checks import CHECKS, DEFAULT_CHECKS, CheckInput, diff_sectors, is_fill
from .learning import Learning
from .memory import Memory, now
from .models import Finding, chip_key
from .profiles import ProfileDB, to_int
from .registry import load_plugins
from .runner import Runner
from .safety import Safety
from .storage import BackupStore, sha256

STEPS = 7

PREFLIGHT = [
    "Устройство обесточено и отключено от сети, конденсаторы разряжены.",
    "Адаптер работает на {pv} В (или на programmer_voltage из настроек плагина) — маркировка "
    "чипа сверена с даташитом (чипам на 1.8 В нужен переходник).",
    "Прищепка стоит ровно: красный провод = вывод 1 (точка или скос на корпусе чипа).",
    "Запись будет только после резервной копии и вашего подтверждения.",
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
        self.profiles = ProfileDB(cfg["profiles_dir"])
        self.safety = Safety(cfg["safety"])
        self.learning = Learning(self)
        self.runner = Runner(self.log)
        self.plugins, self.plugin_report = load_plugins(self, cfg, sim)
        for path, err in self.profiles.errors:
            ui.warn(f"профиль {Path(path).name} пропущен: {err}")

    # ================= служебное =================
    def log(self, message: str, level: str = "info", data: dict | None = None) -> None:
        if self.session is not None:
            self.memory.log(self.session, level, message, data)

    def _start(self, kind: str, parent: int | None = None) -> int:
        self.session = self.memory.start_session(kind, parent)
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
        self.memory.update_session(self.session, plugin=det.plugin, target_class=det.target_class,
                                   fingerprint=det.fingerprint,
                                   profile_id=det.profile.id if det.profile else None)

    def adapter_voltage(self, plugin=None) -> float:
        """Напряжение адаптера: своё у плагина (programmer_voltage в его настройках) или общее."""
        v = plugin.settings.get("programmer_voltage") if plugin is not None else None
        return float(v) if v is not None else self.safety.programmer_voltage

    def _voltage_gate(self, profile, plugin=None) -> bool:
        pv = self.adapter_voltage(plugin)
        status, msg = self.safety.voltage_check(profile, pv)
        self.log(f"напряжение: {status}: {msg}")
        if status == "block":
            self.ui.error(msg)
            return False
        if status == "warn":
            self.ui.warn(msg)
            answer = self.ui.yes_no(f"Вы проверили по даташиту, что чип рассчитан на {pv} В?")
            return answer == "yes"
        self.ui.ok(msg)
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

    def diagnose(self, det, data: bytes, verified: bool, ref) -> list:
        findings = []
        for spec in det.profile.check_specs(DEFAULT_CHECKS):
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
            if r["action"] == "write_reference" and ref is None:
                hints.append("восстановить из эталона нельзя — эталона для этого устройства ещё нет")
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

    # ================= сценарии =================
    def run(self, image_path: str | None = None, label: str | None = None) -> int:
        ui = self.ui
        sid = self._start("run")
        ui.header(f"Сеанс №{sid}" + (" — ПРОБНЫЙ РЕЖИМ, запись отключена" if self.dry_run else "")
                  + (" — симулятор" if self.sim else ""))

        ui.step(1, STEPS, "Подготовка: безопасность прежде всего")
        for line in PREFLIGHT:
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
        ref = self.find_reference(det, label)
        findings = self.diagnose(det, data, verified, ref)

        ui.step(6, STEPS, "План ремонта")
        ranked, hints = self.plan(det, findings, ref, image_path)
        if not ranked:
            for hint in hints:
                ui.info("• " + hint)
            if any(f.status == "block" for f in findings):
                ui.error("есть блокирующие проблемы — устраните их и запустите снова")
                return self._finish("blocked")
            if any(f.status in ("warn", "fail") for f in findings):
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

        ui.step(2, 5, "Совместимость по напряжению")
        if det.profile is None or not self._voltage_gate(det.profile, plugin):
            return self._finish("blocked_voltage")

        ui.step(3, 5, "Копия текущего состояния (чтобы откат тоже можно было отменить)")
        data, sha, verified = self.backup(plugin, det, "before")
        if sha == before["sha"]:
            ui.ok("на чипе уже эта копия")
            return self._finish("already_same")

        ui.step(4, 5, "Проверка условий записи")
        blockers = self.safety.write_blockers(verified=verified, findings=[], profile=None,
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

    def detect_only(self) -> int:
        self._start("detect")
        self.ui.header(f"Сеанс №{self.session}: опознание (только чтение)")
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

    def export(self, sid: int, kind: str, dest: str) -> None:
        snap = self.memory.snapshot(sid, kind)
        if snap is None:
            self.ui.error(f"в сеансе №{sid} нет снимка «{kind}»")
            return
        path = self.store.export(snap["sha"], Path(dest).expanduser())
        self.ui.ok(f"сохранено: {path} ({snap['size']} байт, sha256 {snap['sha']})")
