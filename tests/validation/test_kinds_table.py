"""Fixture tests for tests/validation/kinds_table.py.

The policy fails on a deployer pass that treats a kind apart without saying why, and passes on one that iterates the
table or states its reason.
"""

from __future__ import annotations

import ast
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from kinds_table import kind_findings, kind_vocabulary_problems, kinds_table_problems
from validation_support import write_fixture_tree


@dataclass(frozen=True)
class Kind:
    key: str
    label: str
    root: str


SUFFIX = "; iterate deployer/kinds.py's KINDS, or state why in KINDS_ALLOWED"


class KindsTableFixtures(unittest.TestCase):
    def problems(self, files: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return kinds_table_problems(root)

    def test_a_pass_naming_a_kind_by_literal_branching_on_one_or_listing_kinds_fails(self) -> None:
        files = {
            "deployer/strayed.py": (
                "from . import kinds as table\n"  # 1
                "from .kinds import ADAPTER, SHARED, SKILL\n"  # 2
                "\n"  # 3
                "ORDER = {SKILL: 0, ADAPTER: 1}\n"  # 4: a hand-written table of kinds
                "\n"  # 5
                "\n"  # 6
                "def hashes(data):\n"  # 7
                '    return data["skills"]\n'  # 8: a manifest key
                "\n"  # 9
                "\n"  # 10
                "def describe(kind):\n"  # 11
                "    if kind is SHARED:\n"  # 12: a branch on one kind
                '        return "shared asset"\n'  # 13: a report label
                "    return kind == table.AGENT_KIND.key\n"  # 14: a branch through an aliased module
                "\n"  # 15
                "\n"  # 16
                "class Journal:\n"  # 17
                "    def root(self, entry):\n"  # 18
                '        return entry.get("root", "claude")\n'  # 19: the skills root, looked up
                "\n"  # 20
                "\n"  # 21
                "def nested(kind):\n"  # 22
                "    def inner():\n"  # 23
                "        return kind in (SKILL, table.ADAPTER_KIND)\n"  # 24: a branch on two kinds, listed by hand
                "\n"  # 25
                "    return inner\n"
            ),
            "deploy.py": (
                "from deployer.kinds import ADAPTERS as KEY\n"
                "\n"
                "\n"
                "def adapters(data, key):\n"
                "    return key == KEY\n"  # 5: a branch through an absolute import
            ),
        }
        self.assertEqual(
            [
                "deploy.py:5 branches on a kind in adapters" + SUFFIX,
                "deployer/strayed.py:4 lists kinds by hand in ORDER" + SUFFIX,
                "deployer/strayed.py:8 names the kind literal 'skills' in hashes" + SUFFIX,
                "deployer/strayed.py:12 branches on a kind in describe" + SUFFIX,
                "deployer/strayed.py:13 names the kind literal 'shared asset' in describe" + SUFFIX,
                "deployer/strayed.py:14 branches on a kind in describe" + SUFFIX,
                "deployer/strayed.py:19 names the kind literal 'claude' in Journal.root" + SUFFIX,
                "deployer/strayed.py:24 branches on a kind in nested.inner" + SUFFIX,
                "deployer/strayed.py:24 lists kinds by hand in nested.inner" + SUFFIX,
            ],
            self.problems(files),
        )

    def test_a_pass_that_iterates_the_table_or_states_its_reason_passes(self) -> None:
        files = {
            # The table names every kind on purpose.
            "deployer/kinds.py": 'SKILL = "skills"\nKINDS = (SKILL,)\nORDER = {SKILL: 0, "agents": 1}\n',
            "deployer/kept.py": (
                'KINDS_ALLOWED = {"Plan.build": "plans skills first, and the kinds that depend on them after"}\n'
                "from pathlib import Path\n"
                "\n"
                "from .kinds import KINDS, SHARED, SKILL\n"
                "\n"
                "# A word skill names may not contain, and a command: neither is looked up as the skills root.\n"
                'RESERVED_WORDS = ("anthropic", "claude")\n'
                "\n"
                "\n"
                "def hashes(data):\n"
                '    """skills"""\n'
                "    return {kind.key: data[kind.key] for kind in KINDS}\n"
                "\n"
                "\n"
                "def destination(home):\n"
                '    return Path(home) / ".claude" / "skills", find("claude")\n'
                "\n"
                "\n"
                "def plan_skills(context):\n"
                "    return plan(context, SKILL)\n"
                "\n"
                "\n"
                "class Plan:\n"
                "    def build(self, kind):\n"
                '        return {SKILL: 0, SHARED: 1}[kind] if kind is SKILL else self.owned["shared"]\n'
            ),
            # A pass defined in a module-level block is named as one at the top is.
            "deployer/guarded.py": (
                'KINDS_ALLOWED = {"plan_first": "plans skills before the kinds that depend on them"}\n'
                "from .kinds import SKILL\n"
                "\n"
                "if True:\n"
                "\n"
                "    def plan_first(kind):\n"
                "        return kind is SKILL\n"
            ),
        }
        self.assertEqual([], self.problems(files))

    def test_an_allowance_must_give_a_reason_and_still_be_needed(self) -> None:
        files = {
            "deployer/gone.py": 'KINDS_ALLOWED = {"build": "it no longer branches"}\n\n\ndef build():\n    return 1\n',
            "deployer/unexplained.py": (
                'KINDS_ALLOWED = {"hashes": " "}\n\n\ndef hashes(data):\n    return data["agents"]\n'
            ),
        }
        self.assertEqual(
            [
                "deployer/unexplained.py:5 names the kind literal 'agents' in hashes" + SUFFIX,
                "deployer/gone.py: KINDS_ALLOWED allows build, which no longer treats a kind apart",
                "deployer/unexplained.py: KINDS_ALLOWED must map each pass to the reason it treats a kind apart",
            ],
            self.problems(files),
        )

    def test_a_case_that_matches_a_kind_and_a_table_keyed_by_one_kind_branch_on_it(self) -> None:
        def findings(body: str) -> list[tuple[int, str, str]]:
            return kind_findings(ast.parse(f"from deployer import kinds\n\n\ndef f(kind):\n{body}"), "deployer")

        self.assertEqual(
            [(6, "f", "branches on a kind")],
            findings("    match kind:\n        case kinds.SKILL:\n            return 1\n    return 0\n"),
        )
        self.assertEqual(
            [(6, "f", "names the kind literal 'claude'")],
            findings('    match kind:\n        case "claude":\n            return 1\n    return 0\n'),
        )
        self.assertEqual([(5, "f", "branches on a kind")], findings("    return {kinds.SKILL: 1}.get(kind, 0)\n"))
        self.assertEqual([(5, "f", "branches on a kind")], findings("    return 1 if kind == kinds.SKILL else 0\n"))
        # Iterating the table, and a case on anything else, treat no kind apart.
        self.assertEqual(
            [],
            findings(
                "    for each in kinds.KINDS:\n        print({each.key: 1})\n    match kind:\n"
                '        case "other":\n            return 1\n    return 0\n'
            ),
        )

    def test_every_string_the_table_gives_a_kind_is_pinned(self) -> None:
        pinned = Kind("skills", "", "claude")
        self.assertEqual([], kind_vocabulary_problems([pinned]))
        added = Kind("plugins", "plugin", "claude-plugins")
        self.assertEqual(
            [
                f"deployer/kinds.py gives a kind the {field} {value!r}, which KIND_STRINGS in "
                "tests/validation/kinds_table.py does not pin; add it there"
                for field, value in (
                    ("manifest key", "plugins"),
                    ("report label", "plugin"),
                    ("journal root", "claude-plugins"),
                )
            ],
            kind_vocabulary_problems([pinned, added]),
        )
