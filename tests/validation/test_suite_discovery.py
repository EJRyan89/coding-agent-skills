"""Fixture tests for tests/validation/suite_discovery.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import ast
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
    defined_tests,
    git_isolation_problems,
    pool_shared_state_problems,
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
from validation_support import SUITE_NAME_PATTERNS, is_test_script, write_fixture_tree


class SuiteDiscoveryFixtures(GitTestCase):
    def test_entry_point_policy_requires_a_last_main_block_that_calls_unittest_main(self) -> None:
        main = 'if __name__ == "__main__":\n'
        case = "import unittest\n\n\nclass Case(unittest.TestCase):\n    def test_it(self):\n        pass\n\n\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "tests/test_good.py": f"{case}{main}    unittest.main()\n",
                # The guard written the other way round runs the suite as well.
                "tests/test_reversed.py": f"{case}if '__main__' == __name__:\n    unittest.main()\n",
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

    def test_a_test_a_block_of_its_class_body_holds_is_counted_and_never_recorded(self) -> None:
        source = (
            "import sys\nimport unittest\n\n\nclass Case(unittest.TestCase):\n    def test_everywhere(self): pass\n\n"
            "    if sys.platform == 'win32':\n        def test_on_windows(self): pass\n    else:\n"
            "        def test_elsewhere(self): pass\n\n\nif __name__ == '__main__':\n    unittest.main()\n"
        )
        # The loader finds a test a block of the class body defines on the platforms that run the block.
        self.assertEqual(
            ["Case.test_elsewhere", "Case.test_everywhere", "Case.test_on_windows"], defined_tests(ast.parse(source))
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tests/test_platforms.py": source})
            self.assertEqual([], recorded_cost_problems(root, {"tests/test_platforms.py": {"Case.test_everywhere": 4}}))
            self.assertEqual(
                [
                    "tests/test_platforms.py records Case.test_on_windows, which a block of its class body holds, so "
                    "the loader finds it on some platforms only; record only a test every platform runs"
                ],
                recorded_cost_problems(root, {"tests/test_platforms.py": {"Case.test_on_windows": 4}}),
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
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), SUITE_NAME_PATTERNS)
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
        # A shell suite is found as a Python one is.
        "skills/beta/SKILL.md": "",
        "skills/beta/scripts/tool.sh": "",
        "skills/beta/scripts/test-tool.sh": "",
        # A repository skill's suites run as a shipped skill's do.
        ".claude/skills/local/SKILL.md": "",
        ".claude/skills/local/scripts/local.ps1": "",
        ".claude/skills/local/scripts/local.test.ps1": "",
        # A folder without SKILL.md is no skill, and the skills directory is flat, so nothing in either is found.
        "skills/loose/scripts/test_loose.py": "",
        "skills/group/inner/SKILL.md": "",
        "skills/group/inner/scripts/test_inner.py": "",
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
                    "skills/beta/scripts/test-tool.sh",
                    "tests/test_top.py",
                ],
                [path.relative_to(root).as_posix() for path in regression_suites(root)],
            )

    def test_a_skill_with_scripts_and_no_suite_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suites = {"skills/beta/scripts/test-tool.sh", ".claude/skills/local/scripts/local.test.ps1"}
            files = {name: text for name, text in self.FILES.items() if name not in suites}
            # Scripts that are not executable need no suite.
            write_fixture_tree(root, {**files, "skills/data/SKILL.md": "", "skills/data/scripts/notes.txt": ""})
            advice = "add a test_*, test-*, *_test, *-test, or *.test.* Python, Bash, or PowerShell script beside them"
            self.assertEqual(
                [
                    f"skills/beta/scripts holds scripts but no regression suite; {advice}",
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
                f"skills/grouped/scripts/grouped.py {missing}",
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
                    # A repository skill needs a test as a shipped skill does.
                    "skills/grouped/SKILL.md": "",
                    "skills/grouped/scripts/grouped.py": "",
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

    def test_a_module_counts_as_tested_only_by_an_import_that_loads_its_own_file(self) -> None:
        missing = "is imported by no test_*.py; add a test that imports it"
        common = "def f():\n    return 1\n"
        self.assertEqual(
            [
                f"skills/b/scripts/common.py {missing}",
                f"skills/b/scripts/inspect.py {missing}",
                f"skills/b/scripts/loaded.py {missing}",
            ],
            self.problems(
                {
                    "skills/a/SKILL.md": "",
                    "skills/a/scripts/common.py": common,
                    "skills/a/scripts/loaded.py": common,
                    "skills/b/SKILL.md": "",
                    "skills/b/scripts/common.py": common,
                    "skills/b/scripts/inspect.py": common,
                    "skills/b/scripts/loaded.py": common,
                    "skills/c/SKILL.md": "",
                    "skills/c/scripts/only.py": common,
                    # Skill a's own common, the standard library's inspect, and a by-path load that names its own
                    # loaded.py: none is skill b's module of that name.
                    "skills/a/scripts/test_common.py": "import importlib.util\nimport inspect\n\nimport common\n\n"
                    "SPEC = importlib.util.spec_from_file_location('loaded', HERE / 'loaded.py')\n",
                    # A by-path load whose literal end names one repository file loads that file.
                    "tests/test_only.py": "import importlib.util\n\n"
                    "SPEC = importlib.util.spec_from_file_location('only', ROOT / 'c' / 'scripts' / 'only.py')\n",
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

# The files a fixture of a suite that runs git through lib/runner.py needs.
GIT_RUNNER_FILES = ("pyproject.toml", "lib/runner.py")


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

    def test_a_class_or_module_fixture_in_a_module_level_if_try_or_with_block_is_read_as_a_top_level_one(
        self,
    ) -> None:
        nested = {
            "tests/test_nested_if.py": (
                "import sys\nimport unittest\n\nimport runner\n\nif sys.platform == 'win32':\n\n"
                "    class Base(unittest.TestCase):\n        pass\n\n\nclass Case(Base):\n    pass\n"
            ),
            "tests/test_nested_try.py": (
                "import runner\n\ntry:\n    import git_test_base\nexcept ImportError:\n    raise\nelse:\n\n"
                "    def setUpModule():\n        pass\n\n    class Case(git_test_base.GitTestCase):\n        pass\n"
            ),
            "tests/test_nested_with.py": (
                "import contextlib\n\nimport runner\nfrom git_test_base import GitTestCase\n\n"
                "with contextlib.suppress(ImportError):\n\n    class Case(GitTestCase):\n        @classmethod\n"
                "        def setUpClass(cls):\n            cls.value = 1\n"
            ),
            "tests/test_nested_isolated.py": (
                "import sys\n\nimport git_test_base\nimport runner\n\nif sys.platform == 'win32':\n\n"
                "    def setUpModule():\n        git_test_base.isolate_module()\n\nelse:\n\n"
                "    class Base(git_test_base.GitTestCase):\n        @classmethod\n        def setUpClass(cls):\n"
                "            super().setUpClass()\n\n\nclass Case(Base):\n    pass\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {**{name: GIT_SUITE_FILES[name] for name in GIT_RUNNER_FILES}, **nested})
            plain = "does not inherit git_test_base.GitTestCase, and the suite runs git (through lib/runner.py)"
            self.assertEqual(
                [
                    f"tests/test_nested_if.py:8 Base {plain}",
                    f"tests/test_nested_if.py:12 Case {plain}",
                    "tests/test_nested_try.py:9 runs git (through lib/runner.py), so its setUpModule starts with "
                    "git_test_base.isolate_module()",
                    "tests/test_nested_with.py:10 Case.setUpClass does not start with super().setUpClass(), which "
                    "isolates its git, and the suite runs git (through lib/runner.py)",
                ],
                git_isolation_problems(root),
            )

    def test_a_module_loaded_by_path_and_a_base_bound_by_assignment_are_read(self) -> None:
        case = "    def test_it(self):\n        gitty.status()\n\n\nif __name__ == '__main__':\n    unittest.main()\n"
        files = {
            "tools/__init__.py": "",
            "tools/gitty.py": "import subprocess\n\n\ndef status():\n    subprocess.run(['git', 'status'])\n",
            "tests/test_by_path.py": "import importlib.util\nimport unittest\nfrom pathlib import Path\n\n"
            "LOCATION = Path(__file__).parents[1] / 'tools' / 'gitty.py'\n"
            "SPEC = importlib.util.spec_from_file_location('gitty', LOCATION)\n"
            "gitty = importlib.util.module_from_spec(SPEC)\nSPEC.loader.exec_module(gitty)\n\n\n"
            f"class T(unittest.TestCase):\n{case}",
            "tests/test_aliased_base.py": "import unittest\n\nfrom tools import gitty\n\n"
            f"Base = unittest.TestCase\n\n\nclass T(Base):\n{case}",
            "tests/test_by_name.py": "import importlib\nimport unittest\n\n"
            f"gitty = importlib.import_module('tools.gitty')\n\n\nclass T(unittest.TestCase):\n{case}",
            "tests/test_isolated.py": "import unittest\n\nfrom git_test_base import GitTestCase\n"
            f"from tools import gitty\n\nBase = GitTestCase\n\n\nclass T(Base):\n{case}",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            plain = "T does not inherit git_test_base.GitTestCase, and the suite runs git (through tools/gitty.py)"
            self.assertEqual(
                [
                    f"tests/test_aliased_base.py:8 {plain}",
                    f"tests/test_by_name.py:7 {plain}",
                    f"tests/test_by_path.py:11 {plain}",
                ],
                git_isolation_problems(root),
            )


class PoolSharedStatePolicy(GitTestCase):
    RUNNER = "import alpha\nimport beta\n\nPOLICY_MODULES = (alpha, beta)\n"

    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tests/run_validation.py": self.RUNNER, "tests/validation/beta.py": "", **files})
            return pool_shared_state_problems(root)

    def test_a_policy_check_that_swaps_a_stream_or_changes_the_environment_is_reported(self) -> None:
        alpha = (
            "import contextlib\n"
            "import io\n"
            "import os as system\n"
            "import sys\n"
            "from contextlib import redirect_stderr as quiet\n"
            "from unittest import mock\n"
            "\n"
            "def check():\n"
            "    with contextlib.redirect_stdout(io.StringIO()):\n"
            "        pass\n"
            "    with quiet(io.StringIO()):\n"
            "        pass\n"
            "    with mock.patch.dict(system.environ, {'A': 'b'}):\n"
            "        pass\n"
            "    with mock.patch.object(sys, 'stdout', io.StringIO()):\n"
            "        pass\n"
            "    with mock.patch('os.environ', {}):\n"
            "        pass\n"
            "    system.environ['A'] = 'b'\n"
            "    del system.environ['A']\n"
            "    system.environ.update(A='b')\n"
            "    sys.stderr = io.StringIO()\n"
            "    setattr(sys, 'stdin', None)\n"
            "    system.chdir('..')\n"
        )
        problems = self.problems({"tests/validation/alpha.py": alpha})
        self.assertEqual(
            [
                "tests/validation/alpha.py:9 calls contextlib.redirect_stdout",
                "tests/validation/alpha.py:11 calls contextlib.redirect_stderr",
                "tests/validation/alpha.py:13 patches os.environ",
                "tests/validation/alpha.py:15 patches sys.stdout",
                "tests/validation/alpha.py:17 patches os.environ",
                "tests/validation/alpha.py:19 changes system.environ['A']",
                "tests/validation/alpha.py:20 changes system.environ['A']",
                "tests/validation/alpha.py:21 calls os.environ.update",
                "tests/validation/alpha.py:22 changes sys.stderr",
                "tests/validation/alpha.py:23 patches sys.stdin",
                "tests/validation/alpha.py:24 calls os.chdir",
            ],
            [problem.split(",", 1)[0] for problem in problems],
        )
        self.assertIn("which the validation pool reads while the policy checks run", problems[0])

    def test_a_string_patch_target_a_keyword_target_patch_multiple_contextlib_chdir_and_an_alias_are_reported(
        self,
    ) -> None:
        alpha = (
            "import contextlib\n"
            "import io\n"
            "import os\n"
            "import sys\n"
            "from unittest import mock\n"
            "\n"
            "def check():\n"
            "    mock.patch.dict('os.environ', {'A': 'b'})\n"
            "    mock.patch.dict(in_dict=os.environ, values={'A': 'b'})\n"
            "    with contextlib.chdir('..'):\n"
            "        pass\n"
            "    mock.patch.multiple(sys, stdout=io.StringIO())\n"
            "    mock.patch.multiple('os', **{'environ': {}})\n"
            "    mock.patch.multiple(target=sys, stderr=None)\n"
            "    mock.patch(target='os.environ', new={})\n"
            "    mock.patch(target='sys.stdout')\n"
            "    mock.patch.object(sys, attribute='stdout')\n"
            "    mock.patch.object(target=sys, attribute='stdin')\n"
            "    env = os.environ; env['A'] = 'b'\n"
            "    environment = os.environ; environment.clear()\n"
            "    (other := os.environ)['A'] = 'b'\n"
            "    os.fchdir(0)\n"
            "    streams = sys; streams.stdout, value = io.StringIO(), 1\n"
            "    update = os.environ.update; update(A='b')\n"
            "\n"
            "SAVED = os.environ\n"
            "\n"
            "def later():\n"
            "    del SAVED['A']\n"
            "    SAVED.pop('A')\n"
            "    from os import environ\n"
            "    environ |= {'A': 'b'}\n"
            "    os.environ |= {'A': 'b'}\n"
        )
        problems = self.problems({"tests/validation/alpha.py": alpha})
        self.assertEqual(
            [
                "tests/validation/alpha.py:8 patches os.environ",
                "tests/validation/alpha.py:9 patches os.environ",
                "tests/validation/alpha.py:10 calls contextlib.chdir",
                "tests/validation/alpha.py:12 patches sys.stdout",
                "tests/validation/alpha.py:13 patches os.environ",
                "tests/validation/alpha.py:14 patches sys.stderr",
                "tests/validation/alpha.py:15 patches os.environ",
                "tests/validation/alpha.py:16 patches sys.stdout",
                "tests/validation/alpha.py:17 patches sys.stdout",
                "tests/validation/alpha.py:18 patches sys.stdin",
                "tests/validation/alpha.py:19 changes env['A']",
                "tests/validation/alpha.py:20 calls os.environ.clear",
                "tests/validation/alpha.py:21 changes (other := os.environ)['A']",
                "tests/validation/alpha.py:22 calls os.fchdir",
                "tests/validation/alpha.py:23 changes streams.stdout",
                "tests/validation/alpha.py:24 calls os.environ.update",
                "tests/validation/alpha.py:29 changes SAVED['A']",
                "tests/validation/alpha.py:30 calls os.environ.pop",
                "tests/validation/alpha.py:32 changes environ",
                "tests/validation/alpha.py:33 changes os.environ",
            ],
            [problem.split(",", 1)[0] for problem in problems],
        )

    def test_the_runner_itself_is_read_and_a_missing_policy_module_is_reported(self) -> None:
        runner = self.RUNNER + "import contextlib, io\n\ndef f():\n    contextlib.redirect_stderr(io.StringIO())\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tests/run_validation.py": runner, "tests/validation/alpha.py": ""})
            problems = pool_shared_state_problems(root)
        self.assertEqual(2, len(problems), problems)
        self.assertTrue(problems[0].startswith("tests/run_validation.py:8 calls contextlib.redirect_stderr"))
        self.assertEqual(
            "tests/validation/beta.py: a policy module tests/run_validation.py runs is missing", problems[1]
        )

    def test_reading_the_state_or_taking_it_as_a_parameter_passes(self) -> None:
        alpha = (
            "import io\n"
            "import os\n"
            "import sys\n"
            "from unittest import mock\n"
            "\n"
            "def main(out=None, environment=None):\n"
            "    out = sys.stdout if out is None else out\n"
            "    environment = os.environ if environment is None else environment\n"
            "    merged = dict(os.environ)\n"
            "    merged['A'] = 'b'\n"
            "    print(os.environ.get('A'), file=out)\n"
            "    with mock.patch.dict(merged, {'B': 'c'}), mock.patch.object(io, 'StringIO'):\n"
            "        pass\n"
            "    with mock.patch('module.attribute'):\n"
            "        pass\n"
        )
        self.assertEqual([], self.problems({"tests/validation/alpha.py": alpha}))

    def test_the_conforming_twin_of_each_newly_read_form_passes(self) -> None:
        alpha = (
            "import contextlib\n"
            "import io\n"
            "import os\n"
            "import sys\n"
            "from os import environ\n"
            "from unittest import mock\n"
            "\n"
            "def check(directory, merged):\n"
            "    mock.patch.dict('module.SETTINGS', {'A': 'b'})\n"
            "    mock.patch.dict(in_dict=merged, values={'A': 'b'})\n"
            "    with contextlib.suppress(OSError):\n"
            "        pass\n"
            "    mock.patch.multiple(io, StringIO=None, create=True)\n"
            "    mock.patch.multiple('sys', **{'argv': []})\n"
            "    mock.patch(target='module.environ', new={})\n"
            "    mock.patch.object(sys, attribute='argv')\n"
            "    env = dict(os.environ); env['A'] = 'b'\n"
            "    environment = os.environ; environment = {}; environment.clear()\n"
            "    (other := {})['A'] = 'b'\n"
            "    stream = sys.stdout; stream = io.StringIO()\n"
            "    print(environ.get('A'), os.environ.get('A'), directory)\n"
        )
        self.assertEqual([], self.problems({"tests/validation/alpha.py": alpha}))


if __name__ == "__main__":
    unittest.main()
