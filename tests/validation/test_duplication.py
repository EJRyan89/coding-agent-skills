"""Fixture tests for tests/validation/duplication.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from duplication import (
    containment_problems,
    duplicated_definition_problems,
    shared_fact_owner_problems,
    skill_core_copy_problems,
    skill_core_import_problems,
    skill_script_import_problems,
)
from validation_support import write_fixture_tree

# What a sanction no copy needs is reported as.
STALE = "which no other file defines identically or restates as a shared fact"


class DuplicatedDefinitionPolicy(unittest.TestCase):
    SHARED = "def shared(value):\n    total = value + 1\n    total *= 2\n    return total\n"

    DOCUMENTED = SHARED.replace(":\n", ':\n    """The same body, documented."""\n', 1)

    FIX = "; import one copy, or sanction it in each copy's module with DUPLICATION_ALLOWED"

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            write_fixture_tree(Path(temporary), files)
            return duplicated_definition_problems(Path(temporary))

    def test_a_definition_copied_into_another_file_fails_and_names_both(self) -> None:
        self.assertEqual(
            [f"shared is defined identically in deployer/one.py:1 and tools/two.py:3{self.FIX}"],
            self.problems({"deployer/one.py": self.SHARED, "tools/two.py": '"""Module."""\n\n' + self.DOCUMENTED}),
        )

    def test_two_scripts_of_one_skill_count_because_one_can_import_the_other(self) -> None:
        self.assertEqual(
            [f"shared is defined identically in skills/s/scripts/a.py:1 and skills/s/scripts/b.py:1{self.FIX}"],
            self.problems({"skills/s/scripts/a.py": self.SHARED, "skills/s/scripts/b.py": self.SHARED}),
        )

    def test_a_copy_sanctioned_in_every_module_passes(self) -> None:
        def allowed(other: str) -> str:
            return (
                f'DUPLICATION_ALLOWED = {{"shared": "{other} holds a copy; a skill cannot import the deployer"}}\n\n\n'
            )

        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/one.py": allowed("skills/s/scripts/two.py") + self.SHARED,
                    "skills/s/scripts/two.py": allowed("deployer/one.py's") + self.SHARED,
                }
            ),
        )

    def test_a_sanction_whose_reason_does_not_name_every_other_copy_fails(self) -> None:
        def allowed(reason: str) -> str:
            return f'DUPLICATION_ALLOWED = {{"shared": "{reason}"}}\n\n\n'

        self.assertEqual(
            [
                "deployer/one.py: DUPLICATION_ALLOWED gives a reason for shared that does not name every other copy: "
                "tools/three.py",
                "skills/s/scripts/two.py: DUPLICATION_ALLOWED gives a reason for shared that does not name every other "
                "copy: deployer/one.py and tools/three.py",
            ],
            self.problems(
                {
                    "deployer/one.py": allowed("skills/s/scripts/two.py cannot import the deployer") + self.SHARED,
                    "skills/s/scripts/two.py": allowed("a deployed skill cannot import the deployer") + self.SHARED,
                    "tools/three.py": allowed("deployer/one.py and skills/s/scripts/two.py hold copies") + self.SHARED,
                }
            ),
        )

    def test_no_allowance_sanctions_a_whole_module(self) -> None:
        # A module shared whole lives in ejryan89-skill-core and is imported from there, so "*" is a name nothing
        # defines.
        everything = 'DUPLICATION_ALLOWED = {"*": "the whole module is a sanctioned copy"}\n\n\n'
        self.assertEqual(
            [
                f"whole is defined identically in deployer/whole.py:4 and skills/s/scripts/whole.py:4{self.FIX}",
                f"deployer/whole.py: DUPLICATION_ALLOWED allows *, {STALE}",
                f"skills/s/scripts/whole.py: DUPLICATION_ALLOWED allows *, {STALE}",
            ],
            self.problems(
                {
                    "deployer/whole.py": everything + self.SHARED.replace("shared", "whole"),
                    "skills/s/scripts/whole.py": everything + self.SHARED.replace("shared", "whole"),
                }
            ),
        )

    def test_a_copy_sanctioned_in_only_some_modules_fails_and_names_every_copy(self) -> None:
        allowed = 'DUPLICATION_ALLOWED = {"shared": "a deployed skill cannot import the deployer"}\n'
        self.assertEqual(
            [
                "shared is defined identically in deployer/one.py:2, skills/s/scripts/two.py:2, and tools/three.py:1"
                + self.FIX
            ],
            self.problems(
                {
                    "deployer/one.py": allowed + self.SHARED,
                    "skills/s/scripts/two.py": allowed + self.SHARED,
                    "tools/three.py": self.SHARED,
                }
            ),
        )

    def test_a_stale_or_unexplained_sanction_fails(self) -> None:
        self.assertEqual(
            [
                f"deployer/one.py: DUPLICATION_ALLOWED allows shared, {STALE}",
                f"deployer/whole.py: DUPLICATION_ALLOWED allows *, {STALE}",
                "tools/vague.py: DUPLICATION_ALLOWED must map each name to the reason it is allowed",
            ],
            self.problems(
                {
                    "deployer/one.py": 'DUPLICATION_ALLOWED = {"shared": "copied into tools/two.py"}\n' + self.SHARED,
                    "tools/two.py": self.SHARED.replace("+ 1", "+ 2"),
                    "deployer/whole.py": 'DUPLICATION_ALLOWED = {"*": "no longer copied"}\n'
                    + self.SHARED.replace("shared", "whole"),
                    "tools/vague.py": 'DUPLICATION_ALLOWED = {"shared": ""}\n',
                }
            ),
        )

    def test_a_definition_in_a_module_level_if_try_or_with_block_is_a_copy(self) -> None:
        nested = "".join(f"    {line}" for line in self.SHARED.splitlines(keepends=True))
        self.assertEqual(
            [
                "shared is defined identically in deployer/branch.py:4, deployer/guarded.py:4, deployer/one.py:1, and "
                f"tools/managed.py:2{self.FIX}"
            ],
            self.problems(
                {
                    "deployer/one.py": self.SHARED,
                    "deployer/branch.py": "if FLAG:\n    pass\nelse:\n" + nested,
                    "deployer/guarded.py": "try:\n    import json\nexcept ImportError:\n" + nested,
                    "tools/managed.py": "with OPENED:\n" + nested,
                }
            ),
        )

    def test_a_copy_in_a_repository_skill_or_a_policy_module_fails(self) -> None:
        self.assertEqual(
            [
                "shared is defined identically in .claude/skills/r/scripts/a.py:1 and tests/validation/policy.py:1"
                f"{self.FIX}"
            ],
            self.problems(
                {
                    ".claude/skills/r/scripts/a.py": self.SHARED,
                    "tests/validation/policy.py": self.SHARED,
                    # A policy's fixture suite is a suite, not one of the policies.
                    "tests/validation/test_policy.py": self.SHARED,
                }
            ),
        )

    def test_short_nested_test_and_other_root_definitions_and_repeats_in_one_file_are_not_copies(self) -> None:
        short = "def short(value):\n    total = value + 1\n    return total\n"
        nested = "".join(f"    {line}" for line in self.SHARED.splitlines(keepends=True))
        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/one.py": self.SHARED + "\n\n" + short + "\n\n" + self.SHARED,
                    "deployer/nested.py": "def outer():\n" + nested + "    return shared\n",
                    "deployer/test_one.py": self.SHARED,
                    "skills/s/scripts/test_two.py": self.SHARED,
                    "tests/three.py": self.SHARED,
                    "tests/deployer/four.py": self.SHARED,
                    "tools/short.py": short,
                }
            ),
        )


class DuplicatedConstantPolicy(unittest.TestCase):
    FIX = " hold the same value; import one copy, or sanction it in each copy's module with DUPLICATION_ALLOWED"

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            write_fixture_tree(Path(temporary), files)
            return duplicated_definition_problems(Path(temporary))

    def test_a_pattern_compiled_in_another_module_under_another_name_fails_and_names_both(self) -> None:
        self.assertEqual(
            [f"VERSION at deployer/one.py:3 and TAG at tools/two.py:4{self.FIX}"],
            self.problems(
                {
                    "deployer/one.py": 'import re\n\nVERSION = re.compile(r"v[0-9]+")\n',
                    # The import alias and an annotation do not hide the copy.
                    "tools/two.py": (
                        'import re as regex\nfrom typing import Final\n\nTAG: Final = regex.compile("v[0-9]+")\n'
                    ),
                }
            ),
        )

    def test_a_pattern_is_compared_with_its_flags_by_value_and_through_a_from_import(self) -> None:
        pattern = '"^v[0-9]+$"'
        self.assertEqual(
            [f"FLAGGED at deployer/one.py:3 and SAME at tools/two.py:4{self.FIX}"],
            self.problems(
                {
                    "deployer/one.py": f"import re\n\nFLAGGED = re.compile({pattern}, re.I | re.M)\n",
                    "tools/two.py": (
                        "import re\nfrom re import compile as build\n\n"
                        f"SAME = build(pattern={pattern}, flags=re.MULTILINE | re.IGNORECASE)\n"
                    ),
                    "tools/plain.py": f"import re\n\nPLAIN = re.compile({pattern})\n",
                    "tools/other.py": f"import re\n\nOTHER = re.compile({pattern}, re.ASCII)\n",
                }
            ),
        )

    def test_a_value_under_a_sanction_name_that_is_not_a_dictionary_is_compared(self) -> None:
        self.assertEqual(
            [f"KINDS_ALLOWED at deployer/one.py:1 and KINDS_ALLOWED at tools/two.py:1{self.FIX}"],
            self.problems(
                {
                    "deployer/one.py": 'KINDS_ALLOWED = ("a sanction written as a tuple",)\n',
                    "tools/two.py": 'KINDS_ALLOWED = ("a sanction written as a tuple",)\n',
                }
            ),
        )

    def test_a_long_literal_or_a_collection_of_literals_copied_fails_wherever_it_is_assigned(self) -> None:
        self.assertEqual(
            [
                f"ROOTS at deployer/one.py:2 and FOLDERS at tools/two.py:3{self.FIX}",
                f"SPAN at deployer/one.py:1, FIRST at tools/two.py:1, and SECOND at tools/two.py:1{self.FIX}",
            ],
            self.problems(
                {
                    "deployer/one.py": (
                        'SPAN = "a long enough literal"\nROOTS = frozenset({"skills", "deployer", "tools"})\n'
                        'OTHER = ("skills", "deployer", "docs")\n'
                    ),
                    # A collection compares by its members in any order, and a conditional assignment is still one.
                    "tools/two.py": (
                        'FIRST = SECOND = "a long enough literal"\n'
                        'if READY:\n    FOLDERS = ["tools", "skills", "deployer"]\n'
                    ),
                }
            ),
        )

    def test_a_copy_sanctioned_in_every_module_under_its_own_name_passes(self) -> None:
        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/one.py": (
                        'DUPLICATION_ALLOWED = {"SPAN": "skills/s/scripts/two.py holds a copy"}\n'
                        'SPAN = "a long enough literal"\n'
                    ),
                    "skills/s/scripts/two.py": (
                        'DUPLICATION_ALLOWED = {"WIDE": "a copy of deployer/one.py\'s, which a skill cannot '
                        'import"}\nWIDE = "a long enough literal"\n'
                    ),
                }
            ),
        )

    def test_a_stale_sanction_or_one_that_does_not_name_the_other_copy_fails(self) -> None:
        self.assertEqual(
            [
                f"deployer/one.py: DUPLICATION_ALLOWED allows SPAN, {STALE}",
                "skills/s/scripts/two.py: DUPLICATION_ALLOWED gives a reason for WIDE that does not name every other "
                "copy: tools/three.py",
                "tools/three.py: DUPLICATION_ALLOWED gives a reason for NARROW that does not name every other copy: "
                "skills/s/scripts/two.py",
            ],
            self.problems(
                {
                    "deployer/one.py": (
                        'DUPLICATION_ALLOWED = {"SPAN": "copied into tools/four.py"}\nSPAN = "a long enough literal"\n'
                    ),
                    "tools/four.py": 'SPAN = "a long enough literal, changed"\n',
                    "skills/s/scripts/two.py": (
                        'DUPLICATION_ALLOWED = {"WIDE": "a skill cannot import tools"}\nWIDE = "another long literal"\n'
                    ),
                    "tools/three.py": (
                        'DUPLICATION_ALLOWED = {"NARROW": "a skill cannot import it"}\n'
                        'NARROW = "another long literal"\n'
                    ),
                }
            ),
        )

    def test_short_common_literals_exports_sanctions_and_values_below_module_level_are_not_copies(self) -> None:
        common = (
            'UNKNOWN = "unknown"\nRUN_FILE = "run.json"\nSHELLS = ("bash", "sh", "shell")\nTIMEOUT = 120\n'
            '__all__ = ["first_exported_name", "second_exported_name"]\n'
            'KINDS_ALLOWED = {"a sanction": "written as a dictionary"}\n'
            'def outer():\n    INNER = "a long enough literal"\n    return INNER\n'
            'class Holder:\n    INNER = "a long enough literal"\n'
            'BUILT = "a long enough " + "literal"\n'
        )
        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/one.py": common,
                    "tools/two.py": common,
                    "deployer/test_one.py": 'SPAN = "a long enough literal"\n',
                    "tests/three.py": 'SPAN = "a long enough literal"\n',
                    "tools/four.py": 'SPAN = "a long enough literal"\n',
                }
            ),
        )


class SharedFactPolicy(unittest.TestCase):
    GIT_CLIENT = "skills/ejryan89-skill-core/scripts/git_client.py"
    SUITE_NAMES = "skills/ejryan89-skill-core/scripts/suite_names.py"
    REVIEW_CONFIG = "skills/ejryan89-code-review-core/scripts/review_config.py"
    AGENT_SKILLS = "skills/ejryan89-skill-core/scripts/agent_skills.py"
    FRONTMATTER = "skills/ejryan89-skill-core/scripts/frontmatter.py"
    TYPED_TEXT = "skills/ejryan89-skill-core/scripts/typed_text.py"
    SKILL_LAYOUT = "tests/validation/skill_layout.py"
    OWNERS: ClassVar[dict[str, str]] = {
        GIT_CLIENT: (
            'GLOBAL_VALUE_OPTIONS = frozenset({"-c", "-C", "--attr-source", "--config-env", "--git-dir", '
            '"--work-tree"})\n'
        ),
        SUITE_NAMES: 'SUITE_NAME_PATTERNS = ("test_*", "test-*", "*_test", "*-test", "*.test.*")\n',
        REVIEW_CONFIG: (
            'DEFAULT_CONFIG_NAME = "config.json"\n\n\ndef default_config_path():\n'
            '    return HOME / "code-review" / DEFAULT_CONFIG_NAME\n'
        ),
        AGENT_SKILLS: "MAX_NAME = 64\n",
        FRONTMATTER: (
            'USER_ONLY_KEY = "disable-model-invocation"\n\n\ndef user_only(document):\n'
            '    return (document.string(USER_ONLY_KEY) or "").casefold() == "true"\n'
        ),
        TYPED_TEXT: (
            'TYPED_ENCODING = "utf-8-sig"\n\n\ndef read_typed(path):\n'
            "    return path.read_text(encoding=TYPED_ENCODING)\n"
        ),
        SKILL_LAYOUT: (
            'HOME_SPELLINGS = ("{{HOME}}", "$HOME", "${HOME}", "~")\n'
            "INSTALL_PATH = re.compile('|'.join(map(re.escape, HOME_SPELLINGS)))\n"
        ),
    }

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            write_fixture_tree(Path(temporary), {**self.OWNERS, **files})
            return duplicated_definition_problems(Path(temporary))

    def restated(self, module: str, line: int, fact: str, owner: str) -> str:
        return (
            f"{module}:{line} restates {fact}, which {owner} holds; import it from there, or sanction it in {module} "
            "with DUPLICATION_ALLOWED"
        )

    def test_a_diverged_copy_of_git_value_options_fails_and_an_imported_one_passes(self) -> None:
        guard = "tools/hub_guard.py"
        diverged = 'GIT_OPTIONS_WITH_VALUE = {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}\n'
        self.assertEqual(
            [self.restated(guard, 2, "GLOBAL_VALUE_OPTIONS", self.GIT_CLIENT)],
            self.problems({guard: '"""Guard."""\n' + diverged}),
        )
        imported = "from git_client import GLOBAL_VALUE_OPTIONS as GIT_OPTIONS_WITH_VALUE\n"
        # A command that passes two of the options is a call, not a copy of the vocabulary.
        command = 'def run(path, setting):\n    return ["git", "-C", path, "-c", setting, "status"]\n'
        self.assertEqual([], self.problems({guard: imported + command}))

    def test_a_re_derived_suite_pattern_fails_and_the_shared_rule_passes(self) -> None:
        discovery = "tests/validation/suite_discovery.py"
        derived = 'def suites(paths):\n    return [p for p in paths if fnmatch.fnmatchcase(p.name, "test_*.py")]\n'
        self.assertEqual(
            [self.restated(discovery, 2, "SUITE_NAME_PATTERNS", self.SUITE_NAMES)],
            self.problems({discovery: derived}),
        )
        shared = (
            "from suite_names import is_suite_name\n\n\ndef suites(paths):\n"
            '    """The suites among paths, each a test_* file or another suite name."""\n'
            "    return [path for path in paths if is_suite_name(path.name)]\n"
        )
        self.assertEqual([], self.problems({discovery: shared}))

    def test_a_restated_config_name_fails_in_an_importer_of_its_owner_only(self) -> None:
        dashboard = "skills/update-pr-dashboard/scripts/dashboard_pipeline.py"
        self.assertEqual(
            [self.restated(dashboard, 2, "DEFAULT_CONFIG_NAME", self.REVIEW_CONFIG)],
            self.problems({dashboard: 'from review_config import load\nDEFAULT_CONFIG_NAME = "config.json"\n'}),
        )
        self.assertEqual(
            [],
            self.problems(
                {
                    dashboard: "from review_config import DEFAULT_CONFIG_NAME, load\n",
                    # A module that does not import the owner names a file of its own.
                    "tools/canary.py": 'CONFIG = "config.json"\n',
                }
            ),
        )

    def test_a_restated_name_limit_fails_as_a_number_in_an_importer_or_a_message_anywhere(self) -> None:
        source = "deployer/source.py"
        restated = (
            "from agent_skills import RESERVED_WORDS\n\n\n"
            "def valid(name):\n    return len(name) <= 64\n\n\n"
            'RULES = "at most 64 characters"\n'
        )
        self.assertEqual(
            [
                *(self.restated(source, line, "MAX_NAME", self.AGENT_SKILLS) for line in (5, 8)),
                self.restated("tools/other.py", 1, "MAX_NAME", self.AGENT_SKILLS),
            ],
            self.problems({source: restated, "tools/other.py": 'MESSAGE = f"{name} is at most 64 letters"\n'}),
        )
        imported = (
            "from agent_skills import MAX_NAME\n\n\n"
            "def valid(name):\n    return len(name) <= MAX_NAME\n\n\n"
            'RULES = f"at most {MAX_NAME} characters"\nHASH = re.compile("[0-9a-f]{64}")\n'
        )
        # A module that does not import the owner may hold the number for another reason, such as a hash's length.
        self.assertEqual([], self.problems({source: imported, "tools/other.py": "SHA256_HEX = 64\n"}))

    def test_a_reading_of_the_user_only_key_fails_outside_the_shared_reader(self) -> None:
        reference = "tools/skill_reference.py"
        readings = {
            'user_only = values.get("disable-model-invocation", "").strip() == "true"\n': 1,
            'flag = document.string("disable-model-invocation")\n': 1,
            'user_only = values.get(USER_ONLY_KEY) == "True"\n': 1,
            'user_only = frontmatter.parse(text).value(frontmatter.USER_ONLY_KEY) != "false"\n': 1,
        }
        for reading, line in readings.items():
            with self.subTest(reading=reading):
                self.assertEqual(
                    [self.restated(reference, line, "USER_ONLY_KEY", self.FRONTMATTER)],
                    self.problems({reference: reading}),
                )
        writes = (
            'user_only = frontmatter.parse(text).user_only()\nLINES = ["disable-model-invocation: true"]\n'
            'lines.append("disable-model-invocation: true")\nvalue = values.get(USER_ONLY_KEY)\n'
        )
        self.assertEqual([], self.problems({reference: writes}))

    def test_a_title_or_body_read_with_another_codec_fails_and_the_typed_read_passes(self) -> None:
        support = "tests/validation/validation_support.py"
        reads = (
            'title = self.title.read_text(encoding="utf-8")\n',
            'body = Path(body_file).read_text("utf-8")\n',
            'handle = open(title_path, "r", encoding="utf-8")\n',
            'handle = open(self.body, encoding="UTF-8-SIG")\n',
        )
        for read in reads:
            with self.subTest(read=read):
                self.assertEqual(
                    [self.restated(support, 1, "TYPED_ENCODING", self.TYPED_TEXT)], self.problems({support: read})
                )
        typed = (
            "title = read_typed(self.title)\n"
            'body = self.body.read_text(encoding="utf-8-sig")\n'
            'notes = notes_file.read_text(encoding="utf-8")\n'
            "raw = self.body.read_text()\n"
        )
        self.assertEqual([], self.problems({support: typed}))

    def test_a_pattern_re_deriving_a_home_spelling_fails_and_prose_passes(self) -> None:
        guard = "tests/validation/agent_paths.py"
        for pattern in (r"\{\{HOME\}\}/\.claude", r"\$HOME/x", r"\$\{HOME\}/x"):
            with self.subTest(pattern=pattern):
                self.assertEqual(
                    [self.restated(guard, 1, "HOME_SPELLINGS", self.SKILL_LAYOUT)],
                    self.problems({guard: f"PATH = re.compile(r'{pattern}')\n"}),
                )
        prose = 'MESSAGE = "finds a file through $HOME or ~/.claude"\n'
        self.assertEqual([], self.problems({guard: prose}))

    def test_the_owner_and_the_suites_may_hold_the_fact(self) -> None:
        literal = 'PATTERNS = ("test_*", "*_test")\nOPTIONS = ("-c", "-C", "--git-dir", "--work-tree")\n'
        self.assertEqual(
            [],
            self.problems(
                {
                    self.SUITE_NAMES: self.OWNERS[self.SUITE_NAMES] + "GLOB = 'test_*.py'\n",
                    "tests/validation/test_policy.py": literal,
                    "tools/test_tool.py": literal,
                }
            ),
        )

    def test_a_sanctioned_restatement_passes_only_with_a_reason_naming_its_owner(self) -> None:
        module = "tests/validation/python_checks.py"
        glob = 'IGNORED = {"skills/*/scripts/test_*.py": ["S101"]}\n'

        def sanctioned(reason: str) -> str:
            return f'DUPLICATION_ALLOWED = {{"SUITE_NAME_PATTERNS": "{reason}"}}\n' + glob

        self.assertEqual([], self.problems({module: sanctioned(f"pyproject.toml cannot import {self.SUITE_NAMES}")}))
        self.assertEqual(
            [
                f"{module}: DUPLICATION_ALLOWED gives a reason for SUITE_NAME_PATTERNS that does not name "
                f"{self.SUITE_NAMES}"
            ],
            self.problems({module: sanctioned("pyproject.toml cannot import it")}),
        )
        self.assertEqual(
            [f"{module}: DUPLICATION_ALLOWED allows SUITE_NAME_PATTERNS, {STALE}"],
            self.problems({module: sanctioned(f"once restated {self.SUITE_NAMES}").replace(glob, "")}),
        )

    def test_an_owner_that_no_longer_holds_its_fact_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, self.OWNERS)
            self.assertEqual([], shared_fact_owner_problems(root))
            # The literal moved inline, as review_config held it before its constant.
            (root / self.REVIEW_CONFIG).write_text(
                'def default_config_path():\n    return HOME / "code-review" / "config.json"\n', encoding="utf-8"
            )
            (root / self.GIT_CLIENT).unlink()
            self.assertEqual(
                [
                    f"{owner} no longer holds {fact} as a literal; name its new home in the shared facts of "
                    "tests/validation/duplication.py"
                    for owner, fact in (
                        (self.GIT_CLIENT, "GLOBAL_VALUE_OPTIONS"),
                        (self.REVIEW_CONFIG, "DEFAULT_CONFIG_NAME"),
                    )
                ],
                shared_fact_owner_problems(root),
            )


class SkillCoreHomePolicy(unittest.TestCase):
    STATEMENT = (
        'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))\n'
    )

    HEADER = "import sys\nfrom pathlib import Path\n\n"

    CORE: ClassVar[dict[str, str]] = {
        "skills/ejryan89-skill-core/scripts/shared.py": "import json\n",
        "skills/ejryan89-skill-core/scripts/test_shared.py": "",
    }

    DOC = '"Validation" in docs/adding-a-skill.md'

    def run_policy(self, policy: Callable[[Path], list[str]], files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            write_fixture_tree(Path(temporary), {**self.CORE, **files})
            return policy(Path(temporary))

    def test_a_module_named_like_a_skill_core_module_outside_it_is_a_copy(self) -> None:
        self.assertEqual(
            [
                ".claude/skills/r/scripts/shared.py has the name of skills/ejryan89-skill-core/scripts/shared.py; "
                "import shared "
                f"from ejryan89-skill-core instead of keeping another copy; see {self.DOC}",
                "deployer/shared.py has the name of skills/ejryan89-skill-core/scripts/shared.py; import shared from "
                f"ejryan89-skill-core instead of keeping another copy; see {self.DOC}",
                "skills/other/scripts/shared.py has the name of skills/ejryan89-skill-core/scripts/shared.py; import "
                "shared "
                f"from ejryan89-skill-core instead of keeping another copy; see {self.DOC}",
                "tests/validation/shared.py has the name of skills/ejryan89-skill-core/scripts/shared.py; import "
                "shared from "
                f"ejryan89-skill-core instead of keeping another copy; see {self.DOC}",
            ],
            self.run_policy(
                skill_core_copy_problems,
                {
                    ".claude/skills/r/scripts/shared.py": "import json\n",
                    "deployer/shared.py": "import json\n",
                    "skills/other/scripts/shared.py": "import json\n",
                    "tests/validation/shared.py": "import json\n",
                    # Suites, and files outside the duplication policy's roots, may carry the name.
                    "skills/other/scripts/test_shared.py": "import shared\n",
                    "tests/shared.py": "import json\n",
                    "tools/sharing.py": "import json\n",
                },
            ),
        )

    def test_deployer_and_tools_import_skill_core_after_the_path_statement(self) -> None:
        self.assertEqual(
            [
                "deployer/extra.py:4 puts ejryan89-skill-core on sys.path; deployer/__init__.py is the deployer's one "
                "place; "
                f"see {self.DOC}",
                "tools/early.py:1 imports shared before putting skills/ejryan89-skill-core/scripts on sys.path; "
                f"see {self.DOC}",
                "tools/missing.py:1 imports shared before putting skills/ejryan89-skill-core/scripts on sys.path; see "
                f"{self.DOC}",
            ],
            self.run_policy(
                skill_core_import_problems,
                {
                    "deployer/__init__.py": '"""Package."""\n\n' + self.HEADER + self.STATEMENT,
                    # A package's __init__.py runs before its modules, so a deployer module imports ejryan89-skill-core
                    # freely.
                    "deployer/user.py": "from __future__ import annotations\n\nimport shared\n\nfrom . import extra\n",
                    "deployer/extra.py": self.HEADER + self.STATEMENT,
                    "tools/good.py": self.HEADER + self.STATEMENT + "\nimport shared\n",
                    "tools/early.py": "import shared\n" + self.HEADER + self.STATEMENT,
                    "tools/missing.py": "from shared import thing\n",
                    "tools/unrelated.py": "import json\n",
                },
            ),
        )

    def test_skill_core_imports_nothing_from_the_deployer_and_the_deployer_only_the_standard_library(self) -> None:
        def reaches(name: str, line: int, module: str) -> str:
            return (
                f"{name}:{line} imports {module}; ejryan89-skill-core imports nothing from the deployer or tools/; see "
                f"{self.DOC}"
            )

        def foreign(name: str, line: int, module: str) -> str:
            return (
                f"{name}:{line} imports {module}, which is neither the standard library, the deployer, nor "
                "ejryan89-skill-core; "
                f"see {self.DOC}"
            )

        self.assertEqual(
            [
                foreign("deployer/plan.py", 1, "yaml"),
                reaches("skills/ejryan89-skill-core/scripts/reach.py", 1, "deployer"),
                reaches("skills/ejryan89-skill-core/scripts/reach.py", 2, "tools.thing"),
                foreign("skills/ejryan89-skill-core/scripts/reach.py", 3, "requests"),
                reaches("skills/ejryan89-skill-core/scripts/test_shared.py", 1, "deployer.paths"),
            ],
            self.run_policy(
                skill_core_import_problems,
                {
                    "deployer/__init__.py": self.HEADER + self.STATEMENT,
                    "deployer/plan.py": "import yaml\nimport tomllib\n\nimport shared\n\nfrom . import paths\n",
                    "skills/ejryan89-skill-core/scripts/reach.py": (
                        "from deployer import paths\nimport tools.thing\nimport requests\n"
                    ),
                    "skills/ejryan89-skill-core/scripts/test_shared.py": "import deployer.paths\nimport shared\n",
                    # A suite may import what it tests with; only the direction rule holds it.
                    "skills/ejryan89-skill-core/scripts/test_other.py": "import unittest\nimport shared\nimport "
                    "harness\n",
                },
            ),
        )

    def test_a_module_imported_by_name_is_held_as_an_import_statement_is(self) -> None:
        unread = (
            "deployer/dynamic.py:5 imports a module by a name it cannot read; import it by a statement or one string "
            f"literal; see {self.DOC}"
        )
        self.assertEqual(
            [
                f"deployer/dynamic.py:2 imports yaml, which is neither the standard library, the deployer, nor "
                f"ejryan89-skill-core; see {self.DOC}",
                f"deployer/dynamic.py:3 imports requests, which is neither the standard library, the deployer, nor "
                f"ejryan89-skill-core; see {self.DOC}",
                unread,
            ],
            self.run_policy(
                skill_core_import_problems,
                {
                    "deployer/__init__.py": self.HEADER + self.STATEMENT,
                    "deployer/dynamic.py": "import importlib\nyaml = importlib.import_module('yaml')\n"
                    "requests = __import__('requests')\njson = importlib.import_module('json')\n"
                    "chosen = importlib.import_module(NAME)\n",
                },
            ),
        )

    def test_a_shipped_skill_script_imports_only_what_a_deployment_holds(self) -> None:
        def foreign(line: int, module: str) -> str:
            return (
                f"skills/demo/scripts/run.py:{line} imports {module}, which is neither the standard library, "
                f"ejryan89-skill-core, the skill's own scripts, nor a skill its skill_deps reach; see {self.DOC}"
            )

        self.assertEqual(
            [
                foreign(1, "requests"),
                foreign(6, "lonely"),
                foreign(8, "yaml"),
            ],
            self.run_policy(
                skill_script_import_problems,
                {
                    "deploy-meta/demo.json": '{"skill_deps": ["ejryan89-skill-core", "middle"]}',
                    "deploy-meta/middle.json": '{"skill_deps": ["deep"]}',
                    "skills/ejryan89-skill-core/SKILL.md": "",
                    "skills/demo/SKILL.md": "",
                    "skills/middle/SKILL.md": "",
                    "skills/deep/SKILL.md": "",
                    "skills/other/SKILL.md": "",
                    "skills/middle/scripts/middling.py": "",
                    "skills/deep/scripts/deepest.py": "",
                    "skills/other/scripts/lonely.py": "",
                    "skills/demo/scripts/helper.py": "",
                    "skills/demo/scripts/run.py": "import requests\nimport json\nimport shared\nimport helper\n"
                    "import middling, deepest\nimport lonely\nimport importlib\nimportlib.import_module('yaml')\n",
                    # A suite may import what it tests with.
                    "skills/demo/scripts/test_run.py": "import harness\n",
                },
            ),
        )


@pytest.mark.parametrize(
    "statement",
    [
        pytest.param("", id="no statement"),
        pytest.param(
            'sys.path.insert(0, str(Path(SOURCE) / "skills" / "ejryan89-skill-core" / "scripts"))\n',
            id="located from the configured source",
        ),
        pytest.param(SkillCoreHomePolicy.STATEMENT.replace("insert(0,", "insert(1,"), id="not first"),
        pytest.param(SkillCoreHomePolicy.STATEMENT.replace("insert(0, ", "append("), id="appended"),
        pytest.param("def later() -> None:\n    " + SkillCoreHomePolicy.STATEMENT, id="inside a function"),
    ],
)
def test_the_deployer_states_the_path_first_and_relative_to_itself(tmp_path: Path, statement: str) -> None:
    expected = [
        "deployer/__init__.py does not put skills/ejryan89-skill-core/scripts first on sys.path, located from its own "
        f"file; see {SkillCoreHomePolicy.DOC}"
    ]
    write_fixture_tree(
        tmp_path, {**SkillCoreHomePolicy.CORE, "deployer/__init__.py": SkillCoreHomePolicy.HEADER + statement}
    )
    assert expected == skill_core_import_problems(tmp_path)


CONTAINMENT_SOURCE = """import os
import os.path as osp
from os.path import commonpath
from pathlib import Path

