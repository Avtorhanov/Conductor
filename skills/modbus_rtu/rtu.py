#!/usr/bin/env python3
"""Помощник-процесс: Modbus RTU по RS-485 (USB-переходник), только стандартная библиотека.

Запускается лезвием modbus_rtu ОТДЕЛЬНЫМ ПРОЦЕССОМ через self.run(): таймаут и журнал даёт Runner,
зависание порта не роняет Дирижёр.

    python3 rtu.py --port /dev/ttyUSB0 --baud 9600 --parity N --stopbits 1 --unit 1 probe START
    python3 rtu.py ... [--function 3|4] read START COUNT ФАЙЛ      — регистры подряд, big-endian, в файл
    python3 rtu.py ... [--multi] write РЕГ=ЗНАЧ [РЕГ=ЗНАЧ ...]     — функция 6 (или 16 при --multi)

Коды выхода: 0 — успех; 3 — прибор молчит; 4 — прибор ответил исключением Modbus; 2 — прочие ошибки.

Протокол: Modbus over Serial Line V1.02 (modbus.org). ПРОВЕРИТЬ НА ЖЕЛЕЗЕ: эхо переходника (--echo),
паузы между кадрами на вашей скорости, поддержку функции 16 вашим прибором.
"""
from __future__ import annotations

import argparse
import os
import select
import sys
import termios
import time

BAUDS = {1200: termios.B1200, 2400: termios.B2400, 4800: termios.B4800, 9600: termios.B9600,
         19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}
EXCEPTIONS = {1: "функция не поддерживается", 2: "нет такого регистра", 3: "недопустимое значение",
              4: "сбой прибора", 5: "принято, выполняется", 6: "прибор занят", 11: "шлюз: прибор не ответил"}
MAX_READ = 125


class NoAnswer(Exception):
    pass


class ModbusError(Exception):
    def __init__(self, code: int):
        super().__init__(f"исключение Modbus {code}: {EXCEPTIONS.get(code, 'неизвестный код')}")
        self.code = code


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


class Port:
    def __init__(self, path: str, baud: int, parity: str, stopbits: int):
        if baud not in BAUDS:
            raise ValueError(f"скорость {baud} не поддерживается: {', '.join(map(str, BAUDS))}")
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = 0                                             # iflag: сырой ввод
        attrs[1] = 0                                             # oflag
        cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
        if parity.upper() in ("E", "O"):
            cflag |= termios.PARENB | (termios.PARODD if parity.upper() == "O" else 0)
        if stopbits == 2:
            cflag |= termios.CSTOPB
        attrs[2] = cflag
        attrs[3] = 0                                             # lflag: без эха и канонического режима
        attrs[4] = attrs[5] = BAUDS[baud]
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.char_time = 11.0 / baud
        self.flush()

    def flush(self) -> None:
        try:
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except termios.error:
            pass

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)
        try:
            termios.tcdrain(self.fd)
        except termios.error:
            pass

    def read_exact(self, n: int, timeout: float) -> bytes:
        buf, end = b"", time.monotonic() + timeout
        while len(buf) < n:
            left = end - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if r:
                chunk = os.read(self.fd, n - len(buf))
                if not chunk:
                    break
                buf += chunk
        return buf

    def close(self) -> None:
        os.close(self.fd)


