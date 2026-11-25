"""Согласованность документов с кодом (S6, 0.7.0): команды README, нет чисел тестов, карточки лезвий, ссылки."""
import argparse
import re
import unittest
from pathlib import Path

import yaml

from dirizher import __main__ as cli
from dirizher.core.registry import discover
from dirizher.core.skills import SkillDB

ROOT = Path(__file__).resolve().parent.parent
DOCS = [ROOT / "README.md", ROOT / "PRINCIPLES.md", *sorted((ROOT / "docs").glob("*.md")), *sorted((ROOT / "help").glob("*.md"))]
# журнал сессий и план хранят историю (там числа тестов и пути прошлых версий уместны)
HISTORY = {"STATUS.md", "PLAN.md", "SESSION_PROMPT.md"}


def _subcommands():
    parser = cli.build_parser()
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return set(a.choices)
    return set()


class DocsConsistency(unittest.TestCase):
    def test_readme_commands_match_argparse(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        start = text.index("## Команды")
        section = text[start:text.index("\n## ", start + 5)]
        listed = set()
        for line in section.splitlines():
            m = re.match(r"\|\s*(?:\*\(без команды\)\* или )?`([a-z]+)", line)
            if m:
                listed.add(m.group(1))
        commands = _subcommands()
        self.assertTrue(commands, "в argparse не нашлись команды")
        self.assertEqual(commands - listed, set(), "команды есть в программе, но нет в таблице README")
        self.assertEqual(listed - commands, set(), "в таблице README есть команды, которых нет в программе")

    def test_no_test_counts_in_docs(self):
        pat = re.compile(r"\b\d+\s*(?:автотест\w*|тест(?:ов|а|ы)?)\b(?!\w)", re.I)
        bad = []
        for p in DOCS:
            if p.name in HISTORY:
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if pat.search(line):
                    bad.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()[:90]}")
        self.assertEqual(bad, [], "в документах не должно быть количества тестов — оно устаревает")

    def test_every_plugin_has_engine_card(self):
        found, _ = discover(SkillDB(ROOT / "skills").active)
        cards = set()
        for f in [*(ROOT / "catalog" / "engines").glob("*.yaml"), *(ROOT / "skills").glob("*/catalog/*.yaml")]:
            data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            if data.get("plugin"):
                cards.add(data["plugin"])
        missing = sorted(n for n in found if n not in cards and n != "simulator" and not n.startswith("_"))
        self.assertEqual(missing, [], f"у лезвий нет карточки движка с полем plugin: {missing}")

    def test_file_references_exist(self):
        prefixes = ("docs/", "help/", "catalog/", "tools/", "tests/", "dirizher/", "skills/", "profiles/", "system/")
        exts = (".md", ".py", ".yaml", ".yml", ".js", ".html", ".rules", ".json")
        bad = []
        for p in DOCS:
            if p.name in HISTORY:
                continue
            text = p.read_text(encoding="utf-8")
            refs = set()
            for m in re.finditer(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
                t = m.group(1)
                if not re.match(r"[a-z]+://|mailto:", t):
                    refs.add(("rel", t))
            for m in re.finditer(r"`([^`\s]+)`", text):
                t = m.group(1)
                if t.startswith(prefixes) and t.endswith(exts) and not re.search(r"[<>*{}\[\]]", t):
                    refs.add(("root", t))
            for kind, t in refs:
                path = (p.parent / t) if kind == "rel" else (ROOT / t)
                if not path.resolve().exists():
                    bad.append(f"{p.relative_to(ROOT)}: {t}")
        self.assertEqual(bad, [], "ссылки на несуществующие файлы")

    def test_docs_do_not_send_user_to_config_for_tools(self):
        """Тексты урока и справки ведут на форму, а не просят «впишите в config.yaml» для настроек лезвий."""
        guide = (ROOT / "dirizher" / "interface" / "guide.yaml").read_text(encoding="utf-8")
        self.assertNotRegex(guide, r"(Включите|Впишите|Укажите)[^\n]{0,80}в config\.yaml")
        self.assertNotRegex(guide, r"\n\s+code: \|\n\s+plugins:")

if __name__ == "__main__":
    unittest.main()
