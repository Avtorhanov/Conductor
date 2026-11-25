"""Веб-интерфейс «Дирижёра»: ПК, телефон, сенсорный экран прибора.

Запуск:   python3 -m dirizher web        (адрес и код доступа он напечатает сам)

Как устроено:
  * только стандартная библиотека Python — работает без интернета (принцип 10);
  * ядро не меняется: веб подставляет свой ui (WebUI). Вопросы ядра становятся
    карточками на экране, ответы человека возвращаются в ядро;
  * одновременно идёт одно задание — одна микросхема, один сценарий;
  * запись — по-прежнему только через Conductor.apply() и только после слова «ДА»,
    которое человек вводит сам. Слово проверяет сервер, кнопкой его не заменить;
  * телефон и ПК входят по коду; экран самого прибора (localhost) — без кода;
  * кнопка «Отмена» отвечает ядру так же, как пустой ответ в консоли, —
    безопасным вариантом («нет», «позже», «отмена»);
  * мастерская (0.4.0): справка, навыки, самопроверка, капот. Капот правит файлы прибора, поэтому
    закрыт по умолчанию: hood.edit: true в config.yaml или «Открыть капот» с экрана самого прибора
    (с телефона открыть нельзя). Код и профили сохраняются только после слова «ДА».
"""
from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
import traceback
from contextlib import contextmanager
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from .. import __version__
from ..core.pipeline import OUTCOMES, Conductor
from ..core.storage import sha256
from ..core.ui import ConsoleUI
from ..workshop import catalog, doctor, helpdocs, skillmaker
from ..workshop.hood import Hood, HoodError, classify
from . import builder, guide, playground

STATIC = Path(__file__).with_name("static")
# Файлы, чтобы страницу можно было установить как приложение (PWA). Отдаются без кода входа:
# только описание приложения, значки и сервис-воркер — ни данных прибора, ни действий.
APP_FILES = {
    "manifest.webmanifest": "application/manifest+json; charset=utf-8",
    "sw.js": "text/javascript; charset=utf-8",
    "icon.svg": "image/svg+xml",
    "icon-192.png": "image/png",
    "icon-512.png": "image/png",
    "icon-maskable-512.png": "image/png",
    "apple-touch-icon.png": "image/png",     # iPhone: «На экран "Домой"» (180×180, без прозрачности)
}
# Учебная площадка (тренажёр): страница и её данные из playground.yaml. Тоже без кода входа —
# это учебный текст, в нём нет ни данных прибора, ни действий.
PLAYGROUND_FILES = {"playground.html", "playground.json"}
WEB_DEFAULTS = {"host": "0.0.0.0", "port": 8080, "pin": "", "trust_localhost": True, "max_upload_mb": 64}
MAX_EVENTS = 4000
MAX_JSON = 1 << 20
LONG_POLL = 15.0
LOCK_AFTER = 5            # столько неверных кодов подряд…
LOCK_SECONDS = 60         # …и вход закрывается на минуту
_NOTHING = object()


class Interrupted(Exception):
    """«Отмена» там, где у ядра нет безопасного ответа по умолчанию (пауза перед началом)."""


class Busy(Exception):
    pass


def web_settings(cfg: dict) -> dict:
    s = dict(WEB_DEFAULTS)
    s.update(cfg.get("web") or {})
    return s


# ======================= задание и его «пульт» =======================
class Job:
    """Одно выполнение ядра: журнал событий для экрана и текущий вопрос человеку."""

    def __init__(self, action: str, params: dict, sim: bool, dry_run: bool):
        self.id = secrets.token_hex(4)
        self.action, self.params, self.sim, self.dry_run = action, params, sim, dry_run
        self.title = guide.job_title(action, params, dry_run)
        self.started = time.time()
        self.cond = threading.Condition()
        self.events: list = []
        self.seq = 0
        self.qcount = 0
        self.question = None
        self._validate = None
        self._answer = _NOTHING
        self._cancel = False
        self.closing = False
        self.done = False
        self.result = None
        self.next: list = []
        self.thread: threading.Thread | None = None

    def _bump(self) -> None:
        self.seq += 1
        self.cond.notify_all()

    def emit(self, kind: str, text: str = "", **extra) -> None:
        with self.cond:
            self.seq += 1
            self.events.append({"seq": self.seq, "kind": kind, "text": text, **extra})
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
            self.cond.notify_all()

    def ask(self, qtype: str, text: str, validate=None, **extra) -> tuple:
        """Задать вопрос и ждать ответа. Возвращает (отменено?, ответ)."""
        with self.cond:
            if self.closing:
                return True, None
            self.qcount += 1
            self.question = {"qid": self.qcount, "type": qtype, "text": text, **extra}
            self._validate, self._answer, self._cancel = validate, _NOTHING, False
            self._bump()
            while self._answer is _NOTHING and not self._cancel and not self.closing:
                self.cond.wait(timeout=1.0)
            cancelled = self._cancel or self.closing or self._answer is _NOTHING
            answer = None if cancelled else self._answer
            self.question = None
            self._bump()
        return cancelled, answer

    def answer(self, qid, value) -> bool:
        with self.cond:
            if self.question is None or self.question["qid"] != qid:
                return False
            if self._validate is not None and not self._validate(value):
                raise ValueError("недопустимый ответ")
            self._answer = value
            self.cond.notify_all()
            return True

    def cancel(self) -> bool:
        with self.cond:
            if self.question is None:
                return False
            self._cancel = True
            self.cond.notify_all()
            return True

    def close(self) -> None:
        """Сервер останавливается: все вопросы получают безопасный ответ."""
        with self.cond:
            self.closing = True
            self.cond.notify_all()

    def finish(self) -> None:
        with self.cond:
            self.done = True
            self._bump()

    def state(self, since: int = 0, job_id: str | None = None, wait: float = 0.0) -> dict:
        with self.cond:
            if job_id != self.id:
                since = 0
            if wait and not self.done and self.seq <= since:
                self.cond.wait(timeout=wait)
            return {
                "id": self.id, "title": self.title, "action": self.action, "params": self.params,
                "sim": self.sim, "dry_run": self.dry_run, "running": not self.done, "done": self.done,
                "seq": self.seq, "events": [e for e in self.events if e["seq"] > since],
                "question": self.question, "result": self.result, "next": self.next,
                "started": self.started,
            }