from path_containment import posix_within, within


def hand_written(path: Path, root: Path, name: str, top: str) -> list[bool]:
    return [
        path.is_relative_to(root),
        os.path.commonpath([path, root]) == root,
        osp.commonprefix([str(path), str(root)]) == str(root),
        commonpath([path, root]) == root,
        name.startswith(f"{top}/"),
        name.startswith(top + "/"),
        str(path).startswith(str(root) + os.sep),
        path.parts[: len(root.parts)] == root.parts,
    ]


def conforms(path: Path, root: Path, name: str, top: str) -> list[bool]:
    return [within(path, root), posix_within(name, top), name.startswith(top), name.startswith("prefix-")]


def told_apart(name: str, top: str) -> bool:
    return name.startswith(top + "/")


CONTAINMENT_ALLOWED = {"told_apart": "a reason", "stale": "a reason"}
"""


def test_containment_policy_refuses_a_containment_test_outside_path_containment(tmp_path: Path) -> None:
    write_fixture_tree(
        tmp_path,
        {
            "skills/alpha/scripts/alpha.py": CONTAINMENT_SOURCE,
            # A suite tests the refusals with the forms it refuses, and the module holds the one test.
            "skills/alpha/scripts/test_alpha.py": CONTAINMENT_SOURCE,
            "skills/ejryan89-skill-core/scripts/path_containment.py": CONTAINMENT_SOURCE,
            "tools/broken.py": "CONTAINMENT_ALLOWED = {'scope': ''}\n",
        },
    )
    remedy = (
        "; call ejryan89-skill-core's path_containment, the one containment test, or state why in CONTAINMENT_ALLOWED"
    )
    alpha = "skills/alpha/scripts/alpha.py"
    prefix = "a startswith of a folder and its separator"
    found = containment_problems(tmp_path, sorted(tmp_path.rglob("*.py")))
    assert found == [
        f"{alpha}:11 tests containment with is_relative_to{remedy}",
        f"{alpha}:12 tests containment with commonpath{remedy}",
        f"{alpha}:13 tests containment with commonprefix{remedy}",
        f"{alpha}:14 tests containment with commonpath{remedy}",
        *(f"{alpha}:{line} tests containment with {prefix}{remedy}" for line in (15, 16, 17)),
        f"{alpha}:18 tests containment with a comparison of a path's leading parts{remedy}",
        f"{alpha}: CONTAINMENT_ALLOWED allows stale, which holds nothing it sanctions; remove the entry",
        "tools/broken.py: CONTAINMENT_ALLOWED must map each function, class, or assignment to its reason",
    ]
