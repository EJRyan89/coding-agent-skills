"""Fixture tests for tests/validation/language_readers.py.

The policy fails on a pattern that spells a language's structure outside the module that owns the language, and on a
brace-depth count, and passes on the same pattern in the owner, on one the module sanctions with a reason, and on a
pattern of another language that only looks alike. A sanction that excuses nothing, or gives no reason, fails.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from language_readers import (
    BASH_OWNER,
    MARKDOWN_OWNER,
    PROSE_PATH_OWNER,
    language_reader_problems,
    spelled_families,
)
from validation_support import REPOSITORY_ROOT, parse_python, write_fixture_tree

READER = "tools/reader.py"
# One pattern of each family as a reader outside the owner would write it, with the family it spells and its owner.
FAMILY_PATTERNS = {
    "a Bash function header": (BASH_OWNER, r'HEADER = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\s*\)\s*\{")'),
    "a Bash `function` header": (BASH_OWNER, r'HEADER = re.compile(r"^\s*function\s+(\w+)")'),
    "a `#` comment test": (BASH_OWNER, r'HEADER = re.compile(r"^\s*#")'),
    "an ATX heading by its marks": (MARKDOWN_OWNER, r'HEADER = re.compile(r" {0,3}(#{1,6})[ \t]+(.*)")'),
    "an ATX heading by its first marks": (MARKDOWN_OWNER, r'HEADER = re.compile(r"## (.*)")'),
    "an f-string's ATX heading": (MARKDOWN_OWNER, r'HEADER = re.compile(rf"^## {NAME}[ \t]*$")'),
    "a single-backtick code span": (MARKDOWN_OWNER, r'HEADER = re.compile(r"`([^`]+)`")'),
    "a code span after a lookahead": (MARKDOWN_OWNER, r'HEADER = re.compile(r"`(?!--)[^`\n]+`")'),
    "a fence prompt": (MARKDOWN_OWNER, r'HEADER = re.compile(r"\A[ \t]*\$ ")'),
    "a prose path": (PROSE_PATH_OWNER, r'HEADER = re.compile(r"(?<![\w./-])(?:[\w.-]+/)+[\w.-]+\.\w+")'),
}
BRACE_COUNT = "def depth(line):\n    return line.count('{') - line.count('}')\n"
ALLOWED = 'LANGUAGE_READER_ALLOWED = {"HEADER": "reads a format no language owner reads"}\n'


class LanguageReaderCase(unittest.TestCase):
    """Runs the policy over fixture files written to a temporary root."""

    def problems(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files, newline="\n")
            return language_reader_problems(root, [root / name for name in files])

    def module(self, pattern: str, header: str = "import re\n\nNAME = 'x'\n") -> str:
        return f"{header}{pattern}\n"


class LanguageReaderFixtures(LanguageReaderCase):
    def test_each_family_fails_outside_its_owner_and_passes_in_it(self) -> None:
        for label, (owner, pattern) in FAMILY_PATTERNS.items():
            with self.subTest(label):
                problems = self.problems({READER: self.module(pattern)})
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith(f"{READER}:4 spells "), problems)
                self.assertIn(f"outside {owner}, the module that owns its reading", problems[0])
                self.assertIn("LANGUAGE_READER_ALLOWED", problems[0])
                self.assertEqual([], self.problems({owner: self.module(pattern)}))

    def test_a_brace_depth_count_fails_outside_the_bash_grammar_and_passes_in_it(self) -> None:
        problems = self.problems({READER: BRACE_COUNT})
        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(f"{READER}:2 spells a brace-depth count outside {BASH_OWNER}"))
        self.assertEqual([], self.problems({BASH_OWNER: BRACE_COUNT}))

    def test_a_sanctioned_reader_passes_and_a_stale_or_reasonless_sanction_fails(self) -> None:
        pattern = FAMILY_PATTERNS["a `#` comment test"][1]
        self.assertEqual([], self.problems({READER: self.module(pattern, f"import re\n\n{ALLOWED}")}))
        stale = self.problems({READER: f"import re\n\n{ALLOWED}HEADER = re.compile(r'#\\d+')\n"})
        self.assertEqual(
            [f"{READER}: LANGUAGE_READER_ALLOWED allows HEADER, which holds nothing it sanctions; remove the entry"],
            stale,
        )
        reasonless = self.problems(
            {READER: self.module(pattern, 'import re\n\nLANGUAGE_READER_ALLOWED = {"HEADER": ""}\n')}
        )
        self.assertIn(
            f"{READER}: LANGUAGE_READER_ALLOWED must map each function, class, or assignment to its reason", reasonless
        )

    def test_a_pattern_call_is_read_through_the_module_s_imports(self) -> None:
        aliased = "import re as regex\n\n\ndef read(line):\n    return regex.match(r'^\\s*#', line)\n"
        imported = "from re import fullmatch\n\n\ndef read(line):\n    return fullmatch(r'## (.*)', line)\n"
        for name, text in (("an aliased module", aliased), ("an imported function", imported)):
            with self.subTest(name):
                problems = self.problems({READER: text})
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith(f"{READER}:5 spells "), problems)

    def test_a_pattern_of_another_language_that_only_looks_alike_passes(self) -> None:
        # A noqa or type-ignore comment's `#`, an issue reference, a YAML comment after a blank, a C# directive, and an
        # escaped `#` spell none of the families.
        patterns = [
            r"#\s*noqa\b",
            r"#\d+(?:,? (?:and )?#\d+)*",
            r"(?:^|[ \t])#",
            r"[ \t]*#[ \t]*([A-Za-z]+)",
            r"\\*#",
            r"`--[\w-]+",
        ]
        module = "import re\n\n" + "".join(
            f"P{index} = re.compile(r{pattern!r})\n" for index, pattern in enumerate(patterns)
        )
        self.assertEqual([], self.problems({READER: module}))

    def test_a_pattern_held_in_a_name_and_joined_in_later_is_not_read_as_the_module_states(self) -> None:
        module = "import re\n\nSPAN = r'`[^`]+`'\nCUE = re.compile(rf'see {SPAN}')\n"
        self.assertEqual([], self.problems({READER: module}))

    def test_a_module_that_does_not_parse_is_named_and_the_others_are_still_read(self) -> None:
        # A file no policy can read is a problem by its name, never one read as holding no pattern, and the policy
        # goes on to the modules after it.
        violating = self.module(FAMILY_PATTERNS["a `#` comment test"][1])
        problems = self.problems({"tools/broken.py": "def broken(:\n", READER: violating})
        self.assertEqual(2, len(problems), problems)
        self.assertTrue(problems[0].startswith("tools/broken.py "), problems)
        self.assertTrue(problems[1].startswith(f"{READER}:4 spells a `#` comment test outside {BASH_OWNER}"), problems)

    def test_suites_and_fixtures_are_not_read(self) -> None:
        pattern = self.module(FAMILY_PATTERNS["a `#` comment test"][1])
        files = {"tools/test_reader.py": pattern, "tests/fixtures/tool/reader.py": pattern}
        self.assertEqual(
            ["no Python file was read, so the language reader policy could not judge anything"], self.problems(files)
        )

    def test_the_owners_spell_the_families_they_own(self) -> None:
        # The policy passes over each owner because it reads the language there, as the owner's own patterns show.
        tree = parse_python(REPOSITORY_ROOT / MARKDOWN_OWNER)
        self.assertTrue(isinstance(tree, ast.Module))
        found = {family.name for _, _, family in spelled_families(tree)}
        self.assertLessEqual({"an ATX heading", "a fence prompt"}, found)