class WebUI(ConsoleUI):
    """ui для ядра: вывод — в журнал задания, вопросы — человеку на экран."""

    def __init__(self, job: Job):
        super().__init__(color=False)
        self.job = job

    # ---------- вывод ----------
    def out(self, text: str = "") -> None:
        if text:
            self.job.emit("text", text)

    def header(self, text: str) -> None:
        self.job.emit("header", text)

    def step(self, n: int, total: int, text: str) -> None:
        self.job.emit("step", text, n=n, total=total)

    def info(self, text: str) -> None:
        self.job.emit("info", text)

    def ok(self, text: str) -> None:
        self.job.emit("ok", text)

    def warn(self, text: str) -> None:
        self.job.emit("warn", text)

    def error(self, text: str) -> None:
        self.job.emit("error", text)

    def finding(self, f) -> None:
        self.job.emit("finding", f.message, status=f.status, check=f.check)

    # ---------- вопросы ----------
    def pause(self, text: str) -> None:
        cancelled, _ = self.job.ask("pause", text)
        if cancelled:
            raise Interrupted()

    def yes_no(self, question: str, allow_later: bool = False) -> str:
        options = [{"value": "yes", "label": "Да"}, {"value": "no", "label": "Нет"}]
        if allow_later:
            options.append({"value": "later", "label": "Позже"})
        valid = {o["value"] for o in options}
        cancelled, a = self.job.ask("yes_no", question, validate=lambda v: v in valid, options=options)
        if cancelled:
            return "later" if allow_later else "no"
        return a

    def choose(self, question: str, options: list):
        n = len(options)
        cancelled, a = self.job.ask(
            "choose", question, options=[str(o) for o in options],
            validate=lambda v: v is None or (isinstance(v, int) and not isinstance(v, bool) and 0 <= v < n))
        return None if cancelled else a

    def confirm_word(self, text: str, word: str) -> bool:
        cancelled, a = self.job.ask("confirm_word", text, word=word,
                                    validate=lambda v: isinstance(v, str) and len(v) <= 40)
        if cancelled:
            return False
        return str(a).strip().lower() in {word.lower(), "yes"}


# ======================= приложение =======================
def lan_addresses() -> list:
    found = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))      # пакет не отправляется — только выбор интерфейса
            found.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for ip in found:
        if not ip.startswith("127.") and ip not in out:
            out.append(ip)
    return out


SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class App:
    def __init__(self, cfg: dict, sim_default: bool = False, pin: str | None = None):
        self.cfg = cfg
        self.settings = web_settings(cfg)
        self.sim_default = sim_default
        pin = str(self.settings.get("pin") or "").strip() if pin is None else str(pin)
        self.pin = pin or f"{secrets.randbelow(10 ** 6):06d}"
        self.token = secrets.token_urlsafe(24)
        self.trust_local = bool(self.settings.get("trust_localhost", True))
        self.max_upload = int(float(self.settings.get("max_upload_mb", 64)) * 1024 * 1024)
        self.images_dir = Path(cfg["data_dir"]) / "images"
        self.lock = threading.Lock()
        self.hood = Hood(cfg)
        self.hood_permanent = bool((cfg.get("hood") or {}).get("edit", False))
        self.hood_unlocked = False
        self.job: Job | None = None
        self.fails = 0
        self.locked_until = 0.0
        self.host, self.port = "127.0.0.1", 0

    # ---------- справки ----------
    @contextmanager
    def conductor(self, sim: bool):
        with guide.open_readonly(self.cfg, sim) as c:
            yield c

    def urls(self) -> list:
        if self.host in ("127.0.0.1", "localhost"):
            return [f"http://localhost:{self.port}"]
        urls = [f"http://{ip}:{self.port}" for ip in lan_addresses()]
        hostname = socket.gethostname()
        if hostname:
            urls.append(f"http://{hostname}.local:{self.port}")
        return urls or [f"http://localhost:{self.port}"]

    @property
    def hood_open(self) -> bool:
        return self.hood_permanent or self.hood_unlocked

    def busy(self) -> bool:
        return self.job is not None and not self.job.done

    # ---------- образы прошивок ----------
    def image_path(self, name) -> Path:
        name = str(name)
        if not name or name != Path(name).name or name.startswith(".") or SAFE_NAME.sub("", name) != name:
            raise ValueError("недопустимое имя файла образа")
        path = self.images_dir / name
        if not path.is_file():
            raise ValueError(f"образ «{name}» не найден — загрузите его")
        return path

    def images(self) -> list:
        if not self.images_dir.is_dir():
            return []
        files = sorted(self.images_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "size": p.stat().st_size,
                 "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(p.stat().st_mtime))}
                for p in files if p.is_file() and not p.name.startswith(".")]

    def save_image(self, name: str, read, length: int) -> dict:
        if length <= 0:
            raise ValueError("пустой файл")
        if length > self.max_upload:
            raise ValueError(f"файл больше {self.max_upload // (1024 * 1024)} МБ (web.max_upload_mb)")
        stem = SAFE_NAME.sub("_", Path(name or "image.bin").name).lstrip("._") or "image.bin"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.images_dir / f".upload_{secrets.token_hex(4)}"
        got = 0
        with open(tmp, "wb") as f:
            while got < length:
                chunk = read(min(1 << 16, length - got))
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        if got != length:
            tmp.unlink()
            raise ValueError("файл передан не полностью — попробуйте ещё раз")
        data_sha = sha256(tmp.read_bytes())
        base, dot, ext = stem.rpartition(".")
        if not dot:
            base, ext = stem, ""
        for n in range(1, 1000):                         # существующие файлы не перезаписываются
            cand = stem if n == 1 else f"{base}_{n}" + (f".{ext}" if ext else "")
            path = self.images_dir / cand
            if path.exists():
                if sha256(path.read_bytes()) == data_sha:
                    tmp.unlink()
                    return {"name": cand, "size": got, "sha": data_sha, "same": True}
                continue
            tmp.rename(path)
            return {"name": cand, "size": got, "sha": data_sha}
        tmp.unlink()
        raise ValueError("слишком много файлов с таким именем")

    # ---------- задания ----------
    def clean_params(self, action: str, raw) -> dict:
        meta = guide.ACTIONS.get(action)
        if meta is None or not meta["job"]:
            raise ValueError(f"неизвестное действие «{action}»")
        raw = raw if isinstance(raw, dict) else {}
        p = {}
        for key in meta["params"]:
            v = raw.get(key)
            if v is None or v == "" or v == "ask":
                continue
            if key in ("dry_run", "ok", "scan"):
                p[key] = bool(v)
            elif key == "session":
                p[key] = int(v)
            elif key == "label":
                p[key] = str(v).strip()[:80]
            elif key == "kind":
                if v not in guide.SNAPSHOT_KINDS:
                    raise ValueError("неизвестный вид снимка")
                p[key] = v
            elif key == "do":
                if v not in guide.SIM_ACTIONS:
                    raise ValueError("неизвестная учебная команда")
                p[key] = v
            elif key == "image":
                self.image_path(v)
                p[key] = str(v)
            elif key == "set":
                p[key] = guide.clean_set(v)
            elif key in ("target", "adapter"):
                if not isinstance(v, str) or not guide.ID_PARAM.match(v):
                    raise ValueError(f"{key}: ожидался id (латиница, цифры, _)")
                p[key] = v
        if meta.get("pick") and "session" not in p:
            raise ValueError("не выбран сеанс")
        if action == "sim" and "do" not in p:
            raise ValueError("не выбрана учебная команда")
        if raw.get("image") == "ask":
            raise ValueError("не выбран файл образа")
        if action == "tune" and "set" not in p:
            raise ValueError("не задано ни одного нового значения")
        return p

    def start_job(self, action: str, raw_params, sim: bool) -> Job:
        params = self.clean_params(action, raw_params)
        sim = True if action == "sim" else bool(sim)
        dry_run = bool(params.pop("dry_run", False))
        with self.lock:
            if self.job is not None and not self.job.done:
                raise Busy("уже идёт задание — дождитесь окончания или ответьте на вопрос")
            job = Job(action, dict(params, **({"dry_run": True} if dry_run else {})), sim, dry_run)
            job.thread = threading.Thread(target=self._worker, args=(job, action, params, sim, dry_run),
                                          name=f"dirizher-job-{job.id}")
            self.job = job
            job.thread.start()
        return job

    def _worker(self, job: Job, action: str, params: dict, sim: bool, dry_run: bool) -> None:
        ui = WebUI(job)
        c = None
        hints = []
        exec_params = dict(params)
        try:
            if "image" in exec_params:
                exec_params["image"] = str(self.image_path(exec_params["image"]))
            c = Conductor(self.cfg, ui, sim=sim, dry_run=dry_run)
            sid = guide.execute(c, action, exec_params)
            outcome = guide.outcome_of(c, sid)
            if sid:
                hints = c.learning.hints(sid)
        except Interrupted:
            if c is not None:
                c.interrupted()
            ui.warn("остановлено. Все сделанные копии сохранены.")
            sid, outcome = (c.session if c else None), "interrupted"
        except Exception as e:
            traceback.print_exc()
            ui.error(f"ошибка: {e}")
            if c is not None:
                c.interrupted()
            sid, outcome = (c.session if c else None), None
            job.result = {"session": sid, "outcome": None, "outcome_text": "ошибка", "error": str(e)}
        finally:
            if c is not None:
                c.close()
        if job.result is None:
            job.result = {"session": sid, "outcome": outcome,
                          "outcome_text": OUTCOMES.get(outcome, outcome) if outcome else "готово"}
        chosen = {"target": exec_params["target"]} if exec_params.get("target") else {}   # модель выбрана
        job.next = guide.after(action, dict(params, dry_run=dry_run, **chosen), outcome, sid, sim, hints)
        job.finish()

    def shutdown(self) -> None:
        job = self.job
        if job is None or job.done:
            return
        job.close()
        if job.thread is not None and job.thread.is_alive():
            print("идёт работа с микросхемой — дожидаюсь окончания, не отключайте питание…", flush=True)
            job.thread.join()


