"""Правила безопасности. Здесь решается, можно ли вообще что-то записывать.

Запись разрешена только если выполнены ВСЕ условия:
  1. есть копия «до», подтверждённая повторным чтением;
  2. нет блокирующих находок диагностики;
  3. напряжение чипа совместимо с адаптером (и с замером, если лезвие мерит напряжение цели);
  4. размер образа равен размеру чипа;
  5. профиль разрешает запись;
  6. не включён пробный режим (--dry-run);
  7. человек ввёл слово подтверждения.
"""
from __future__ import annotations


NO_POWER_V = 0.5          # ниже — «на цели нет питания»


def volts(x) -> str:
    """3.3 → «3,3», 3.241 → «3,24», 5.0 → «5»."""
    return f"{float(x):.2f}".rstrip("0").rstrip(".").replace(".", ",")


class Safety:
    def __init__(self, cfg: dict):
        self.cfg = cfg

    @property
    def programmer_voltage(self) -> float:
        return float(self.cfg["programmer_voltage"])

    def voltage_check(self, profile, programmer_voltage: float | None = None) -> tuple:
        """Возвращает ("ok" | "warn" | "block", сообщение).

        programmer_voltage — напряжение конкретного адаптера (из настроек плагина);
        если не задано, берётся общее safety.programmer_voltage.
        """
        pv = self.programmer_voltage if programmer_voltage is None else float(programmer_voltage)
        tol = float(self.cfg.get("voltage_tolerance", 0.3))
        v = profile.voltage if profile else None
        if v is None:
            return "warn", (f"напряжение чипа в профиле не указано. Проверьте маркировку по даташиту: "
                            f"адаптер работает на {pv} В.")
        if abs(v - pv) > tol:
            return "block", (f"чип рассчитан на {v} В, а адаптер — на {pv} В. Работа остановлена, "
                             f"чтобы не повредить чип: нужен переходник на {v} В "
                             f"(и смените programmer_voltage в config.yaml — общий или у плагина).")
        return "ok", f"напряжение совместимо: чип {v} В, адаптер {pv} В"

    def measured_check(self, profile, programmer_voltage: float, measured: float) -> tuple:
        """Сверить ЗАМЕРЕННОЕ напряжение цели с профилем и адаптером: ("ok" | "block", сообщение).

        Только добавляет запрет к voltage_check, никогда не разрешает то, что та запретила: ядро зовёт её
        после voltage_check и лишь тогда, когда прежняя проверка не остановила сеанс."""
        tol = float(self.cfg.get("voltage_tolerance", 0.3))
        pv = float(programmer_voltage)
        m = float(measured)
        v = profile.voltage if profile is not None else None
        if m < NO_POWER_V:
            return "block", (f"замерено {volts(m)} В — на цели нет питания (или отладчик не видит вывод питания). "
                             f"Подайте питание платы и проверьте провод VCC/VTref, затем запустите снова.")
        if v is not None and abs(m - float(v)) > tol:
            return "block", (f"замерено {volts(m)} В, а профиль на {volts(v)} В. Работа остановлена: "
                             f"проверьте, тот ли выбран чип и та ли это плата (нужен профиль на {volts(m)} В "
                             f"или переходник уровней).")
        if abs(m - pv) > tol:
            return "block", (f"замерено {volts(m)} В, а адаптер на {volts(pv)} В. Работа остановлена, чтобы не "
                             f"повредить чип: проверьте питание цели (не просело ли, нет ли замыкания) и "
                             f"напряжение адаптера (programmer_voltage) — они должны совпадать.")
        return "ok", f"замер совпал: на цели {volts(m)} В"

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
