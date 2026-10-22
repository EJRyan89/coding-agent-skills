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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from job_selection import name_patterns
from suite_discovery import (
    MAXIMUM_SHARDS,
    PYTEST_CONFIGURATION,
    TESTS_PER_SHARD,
    assertion_holder_problems,
    defined_tests,
    git_isolation_problems,
    pipe_race_problems,
    pool_shared_state_problems,
    pytest_command,
    pytest_configuration_problems,
    recorded_cost_problems,
    recorded_shard_problems,
    regression_suites,
    shard_count,
    shard_options,
    suite_discovery_documentation_problems,
    suite_jobs,
    suite_shape_problems,
    unsuited_script_problems,
    untested_module_problems,
)
from validation_support import REPOSITORY_ROOT, SUITE_NAME_PATTERNS, is_test_script, write_fixture_tree


def run_pytest(suite: Path, *options: str) -> subprocess.CompletedProcess[str]:
    """Run a fixture suite as the runner runs a suite, from the repository root, with its conftest.py as a plugin,
    since a suite outside the repository is outside the root that loads it. A pytest.ini beside the suite makes its
    folder the run's root, so pytest reads no folder above it, where other tests' temporary folders come and go."""
    (suite.parent / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    return subprocess.run(
        [*pytest_command(str(suite), *options), "-p", "conftest", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=REPOSITORY_ROOT,
        check=False,
    )


class SuiteDiscoveryFixtures(unittest.TestCase):
    def test_shape_policy_requires_a_test_pytest_collects_and_no_main_block(self) -> None:
        main = 'if __name__ == "__main__":\n'
        case = "import unittest\n\n\nclass Case(unittest.TestCase):\n    def test_it(self):\n        pass\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "tests/test_case.py": case,
                "tests/test_function.py": "def test_it():\n    assert True\n",
                # A test pytest collects on one platform only is still a test.
                "tests/test_platform.py": (
                    "import sys\n\nif sys.platform == 'win32':\n\n    def test_it():\n        pass\n"
                ),
                "tests/test_main.py": f"{case}\n\n{main}    import unittest\n\n    unittest.main()\n",
                "tests/test_reversed.py": f"{case}\n\nif '__main__' == __name__:\n    pass\n",
                "tests/test_none.py": "import unittest\n\n\ndef helper():\n    pass\n",
                # A helper named like a test but not test*, and a main block named only in a comment, are neither.
                "tests/test_comment.py": f"{case}# {main}#     unittest.main()\n",
                "tests/test_named.py": (
                    "def check_it():\n    pass\n\n\nclass Helper:\n    def run_it(self):\n        pass\n"
                ),
                "tests/helper.py": "def helper():\n    pass\n",
                "tests/test_shell.sh": "exit 0\n",
            }
            write_fixture_tree(root, files)
            main_block = (
                'holds an `if __name__ == "__main__":` block; the runner starts the suite through pytest, so remove it'
            )
            none = "defines no test pytest collects: no test* method of a class and no test* function"
            self.assertEqual(
                [
                    f"tests/test_main.py:9 {main_block}",
                    f"tests/test_named.py {none}",
                    f"tests/test_none.py {none}",
                    f"tests/test_reversed.py:9 {main_block}",
                ],
                suite_shape_problems(root),
            )

    def test_pipe_race_policy_refuses_a_pipe_into_a_grep_that_ends_at_its_match(self) -> None:
        pipefail = "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        racing = "printf x | grep -q x\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "---\nname: alpha\n---\n",
                    "skills/alpha/scripts/test_alpha.sh": (
                        f"{pipefail}"
                        'printf \'%s\\n\' "$OUTPUT" | grep -q "^UPDATED " || fail\n'
                        "if tr -d '\\r' <err | grep -qxF -e \"$REFUSAL\"; then :; fi\n"
                        "printf x |\n  grep --quiet x\n"
                        "printf x \\\n  | grep -m 1 x\n"
                        "printf x | LC_ALL=C grep -Eq x # a comment\n"
                        # An ANSI-C string's escaped quote does not end it, and a string's lines count.
                        "LINE=$'it\\'s'\nTEXT='one\ntwo'\nprintf x |& egrep --max-count=1 x\n"
                    ),
                    "tests/test_beta.sh": (
                        f"{pipefail}"
                        # A here-string, a file, a grep that reads to the end, a -q that is a pattern, a comment that
                        # ends its command, a list operator, and a pipe in a string or a comment race nothing.
                        'grep -q "^UPDATED " <<<"$OUTPUT"\n'
                        "grep -q x file\n"
                        "printf x | grep -c x\n"
                        "printf x | grep -e -q x\n"
                        "printf x | grep -- -q\n"
                        "printf x | cat # a comment\ngrep -q x file\n"
                        "true || grep -q x file\n"
                        "echo 'printf x | grep -q x'\n"
                        f"# {racing}"
                    ),
                    # Without pipefail the pipeline's status is grep's own.
                    "tests/test_gamma.sh": f"#!/usr/bin/env bash\nset -eu\n{racing}",
                    # A script that is not a suite, and a fixture tree's suite, are not read.
                    "skills/alpha/scripts/alpha.sh": f"{pipefail}{racing}",
                    "tests/fixtures/review/test_other.sh": f"{pipefail}{racing}",
                    "tests/test_unread.sh": f"{pipefail}echo 'open\n",
                },
            )
            race = (
                "under pipefail, which fails the check when the writer dies of SIGPIPE after the match; give grep its "
                "text in a here-string or a file"
            )
            self.assertEqual(
                [
                    f"skills/alpha/scripts/test_alpha.sh:3 pipes into grep -q {race}",
                    f"skills/alpha/scripts/test_alpha.sh:4 pipes into grep -q {race}",
                    f"skills/alpha/scripts/test_alpha.sh:6 pipes into grep --quiet {race}",
                    f"skills/alpha/scripts/test_alpha.sh:8 pipes into grep -m {race}",
                    f"skills/alpha/scripts/test_alpha.sh:9 pipes into grep -q {race}",
                    f"skills/alpha/scripts/test_alpha.sh:13 pipes into egrep --max-count {race}",
                    "tests/test_unread.sh cannot be read as Bash to find a pipe into grep -q: No closing quotation",
                ],
                pipe_race_problems(root),
            )

    def test_assertion_holder_policy_refuses_a_test_case_made_outside_a_class(self) -> None:
        holder = "CHECK = unittest.TestCase()\n"
        suite = f"import unittest\n\n{holder}\n\ndef test_it():\n    pass\n"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "conftest.py": f"import unittest\n\n{holder}",
                    "tests/test_module.py": suite,
                    "tests/deployer/harness.py": "from unittest import TestCase as Case\n\nCHECK = Case()\n",
                    "tests/test_function.py": (
                        "import unittest\n\n\ndef test_it():\n    unittest.TestCase().assertTrue(True)\n"
                    ),
                    "skills/alpha/SKILL.md": "---\nname: alpha\n---\n",
                    "skills/alpha/scripts/test_alpha.py": suite,
                    # A subclass made in a test, a holder a class body makes, a plain assert, a module a skill
                    # deploys, and a fixture tree are none of them.
                    "tests/test_clean.py": (
                        "import unittest\n\n\nclass Case(unittest.TestCase):\n    HOLDER = unittest.TestCase()\n\n"
                        "    def test_it(self):\n        Case('test_it')\n\n\ndef test_plain():\n    assert True\n"
                    ),
                    "skills/alpha/scripts/alpha.py": f"import unittest\n\n{holder}",
                    "tests/fixtures/review/test_other.py": suite,
                },
            )
            problem = "makes a unittest.TestCase to check through; check with a plain assert, which pytest rewrites"
            self.assertEqual(
                [
                    f"conftest.py:3 {problem} to show both sides",
                    f"skills/alpha/scripts/test_alpha.py:3 {problem} to show both sides",
                    f"tests/deployer/harness.py:3 {problem} to show both sides",
                    f"tests/test_function.py:5 {problem} to show both sides",
                    f"tests/test_module.py:3 {problem} to show both sides",
                ],
                assertion_holder_problems(root),
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
                f"{tests}",
                encoding="utf-8",
            )
            for index in range(3):
                completed = run_pytest(suite, *shard_options(index, 3, {}))
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
            entries = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                sorted(f"test_{number}" for number in range(7)), sorted(e for e in entries if e.startswith("test_"))
            )
            self.assertEqual(3, entries.count("module from the suite directory"))
            self.assertEqual(3, entries.count("class"))

            suite.write_text(
                suite.read_text(encoding="utf-8").replace("record('test_3')", "self.fail('boom')"), encoding="utf-8"
            )
            failing = run_pytest(suite, *shard_options(0, 3, {}))
            self.assertEqual(1, failing.returncode)
            self.assertIn("boom", failing.stdout)
            for options in (["--suite-shard", "4/3"], ["--suite-shard", "0/3"], ["--suite-shard", "two"]):
                with self.subTest(options=options):
                    refused = run_pytest(suite, *options)
                    self.assertEqual(4, refused.returncode, refused.stdout + refused.stderr)
                    expected = f"--suite-shard expects <index>/<count> with 1 <= index <= count, found {options[1]!r}"
                    self.assertIn(expected, refused.stderr)

    def test_the_runner_starts_a_python_suite_through_pytest_on_its_one_file(self) -> None:
        self.assertEqual(
            [sys.executable, "-B", "-m", "pytest", "tests/test_a.py", "--suite-shard", "2/3"],
            pytest_command("tests/test_a.py", "--suite-shard", "2/3"),
        )
        self.assertEqual(
            ["--suite-shard", "1/4", "--test-seconds", "A::test_a=13", "--test-seconds", "B::test_b=4.5"],
            shard_options(0, 4, {"B::test_b": 4.5, "A::test_a": 13}),
        )

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
                f"class Fixture(unittest.TestCase):\n{tests}",
                encoding="utf-8",
            )

            def shard(index: int, recorded: dict[str, float]) -> list[str]:
                log.unlink(missing_ok=True)
                completed = run_pytest(suite, *shard_options(index, 3, recorded))
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                return log.read_text(encoding="utf-8").splitlines()

            # Nothing recorded: round-robin in test-name order, as before.
            self.assertEqual(["test_0", "test_3", "test_6"], shard(0, {}))
            # test_3 recorded at five seconds: packed first, alone on shard 0; the rest share shards 1 and 2.
            packed = [shard(index, {"Fixture::test_3": 5}) for index in range(3)]
            self.assertEqual(["test_3"], packed[0])
            self.assertEqual(["test_0", "test_2", "test_5"], packed[1])
            ran = sorted(entry for entries in packed for entry in entries)
            self.assertEqual(sorted(f"test_{number}" for number in range(7)), ran)
            for recorded, refusal in (
                ("Fixture::test_9=5", "the suite defines no test Fixture::test_9"),
                ("Fixture::test_3", "--test-seconds expects <test>=<seconds> above 0"),
                ("Fixture::test_3=many", "--test-seconds expects <test>=<seconds> above 0"),
                ("Fixture::test_3=0", "--test-seconds expects <test>=<seconds> above 0"),
            ):
                with self.subTest(recorded=recorded):
                    refused = run_pytest(suite, "--suite-shard", "1/3", "--test-seconds", recorded)
                    self.assertEqual(4, refused.returncode, refused.stdout + refused.stderr)
                    self.assertIn(refusal, refused.stderr)
            alone = run_pytest(suite, "--test-seconds", "Fixture::test_3=5")
            self.assertEqual(4, alone.returncode)
            self.assertIn("--test-seconds needs --suite-shard", alone.stderr)

    def test_recorded_seconds_raise_the_shard_count_and_reach_every_shard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "test_costly.py"
            body = "".join(f"    def test_{number}(self): pass\n" for number in range(17))
            suite.write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            self.assertEqual(17 // TESTS_PER_SHARD, shard_count(suite, {}))
            self.assertEqual((17 + 12) // TESTS_PER_SHARD, shard_count(suite, {"T::test_4": 13}))
            jobs = suite_jobs([suite], {"test_costly.py": {"T::test_4": 13}}, root=Path(temporary))
            self.assertEqual(shard_count(suite, {"T::test_4": 13}), len(jobs))
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
            recorded = {"tests/test_fits.py": {"T::test_4": 13}, "tests/test_outgrown.py": {"T::test_4": 13}}
            self.assertEqual(
                [
                    "tests/test_outgrown.py deals T::test_4 onto a shard beside other tests, since the suite fills its "
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
                    "    def test_long(self): pass\n",
                },
            )
            self.assertEqual([], recorded_cost_problems(root, {"tests/test_costly.py": {"Slow::test_long": 13}}))
            self.assertEqual(
                [
                    "tests/test_costly.py records Slow::test_gone, which it does not define",
                    "tests/test_costly.py records Fast::test_long, which it does not define",
                    "tests/test_missing.py records test costs but is not a regression suite",
                    "tests/test_costly.py records Slow::test_long at 0 seconds; record only tests that cost more "
                    "than 1",
                ],
                recorded_cost_problems(
                    root,
                    {
                        "tests/test_costly.py": {"Slow::test_gone": 4, "Fast::test_long": 4},
                        "tests/test_missing.py": {"Slow::test_long": 4},
                    },
                )
                + recorded_cost_problems(root, {"tests/test_costly.py": {"Slow::test_long": 0}}),
            )

    def test_a_test_a_block_of_its_class_body_holds_is_counted_and_never_recorded(self) -> None:
        source = (
            "import sys\nimport unittest\n\n\nclass Case(unittest.TestCase):\n    def test_everywhere(self): pass\n\n"
            "    if sys.platform == 'win32':\n        def test_on_windows(self): pass\n    else:\n"
            "        def test_elsewhere(self): pass\n\n\nif sys.platform == 'linux':\n\n    def test_on_linux(): pass\n"
        )
        # pytest collects a test a block of the class body or of the module defines on the platforms that run it.
        self.assertEqual(
            ["Case::test_elsewhere", "Case::test_everywhere", "Case::test_on_windows", "test_on_linux"],
            defined_tests(ast.parse(source)),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tests/test_platforms.py": source})
            self.assertEqual(
                [], recorded_cost_problems(root, {"tests/test_platforms.py": {"Case::test_everywhere": 4}})
            )
            self.assertEqual(
                [
                    "tests/test_platforms.py records Case::test_on_windows, which a block of its suite holds, so "
                    "pytest collects it on some platforms only; record only a test every platform runs"
                ],
                recorded_cost_problems(root, {"tests/test_platforms.py": {"Case::test_on_windows": 4}}),
            )

    def test_a_parametrized_test_is_never_recorded_since_the_sharded_run_collects_only_its_cases(self) -> None:
        source = (
            "import pytest\n\n\n@pytest.mark.parametrize('case', [1, 2])\ndef test_cases(case): pass\n\n\n"
            "def test_plain(): pass\n\n\n@pytest.mark.parametrize('case', [1, 2])\nclass TestWhole:\n"
            "    def test_each(self, case): pass\n\n\nclass TestMarked:\n"
            "    pytestmark = pytest.mark.parametrize('case', [1, 2])\n\n    def test_each(self, case): pass\n\n\n"
            "class TestOne:\n    @pytest.mark.parametrize('case', [1, 2])\n    def test_each(self, case): pass\n\n"
            "    def test_plain(self): pass\n"
        )
        marked = (
            "import pytest\n\npytestmark = [pytest.mark.parametrize('case', [1, 2])]\n\n\ndef test_any(case): pass\n"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tests/test_cases.py": source, "tests/test_marked.py": marked})
            self.assertEqual(
                [],
                recorded_cost_problems(root, {"tests/test_cases.py": {"test_plain": 4, "TestOne::test_plain": 4}}),
            )
            recorded = {
                "tests/test_cases.py": {
                    "test_cases": 4,
                    "TestWhole::test_each": 4,
                    "TestMarked::test_each": 4,
                    "TestOne::test_each": 4,
                },
                "tests/test_marked.py": {"test_any": 4},
            }
            self.assertEqual(
                [
                    f"tests/test_cases.py records {test}, which parametrize expands into one test per case, each "
                    f"collected as {test}[<id>]; record only a test that is not parametrized, or make the costly case "
                    "a test of its own"
                    for test in ("test_cases", "TestWhole::test_each", "TestMarked::test_each", "TestOne::test_each")
                ]
                + [
                    "tests/test_marked.py records test_any, which parametrize expands into one test per case, each "
                    "collected as test_any[<id>]; record only a test that is not parametrized, or make the costly "
                    "case a test of its own"
                ],
                recorded_cost_problems(root, recorded),
            )
            # The sharded run the policy keeps it from: pytest collects test_cases[1] and test_cases[2], never the name.
            refused = run_pytest(
                root / "tests" / "test_cases.py", "--suite-shard", "1/2", "--test-seconds", "test_cases=4"
            )
            self.assertNotEqual(0, refused.returncode)
            self.assertIn("the suite defines no test test_cases", refused.stdout + refused.stderr)

    def test_suite_discovery_documentation_policy_detects_each_missing_rule(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            guide = root / "docs" / "adding-a-skill.md"
            guide.write_text(
                "# Adding a skill\n\n## Files\n\n`test-*` `*.test.*` `.sh` `conftest.py`\n\n"
                "## Validation\n\nName it `test_*`, `*_test`, or `*-test` and use `.py` or `.ps1`.\n\n## Later\n",
                encoding="utf-8",
            )
            missing = 'docs/adding-a-skill.md "Validation" does not name'
            self.assertEqual(
                [
                    f"{missing} `test-*`",
                    f"{missing} `*.test.*`",
                    f"{missing} `.sh`",
                    f"{missing} `conftest.py`",
                ],
                suite_discovery_documentation_problems(root),
            )
            guide.write_text("# Adding a skill\n", encoding="utf-8")
            self.assertEqual(
                ['docs/adding-a-skill.md has no "Validation" section'], suite_discovery_documentation_problems(root)
            )

    def test_suite_names_follow_the_documented_patterns(self) -> None:
        self.assertEqual(("test_*", "test-*", "*_test", "*-test", "*.test.*"), SUITE_NAME_PATTERNS)


@pytest.mark.parametrize(
    "name",
    [
        "test_a.py",
        "test-a.sh",
        "a_test.ps1",
        "a-test.py",
        "a.test.py",
        "a.test.b.sh",
        "TEST_A.PY",
        "Test-A.Ps1",
        "test_.py",
    ],
)
def test_a_name_a_pattern_matches_is_a_suite(name: str) -> None:
    assert is_test_script(Path(name))


@pytest.mark.parametrize(
    "name", ["a.py", "testa.py", "atest.py", "test_a.txt", "a_tests.py", "contest.py", "a.tests.py", "test_a.js"]
)
def test_a_name_no_pattern_matches_is_no_suite(name: str) -> None:
    assert not is_test_script(Path(name))


class SkillSuiteFixtures(unittest.TestCase):
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


# A root conftest.py that defines the isolation fixtures as the policy requires, beside a helper.
ISOLATING_CONFTEST = (
    "import pytest\nfrom pytest import fixture\n\n\n"
    '@pytest.fixture(autouse=True, scope="module")\ndef isolated_git_module():\n    yield\n\n\n'
    "@fixture(scope='class', autouse=True)\ndef isolated_git_class():\n    yield\n\n\n"
    "@pytest.fixture(autouse=True)\ndef git_home():\n    yield\n\n\ndef helper():\n    pass\n"
)


class GitIsolationPolicy(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return git_isolation_problems(root)

    def test_a_root_conftest_that_defines_each_fixture_autouse_at_its_scope_passes(self) -> None:
        self.assertEqual(
            [],
            self.problems(
                {
                    "conftest.py": ISOLATING_CONFTEST,
                    "tests/test_a.py": "class Case:\n    git_config = {'init.defaultBranch': 'main'}\n"
                    "    def test_it(self):\n        assert self.git_home\n",
                    "skills/a/SKILL.md": "",
                    "skills/a/scripts/test_a.py": "def test_it(git_home):\n    assert git_home\n",
                    # A fixture tree is another repository, whose conftest.py is its own.
                    "tests/fixtures/repo/conftest.py": "def git_home():\n    pass\n",
                }
            ),
        )

    def test_a_missing_conftest_is_reported(self) -> None:
        self.assertEqual(
            ["conftest.py is missing; its autouse fixtures isolate git for every Python suite"],
            self.problems({"tests/test_a.py": "def test_it():\n    pass\n"}),
        )

    def test_a_fixture_that_is_absent_not_autouse_or_at_another_scope_is_reported(self) -> None:
        conftest = (
            "import pytest\n\n\n"
            '@pytest.fixture(scope="module")\ndef isolated_git_module():\n    yield\n\n\n'
            '@pytest.fixture(autouse=True, scope="session")\ndef isolated_git_class():\n    yield\n\n\n'
            "def git_home():\n    pass\n"
        )
        self.assertEqual(
            [
                "conftest.py does not define isolated_git_module as an autouse fixture of module scope, which "
                "isolates git for each module of every suite",
                "conftest.py does not define isolated_git_class as an autouse fixture of class scope, which "
                "isolates git for each class of every suite",
                "conftest.py does not define git_home as an autouse fixture of function scope, which isolates git "
                "for each test of every suite",
            ],
            self.problems({"conftest.py": conftest}),
        )

    def test_a_nearer_conftest_and_a_definition_of_a_fixture_name_are_reported(self) -> None:
        override = "takes the place of the root conftest.py's fixture that isolates git"
        self.assertEqual(
            [
                ".claude/skills/local/scripts/conftest.py is a conftest.py below the root; the root's conftest.py "
                "is the one that sets up every suite",
                f".claude/skills/local/scripts/conftest.py:1 defines git_home, which {override}",
                f"skills/a/scripts/test_a.py:4 defines isolated_git_class, which {override}",
                f"tests/deployer/harness.py:2 defines isolated_git_module, which {override}",
                f"tests/test_a.py:1 defines git_home, which {override}",
                f"tests/test_b.py:4 defines git_home, which {override}",
            ],
            self.problems(
                {
                    "conftest.py": ISOLATING_CONFTEST,
                    "tests/test_a.py": "def git_home():\n    pass\n\n\ndef test_it():\n    pass\n",
                    # An annotation alone binds nothing; a parametrize of the name replaces the fixture.
                    "tests/test_b.py": "import pytest\n\n\n@pytest.mark.parametrize('value, git_home', [(1, 2)])\n"
                    "def test_it(value, git_home):\n    pass\n\n\nclass Case:\n    git_home: object\n",
                    "tests/deployer/harness.py": "class Base:\n    isolated_git_module = None\n",
                    "skills/a/SKILL.md": "",
                    "skills/a/scripts/test_a.py": "import pytest\n\n\nisolated_git_class: object = None\n",
                    ".claude/skills/local/SKILL.md": "",
                    ".claude/skills/local/scripts/conftest.py": "def git_home():\n    pass\n",
                }
            ),
        )


class PytestConfigurationPolicy(unittest.TestCase):
    def problems(self, files: Mapping[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, files)
            return pytest_configuration_problems(root)

    def test_the_pinned_table_passes_and_any_other_or_an_earlier_file_fails(self) -> None:
        pinned = (
            "[tool.pytest.ini_options]\n"
            'addopts = ["-p", "no:cacheprovider", "--capture=no", "--strict-markers", "--strict-config"]\n'
        )
        self.assertEqual(
            {"addopts": ["-p", "no:cacheprovider", "--capture=no", "--strict-markers", "--strict-config"]},
            PYTEST_CONFIGURATION,
        )
        self.assertEqual([], self.problems({"pyproject.toml": pinned}))
        deselects = pinned.replace('"--strict-config"]', '"--strict-config", "--deselect", "tests/test_a.py"]')
        self.assertEqual(
            [
                "pyproject.toml: [tool.pytest] must be ini_options = "
                f"{PYTEST_CONFIGURATION!r}, not {{'ini_options': {{'addopts': "
                "['-p', 'no:cacheprovider', '--capture=no', '--strict-markers', '--strict-config', '--deselect', "
                "'tests/test_a.py']}}"
            ],
            self.problems({"pyproject.toml": deselects}),
        )
        self.assertEqual(
            [
                "pytest.ini holds pytest configuration ahead of pyproject.toml; keep it in [tool.pytest.ini_options] "
                "alone",
                "setup.cfg holds pytest configuration ahead of pyproject.toml; keep it in [tool.pytest.ini_options] "
                "alone",
                f"pyproject.toml: [tool.pytest] must be ini_options = {PYTEST_CONFIGURATION!r}, not {{}}",
            ],
            self.problems({"pyproject.toml": "[tool.ruff]\n", "pytest.ini": "[pytest]\n", "setup.cfg": ""}),
        )


class PoolSharedStatePolicy(unittest.TestCase):
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

    def test_an_aliased_in_place_union_dup2_the_bytes_environment_and_a_lookup_by_name_are_reported(self) -> None:
        alpha = (
            "import importlib\n"
            "import os\n"
            "import sys\n"
            "from os import environb as raw\n"
            "\n"
            "def check(descriptor, name):\n"
            "    env = os.environ\n"
            "    env |= {'A': 'b'}\n"
            "    os.dup2(descriptor, 1)\n"
            "    os.environb[b'A'] = b'b'\n"
            "    raw.update({b'A': b'b'})\n"
            "    getattr(os, 'environ')['A'] = 'b'\n"
            "    sys.modules['os'].environ['A'] = 'b'\n"
            "    importlib.import_module('os').chdir('..')\n"
            "    __import__('os.path').environ.pop('A')\n"
            "    getattr(sys.modules['sys'], 'stdout').write\n"
            "    sys.modules['sys'].stderr = None\n"
            "    getattr(os, name)('..')\n"
            "    setattr(sys, name, None)\n"
            "    sys.modules[name].environ['A'] = 'b'\n"
        )
        problems = self.problems({"tests/validation/alpha.py": alpha})
        lookup = "may change a standard stream, os.environ, or the working directory through"
        self.assertEqual(
            [
                "tests/validation/alpha.py:8 changes env",
                "tests/validation/alpha.py:9 calls os.dup2",
                "tests/validation/alpha.py:10 changes os.environb[b'A']",
                "tests/validation/alpha.py:11 calls os.environb.update",
                "tests/validation/alpha.py:12 changes getattr(os, 'environ')['A']",
                "tests/validation/alpha.py:13 changes sys.modules['os'].environ['A']",
                "tests/validation/alpha.py:14 calls os.chdir",
                "tests/validation/alpha.py:15 calls os.environ.pop",
                "tests/validation/alpha.py:17 changes sys.modules['sys'].stderr",
                f"tests/validation/alpha.py:18 {lookup} `getattr(os, name)`",
                f"tests/validation/alpha.py:19 {lookup} `setattr(sys, name, None)`",
                f"tests/validation/alpha.py:20 {lookup} `sys.modules[name].environ['A']`",
            ],
            [problem.split(", a lookup", 1)[0].split(", which", 1)[0] for problem in problems],
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

    def test_a_rebound_name_a_copy_and_a_read_through_a_lookup_pass(self) -> None:
        alpha = (
            "import importlib\n"
            "import os\n"
            "import sys\n"
            "\n"
            "def check(node, name, first, second):\n"
            "    env = os.environ\n"
            "    env = {}\n"
            "    env |= {'A': 'b'}\n"
            "    copied = dict(os.environb)\n"
            "    copied |= {b'A': b'b'}\n"
            "    os.dup(1)\n"
            "    print(getattr(os, 'environ')['A'], sys.modules['os'].environ.get('A'))\n"
            "    print(importlib.import_module('os').getcwd(), getattr(os, name), sys.modules[name])\n"
            "    getattr(node, name)()\n"
            "    setattr(node, name, None)\n"
            "    sys.modules['json'].dumps = None\n"
            "    first |= second\n"
        )
        self.assertEqual([], self.problems({"tests/validation/alpha.py": alpha}))
