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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase
from job_selection import name_patterns
from suite_discovery import (
    MAXIMUM_SHARDS,
    SHARD_RUNNER,
    TESTS_PER_SHARD,
    git_isolation_problems,
    recorded_cost_problems,
    recorded_shard_problems,
    regression_suites,
    shard_count,
    suite_discovery_documentation_problems,
    suite_entry_point_problems,
    suite_jobs,
    unsuited_script_problems,
    untested_module_problems,
)
from validation_support import TEST_NAME_PATTERNS, is_test_script, write_fixture_tree


class SuiteDiscoveryFixtures(GitTestCase):
    def test_entry_point_policy_requires_a_last_main_block_that_calls_unittest_main(self) -> None:
        main = 'if __name__ == "__main__":\n'
        case = "import unittest\n\n\nclass Case(unittest.TestCase):\n    def test_it(self):\n        pass\n\n\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "tests/test_good.py": f"{case}{main}    unittest.main()\n",
                "tests/test_exits.py": f"import sys\n{case}{main}    sys.exit(unittest.main())\n",
                "tests/test_imported.py": f"from unittest import main as run\n{case}{main}    run(verbosity=2)\n",
                "tests/test_none.py": case,
                # The entry point named only in a comment runs nothing.
                "tests/test_comment.py": f"{case}# {main}#     unittest.main()\n",
                "tests/test_early.py": f"{case}{main}    unittest.main()\n\n\ndef helper():\n    pass\n",
                "tests/test_other_call.py": f"{case}def main():\n    pass\n\n\n{main}    main()\n",
                "tests/test_local_main.py": f"{case}class runner:\n    main = print\n\n\n{main}    runner.main()\n",
                "tests/helper.py": "def helper():\n    pass\n",
                "tests/test_shell.sh": "exit 0\n",
            }
            write_fixture_tree(root, files)
            ends = 'does not end with an `if __name__ == "__main__":` block, so it runs no tests'
            calls = 'its `if __name__ == "__main__":` block does not call unittest.main()'
            self.assertEqual(
                [
                    f"tests/test_comment.py {ends}",
                    f"tests/test_early.py {ends}",
                    f"tests/test_local_main.py:13 {calls}",
                    f"tests/test_none.py {ends}",
                    f"tests/test_other_call.py:13 {calls}",
                ],
                suite_entry_point_problems(root),
            )

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

    def test_a_recorded_test_gets_a_shard_to_itself_and_every_test_still_runs_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "suite with spaces"
            root.mkdir()
            log = root / "log.txt"
            tests = "".join(f"    def test_{number}(self):\n        record('test_{number}')\n" for number in range(7))
            suite = root / "test_fixture.py"
            suite.write_text(
                "import unittest\nfrom pathlib import Path\n"
                f"LOG = Path({str(log)!r})\n"
                "def record(entry):\n    with LOG.open('a', encoding='utf-8') "
                "as handle:\n        handle.write(entry + '\\n')\n"
                f"class Fixture(unittest.TestCase):\n{tests}"
                "if __name__ == '__main__':\n    unittest.main()\n",
                encoding="utf-8",
            )

            def shard(index: int, *recorded: str) -> list[str]:
                log.unlink(missing_ok=True)
                completed = subprocess.run(
                    [sys.executable, "-B", str(SHARD_RUNNER), str(suite), str(index), "3", *recorded],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                return log.read_text(encoding="utf-8").splitlines()

            # Nothing recorded: round-robin in test-ID order, as before.
            self.assertEqual(["test_0", "test_3", "test_6"], shard(0))
            # test_3 recorded at five seconds: packed first, alone on shard 0; the rest share shards 1 and 2.
            packed = [shard(index, "Fixture.test_3=5") for index in range(3)]
            self.assertEqual(["test_3"], packed[0])
            self.assertEqual(["test_0", "test_2", "test_5"], packed[1])
            ran = sorted(entry for entries in packed for entry in entries)
            self.assertEqual(sorted(f"test_{number}" for number in range(7)), ran)
            for recorded in ("Fixture.test_9=5", "Fixture.test_3", "Fixture.test_3=many"):
                with self.subTest(recorded=recorded):
                    refused = subprocess.run(
                        [sys.executable, "-B", str(SHARD_RUNNER), str(suite), "0", "3", recorded],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(2, refused.returncode, refused.stderr)

    def test_recorded_seconds_raise_the_shard_count_and_reach_every_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "test_costly.py"
            body = "".join(f"    def test_{number}(self): pass\n" for number in range(17))
            suite.write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            self.assertEqual(17 // TESTS_PER_SHARD, shard_count(suite, {}))
            self.assertEqual((17 + 12) // TESTS_PER_SHARD, shard_count(suite, {"T.test_4": 13}))
            jobs = suite_jobs([suite], {"test_costly.py": {"T.test_4": 13}}, root=Path(temporary))
            self.assertEqual(shard_count(suite, {"T.test_4": 13}), len(jobs))
            self.assertEqual({(17 + 12) / len(jobs)}, {job.weight for job in jobs})

    def test_a_recorded_test_shares_a_shard_once_the_suite_outgrows_the_shard_cap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, tests in (("tests/test_fits.py", 17), ("tests/test_outgrown.py", 200)):
                body = "".join(f"    def test_{number}(self): pass\n" for number in range(tests))
                # A helper is not a test, so it neither adds to the suite's cost nor joins a shard.
                write_fixture_tree(
                    root, {name: f"import unittest\nclass T(unittest.TestCase):\n{body}    def run_x(self): pass\n"}
                )
            recorded = {"tests/test_fits.py": {"T.test_4": 13}, "tests/test_outgrown.py": {"T.test_4": 13}}
            self.assertEqual(
                [
                    "tests/test_outgrown.py deals T.test_4 onto a shard beside other tests, since the suite fills its "
                    f"{MAXIMUM_SHARDS} shards; raise MAXIMUM_SHARDS or split the suite"
                ],
                recorded_shard_problems(root, recorded),
            )

    def test_recorded_seconds_must_name_a_suite_and_a_test_it_defines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "tests/test_costly.py": "import unittest\nclass Slow(unittest.TestCase):\n"
                    "    def test_long(self): pass\nif __name__ == '__main__':\n    unittest.main()\n",
                },
            )
            self.assertEqual([], recorded_cost_problems(root, {"tests/test_costly.py": {"Slow.test_long": 13}}))
            self.assertEqual(
                [
                    "tests/test_costly.py records Slow.test_gone, which it does not define",
                    "tests/test_costly.py records Fast.test_long, which it does not define",
                    "tests/test_missing.py records test costs but is not a regression suite",
                    "tests/test_costly.py records Slow.test_long at 0 seconds; record only tests that cost more than 1",
                ],
                recorded_cost_problems(
                    root,
                    {
                        "tests/test_costly.py": {"Slow.test_gone": 4, "Fast.test_long": 4},
                        "tests/test_missing.py": {"Slow.test_long": 4},
                    },
                )
                + recorded_cost_problems(root, {"tests/test_costly.py": {"Slow.test_long": 0}}),
            )

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


class SkillSuiteFixtures(GitTestCase):
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
        # A fixture tree is another repository a review reads, so its suites are not this one's.
        "tests/fixtures/skill-evals/demo/scenario/head/tests/test_report.sh": "",
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


class TestedModulePolicy(GitTestCase):
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


PLAIN_CASE = "class Case(unittest.TestCase):\n    def test_it(self):\n        pass\n"
GIT_SUITE_FILES = {
    "pyproject.toml": '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/lib"]\n',
    "lib/runner.py": "import subprocess\n\n\ndef run():\n    subprocess.run(['git', 'status'])\n",
    "lib/finder.py": "def find(which):\n    return which('git')\n",
    "lib/git_client.py": "class GitClient:\n    pass\n",
    "lib/client_user.py": "from git_client import GitClient\n\nCLIENT = GitClient()\n",
    "lib/quiet.py": "URL = 'https://github.com/owner/repo'\nCOMMAND = ['gh', 'git']\n",
    "pkg/__init__.py": "",
    "pkg/helper.py": "from . import located\n",
    "pkg/located.py": "import shutil\n\nGIT = shutil.which('git')\n",
    "tests/harness.py": (
        "import subprocess\nimport unittest\n\nfrom git_test_base import GitTestCase\n\n\n"
        "class Isolated(GitTestCase):\n    pass\n\n\nclass Plain(unittest.TestCase):\n    pass\n\n\n"
        "def git(*arguments):\n    subprocess.run(['git', *arguments])\n"
    ),
    "tests/test_direct_plain.py": f"import subprocess\nimport unittest\n\nCOMMAND = ['git', 'init']\n\n\n{PLAIN_CASE}",
    "tests/test_direct_isolated.py": (
        "import subprocess\n\nfrom git_test_base import GitTestCase as Base\n\n\nclass Local(Base):\n    pass\n\n\n"
        "class Case(Local):\n    def test_it(self):\n        subprocess.run(['git', 'init'])\n\n\n"
        "class Helper:\n    pass\n"
    ),
    "tests/test_through_runner.py": f"import unittest\n\nimport runner\n\n\n{PLAIN_CASE}",
    "tests/test_through_harness_plain.py": (
        "from harness import Plain, git\n\n\nclass Case(Plain):\n    def test_it(self):\n        git('init')\n"
    ),
    "tests/test_through_harness_isolated.py": "import harness\n\n\nclass Case(harness.Isolated):\n    pass\n",
    "tests/test_through_package.py": f"import unittest\n\nfrom pkg import helper\n\n\n{PLAIN_CASE}",
    "tests/test_through_lookup.py": f"import unittest\n\nimport finder\n\n\n{PLAIN_CASE}",
    "tests/test_through_client.py": f"import unittest\n\nimport client_user\n\n\n{PLAIN_CASE}",
    "tests/test_no_git.py": f"import unittest\n\nimport quiet\n\nTEXT = 'git is named here'\n\n\n{PLAIN_CASE}",
    "tests/test_async.py": (
        "import unittest\n\nimport runner\n\n\nclass Case(unittest.IsolatedAsyncioTestCase):\n    pass\n"
    ),
    "tests/test_class_fixture.py": (
        "import runner\nfrom git_test_base import GitTestCase\n\n\nclass Late(GitTestCase):\n    @classmethod\n"
        "    def setUpClass(cls):\n        cls.value = 1\n        super().setUpClass()\n\n\n"
        'class First(GitTestCase):\n    @classmethod\n    def setUpClass(cls):\n        """Isolated first."""\n'
        "        super().setUpClass()\n        cls.value = 1\n"
    ),
    "tests/test_module_plain.py": (
        "import os\n\nimport runner\nfrom git_test_base import GitTestCase\n\n\n"
        "def setUpModule():\n    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'\n\n\nclass Case(GitTestCase):\n    pass\n"
    ),
    "tests/test_module_isolated.py": (
        'import git_test_base\nimport runner\n\n\ndef setUpModule():\n    """Fixtures."""\n'
        "    git_test_base.isolate_module({'init.defaultBranch': 'main'})\n\n\n"
        "class Case(git_test_base.GitTestCase):\n    pass\n"
    ),
}


class GitIsolationPolicy(GitTestCase):
    def test_a_suite_that_runs_git_must_isolate_every_test_class_class_fixture_and_module_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, GIT_SUITE_FILES)
            plain = "does not inherit git_test_base.GitTestCase, and the suite runs git"
            self.assertEqual(
                [
                    f"tests/test_async.py:6 Case {plain} (through lib/runner.py)",
                    "tests/test_class_fixture.py:7 Late.setUpClass does not start with super().setUpClass(), which "
                    "isolates its git, and the suite runs git (through lib/runner.py)",
                    f"tests/test_direct_plain.py:7 Case {plain} (itself)",
                    "tests/test_module_plain.py:7 runs git (through lib/runner.py), so its setUpModule starts with "
                    "git_test_base.isolate_module()",
                    f"tests/test_through_client.py:6 Case {plain} (through lib/client_user.py)",
                    f"tests/test_through_harness_plain.py:4 Case {plain} (through tests/harness.py)",
                    f"tests/test_through_lookup.py:6 Case {plain} (through lib/finder.py)",
                    f"tests/test_through_package.py:6 Case {plain} (through pkg/located.py)",
                    f"tests/test_through_runner.py:6 Case {plain} (through lib/runner.py)",
                ],
                git_isolation_problems(root),
            )


if __name__ == "__main__":
    unittest.main()
