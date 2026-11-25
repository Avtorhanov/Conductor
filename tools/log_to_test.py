#!/usr/bin/env python3
"""Пакет журнала с железа → постоянный тест.

    python3 -m dirizher journal 12 --to s12.json      # на приборе (или «Журнал» → сеанс → «Скачать…»)
    python3 tools/log_to_test.py s12.json            # у разработчика

Создаёт tests/fixtures/real/<программа>_<версия>_<n>.json: записанные команды (команда, код, вывод),
настройки лезвия и ожидаемый результат опознания (класс, отпечаток), взятый из сеанса. Сразу
прогоняет фикстуру через настоящий плагин и говорит, совпал ли разбор.

ВАЖНО: ожидаемое берётся из того, что плагин разобрал на железе. Сверьте его с маркировкой чипа
и даташитом. Если разбор был неверный — поправьте "expect" в фикстуре на правду (или --expect),
и тогда тест будет падать, пока разбор в плагине не исправят.

Параметры:
  --out ПАПКА          куда положить фикстуру (по умолчанию tests/fixtures/real)
  --expect ключ=знач   поправить ожидаемое (можно несколько раз): jedec_id=EF4017, size=8388608,
                       target_class=memory.spi_nor, detected=false
  --plugin ИМЯ         лезвие, если в сеансе их было несколько или опознание не удалось
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dirizher.core import journal  # noqa: E402

def _value(text: str):
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _slug(text) -> str:
    return re.sub(r"[^0-9A-Za-z.]+", "-", str(text or "unknown")).strip("-.") or "unknown"


def make_fixture(b: dict, plugin: str | None = None, expect_over: dict | None = None) -> dict:
    if b.get("format") != journal.FORMAT:
        raise ValueError("это не пакет журнала Дирижёра (нет format: dirizher-journal)")
    s = b["session"]
    name = plugin or s.get("plugin") or (b["tools"][0]["plugin"] if b.get("tools") else None)
    if not name:
        raise ValueError("в сеансе не указано лезвие — задайте --plugin")
    tool = next((t for t in b.get("tools", []) if t["plugin"] == name), {})
    cmds = [{"argv": c["argv"], "code": c["code"], "stdout": c.get("stdout", ""), "stderr": c.get("stderr", "")}
            for c in b.get("commands", []) if c.get("kept", True) and "argv" in c]
    if not cmds:
        raise ValueError("в пакете нет записанных команд (журнал старше 0.7.0 или сеанс симулятора)")
    detected = s.get("plugin") == name and s.get("outcome") != "not_detected"
    expect = {"detected": detected}
    if detected:
        expect["target_class"] = s.get("target_class")
        expect["fingerprint"] = dict(s.get("fingerprint") or {})
    for k, v in (expect_over or {}).items():
        if k in ("detected", "target_class"):
            expect[k] = v
        else:
            expect.setdefault("fingerprint", {})[k] = v
    program = Path(str(cmds[0]["argv"][0])).name     # первая команда сеанса — опознание
    return {
        "format": journal.FIXTURE_FORMAT, "format_version": 1, "synthetic": False,
        "program": program, "program_version": tool.get("version"),
        "plugin": name, "settings": tool.get("settings") or {},
        "source": {"dirizher": b.get("dirizher"), "session": s.get("id"), "started": s.get("started"),
                   "outcome": s.get("outcome")},
        "commands": cmds,
        "expect": expect,
        "note": "Записано с железа. Ожидаемое сверено с маркировкой? Если нет — сверьте и поправьте expect.",
    }


def fixture_path(out_dir: Path, fx: dict) -> Path:
    base = f"{_slug(fx['program'])}_{_slug(fx['program_version'])}"
    n = 1
    while (out_dir / f"{base}_{n}.json").exists():
        n += 1
    return out_dir / f"{base}_{n}.json"


def check(fx: dict) -> list:
    with tempfile.TemporaryDirectory() as td:
        try:
            det, _ = journal.replay_probe(fx, Path(td), ROOT / "skills")
        except Exception as e:
            return [f"плагин упал на записанном выводе: {e}"]
    return journal.compare(fx["expect"], det)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="пакет журнала → фикстура tests/fixtures/real/")
    ap.add_argument("bundle")
    ap.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "real"))
    ap.add_argument("--expect", action="append", default=[], metavar="КЛЮЧ=ЗНАЧ")
    ap.add_argument("--plugin")
    a = ap.parse_args(argv)
    try:
        b = json.loads(Path(a.bundle).read_text(encoding="utf-8"))
        over = {}
        for item in a.expect:
            if "=" not in item:
                raise ValueError(f"--expect {item}: нужно ключ=значение")
            k, v = item.split("=", 1)
            over[k.strip()] = _value(v.strip())
        fx = make_fixture(b, a.plugin, over)
    except (OSError, ValueError, KeyError) as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1
    out_dir = Path(a.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = fixture_path(out_dir, fx)
    with open(dest, "x", encoding="utf-8") as f:
        json.dump(fx, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"фикстура: {dest}")
    print("ожидаемое: " + json.dumps(fx["expect"], ensure_ascii=False))
    problems = check(fx)
    if problems:
        print("разбор НЕ совпал с ожидаемым:\n  " + "\n  ".join(problems))
        print("тест tests/test_real_outputs.py будет падать, пока разбор не исправят (или expect).")
        return 2
    print("разбор совпал. Сверьте ожидаемое с маркировкой чипа, затем: python3 -m unittest tests.test_real_outputs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