# ======================= HTTP =======================
ROUTES: dict = {}


def route(method: str, path: str):
    def deco(fn):
        ROUTES[(method, path)] = fn
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = f"Dirizher/{__version__}"

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, fmt, *args):            # тихо: журнал ведёт ядро
        pass

    # ---------- доступ ----------
    def is_local(self) -> bool:
        ip = self.client_address[0]
        return ip == "::1" or ip.startswith("127.") or ip.startswith("::ffff:127.")

    def authed(self) -> bool:
        if self.app.trust_local and self.is_local():
            return True
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except cookies.CookieError:
            return False
        morsel = jar.get("drz")
        return morsel is not None and secrets.compare_digest(morsel.value, self.app.token)

    # ---------- ответы ----------
    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def send_json(self, obj, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._common_headers()
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, code: int, message: str) -> None:
        self.send_json({"error": message}, code)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_JSON:
            raise ValueError("слишком большой запрос")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("запрос не в формате JSON") from None
        if not isinstance(data, dict):
            raise ValueError("ожидался объект JSON")
        return data

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        try:
            if method == "GET" and path in ("/", "/index.html"):
                return self.send_index()
            if method == "GET" and path.lstrip("/") in APP_FILES:
                return self.send_app_file(path.lstrip("/"))
            if method == "GET" and path.lstrip("/") in PLAYGROUND_FILES:
                return self.send_playground(path.lstrip("/"))
            if method == "POST" and self.headers.get("X-Dirizher") != "1":
                return self.fail(403, "запрос без заголовка X-Dirizher")
            if (method, path) == ("GET", "/api/hello"):
                return self.api_hello(q)
            if (method, path) == ("POST", "/api/login"):
                return self.api_login(q)
            fn = ROUTES.get((method, path))
            if fn is None:
                return self.fail(404, "нет такого адреса")
            if not self.authed():
                return self.fail(401, "нужен код доступа")
            return fn(self, q)
        except Busy as e:
            return self.fail(409, str(e))
        except (ValueError, KeyError, TypeError, guide.GuideError, builder.BuilderError, HoodError) as e:
            return self.fail(400, str(e).strip("'\""))
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception as e:
            traceback.print_exc()
            return self.fail(500, f"внутренняя ошибка: {e}")

    PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")

    def send_index(self) -> None:
        body = (STATIC / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", self.PAGE_CSP)
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_playground(self, name: str) -> None:
        """Учебная площадка: страница и данные (playground.yaml → JSON). Без кода входа — только учебный текст."""
        if name == "playground.json":
            body = playground.to_json(playground.load()).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = playground.PAGE_PATH.read_bytes()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if name.endswith(".html"):
            self.send_header("Content-Security-Policy", self.PAGE_CSP)
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_app_file(self, name: str) -> None:
        """Файлы установки приложения (PWA): без кода входа — в них нет данных прибора."""
        body = (STATIC / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", APP_FILES[name])
        self.send_header("Content-Length", str(len(body)))
        if name.endswith(".svg"):
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    # ---------- вход ----------
    def connect_info(self) -> dict:
        local = self.is_local()
        return {"urls": self.app.urls(), "pin": self.app.pin if local else None,
                "need_pin": not (self.app.trust_local and local)}

    def api_hello(self, q):
        self.send_json({"version": __version__, "authed": self.authed(), "local": self.is_local(),
                        "sim_default": self.app.sim_default, "connect": self.connect_info()})

    def api_login(self, q):
        app = self.app
        now = time.time()
        if now < app.locked_until:
            return self.fail(429, f"слишком много неверных попыток — подождите {int(app.locked_until - now) + 1} с")
        pin = str(self.read_json().get("pin") or "").strip()
        if not secrets.compare_digest(pin.encode(), app.pin.encode()):
            app.fails += 1
            if app.fails >= LOCK_AFTER:
                app.fails, app.locked_until = 0, now + LOCK_SECONDS
            return self.fail(403, "неверный код")
        app.fails = 0
        cookie = f"drz={app.token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000"
        self.send_json({"ok": True}, headers={"Set-Cookie": cookie})


def _sim(q) -> bool:
    return q.get("sim") in ("1", "true", "yes")


@route("GET", "/api/guide")
def api_guide(h: Handler, q):
    h.send_json(guide.load_guide())


@route("GET", "/api/suggest")
def api_suggest(h: Handler, q):
    with h.app.conductor(_sim(q)) as c:
        h.send_json({"items": guide.suggest(c)})


@route("GET", "/api/pick")
def api_pick(h: Handler, q):
    with h.app.conductor(_sim(q)) as c:
        h.send_json({"items": guide.pickable(c, q.get("purpose", ""))})


@route("GET", "/api/history")
def api_history(h: Handler, q):
    n = max(1, min(500, int(q.get("n") or 50)))
    with h.app.conductor(_sim(q)) as c:
        h.send_json({"items": guide.history_rows(c, n)})


@route("GET", "/api/session")
def api_session(h: Handler, q):
    with h.app.conductor(_sim(q)) as c:
        detail = guide.session_detail(c, int(q["id"]))
    if detail is None:
        return h.fail(404, "нет такого сеанса")
    h.send_json(detail)


@route("GET", "/api/device")
def api_device(h: Handler, q):
    with h.app.conductor(_sim(q)) as c:
        info = guide.device_info(c)
    info["connect"] = h.connect_info()
    info["version"] = __version__
    info["hood"] = hood_state(h)
    h.send_json(info)


@route("GET", "/api/roadmap")
def api_roadmap(h: Handler, q):
    h.send_json({"items": guide.roadmap(h.app.cfg)})


@route("GET", "/api/download")
def api_download(h: Handler, q):
    sid, kind = int(q["sid"]), q.get("kind", "before")
    if kind not in guide.SNAPSHOT_KINDS:
        raise ValueError("неизвестный вид снимка")
    with h.app.conductor(_sim(q)) as c:
        snap = c.memory.snapshot(sid, kind)
        if snap is None:
            return h.fail(404, f"в сеансе №{sid} нет снимка «{kind}»")
        data = c.store.get(snap["sha"])           # хеш сверяется заново; при порче — из зеркала
    name = f"dirizher_s{sid}_{kind}_{snap['sha'][:8]}{'_sim' if _sim(q) else ''}.bin"
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", str(len(data)))
    h.send_header("Content-Disposition", f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}")
    h._common_headers()
    h.end_headers()
    h.wfile.write(data)


@route("GET", "/api/journal")
def api_journal(h: Handler, q):
    """Журнал сеанса для разработки: JSON-файл без дампов и личных данных (скачать на телефон)."""
    from ..core import journal

    sid = int(q["sid"])
    with h.app.conductor(_sim(q)) as c:
        b = journal.bundle(c, sid)
    if b is None:
        return h.fail(404, f"нет сеанса №{sid}")
    body = json.dumps(b, ensure_ascii=False, indent=1).encode("utf-8")
    name = journal.filename(sid, _sim(q))
    h.send_response(200)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Content-Disposition", f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(name)}")
    h._common_headers()
    h.end_headers()
    h.wfile.write(body)


@route("GET", "/api/images")
def api_images(h: Handler, q):
    h.send_json({"items": h.app.images(), "max_mb": h.app.max_upload // (1024 * 1024)})


@route("POST", "/api/upload")
def api_upload(h: Handler, q):
    length = int(h.headers.get("Content-Length") or 0)
    h.send_json(h.app.save_image(q.get("name", "image.bin"), h.rfile.read, length))


@route("GET", "/api/job")
def api_job_get(h: Handler, q):
    job = h.app.job
    if job is None:
        return h.send_json({"job": None})
    wait = LONG_POLL if q.get("wait") == "1" else 0.0
    h.send_json({"job": job.state(int(q.get("since") or 0), q.get("job"), wait)})


@route("POST", "/api/job")
def api_job_post(h: Handler, q):
    body = h.read_json()
    job = h.app.start_job(str(body.get("action")), body.get("params"), bool(body.get("sim")))
    h.send_json({"job": job.state()})


@route("POST", "/api/answer")
def api_answer(h: Handler, q):
    body = h.read_json()
    job = h.app.job
    if job is None or job.done or body.get("job") not in (None, job.id):
        return h.fail(409, "задание уже завершено")
    if not job.answer(body.get("qid"), body.get("value")):
        return h.fail(409, "на этот вопрос уже ответили")
    h.send_json({"ok": True})


@route("POST", "/api/cancel")
def api_cancel(h: Handler, q):
    job = h.app.job
    ok = job is not None and not job.done and job.cancel()
    h.send_json({"ok": ok})


@route("GET", "/api/builder/fields")
def api_builder_fields(h: Handler, q):
    h.send_json({"fields": builder.FIELDS})


def _profiles(h: Handler):
    with h.app.conductor(False) as c:
        return list(c.profiles.profiles)


def _draft_dirs(h: Handler) -> list:
    base = Path(h.app.cfg["data_dir"])
    return [base / "drafts", base / "simulator" / "drafts"]


@route("GET", "/api/builder/drafts")
def api_builder_drafts(h: Handler, q):
    h.send_json({"items": builder.list_drafts(_draft_dirs(h))})


@route("POST", "/api/builder/preview")
def api_builder_preview(h: Handler, q):
    res = builder.build(h.read_json().get("fields") or {}, _profiles(h))
    h.send_json({k: res[k] for k in ("yaml", "errors", "warnings", "hints")})


@route("POST", "/api/builder/save")
def api_builder_save(h: Handler, q):
    path = builder.save_draft(_draft_dirs(h)[0], h.read_json().get("fields") or {}, _profiles(h))
    h.send_json({"ok": True, "path": str(path), "file": path.name})


@route("POST", "/api/builder/enable")
def api_builder_enable(h: Handler, q):
    body = h.read_json()
    path = builder.enable(Path(h.app.cfg["profiles_dir"]), body.get("fields") or {},
                          bool(body.get("confirmed")), _profiles(h))
    h.send_json({"ok": True, "path": str(path), "file": path.name})


# ======================= мастерская: параметры, обучение =======================
@route("GET", "/api/params")
def api_params(h: Handler, q):
    sid = int(q["session"]) if q.get("session") else None
    with h.app.conductor(_sim(q)) as c:
        view = guide.params_view(c, sid)
    if view is None:
        return h.fail(404, "нет прочитанных параметров — сначала «Прочитать параметры»")
    h.send_json(view)


@route("GET", "/api/fixes")
def api_fixes(h: Handler, q):
    sid = int(q["session"])
    with h.app.conductor(_sim(q)) as c:
        h.send_json({"session": sid, "signature": c.learning.signature(sid), "hints": c.learning.hints(sid, 5)})


@route("POST", "/api/fix")
def api_fix(h: Handler, q):
    body = h.read_json()
    sid = int(body["session"])
    with h.app.conductor(bool(body.get("sim"))) as c:
        if body.get("fix") is not None:
            c.learning.confirm_fix(int(body["fix"]), sid)
            return h.send_json({"ok": True})
        fid = c.learning.remember_fix(sid, str(body.get("text") or ""))
    h.send_json({"ok": True, "id": fid})


# ======================= мастерская: навыки и справка =======================
@route("GET", "/api/skills")
def api_skills(h: Handler, q):
    with h.app.conductor(_sim(q)) as c:
        info = guide.skills_info(c)
    info["kinds"] = [{"id": k, "title": v} for k, v in skillmaker.KINDS.items()]
    info["hood"] = hood_state(h)
    h.send_json(info)


@route("POST", "/api/skills/new")
def api_skills_new(h: Handler, q):
    need_hood(h)
    body = h.read_json()
    res = skillmaker.create_skill(h.app.hood, str(body.get("kind")), str(body.get("id") or "").strip(),
                                  str(body.get("title") or ""), str(body.get("target_class") or "device.modbus"),
                                  actor=actor(h))
    h.send_json(dict(res, ok=True))


@route("GET", "/api/help")
def api_help(h: Handler, q):
    items = helpdocs.search(helpdocs.articles(h.app.cfg), q.get("q", "")[:100], q.get("section", ""))
    h.send_json({"items": items[:80], "hood": hood_state(h)})


@route("GET", "/api/help/article")
def api_help_article(h: Handler, q):
    art = helpdocs.article(h.app.cfg, q.get("id", ""))
    if art is None:
        return h.fail(404, "нет такой статьи")
    art["editable"] = bool(art["path"]) and classify(art["path"])[0] == "edit"
    h.send_json(art)


@route("POST", "/api/help/new")
def api_help_new(h: Handler, q):
    need_hood(h)
    body = h.read_json()
    rel = skillmaker.create_article(h.app.hood, str(body.get("id") or "").strip(), str(body.get("title") or ""),
                                    str(body.get("section") or "dev"), actor=actor(h))
    h.send_json({"ok": True, "open": rel, "id": Path(rel).stem})


# ======================= справочник: движки и переходники =======================
def _catalog(h: Handler, q) -> tuple:
    cards, errors = catalog.load(h.app.cfg)
    with h.app.conductor(_sim(q)) as c:
        report = list(c.plugin_report)
    return catalog.with_status(cards, report, sim=_sim(q)), errors, report


@route("GET", "/api/catalog")
def api_catalog(h: Handler, q):
    cards, errors, report = _catalog(h, q)
    shown = catalog.search(cards, q.get("q", "")[:100], q.get("kind", ""))
    h.send_json({
        "items": [catalog.public(c) for c in shown], "total": len(cards),
        "errors": [{"path": p, "error": e} for p, e in errors],
        "kinds": catalog.KINDS, "groups": catalog.GROUPS, "targets": catalog.TARGETS, "levels": catalog.levels_public(),
        "colors": catalog.COLORS, "icons": list(catalog.ICONS), "overview": catalog.overview(cards),
        "engines": [{"id": c["id"], "title": c["title"]} for c in cards if c["kind"] == "engine"],
        "adapters": [{"id": c["id"], "title": c["title"]} for c in cards if c["kind"] == "adapter"],
        "plugins": sorted(r["name"] for r in report if r["title"] != "ошибка загрузки"),
        "hood": hood_state(h),
    })


@route("GET", "/api/catalog/card")
def api_catalog_card(h: Handler, q):
    cards, _, _ = _catalog(h, q)
    card = catalog.get(cards, q.get("id", ""))
    if card is None:
        return h.fail(404, "нет такой карточки")
    h.send_json({"card": catalog.public(card), "related": catalog.related(cards, card),
                 "editable": bool(card["path"]) and classify(card["path"])[0] == "edit", "hood": hood_state(h)})


@route("POST", "/api/catalog/preview")
def api_catalog_preview(h: Handler, q):
    h.send_json(catalog.preview(h.app.cfg, h.read_json().get("fields") or {}))


@route("POST", "/api/catalog/new")
def api_catalog_new(h: Handler, q):
    need_hood(h)
    body = h.read_json()
    if body.get("template"):
        rel = catalog.create_from_template(h.app.hood, str(body.get("kind") or ""), str(body.get("id") or "").strip(),
                                           str(body.get("title") or ""), actor=actor(h))
        cid = Path(rel).stem
    else:
        fields = body.get("fields") or {}
        rel = catalog.create(h.app.hood, h.app.cfg, fields, actor=actor(h))
        cid = Path(rel).stem
    h.send_json({"ok": True, "id": cid, "open": rel})


# ======================= прибор: начать с переходника =======================
@route("GET", "/api/connect")
def api_connect(h: Handler, q):
    from ..workshop import connect
    with guide.open_readonly(h.app.cfg, False) as c:          # проверки — про настоящее железо
        try:
            v = connect.view(h.app.cfg, q.get("card") or None, c.plugin_report, profiles=c.profiles,
                             sim=_sim(q))
        except ValueError as e:
            return h.fail(404, str(e))
    h.send_json(v)


# ======================= прибор: настройки инструментов =======================
@route("GET", "/api/tool")
def api_tool(h: Handler, q):
    from ..workshop import toolsettings
    name = q.get("name", "")
    try:
        desc = toolsettings.describe(h.app.cfg, name, h.app.hood.root)
    except toolsettings.ToolSettingsError as e:
        return h.fail(404, str(e))
    desc["status"] = toolsettings.status_with(h.app.cfg, name)
    desc["hood"] = hood_state(h)
    h.send_json(desc)


@route("POST", "/api/tool/check")
def api_tool_check(h: Handler, q):
    from ..workshop import toolsettings
    body = h.read_json()
    try:
        res = toolsettings.check(h.app.cfg, h.app.hood, str(body.get("name") or ""), body.get("values") or {})
    except toolsettings.ToolSettingsError as e:
        return h.fail(400, str(e))
    h.send_json(res)


@route("POST", "/api/tool/save")
def api_tool_save(h: Handler, q):
    from ..workshop import toolsettings
    need_hood(h)
    if h.app.busy():
        raise Busy("идёт задание — настройки инструментов после его окончания")
    body = h.read_json()
    try:
        res = toolsettings.save(h.app.cfg, h.app.hood, str(body.get("name") or ""), body.get("values") or {},
                                body.get("word"), actor(h))
    except toolsettings.ToolSettingsError as e:
        return h.fail(400, str(e))
    h.send_json(res)


# ======================= мастерская: самопроверка =======================
@route("GET", "/api/doctor")
def api_doctor(h: Handler, q):
    h.send_json({"items": doctor.check(h.app.cfg, _sim(q), deep=True, hood=h.app.hood), "hood": hood_state(h)})


@route("POST", "/api/doctor/heal")
def api_doctor_heal(h: Handler, q):
    if h.app.busy():
        raise Busy("идёт задание — самоисцеление после его окончания")
    body = h.read_json()
    iid = str(body.get("id") or "")
    if iid.startswith("file:"):
        need_hood(h)
    done = doctor.heal(h.app.cfg, iid, bool(body.get("sim")), hood=h.app.hood, word=body.get("word"))
    h.send_json({"ok": True, "done": done})


# ======================= мастерская: капот =======================
def hood_state(h: Handler) -> dict:
    return {"open": h.app.hood_open, "permanent": h.app.hood_permanent, "local": h.is_local(),
            "word": h.app.hood.word}


def need_hood(h: Handler) -> None:
    if not h.app.hood_open:
        raise HoodError("капот закрыт: откройте его на экране самого прибора («Прибор» → «Открыть капот») "
                        "или hood.edit: true в config.yaml")


def actor(h: Handler) -> str:
    return "экран прибора" if h.is_local() else f"сеть {h.client_address[0]}"


@route("GET", "/api/hood")
def api_hood(h: Handler, q):
    area = q.get("area") or "dev"
    h.send_json(dict(hood_state(h), area=area, areas=h.app.hood.areas(), files=h.app.hood.files(area)))


@route("POST", "/api/hood/unlock")
def api_hood_unlock(h: Handler, q):
    if not h.is_local():
        return h.fail(403, "открыть капот можно только с экрана самого прибора")
    h.app.hood_unlocked = bool(h.read_json().get("open", True))
    h.send_json(hood_state(h))


@route("GET", "/api/hood/file")
def api_hood_file(h: Handler, q):
    data = h.app.hood.read(q.get("path", ""))
    data["open"] = h.app.hood_open
    h.send_json(data)


@route("POST", "/api/hood/check")
def api_hood_check(h: Handler, q):
    body = h.read_json()
    h.send_json(h.app.hood.check(str(body.get("path") or ""), body.get("text")))


@route("POST", "/api/hood/save")
def api_hood_save(h: Handler, q):
    need_hood(h)
    body = h.read_json()
    h.send_json(h.app.hood.save(str(body.get("path") or ""), body.get("text"), body.get("word"), actor(h)))


@route("GET", "/api/hood/history")
def api_hood_history(h: Handler, q):
    rel = q.get("path", "")
    h.app.hood.path(rel, must_exist=False)
    h.send_json({"path": rel, "items": h.app.hood.history(rel)})


@route("GET", "/api/hood/version")
def api_hood_version(h: Handler, q):
    rel = q.get("path", "")
    h.app.hood.path(rel, must_exist=False)
    h.send_json({"path": rel, "v": q.get("v"), "text": h.app.hood.version(rel, q.get("v", ""))})


@route("POST", "/api/hood/restore")
def api_hood_restore(h: Handler, q):
    need_hood(h)
    body = h.read_json()
    h.send_json(h.app.hood.restore(str(body.get("path") or ""), str(body.get("v") or ""), body.get("word"),
                                   actor(h)))


# ======================= запуск =======================
def make_server(app: App, host: str, port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    app.host, app.port = host, httpd.server_address[1]
    return httpd


def serve(cfg: dict, host: str | None = None, port: int | None = None, sim: bool = False) -> int:
    s = web_settings(cfg)
    host = host or str(s["host"])
    port = int(port if port is not None else s["port"])
    app = App(cfg, sim_default=sim)
    try:
        httpd = make_server(app, host, port)
    except OSError as e:
        print(f"не удалось открыть порт {port}: {e}. Другой порт: python3 -m dirizher web --port 8081")
        return 1
    print(f"\n══ Дирижёр {__version__} — интерфейс в браузере ══")
    print(f"   экран прибора:  http://localhost:{app.port}")
    if host not in ("127.0.0.1", "localhost"):
        for url in app.urls():
            print(f"   телефон и ПК:   {url}")
        print(f"   код доступа:    {app.pin}")
    else:
        print("   доступ только с этого устройства (web.host: 127.0.0.1)")
    print(f"   капот:          {'открыт (hood.edit: true)' if app.hood_open else 'закрыт — открыть на экране прибора'}")
    try:
        found = [i for i in doctor.check(cfg, sim, deep=False, hood=app.hood) if i["level"] == "bad"]
        print("   самопроверка:   " + (f"проблем: {len(found)} — «Прибор» → «Самопроверка»" if found else "в порядке"))
    except Exception as e:                       # самопроверка не должна мешать запуску
        print(f"   самопроверка:   не выполнилась: {e}")
    print("   остановить:     Ctrl+C\n", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаю…", flush=True)
    finally:
        httpd.server_close()
        app.shutdown()
    return 0
