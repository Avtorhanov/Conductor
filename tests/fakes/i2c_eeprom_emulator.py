"""Эмулятор I2C EEPROM 24Cxx — транспорт для помощника dirizher/helpers/i2c_eeprom.py без железа.

Ведёт себя как настоящая микросхема:
  * отвечает только на свои адреса (у 24C04/08/16 — 2/4/8 адресов, старшие биты адреса памяти);
  * внутренний указатель адреса: запись адреса, затем чтение; «чтение текущего адреса»;
  * запись страницы «заворачивается» внутри страницы (как у настоящей 24Cxx) — так видно, что помощник
    не пересекает границу;
  * после записи busy_polls опросов не отвечает (идёт цикл записи);
  * wp=True — запись принимается (ACK), но ячейки не меняются, как при WP на питании.

Запуск помощника отдельным процессом с эмулятором (так его зовёт лезвие в тестах):
    python3 tests/fakes/i2c_eeprom_emulator.py --state ФАЙЛ.json -- <аргументы помощника>
В ФАЙЛ.json — содержимое (hex), модель, адрес, страница, wp; после работы он перезаписывается,
а журнал шины дописывается в поле log.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dirizher.helpers import i2c_eeprom as helper  # noqa: E402

PAGES = {"24C01": 8, "24C02": 8, "24C04": 16, "24C08": 16, "24C16": 16, "24C32": 32, "24C64": 32,
         "24C128": 64, "24C256": 64, "24C512": 128}


class EepromEmulator:
    def __init__(self, mem: bytes, model: str = "24C02", address: int = 0x50, page: int | None = None,
                 busy_polls: int = 2, wp: bool = False, present: bool = True):
        self.model = helper.normalize(model)
        self.size, self.abytes, _ = helper.EEPROMS[self.model]
        self.mem = bytearray(mem)
        assert len(self.mem) == self.size, "размер содержимого не равен объёму модели"
        self.address = address
        self.page = page or PAGES[self.model]
        self.busy_polls = busy_polls
        self.wp = wp
        self.present = present
        self.ptr = 0
        self.busy = 0
        self.log = []                                  # («w»|«r», адрес, длина)
        self.page_writes = 0

    # --- адресация ---
    def _blocks(self) -> int:
        return {"24C04": 2, "24C08": 4, "24C16": 8}.get(self.model, 1) if self.abytes == 1 else 1

    def _mine(self, addr: int) -> bool:
        return self.present and self.address <= addr < self.address + self._blocks()

    def _ack(self, addr: int) -> None:
        if not self._mine(addr):
            raise helper.NoAck(f"0x{addr:02X}")
        if self.busy:
            self.busy -= 1
            raise helper.NoAck(f"0x{addr:02X} занят записью")

    # --- транспорт ---
    def write(self, addr: int, data: bytes) -> None:
        self._ack(addr)
        self.log.append(("w", addr, len(data)))
        data = bytes(data)
        if self.abytes == 1:
            if not data:
                return
            base = ((addr - self.address) << 8) | data[0]
            payload = data[1:]
        else:
            if len(data) < 2:
                return
            base = (data[0] << 8) | data[1]
            payload = data[2:]
        base %= self.size
        self.ptr = base
        if not payload:
            return
        start = base - base % self.page
        for i, b in enumerate(payload):
            pos = start + (base - start + i) % self.page        # заворот внутри страницы
            if not self.wp:
                self.mem[pos] = b
        self.ptr = (start + (base - start + len(payload)) % self.page)
        self.page_writes += 1
        self.busy = self.busy_polls

    def read(self, addr: int, n: int) -> bytes:
        self._ack(addr)
        self.log.append(("r", addr, n))
        out = bytes(self.mem[(self.ptr + i) % self.size] for i in range(n))
        self.ptr = (self.ptr + n) % self.size
        return out

    def write_read(self, addr: int, data: bytes, n: int) -> bytes:
        self.write(addr, data)
        return self.read(addr, n)

    def close(self) -> None:
        pass

    # --- состояние в файле ---
    def to_state(self) -> dict:
        return {"mem": self.mem.hex(), "model": self.model, "address": self.address, "page": self.page,
                "busy_polls": self.busy_polls, "wp": self.wp, "present": self.present,
                "page_writes": self.page_writes}

    @classmethod
    def from_state(cls, st: dict) -> "EepromEmulator":
        emu = cls(bytes.fromhex(st["mem"]), st["model"], st.get("address", 0x50), st.get("page"),
                  st.get("busy_polls", 2), st.get("wp", False), st.get("present", True))
        emu.page_writes = st.get("page_writes", 0)
        return emu


def save_state(path: Path, emu: EepromEmulator, log: list | None = None) -> None:
    st = emu.to_state()
    st["log"] = (log or []) + [list(x) for x in emu.log]
    Path(path).write_text(json.dumps(st), encoding="utf-8")


def load_state(path: Path) -> tuple:
    st = json.loads(Path(path).read_text(encoding="utf-8"))
    return EepromEmulator.from_state(st), st.get("log", [])


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3 or argv[0] != "--state" or argv[2] != "--":
        print("использование: i2c_eeprom_emulator.py --state ФАЙЛ -- аргументы помощника", file=sys.stderr)
        return 2
    state = Path(argv[1])
    emu, log = load_state(state)
    try:
        return helper.main(argv[3:], bus=emu)
    finally:
        save_state(state, emu, log)


if __name__ == "__main__":
    sys.exit(main())
