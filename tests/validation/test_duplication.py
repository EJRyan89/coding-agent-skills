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

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from duplication import duplicated_definition_problems, skill_core_copy_problems, skill_core_import_problems
from validation_support import write_fixture_tree


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
        allowed = 'DUPLICATION_ALLOWED = {"shared": "a deployed skill cannot import the deployer"}\n\n\n'
        self.assertEqual(
            [],
            self.problems({"deployer/one.py": allowed + self.SHARED, "skills/s/scripts/two.py": allowed + self.SHARED}),
        )

    def test_no_allowance_sanctions_a_whole_module(self) -> None:
        # A module shared whole lives in skill-core and is imported from there, so "*" is a name nothing defines.
        everything = 'DUPLICATION_ALLOWED = {"*": "the whole module is a sanctioned copy"}\n\n\n'
        self.assertEqual(
            [
                f"whole is defined identically in deployer/whole.py:4 and skills/s/scripts/whole.py:4{self.FIX}",
                "deployer/whole.py: DUPLICATION_ALLOWED allows *, which no other file defines identically",
                "skills/s/scripts/whole.py: DUPLICATION_ALLOWED allows *, which no other file defines identically",
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
                "shared is defined identically in deployer/one.py:2, skills/s/scripts/two.py:2 and tools/three.py:1"
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
                "deployer/one.py: DUPLICATION_ALLOWED allows shared, which no other file defines identically",
                "deployer/whole.py: DUPLICATION_ALLOWED allows *, which no other file defines identically",
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

    def test_short_nested_test_and_other_root_definitions_and_repeats_in_one_file_are_not_copies(self) -> None:
        short = "def short(value):\n    total = value + 1\n    return total\n"
        nested = "".join(f"    {line}" for line in self.SHARED.splitlines(keepends=True))
        self.assertEqual(
            [],
            self.problems(
                {
                    "deployer/one.py": self.SHARED + "\n\n" + short + "\n\n" + self.SHARED,
                    "deployer/nested.py": "if True:\n" + nested,
                    "deployer/test_one.py": self.SHARED,
                    "skills/s/scripts/test_two.py": self.SHARED,
                    "tests/three.py": self.SHARED,
                    "tools/short.py": short,
                }
            ),
        )


class SkillCoreHomePolicy(unittest.TestCase):
    STATEMENT = 'sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))\n'

    HEADER = "import sys\nfrom pathlib import Path\n\n"

    CORE: ClassVar[dict[str, str]] = {
        "skills/skill-core/scripts/shared.py": "import json\n",
        "skills/skill-core/scripts/test_shared.py": "",
    }

    DOC = '"Validation" in docs/adding-a-skill.md'

    def run_policy(self, policy: Callable[[Path], list[str]], files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            write_fixture_tree(Path(temporary), {**self.CORE, **files})
            return policy(Path(temporary))

    def test_a_module_named_like_a_skill_core_module_outside_it_is_a_copy(self) -> None:
        self.assertEqual(
            [
                "deployer/shared.py has the name of skills/skill-core/scripts/shared.py; import shared from "
                f"skill-core instead of keeping another copy; see {self.DOC}",
                "skills/other/scripts/shared.py has the name of skills/skill-core/scripts/shared.py; import shared "
                f"from skill-core instead of keeping another copy; see {self.DOC}",
            ],
            self.run_policy(
                skill_core_copy_problems,
                {
                    "deployer/shared.py": "import json\n",
                    "skills/other/scripts/shared.py": "import json\n",
                    # Suites, and files outside deployer/, tools/, and skills/, may carry the name.
                    "skills/other/scripts/test_shared.py": "import shared\n",
                    "tests/shared.py": "import json\n",
                    "tools/sharing.py": "import json\n",
                },
            ),
        )

    def test_deployer_and_tools_import_skill_core_after_the_path_statement(self) -> None:
        self.assertEqual(
            [
                "deployer/extra.py:4 puts skill-core on sys.path; deployer/__init__.py is the deployer's one place; "
                f"see {self.DOC}",
                f"tools/early.py:1 imports shared before putting skills/skill-core/scripts on sys.path; see {self.DOC}",
                "tools/missing.py:1 imports shared before putting skills/skill-core/scripts on sys.path; see "
                f"{self.DOC}",
            ],
            self.run_policy(
                skill_core_import_problems,
                {
                    "deployer/__init__.py": '"""Package."""\n\n' + self.HEADER + self.STATEMENT,
                    # A package's __init__.py runs before its modules, so a deployer module imports skill-core freely.
                    "deployer/user.py": "from __future__ import annotations\n\nimport shared\n\nfrom . import extra\n",
                    "deployer/extra.py": self.HEADER + self.STATEMENT,
                    "tools/good.py": self.HEADER + self.STATEMENT + "\nimport shared\n",
                    "tools/early.py": "import shared\n" + self.HEADER + self.STATEMENT,
                    "tools/missing.py": "from shared import thing\n",
                    "tools/unrelated.py": "import json\n",
                },
            ),
        )

    def test_the_deployer_states_the_path_first_and_relative_to_itself(self) -> None:
        expected = [
            "deployer/__init__.py does not put skills/skill-core/scripts first on sys.path, located from its own "
            f"file; see {self.DOC}"
        ]
        for statement in (
            "",
            'sys.path.insert(0, str(Path(SOURCE) / "skills" / "skill-core" / "scripts"))\n',
            self.STATEMENT.replace("insert(0,", "insert(1,"),
            self.STATEMENT.replace("insert(0, ", "append("),
            "def later() -> None:\n    " + self.STATEMENT,
        ):
            with self.subTest(statement=statement):
                files = {"deployer/__init__.py": self.HEADER + statement}
                self.assertEqual(expected, self.run_policy(skill_core_import_problems, files))

    def test_skill_core_imports_nothing_from_the_deployer_and_the_deployer_only_the_standard_library(self) -> None:
        def reaches(name: str, line: int, module: str) -> str:
            return (
                f"{name}:{line} imports {module}; skill-core imports nothing from the deployer or tools/; see "
                f"{self.DOC}"
            )

        def foreign(name: str, line: int, module: str) -> str:
            return (
                f"{name}:{line} imports {module}, which is neither the standard library, the deployer, nor skill-core; "
                f"see {self.DOC}"
            )

        self.assertEqual(
            [
                foreign("deployer/plan.py", 1, "yaml"),
                reaches("skills/skill-core/scripts/reach.py", 1, "deployer"),
                reaches("skills/skill-core/scripts/reach.py", 2, "tools.thing"),
                foreign("skills/skill-core/scripts/reach.py", 3, "requests"),
                reaches("skills/skill-core/scripts/test_shared.py", 1, "deployer.paths"),
            ],
            self.run_policy(
                skill_core_import_problems,
                {
                    "deployer/__init__.py": self.HEADER + self.STATEMENT,
                    "deployer/plan.py": "import yaml\nimport tomllib\n\nimport shared\n\nfrom . import paths\n",
                    "skills/skill-core/scripts/reach.py": (
                        "from deployer import paths\nimport tools.thing\nimport requests\n"
                    ),
                    "skills/skill-core/scripts/test_shared.py": "import deployer.paths\nimport shared\n",
                    # A suite may import what it tests with; only the direction rule holds it.
                    "skills/skill-core/scripts/test_other.py": "import unittest\nimport shared\nimport harness\n",
                },
            ),
        )


if __name__ == "__main__":
    unittest.main()
