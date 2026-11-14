"""Правила безопасности. Здесь решается, можно ли вообще что-то записывать.

Запись разрешена только если выполнены ВСЕ условия:
  1. есть копия «до», подтверждённая повторным чтением;
  2. нет блокирующих находок диагностики;
  3. напряжение чипа совместимо с адаптером;
  4. размер образа равен размеру чипа;
  5. профиль разрешает запись;
  6. не включён пробный режим (--dry-run);
  7. человек ввёл слово подтверждения.
"""
from __future__ import annotations


class Safety:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def programmer_voltage(self) -> float:
        return float(self.cfg["programmer_voltage"])

    def voltage_check(self, profile) -> tuple:
        """Возвращает ("ok" | "warn" | "block", сообщение)."""
        pv = self.programmer_voltage
        tol = float(self.cfg.get("voltage_tolerance", 0.3))
        v = profile.voltage if profile else None
        if v is None:
            return "warn", (f"напряжение чипа в профиле не указано. Проверьте маркировку по даташиту: "
                            f"адаптер работает на {pv} В.")
        if abs(v - pv) > tol:
            return "block", (f"чип рассчитан на {v} В, а адаптер — на {pv} В. Работа остановлена, "
                             f"чтобы не повредить чип: нужен переходник на {v} В "
                             f"(и смените programmer_voltage в config.yaml).")
        return "ok", f"напряжение совместимо: чип {v} В, адаптер {pv} В"

    def write_blockers(self, *, verified: bool, findings: list, profile, image: bytes,
                       chip_size: int, dry_run: bool) -> list:
        reasons = []
        if not verified:
            reasons.append("копия «до» не подтверждена повторным чтением — добейтесь стабильного чтения")
        for f in findings:
            if f.status == "block" and f.check != "stable_read":
                reasons.append(f"блокирующая проблема: {f.message}")
        if profile is not None and not profile.allow_write:
            reasons.append(f"в профиле «{profile.name}» запись запрещена (allow_write: false)")
        if chip_size and len(image) != chip_size:
            reasons.append(f"размер образа {len(image)} байт не равен размеру чипа {chip_size} байт")
        if dry_run:
            reasons.append("пробный режим (--dry-run): запись отключена")
        return reasons

    def confirm(self, ui, text: str) -> bool:
        return ui.confirm_word(text, str(self.cfg.get("confirm_word", "ДА")))