class Client:
    def __init__(self, port: Port, unit: int, timeout: float, retries: int, echo: bool):
        self.port, self.unit, self.timeout, self.retries, self.echo = port, unit, timeout, retries, echo

    def _transact(self, pdu: bytes, expect_len: int) -> bytes:
        frame = bytes([self.unit]) + pdu
        frame += crc16(frame)
        last = None
        for _ in range(self.retries + 1):
            time.sleep(max(0.002, 3.5 * self.port.char_time))   # тишина 3,5 символа перед кадром
            self.port.flush()
            self.port.write(frame)
            if self.echo:
                self.port.read_exact(len(frame), self.timeout)
            head = self.port.read_exact(3, self.timeout)
            if len(head) < 3:
                last = NoAnswer("прибор не ответил (адрес, скорость, чётность, A/B)")
                continue
            if head[1] == pdu[0] | 0x80:
                rest = self.port.read_exact(2, self.timeout)
                if crc16(head) != rest:
                    last = RuntimeError("помеха: неверная контрольная сумма ответа")
                    continue
                raise ModbusError(head[2])
            rest = self.port.read_exact(expect_len - 3, self.timeout)
            resp = head + rest
            if len(resp) < expect_len:
                last = RuntimeError("ответ оборвался — помехи или неверная скорость")
                continue
            if crc16(resp[:-2]) != resp[-2:]:
                last = RuntimeError("помеха: неверная контрольная сумма ответа")
                continue
            if resp[0] != self.unit or resp[1] != pdu[0]:
                last = RuntimeError(f"ответил не тот прибор или не на тот запрос: {resp[:2].hex()}")
                continue
            return resp
        raise last

    def read(self, start: int, count: int, function: int = 3) -> bytes:
        out = b""
        while count > 0:
            n = min(count, MAX_READ)
            resp = self._transact(bytes([function]) + start.to_bytes(2, "big") + n.to_bytes(2, "big"), 5 + 2 * n)
            if resp[2] != 2 * n:
                raise RuntimeError(f"прибор вернул {resp[2]} байт вместо {2 * n}")
            out += resp[3:3 + 2 * n]
            start, count = start + n, count - n
        return out

    def write_single(self, reg: int, value: int) -> None:
        pdu = bytes([6]) + reg.to_bytes(2, "big") + value.to_bytes(2, "big")
        resp = self._transact(pdu, 8)
        if resp[1:6] != pdu:
            raise RuntimeError(f"регистр {reg}: прибор подтвердил другое значение")

    def write_multi(self, start: int, values: list) -> None:
        data = b"".join(v.to_bytes(2, "big") for v in values)
        pdu = bytes([16]) + start.to_bytes(2, "big") + len(values).to_bytes(2, "big") + bytes([len(data)]) + data
        resp = self._transact(pdu, 8)
        if resp[2:6] != pdu[1:5]:
            raise RuntimeError("прибор подтвердил запись не тех регистров")


def parse_pairs(items: list) -> list:
    pairs = []
    for item in items:
        reg, _, val = item.partition("=")
        r, v = int(reg, 0), int(val, 0)
        if not (0 <= r <= 0xFFFF and 0 <= v <= 0xFFFF):
            raise ValueError(f"«{item}»: регистр и значение — 0…65535")
        pairs.append((r, v))
    return sorted(pairs)


def runs(pairs: list) -> list:
    """Подряд идущие регистры — одним кадром функции 16."""
    groups = []
    for reg, val in pairs:
        if groups and reg == groups[-1][0] + len(groups[-1][1]) and len(groups[-1][1]) < 120:
            groups[-1][1].append(val)
        else:
            groups.append((reg, [val]))
    return groups


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Modbus RTU для Дирижёра")
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--parity", default="N", choices=["N", "E", "O", "n", "e", "o"])
    ap.add_argument("--stopbits", type=int, default=1, choices=[1, 2])
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--function", type=int, default=3, choices=[3, 4])
    ap.add_argument("--echo", action="store_true", help="переходник возвращает свои же байты")
    ap.add_argument("--multi", action="store_true", help="запись функцией 16")
    sub = ap.add_subparsers(dest="op", required=True)
    pr = sub.add_parser("probe")
    pr.add_argument("start", type=lambda x: int(x, 0))
    rd = sub.add_parser("read")
    rd.add_argument("start", type=lambda x: int(x, 0))
    rd.add_argument("count", type=lambda x: int(x, 0))
    rd.add_argument("file")
    wr = sub.add_parser("write")
    wr.add_argument("pairs", nargs="+")
    a = ap.parse_args(argv)
    if not 1 <= a.unit <= 247:
        print("адрес прибора (unit) — 1…247", file=sys.stderr)
        return 2
    try:
        port = Port(a.port, a.baud, a.parity, a.stopbits)
    except (OSError, ValueError) as e:
        print(f"не открыть {a.port}: {e}", file=sys.stderr)
        return 2
    cl = Client(port, a.unit, a.timeout, a.retries, a.echo)
    try:
        if a.op == "probe":
            data = cl.read(a.start, 1, a.function)
            print(f"FOUND unit={a.unit} reg={a.start} value=0x{data.hex()}")
        elif a.op == "read":
            data = cl.read(a.start, a.count, a.function)
            with open(a.file, "wb") as f:
                f.write(data)
            print(f"READ {a.count} регистров с {a.start}")
        else:
            pairs = parse_pairs(a.pairs)
            if a.multi:
                for start, values in runs(pairs):
                    cl.write_multi(start, values)
            else:
                for reg, val in pairs:
                    cl.write_single(reg, val)
            print(f"WROTE {len(pairs)} регистров: " + ", ".join(f"{r}=0x{v:04X}" for r, v in pairs))
        return 0
    except NoAnswer as e:
        print(str(e), file=sys.stderr)
        return 3
    except ModbusError as e:
        print(str(e), file=sys.stderr)
        return 4
    except Exception as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 2
    finally:
        port.close()


if __name__ == "__main__":
    sys.exit(main())
