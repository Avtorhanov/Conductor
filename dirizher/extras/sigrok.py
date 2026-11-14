"""Замер логическим анализатором через sigrok-cli — «замер» в порядке «замер → чтение → запись».

Логический анализатор только принимает сигналы: в устройство ничего не пишется. Поэтому это
не «лезвие» (у него нет read/write), а отдельная команда со своей точкой входа
(командную строку __main__.py и интерфейсы она не меняет):

    python3 -m dirizher.extras.sigrok --scan
    python3 -m dirizher.extras.sigrok --channels D0,D1,D2,D3 --samplerate 1m --time 2000 \\
        --decode "spi:clk=D0:mosi=D1:miso=D2:cs=D3" --annotation spi

Настройки по умолчанию — секция sigrok: в config.yaml (необязательна).

Пример пользы: снять SPI при включении платы и увидеть, читает ли процессор флеш (команда 0x03/0x0B)
и где останавливается загрузка.

sigrok-cli (лицензия GPL-3.0) вызывается отдельным процессом. Запись (.sr) и расшифровка (.txt)
сохраняются в data/captures/, команда и вывод — в журнал сеанса.

ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: имя драйвера вашего анализатора (`--scan`), синтаксис декодера
(`sigrok-cli -P spi --show`). Входы дешёвых анализаторов — до 5 В: не подключайте к 12 В и выше.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from ..core.config import load_config
from ..core.pipeline import OUTCOMES, Conductor
from ..core.ui import ConsoleUI

ROOT = Path(__file__).resolve().parent.parent.parent

OUTCOMES.setdefault("captured", "сигналы записаны")
OUTCOMES.setdefault("capture_failed", "запись сигналов не удалась")

DEFAULTS = {"driver": "fx2lafw", "channels": "D0,D1,D2,D3", "samplerate": "1m", "time_ms": 2000,
            "timeout": 120}


def settings(cfg: dict, **override) -> dict:
    s = dict(DEFAULTS)
    s.update(cfg.get("sigrok") or {})
    s.update({k: v for k, v in override.items() if v not in (None, "")})
    return s


def scan(c) -> int:
    c._start("capture")
    c.ui.header(f"Сеанс №{c.session}: поиск логических анализаторов")
    if not shutil.which("sigrok-cli"):
        c.ui.error("sigrok-cli не установлен (sudo apt install sigrok-cli)")
        return c._finish("capture_failed")
    res = c.runner.run(["sigrok-cli", "--scan"], timeout=60)
    lines = [ln for ln in (res.out + res.err).splitlines() if ln.strip()]
    for ln in lines or ["ничего не найдено"]:
        c.ui.info(ln)
    return c._finish("captured" if res.ok else "capture_failed")


def capture(c, cfg: dict, **override) -> int:
    s = settings(cfg, **override)
    sid = c._start("capture")
    c.ui.header(f"Сеанс №{sid}: замер логическим анализатором (только приём сигналов)")
    if not shutil.which("sigrok-cli"):
        c.ui.error("sigrok-cli не установлен (sudo apt install sigrok-cli)")
        return c._finish("capture_failed")
    out_dir = c.data_dir / "captures"
    out_dir.mkdir(parents=True, exist_ok=True)
    sr = out_dir / f"session_{sid:05d}.sr"
    cmd = ["sigrok-cli", "--driver", str(s["driver"]), "--config", f"samplerate={s['samplerate']}",
           "--channels", str(s["channels"]), "--time", str(s["time_ms"]), "-o", str(sr)]
    if s.get("triggers"):
        cmd += ["--triggers", str(s["triggers"])]
    c.ui.info(f"запись {s['time_ms']} мс, {s['samplerate']} выборок/с, каналы {s['channels']}…")
    res = c.runner.run(cmd, timeout=int(s["timeout"]))
    if not res.ok or not sr.exists():
        c.ui.error("анализатор не записал сигналы: " + (res.tail(4) or "нет файла"))
        return c._finish("capture_failed")
    c.ui.ok(f"записано: {sr}")
    if s.get("decode"):
        dec = ["sigrok-cli", "-i", str(sr), "-P", str(s["decode"])]
        if s.get("annotation"):
            dec += ["-A", str(s["annotation"])]
        res2 = c.runner.run(dec, timeout=int(s["timeout"]))
        txt = sr.with_suffix(".txt")
        txt.write_text(res2.out, encoding="utf-8")
        lines = [ln for ln in res2.out.splitlines() if ln.strip()]
        if not res2.ok:
            c.ui.warn("декодер завершился с ошибкой: " + res2.tail(3))
        c.ui.ok(f"расшифровка: {len(lines)} строк → {txt}")
        for ln in lines[:20]:
            c.ui.info(ln)
        if len(lines) > 20:
            c.ui.info("…")
    return c._finish("captured")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m dirizher.extras.sigrok",
                                description="Замер логическим анализатором (sigrok-cli), только приём сигналов")
    p.add_argument("--config", default=str(ROOT / "config.yaml"), help="путь к config.yaml")
    p.add_argument("--scan", action="store_true", help="найти подключённые анализаторы")
    p.add_argument("--driver", help="драйвер sigrok, например fx2lafw")
    p.add_argument("--channels", help="каналы, например D0,D1,D2,D3")
    p.add_argument("--samplerate", help="частота выборки, например 1m или 24m")
    p.add_argument("--time", type=int, help="длительность записи, мс")
    p.add_argument("--triggers", help="условие запуска, например D3=f (спад CS)")
    p.add_argument("--decode", help='декодер, например "spi:clk=D0:mosi=D1:miso=D2:cs=D3"')
    p.add_argument("--annotation", help="что показать из декодера, например spi")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    cfg = load_config(Path(a.config))
    c = Conductor(cfg, ConsoleUI())
    try:
        if a.scan:
            sid = scan(c)
        else:
            sid = capture(c, cfg, driver=a.driver, channels=a.channels, samplerate=a.samplerate,
                          time_ms=a.time, decode=a.decode, annotation=a.annotation, triggers=a.triggers)
        return 0 if c.memory.session(sid)["outcome"] == "captured" else 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
