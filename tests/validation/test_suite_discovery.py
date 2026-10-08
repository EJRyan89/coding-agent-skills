"""Fixture tests for tests/validation/suite_discovery.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from job_selection import name_patterns
from suite_discovery import (
    MAXIMUM_SHARDS,
    SHARD_RUNNER,
    TESTS_PER_SHARD,
    regression_suites,
    shard_count,
    suite_discovery_documentation_problems,
    unsuited_script_problems,
    untested_module_problems,
)
from validation_support import TEST_NAME_PATTERNS, is_test_script, write_fixture_tree


class SuiteDiscoveryFixtures(unittest.TestCase):
    def test_shards_run_each_test_once_with_its_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "suite with spaces"
            root.mkdir()
            log = root / "log.txt"
            (root / "helper.py").write_text("VALUE = 'from the suite directory'\n", encoding="utf-8")
            tests = "".join(f"    def test_{number}(self):\n        record('test_{number}')\n" for number in range(7))
            suite = root / "test_fixture.py"
            suite.write_text(
                "import unittest\nfrom pathlib import Path\nimport helper\n"
                f"LOG = Path({str(log)!r})\n"
                "def record(entry):\n    with LOG.open('a', encoding='utf-8') "
                "as handle:\n        handle.write(entry + '\\n')\n"
                "def setUpModule():\n    record('module ' + helper.VALUE)\n"
                "class Fixture(unittest.TestCase):\n"
                "    @classmethod\n    def setUpClass(cls):\n        record('class')\n"
                f"{tests}"
                "if __name__ == '__main__':\n    record('ran as __main__')\n    unittest.main()\n",
                encoding="utf-8",
            )
            for index in range(3):
                completed = subprocess.run(
                    [sys.executable, "-B", str(SHARD_RUNNER), str(suite), str(index), "3"],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
            entries = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                sorted(f"test_{number}" for number in range(7)), sorted(e for e in entries if e.startswith("test_"))
            )
            self.assertEqual(3, entries.count("module from the suite directory"))
            self.assertEqual(3, entries.count("class"))
            self.assertNotIn("ran as __main__", entries)

            suite.write_text(
                suite.read_text(encoding="utf-8").replace("record('test_3')", "self.fail('boom')"), encoding="utf-8"
            )
            failing = subprocess.run(
                [sys.executable, "-B", str(SHARD_RUNNER), str(suite), "0", "3"], capture_output=True, text=True
            )
            self.assertEqual(1, failing.returncode)
            self.assertIn("boom", failing.stderr)
            refused = subprocess.run(
                [sys.executable, "-B", str(SHARD_RUNNER), str(suite), "3", "3"], capture_output=True, text=True
            )
            self.assertEqual(2, refused.returncode)

    def test_large_python_suites_are_sharded_and_others_run_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, tests in (("test_small.py", 3), ("test_large.py", 40), ("test_huge.py", 200)):
                body = "".join(f"    def test_{number}(self): pass\n" for number in range(tests))
                (root / name).write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            (root / "test_script.ps1").write_text("exit 0\n", encoding="utf-8")
            self.assertEqual(1, shard_count(root / "test_small.py"))
            self.assertEqual(40 // TESTS_PER_SHARD, shard_count(root / "test_large.py"))
            self.assertEqual(MAXIMUM_SHARDS, shard_count(root / "test_huge.py"))
            self.assertEqual(1, shard_count(root / "test_script.ps1"))
            self.assertEqual(["*deployer*", "test_*.py"], name_patterns(["deployer", "test_*.py"]))

    def test_suite_discovery_documentation_policy_detects_each_missing_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            guide = root / "docs" / "adding-a-skill.md"
            guide.write_text(
                '# Adding a skill\n\n## Files\n\n`test-*` `*.test.*` `.sh` `if __name__ == "__main__":`\n\n'
                "## Validation\n\nName it `test_*`, `*_test`, or `*-test` and use `.py` or `.ps1`.\n\n## Later\n",
                encoding="utf-8",
            )
            missing = 'docs/adding-a-skill.md "Validation" does not name'
            self.assertEqual(
                [
                    f"{missing} `test-*`",
                    f"{missing} `*.test.*`",
                    f"{missing} `.sh`",
                    f'{missing} `if __name__ == "__main__":`',
                ],
                suite_discovery_documentation_problems(root),
            )
            guide.write_text("# Adding a skill\n", encoding="utf-8")
            self.assertEqual(
                ['docs/adding-a-skill.md has no "Validation" section'], suite_discovery_documentation_problems(root)
            )

    def test_suite_names_follow_the_documented_patterns(self) -> None:
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), TEST_NAME_PATTERNS)
        for name in (
            "test_a.py",
            "test-a.sh",
            "a_test.ps1",
            "a-test.py",
            "a.test.py",
            "a.test.b.sh",
            "TEST_A.PY",
            "Test-A.Ps1",
            "test_.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_test_script(Path(name)))
        for name in (
            "a.py",
            "testa.py",
            "atest.py",
            "test_a.txt",
            "a_tests.py",
            "contest.py",
            "a.tests.py",
            "test_a.js",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_test_script(Path(name)))


class SkillSuiteFixtures(unittest.TestCase):
    FILES: ClassVar[dict[str, str]] = {
        "tests/test_top.py": "",
        "tests/deployer/harness.py": "",
        "skills/alpha/SKILL.md": "",
        "skills/alpha/scripts/run.py": "",
        "skills/alpha/scripts/test_run.py": "",
        # deployer/source.py reads skills/<category>/<name>, so a category's skills are found.
        "skills/group/inner/SKILL.md": "",
        "skills/group/inner/scripts/tool.sh": "",
        "skills/group/inner/scripts/test-tool.sh": "",
        # A repository skill's suites run as a shipped skill's do.
        ".claude/skills/local/SKILL.md": "",
        ".claude/skills/local/scripts/local.ps1": "",
        ".claude/skills/local/scripts/local.test.ps1": "",
        # A folder without SKILL.md is no skill, so nothing in it is found.
        "skills/loose/scripts/test_loose.py": "",
    }

    def test_suites_are_found_under_tests_and_every_shipped_and_repository_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, self.FILES)
            self.assertEqual(
                [
                    ".claude/skills/local/scripts/local.test.ps1",
                    "skills/alpha/scripts/test_run.py",
                    "skills/group/inner/scripts/test-tool.sh",
                    "tests/test_top.py",
                ],
                [path.relative_to(root).as_posix() for path in regression_suites(root)],
            )

    def test_a_skill_with_scripts_and_no_suite_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suites = {"skills/group/inner/scripts/test-tool.sh", ".claude/skills/local/scripts/local.test.ps1"}
            files = {name: text for name, text in self.FILES.items() if name not in suites}
            # Scripts that are not executable need no suite.
            write_fixture_tree(root, {**files, "skills/data/SKILL.md": "", "skills/data/scripts/notes.txt": ""})
            advice = "add a test_*, test-*, *_test, *-test, or *.test.* Python, Bash, or PowerShell script beside them"
            self.assertEqual(
                [
                    f"skills/group/inner/scripts holds scripts but no regression suite; {advice}",
                    f".claude/skills/local/scripts holds scripts but no regression suite; {advice}",
                ],
                unsuited_script_problems(root),
            )


class TestedModulePolicy(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            write_fixture_tree(root, files)
            return untested_module_problems(root)

    def test_a_module_no_test_imports_fails_however_often_it_is_named(self) -> None:
        missing = "is imported by no test_*.py; add a test that imports it"
        self.assertEqual(
            [
                f".claude/skills/local/scripts/local_tool.py {missing}",
                f"deployer/lonely.py {missing}",
                f"deployer/mentioned.py {missing}",
                f"deployer/run.py {missing}",
                f"skills/group/inner/scripts/grouped.py {missing}",
                f"skills/s/scripts/quoted.py {missing}",
                f"tools/tool.py {missing}",
            ],
            self.problems(
                {
                    **{
                        f"deployer/{name}.py": ""
                        for name in ("imported", "dotted", "member", "named", "lonely", "mentioned", "run")
                    },
                    "tools/tool.py": "",
                    "tools/by_path.py": "",
                    "skills/s/SKILL.md": "",
                    "skills/s/scripts/helper.py": "",
                    "skills/s/scripts/quoted.py": "",
                    # A skill in a category, and a repository skill, need a test as a shipped skill does.
                    "skills/group/inner/SKILL.md": "",
                    "skills/group/inner/scripts/grouped.py": "",
                    ".claude/skills/local/SKILL.md": "",
                    ".claude/skills/local/scripts/local_tool.py": "",
                    "skills/s/notes.py": "",
                    "tests/support.py": "",
                    "skills/s/scripts/test_s.py": "import helper\n\nTEXT = 'import quoted'\n",
                    # A test named after a module, a mention, and a run of it by path are not imports.
                    "tests/deployer/test_lonely.py": "pass\n",
                    "tests/deployer/test_suite.py": "import subprocess\n\n"
                    "from deployer import imported\n"
                    "import deployer.dotted\n"
                    "from deployer.member import thing\n"
                    "# Runs tools/tool.py and reaches deployer.mentioned.\n"
                    "subprocess.run(['python', 'deployer/run.py'])\n",
                    "tests/tools/test_tools.py": "import importlib\nimport importlib.util\n\n"
                    "importlib.import_module('deployer.named')\n"
                    "SPEC = importlib.util.spec_from_file_location('by_path', ROOT / 'tools' / 'by_path.py')\n",
                }
            ),
        )

    def test_package_initializers_need_no_test(self) -> None:
        self.assertEqual(
            [], self.problems({"deployer/__init__.py": "", "tools/__init__.py": "", "skills/s/scripts/__init__.py": ""})
        )


if __name__ == "__main__":
    unittest.main()
