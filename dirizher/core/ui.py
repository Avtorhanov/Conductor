"""Ввод-вывод. Сейчас — консоль; позже здесь же появится веб-интерфейс для своего экрана.

Ядро общается с человеком только через этот класс, поэтому интерфейс можно заменить,
не трогая остальной код.
"""
from __future__ import annotations

import sys

YES = {"да", "д", "yes", "y"}
NO = {"нет", "н", "no", "n"}
LATER = {"позже", "п", "later", "l"}


class ConsoleUI:
    def __init__(self, color: bool | None = None):
        self.color = sys.stdout.isatty() if color is None else color

    # ---------- вывод ----------
    def out(self, text: str = "") -> None:
        print(text, flush=True)

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def header(self, text: str) -> None:
        self.out()
        self.out(self._c("1", f"══ {text} ══"))

    def step(self, n: int, total: int, text: str) -> None:
        self.out()
        self.out(self._c("1;36", f"[{n}/{total}] {text}"))

    def info(self, text: str) -> None:
        self.out(f"   {text}")

    def ok(self, text: str) -> None:
        self.out(self._c("32", f" ✔ {text}"))

    def warn(self, text: str) -> None:
        self.out(self._c("33", f" ⚠ {text}"))

    def error(self, text: str) -> None:
        self.out(self._c("31", f" ✖ {text}"))

    def finding(self, f) -> None:
        show = {"ok": self.ok, "info": self.info, "warn": self.warn,
                "fail": self.error, "block": self.error}.get(f.status, self.info)
        show(f"{f.check}: {f.message}")

    # ---------- ввод ----------
    def _ask(self, prompt: str) -> str:
        try:
            return input(prompt).strip().lower()
        except EOFError:
            return ""

    def pause(self, text: str) -> None:
        self._ask(f"   {text} ")

    def yes_no(self, question: str, allow_later: bool = False) -> str:
        """Возвращает "yes", "no" или "later". Пустой ответ — безопасный вариант."""
        hint = "да/нет/позже" if allow_later else "да/нет"
        while True:
            a = self._ask(f" ? {question} [{hint}]: ")
            if a in YES:
                return "yes"
            if a in NO:
                return "no"
            if allow_later and a in LATER:
                return "later"
            if a == "":
                return "later" if allow_later else "no"
            self.out(f"   Ответьте: {hint}")

    def choose(self, question: str, options: list) -> int | None:
        self.out(f" ? {question}")
        for i, opt in enumerate(options, 1):
            self.out(f"   {i}) {opt}")
        while True:
            a = self._ask("   Номер (Enter — отмена): ")
            if a == "":
                return None
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a) - 1

    def confirm_word(self, text: str, word: str) -> bool:
        """Подтверждение записи: нужно ввести слово целиком (ДА или YES)."""
        self.out(self._c("1;33", f" ! {text}"))
        a = self._ask(f"   Для записи введите {word} (или YES), иначе — отмена: ")
        return a in {word.lower(), "yes"}


class AutoUI(ConsoleUI):
    """Для автотестов: отвечает по заранее заданному сценарию и собирает вывод."""

    def __init__(self, answers=None, confirm: bool = True, echo: bool = False):
        super().__init__(color=False)
        self.answers = list(answers or [])
        self.confirm = confirm
        self.echo = echo
        self.lines: list = []

    def out(self, text: str = "") -> None:
        self.lines.append(text)
        if self.echo:
            print(text)

    def _next(self, default):
        return self.answers.pop(0) if self.answers else default

    def pause(self, text: str) -> None:
        pass

    def yes_no(self, question: str, allow_later: bool = False) -> str:
        a = self._next("yes")
        self.out(f" ? {question} -> {a}")
        return a

    def choose(self, question: str, options: list) -> int | None:
        a = self._next(0)
        self.out(f" ? {question} -> {a}")
        return a

    def confirm_word(self, text: str, word: str) -> bool:
        self.out(f" ! {text} -> {'подтверждено' if self.confirm else 'отказ'}")
        return self.confirm
