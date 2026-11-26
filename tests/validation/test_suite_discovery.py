"""Fixture tests for tests/validation/suite_discovery.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import file_presence
import job_pool
import suite_discovery
import validation_support
from job_selection import name_patterns
from suite_discovery import (
    MAXIMUM_SHARDS,
    PYTEST_CONFIGURATION,
    assertion_holder_problems,
    defined_tests,
    git_isolation_problems,
    pipe_race_problems,
    pool_shared_state_problems,
    pytest_base,
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
from toolchain import find_git_bash
from validation_support import REPOSITORY_ROOT, SUITE_NAME_PATTERNS, is_test_script, write_fixture_tree

# The operating-system names this module states on purpose, with the reason, which the platform policy reads.
PLATFORM_ALLOWED = {
    "sys.platform": "skips the run of GNU_RACES on macOS, which has no timeout and whose env reads no long option; a "
    "skip whose condition reads the platform alone is what the workaround policy allows, which a seam constant is not",
}


def run_pytest(
    suite: Path, *options: str, environment: Mapping[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a fixture suite as the runner runs a suite, from the repository root with a base folder of its own, with its
    conftest.py as a plugin, since a suite outside the repository is outside the root that loads it, and `environment`
    over this process's. A pytest.ini beside the suite makes its folder the run's root, so pytest reads no folder above
    it, where other tests' temporary folders come and go."""
    (suite.parent / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    with pytest_base() as basetemp:
        return subprocess.run(
            [*pytest_command(str(suite), *options, basetemp=basetemp), "-p", "conftest", "-p", "no:cacheprovider"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=REPOSITORY_ROOT,
            env={**os.environ, **(environment or {})},
            check=False,
        )


# A suite whose test makes a temporary folder through pytest's tmp_path, and prints the folder it was given.
TMP_PATH_SUITE = b"""\
def test_makes_a_temporary_folder(tmp_path):
    (tmp_path / "made").write_bytes(b"")
    print("TMP_PATH", tmp_path)
"""


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

    def test_pipe_race_policy_refuses_a_pipe_into_a_reader_that_ends_early(self) -> None:
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
                        # The lesson's Search for names head in every form that sets a count, and alone.
                        "seq 1 1000000 | head -n 1 >/dev/null\n"
                        "seq 1 1000000 | head -n1\n"
                        "seq 1 1000000 | head -1\n"
                        "seq 1 1000000 | head -c 5\n"
                        "seq 1 1000000 | head --lines=1\n"
                        "seq 1 1000000 | head\n"
                        'first="$(printf \'%s\\n\' "$OUTPUT" | head -n 1)"\n'
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
                        # A negative count, all but the last, reads to the end; an expansion takes a line unpiped.
                        "seq 1 9 | head -n -1\n"
                        "seq 1 9 | head -n-1\n"
                        "seq 1 9 | head --lines=-2\n"
                        "seq 1 9 | head -c -3\n"
                        "first=\"${OUTPUT%%$'\\n'*}\"\n"
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
                "under pipefail, which fails the check when the writer dies of SIGPIPE after the reader ends; give it "
                "its text in a here-string or a file, or take a line by parameter expansion"
            )
            alpha = "skills/alpha/scripts/test_alpha.sh"
            self.assertEqual(
                [
                    f"{alpha}:3 pipes into grep -q {race}",
                    f"{alpha}:4 pipes into grep -q {race}",
                    f"{alpha}:6 pipes into grep --quiet {race}",
                    f"{alpha}:8 pipes into grep -m {race}",
                    f"{alpha}:9 pipes into grep -q {race}",
                    f"{alpha}:13 pipes into egrep --max-count {race}",
                    f"{alpha}:14 pipes into head -n {race}",
                    f"{alpha}:15 pipes into head -n {race}",
                    f"{alpha}:16 pipes into head -1 {race}",
                    f"{alpha}:17 pipes into head -c {race}",
                    f"{alpha}:18 pipes into head --lines {race}",
                    f"{alpha}:19 pipes into head {race}",
                    f"{alpha}:20 pipes into head -n {race}",
                    "tests/test_unread.sh cannot be read as Bash to find a pipe into a reader that ends early: a "
                    "single quote is not closed",
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
            # A count past the interpreter's digit limit for converting text is refused by the same expected form.
            past_digit_limit = f"1/{'9' * 5000}"
            for options in (
                ["--suite-shard", "4/3"],
                ["--suite-shard", "0/3"],
                ["--suite-shard", "two"],
                ["--suite-shard", past_digit_limit],
            ):
                with self.subTest(options=options):
                    refused = run_pytest(suite, *options)
                    self.assertEqual(4, refused.returncode, refused.stdout + refused.stderr)
                    expected = f"--suite-shard expects <index>/<count> with 1 <= index <= count, found {options[1]!r}"
                    self.assertIn(expected, refused.stderr)

    def test_the_runner_starts_a_python_suite_through_pytest_on_its_one_file(self) -> None:
        base = Path("pytest base") / "base"
        self.assertEqual(
            [sys.executable, "-B", "-m", "pytest", "tests/test_a.py", "--basetemp", str(base), "--suite-shard", "2/3"],
            pytest_command("tests/test_a.py", "--suite-shard", "2/3", basetemp=base),
        )
        self.assertEqual(
            ["--suite-shard", "1/4", "--test-seconds", "A::test_a=13", "--test-seconds", "B::test_b=4.5"],
            shard_options(0, 4, {"B::test_b": 4.5, "A::test_a": 13}),
        )

    def test_large_python_suites_are_sharded_and_others_run_whole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suites = (("test_small.py", 3), ("test_whole.py", 15), ("test_split.py", 16), ("test_large.py", 40))
            for name, tests in (*suites, ("test_huge.py", 200)):
                body = "".join(f"    def test_{number}(self): pass\n" for number in range(tests))
                (root / name).write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            (root / "test_script.ps1").write_text("exit 0\n", encoding="utf-8")
            self.assertEqual(1, shard_count(root / "test_small.py"))
            # A shard carries eight tests, so a suite of fewer than sixteen runs in one process.
            self.assertEqual(1, shard_count(root / "test_whole.py"))
            self.assertEqual(2, shard_count(root / "test_split.py"))
            self.assertEqual(5, shard_count(root / "test_large.py"))
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
                *(
                    (f"Fixture::test_3{value}", "--test-seconds expects <test>=<seconds>, a finite number above 0")
                    for value in ("", "=many", "=0", "=-1", "=nan", "=inf", "=Infinity", "=1e999")
                ),
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
            self.assertEqual(2, shard_count(suite, {}))
            self.assertEqual(3, shard_count(suite, {"T::test_4": 13}))
            jobs = suite_jobs([suite], {"test_costly.py": {"T::test_4": 13}}, root=Path(temporary))
            self.assertEqual(shard_count(suite, {"T::test_4": 13}), len(jobs))
            self.assertEqual({(17 + 12) / len(jobs)}, {job.weight for job in jobs})

    def test_a_suite_of_few_costly_tests_gets_no_more_shards_than_tests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "test_costly.py"
            body = "".join(f"    def test_{number}(self): pass\n" for number in range(3))
            suite.write_text(f"import unittest\nclass T(unittest.TestCase):\n{body}", encoding="utf-8")
            recorded = {"T::test_0": 40, "T::test_1": 40, "T::test_2": 40}
            self.assertEqual(3, shard_count(suite, recorded))
            jobs = suite_jobs([suite], {"test_costly.py": recorded}, root=Path(temporary))
            self.assertEqual(3, len(jobs))

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

    def test_a_recorded_suite_a_stat_cannot_examine_is_named_so_never_as_no_suite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            denied = PermissionError(13, "Permission denied")
            with mock.patch.object(file_presence, "is_present", side_effect=denied):
                problems = recorded_cost_problems(Path(temporary), {"tests/test_denied.py": {"Slow::test_long": 4}})
            self.assertEqual(
                ["tests/test_denied.py records test costs but cannot be examined: Permission denied"], problems
            )

    def test_a_recorded_suite_of_another_kind_is_named_so_never_as_no_suite(self) -> None:
        # A folder where a recorded suite belongs is not a file, which the problem says, rather than reading it as
        # a suite that is not there.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tests" / "test_folder.py").mkdir(parents=True)
            self.assertEqual(
                ["tests/test_folder.py records test costs but is not a file"],
                recorded_cost_problems(root, {"tests/test_folder.py": {"Slow::test_long": 4}}),
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


def races_in(tmp_path: Path, script: str) -> list[str]:
    """What the pipe-race policy reports for a suite that sets pipefail and runs `script`, each as `<line> <reader>`."""
    write_fixture_tree(tmp_path, {"tests/test_race.sh": f"#!/usr/bin/env bash\nset -euo pipefail\n{script}\n"})
    return [
        problem.split(" under pipefail")[0].removeprefix("tests/test_race.sh:")
        for problem in pipe_race_problems(tmp_path)
    ]


# The shapes around a reader that ends early, and the spellings of its program and options: each fails at random under
# pipefail, as `printf '%s\n' a b | { head -n 1; }` and the backtick form exited 141 in every Git Bash run.
@pytest.mark.parametrize(
    ("script", "race"),
    [
        pytest.param("printf '%s\\n' a b | { head -n 1; }", "3 pipes into head -n", id="brace group"),
        pytest.param("printf '%s\\n' a b | ( head -n 1 )", "3 pipes into head -n", id="subshell group"),
        pytest.param("printf a | { cat >/dev/null; head; }", "3 pipes into head", id="later command of a group"),
        pytest.param("printf a | { ( head -c 1 ); }", "3 pipes into head -c", id="group inside a fed group"),
        pytest.param('x="`printf a | head -n 1`"', "3 pipes into head -n", id="quoted backticks"),
        pytest.param("x=`printf a | head -n 1`", "3 pipes into head -n", id="unquoted backticks"),
        pytest.param("x=\"$(printf ')' | head -n 1)\"", "3 pipes into head -n", id="a quoted ) in a substitution"),
        pytest.param('x="$(printf "(" | head -n 1)"', "3 pipes into head -n", id="a double-quoted ( in a substitution"),
        pytest.param('x="$(echo "$(printf a | head -n 1)")"', "3 pipes into head -n", id="nested substitution"),
        pytest.param("x=$'it\\'s'; y=\"$(printf a | head)\"", "3 pipes into head", id="after an ANSI-C string"),
        pytest.param('x="$(true\n)"\ny="$(printf a |\n  head -n 1)"', "6 pipes into head -n", id="its own line"),
        pytest.param("printf a | /usr/bin/head -n 1", "3 pipes into head -n", id="a path"),
        pytest.param("printf a | head.exe -n 1", "3 pipes into head -n", id="the Windows name"),
        pytest.param("printf a | env head -n 1", "3 pipes into head -n", id="env"),
        pytest.param("printf a | env -i LC_ALL=C head -n 1", "3 pipes into head -n", id="env with options"),
        pytest.param("printf a | exec head -n 1", "3 pipes into head -n", id="exec"),
        pytest.param("printf a | timeout 5 head -n 1", "3 pipes into head -n", id="timeout"),
        pytest.param("printf a | timeout -s KILL 5 head -n 1", "3 pipes into head -n", id="timeout with a signal"),
        pytest.param("printf a | command head -n 1", "3 pipes into head -n", id="command"),
        pytest.param("printf a | nice -n 5 stdbuf -o0 head", "3 pipes into head", id="wrappers in a row"),
        pytest.param("printf a | LC_ALL=C env grep -q a", "3 pipes into grep -q", id="grep through env"),
        pytest.param("printf a | /bin/grep -q a", "3 pipes into grep -q", id="grep by a path"),
        pytest.param("printf a | grep --qui a", "3 pipes into grep --quiet", id="grep's abbreviated option"),
        pytest.param("printf a | head -n -1 -n 1", "3 pipes into head -n", id="a later positive count"),
        pytest.param("printf a | head --lines=-1 -c 2", "3 pipes into head -c", id="a later count of bytes"),
        pytest.param("printf a | head -qn1", "3 pipes into head -n", id="a count in a cluster"),
        pytest.param("printf a | head --li 1", "3 pipes into head --lines", id="an abbreviated long option"),
        pytest.param("printf a | head - -n 1", "3 pipes into head -n", id="an option after an operand"),
        # After --, -n is a file head reads instead of the pipe, which it leaves unread.
        pytest.param("printf a | head -- -n -1", "3 pipes into head", id="a count after --"),
    ],
)
def test_the_pipe_race_policy_finds_the_reader_in_every_shape_and_spelling(
    tmp_path: Path, script: str, race: str
) -> None:
    assert races_in(tmp_path, script) == [race]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("printf a | head -n 1 -n -1", id="a later negative count"),
        pytest.param("printf a | head -n 1 --bytes=-2", id="a later negative count of bytes"),
        pytest.param("printf a | head -qn-1", id="a negative count in a cluster"),
        pytest.param("printf a | { cat; } && head -n 1 file", id="a command after the group"),
        pytest.param("f() { printf a | cat; }\nhead -n 1 file", id="a command after a function"),
        pytest.param("echo '$(printf a | head)'", id="a substitution in single quotes"),
        pytest.param("echo hi # $(printf a | head)", id="a substitution in a comment"),
        pytest.param("printf a | command -v head", id="a lookup"),
        pytest.param("printf a | xargs head -n 1", id="xargs, which reads to the end"),
        pytest.param("printf a | gxargs head -n 1", id="xargs under a prefix, which reads to the end"),
        pytest.param("printf a '|' head -n 1", id="a single-quoted pipe, a word"),
        pytest.param('printf a "|" head -n 1', id="a double-quoted pipe, a word"),
        pytest.param("printf a \\| head -n 1", id="an escaped pipe, a word"),
        pytest.param("printf a \\|\\& head -n 1", id="an escaped |&, a word"),
        # A quoted or escaped reserved word is the program Bash runs, never a leader before the reader.
        pytest.param("printf a | 'do' head -n 1", id="a quoted do, a program"),
        pytest.param("printf a | \\! head -n 1", id="an escaped !, a program"),
        pytest.param('printf a | "if" head -n 1', id="a quoted if, a program"),
        pytest.param("printf a | 'then' head -n 1", id="a quoted then, a program"),
        # A reserved word after a command's program is its argument, which opens no compound the pipe feeds.
        pytest.param("printf a | echo while; head -n 1 file", id="while as an argument"),
        # A case word is one word to its `in`, a substitution and what follows it included, so no part of it runs.
        pytest.param("printf a | case $(echo a)head in ahead) cat;; esac", id="a case word past a substitution"),
        # A quoted or escaped ) or esac is a case pattern, so the | after it separates patterns, never pipes.
        pytest.param("printf a | case x in ')'|head) cat;; esac", id="a quoted ) pattern"),
        pytest.param("printf a | case x in \\)|head) cat;; esac", id="an escaped ) pattern"),
        pytest.param("printf a | case x in 'esac'|head) cat;; esac", id="a quoted esac pattern"),
    ],
)
def test_the_pipe_race_policy_passes_a_reader_that_reads_to_the_end(tmp_path: Path, script: str) -> None:
    assert races_in(tmp_path, script) == []


# Each shape Bash gives a command a pipe feeds: a wrapper by its path, each compound command, a function the pipe runs,
# a script a shell or eval runs, and a wrapper the reader table reads past. Each race below exited 141 under pipefail
# in Git Bash, which the test after this one checks wherever Bash runs.
BASH_RACES = [
    pytest.param("seq 1 200000 | /usr/bin/env head -n 1", "head -n", id="env by a path"),
    pytest.param("seq 1 200000 | if true; then head -n 1; fi", "head -n", id="if's body"),
    pytest.param("seq 1 200000 | if head -n 1; then :; fi", "head -n", id="if's condition"),
    pytest.param("seq 1 200000 | if false; then :; else head -n 1; fi", "head -n", id="else"),
    pytest.param("seq 1 200000 | if false; then :; elif head -n 1; then :; fi", "head -n", id="elif"),
    pytest.param("seq 1 200000 | for x in a; do head -n 1; done", "head -n", id="for"),
    pytest.param("seq 1 200000 | while true; do head -n 1; break; done", "head -n", id="while"),
    pytest.param("seq 1 200000 | until false; do head -n 1; break; done", "head -n", id="until"),
    pytest.param("seq 1 200000 | case a in a) head -n 1;; esac", "head -n", id="case"),
    pytest.param("seq 1 200000 | case a in b|a) head -n 1;; esac", "head -n", id="case with alternatives"),
    pytest.param("seq 1 200000 | case a in\n  (a)\n    head -n 1\n    ;;\nesac", "head -n", id="case over lines"),
    pytest.param("seq 1 200000 | { if true; then head -n 1; fi; }", "head -n", id="a compound in a group"),
    pytest.param("seq 1 200000 | if true; then { head -n 1; }; fi", "head -n", id="a group in a compound"),
    pytest.param("seq 1 200000 | if true; then if true; then head -n 1; fi; fi", "head -n", id="nested ifs"),
    pytest.param("seq 1 200000 | { echo ';' '}'; head -n 1; }", "head -n", id="a group past a quoted ; and }"),
    pytest.param("seq 1 200000 | { echo \\; \\}; head -n 1; }", "head -n", id="a group past an escaped ; and }"),
    pytest.param("seq 1 200000 | case a in 'esac'|a) head -n 1;; esac", "head -n", id="case past a quoted esac"),
    pytest.param("seq 1 200000 | { echo '>' & head -n 1; }", "head -n", id="a group past a quoted >, then &"),
    pytest.param("seq 1 200000 | { echo '()' { ; head -n 1; }", "head -n", id="a group past a quoted (), then {"),
    pytest.param(
        "seq 1 200000 | if true; then\n  'fi' 2>/dev/null\n  head -n 1\nfi", "head -n", id="if's body past a quoted fi"
    ),
    pytest.param(
        "seq 1 200000 | while true; do\n  \\done 2>/dev/null\n  head -n 1\n  break\ndone",
        "head -n",
        id="while's body past an escaped done",
    ),
    pytest.param("seq 1 200000 | if true; then echo fi; head -n 1; fi", "head -n", id="if's body past fi as a word"),
    pytest.param("case a in a) :;; esac; seq 1 200000 | head -n 1", "head -n", id="a pipe after a case"),
    pytest.param("seq 1 200000 | case $(echo a) in a) head -n 1;; esac", "head -n", id="case on a substitution"),
    pytest.param("seq 1 200000 | { echo a { ; head -n 1; }", "head -n", id="a group past an argument {"),
    pytest.param(
        " ".join(f"f{index}() {{ f{index + 1}; }};" for index in range(1, 16))
        + " f16() { head -n 1; }; seq 1 200000 | f1",
        "head -n",
        id="sixteen functions deep",
    ),
    pytest.param("f() { head -n 1; }; seq 1 200000 | f", "head -n", id="a function"),
    pytest.param("function g { head -n 1; }; seq 1 200000 | g", "head -n", id="a function keyword"),
    pytest.param("function g() ( head -n 1 ); seq 1 200000 | g", "head -n", id="a subshell function"),
    pytest.param("f() { g; }; g() { head -n 1; }; seq 1 200000 | f", "head -n", id="a function calling one"),
    pytest.param("f() { head -n 1; }; seq 1 200000 | { f; }", "head -n", id="a function in a group"),
    pytest.param("f() if true; then head -n 1; fi; seq 1 200000 | f", "head -n", id="a function whose body is an if"),
    pytest.param("f() for x in a; do head -n 1; done; seq 1 200000 | f", "head -n", id="a function whose body loops"),
    pytest.param("seq 1 200000 | { head -n 1 & wait; }", "head -n", id="a reader in the background"),
    pytest.param("seq 1 200000 | bash -c 'head -n 1'", "head -n", id="bash -c"),
    pytest.param("seq 1 200000 | sh -c 'head -n 1'", "head -n", id="sh -c"),
    pytest.param("seq 1 200000 | bash -ec 'head -n 1'", "head -n", id="bash -c in a cluster"),
    pytest.param("seq 1 200000 | bash -o errexit -c 'head -n 1'", "head -n", id="bash -c after -o"),
    # /bin/bash is where Linux, macOS, and Git Bash each hold a Bash; macOS has none at /usr/bin/bash.
    pytest.param("seq 1 200000 | /bin/bash -c 'head -n 1'", "head -n", id="bash by a path"),
    pytest.param("seq 1 200000 | eval 'head -n 1'", "head -n", id="eval"),
    pytest.param("seq 1 200000 | eval head -n 1", "head -n", id="eval's words"),
    pytest.param("seq 1 200000 | command eval 'head -n 1'", "head -n", id="eval behind command"),
    pytest.param("seq 1 200000 | bash -c 'eval \"head -n 1\"'", "head -n", id="eval in a shell"),
    # The pipeline's subshell exits at once and runs its EXIT trap with the pipe still its standard input.
    pytest.param("seq 1 200000 | trap 'head -n 1' EXIT", "head -n", id="a trap's action"),
    pytest.param("seq 1 200000 | env -S 'head -n 1'", "head -n", id="env -S"),
    pytest.param('seq 1 200000 | env -S"head -n 1"', "head -n", id="env -S glued"),
    pytest.param("seq 1 200000 | env -u HOME -S 'LC_ALL=C head -n 1'", "head -n", id="env -S after an option"),
    # A substitution reads its command's standard input, so a pipe that feeds the command feeds the reader in it.
    pytest.param("seq 1 200000 | echo $(head -n 1)", "head -n", id="a substitution in a fed command"),
    pytest.param("seq 1 200000 | { echo $(head -n 1); }", "head -n", id="a substitution in a fed group"),
    pytest.param("seq 1 200000 | ( echo $(head -n 1) )", "head -n", id="a substitution in a fed subshell"),
    pytest.param("seq 1 200000 | echo `head -n 1`", "head -n", id="backticks in a fed command"),
    pytest.param('seq 1 200000 | echo "$(head -n 1)"', "head -n", id="a quoted substitution in a fed command"),
    pytest.param('seq 1 200000 | echo "`head -n 1`"', "head -n", id="quoted backticks in a fed command"),
    pytest.param("seq 1 200000 | x=$(head -n 1)", "head -n", id="a fed assignment"),
    pytest.param("seq 1 200000 | cat <(head -n 1)", "head -n", id="a process substitution in a fed command"),
    pytest.param("seq 1 200000 | { x=$(head -n 1); }", "head -n", id="an assignment in a fed group"),
    pytest.param("seq 1 200000 | printf '%s' a \"$(head -n 1)\"", "head -n", id="a later argument"),
    pytest.param("seq 1 200000 | echo $(true; head -n 1)", "head -n", id="a later command of the substitution"),
    pytest.param('seq 1 200000 | echo "$(echo $(head -n 1))"', "head -n", id="a nested substitution"),
    pytest.param("seq 1 200000 | if true; then echo $(head -n 1); fi", "head -n", id="a substitution in a fed if"),
    pytest.param("seq 1 200000 | {\n  true\n  echo $(head -n 1)\n}", "head -n", id="a fed group over lines"),
    pytest.param("seq 1 200000 |\n  echo $(head -n 1)", "head -n", id="a pipe at the end of a line"),
    # A function, a shell's -c script, and eval read the pipe, so each substitution they hold reads it too.
    pytest.param("f() { echo $(head -n 1); }; seq 1 200000 | f", "head -n", id="a substitution in a fed function"),
    pytest.param('function g { x="$(head -n 1)"; }; seq 1 200000 | g', "head -n", id="a quoted one in a function"),
    pytest.param("f() ( echo `head -n 1` ); seq 1 200000 | f", "head -n", id="backticks in a subshell function"),
    pytest.param("f() { echo $(echo $(head -n 1)); }; seq 1 200000 | f", "head -n", id="nested in a function"),
    pytest.param("f() { echo $(head -n 1); }; g() { f; }; seq 1 200000 | g", "head -n", id="a function's callee"),
    pytest.param("seq 1 200000 | bash -c 'echo $(head -n 1)'", "head -n", id="a substitution in bash -c"),
    pytest.param("seq 1 200000 | bash -c 'echo `head -n 1`'", "head -n", id="backticks in bash -c"),
    pytest.param("seq 1 200000 | eval 'echo $(head -n 1)'", "head -n", id="a substitution in eval"),
    pytest.param("seq 1 200000 | bash -c 'f() { echo $(head -n 1); }; f'", "head -n", id="a function in bash -c"),
    pytest.param("eval 'seq 1 200000 | echo $(head -n 1)'", "head -n", id="a fed substitution inside eval"),
]


# Forms whose program GNU's userland has and macOS's lacks: macOS ships no timeout, and its env reads no long option.
GNU_RACES = [
    pytest.param("seq 1 200000 | /usr/bin/timeout 5 head -n 1", "head -n", id="timeout by a path"),
    pytest.param("seq 1 200000 | env --split-string='head -n 1'", "head -n", id="env --split-string"),
]


@pytest.mark.parametrize(("script", "reader"), BASH_RACES + GNU_RACES)
def test_the_pipe_race_policy_finds_the_reader_in_every_bash_form(tmp_path: Path, script: str, reader: str) -> None:
    # The race is named at the reader's line, or at the line that runs the function or shell holding it.
    line = 3 + script[: script.rindex(reader.split()[0])].count("\n")
    assert races_in(tmp_path, script) == [f"{line} pipes into {reader}"]


def assert_fails_under_pipefail(script: str) -> None:
    # The table is held to real Bash: each form ends with the writer's SIGPIPE status, 141, under pipefail.
    result = subprocess.run(
        [find_git_bash(), "-c", f"set -o pipefail\n{script}"], capture_output=True, encoding="utf-8", check=False
    )
    assert result.returncode == 141, (result.returncode, result.stderr)


@pytest.mark.parametrize(("script", "reader"), BASH_RACES)
def test_each_bash_form_the_policy_refuses_fails_under_pipefail_in_bash(script: str, reader: str) -> None:
    del reader
    assert_fails_under_pipefail(script)


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS ships no timeout, and its env reads no long option")
@pytest.mark.parametrize(("script", "reader"), GNU_RACES)
def test_each_gnu_form_the_policy_refuses_fails_under_pipefail_in_bash(script: str, reader: str) -> None:
    del reader
    assert_fails_under_pipefail(script)


# Readers that end early by their own options, as their programs document, which a shell may not show in every run.
@pytest.mark.parametrize(
    ("script", "race"),
    [
        pytest.param("seq 1 200000 | grep -l 1", "3 pipes into grep -l", id="grep -l"),
        pytest.param("seq 1 200000 | grep -L 1", "3 pipes into grep -L", id="grep -L"),
        pytest.param("seq 1 200000 | grep -sl 1", "3 pipes into grep -l", id="grep -l in a cluster"),
        pytest.param("seq 1 200000 | grep --files-with 1", "3 pipes into grep --files-with-matches", id="grep long"),
        pytest.param("seq 1 200000 | grep --files-witho 1", "3 pipes into grep --files-without-match", id="-L long"),
        pytest.param("seq 1 200000 | rg -q 1", "3 pipes into rg -q", id="rg -q"),
        pytest.param("seq 1 200000 | rg -m1 1", "3 pipes into rg -m", id="rg -m"),
        pytest.param("seq 1 200000 | rg -l 1", "3 pipes into rg -l", id="rg -l"),
        pytest.param("seq 1 200000 | rg --quiet 1", "3 pipes into rg --quiet", id="rg --quiet"),
        pytest.param("seq 1 200000 | sudo head -n 1", "3 pipes into head -n", id="sudo"),
        pytest.param("seq 1 200000 | sudo -u someone VAR=1 head -n 1", "3 pipes into head -n", id="sudo's options"),
        pytest.param("seq 1 200000 | doas -u someone head -n 1", "3 pipes into head -n", id="doas"),
        pytest.param("seq 1 200000 | setsid head -n 1", "3 pipes into head -n", id="setsid"),
        pytest.param("seq 1 200000 | sudo --user=a -E head -n 1", "3 pipes into head -n", id="sudo's long options"),
        pytest.param("seq 1 200000 | ionice -c2 -n 7 head -n 1", "3 pipes into head -n", id="ionice"),
        pytest.param("seq 1 200000 | env -vS'head -n 1'", "3 pipes into head -n", id="env -S in a bundle"),
        pytest.param("seq 1 200000 | bash -c -e 'head -n 1'", "3 pipes into head -n", id="bash -c before -e"),
        # A wrapper option the reader cannot place may hide a reader, so the pipe is named by it.
        pytest.param("seq 1 200000 | sudo --us root head -n 1", "3 pipes into sudo --us", id="sudo abbreviated"),
        pytest.param("seq 1 200000 | ionice --cl 2 head -n 1", "3 pipes into ionice --cl", id="ionice abbreviated"),
        pytest.param("seq 1 200000 | env --split='head -n 1'", "3 pipes into env --split=head -n 1", id="env --split"),
        pytest.param("seq 1 200000 | timeout --sig TERM 5 head", "3 pipes into timeout --sig", id="timeout --sig"),
        pytest.param("seq 1 200000 | builtin eval 'head -n 1'", "3 pipes into head -n", id="eval behind builtin"),
        # A wrapper a package manager installs under a prefix, as Homebrew installs GNU's, is the wrapper it builds.
        pytest.param("seq 1 200000 | gtimeout 5 head -n 1", "3 pipes into head -n", id="gtimeout"),
        pytest.param("seq 1 200000 | genv -S 'head -n 1'", "3 pipes into head -n", id="genv -S"),
        pytest.param("seq 1 200000 | ustdbuf -oL head -n 1", "3 pipes into head -n", id="ustdbuf"),
        pytest.param("seq 1 200000 | gtimeout --sig TERM 5 head", "3 pipes into gtimeout --sig", id="gtimeout --sig"),
        # A race inside a script a shell runs is the suite's too.
        pytest.param("bash -c 'seq 1 200000 | head -n 1'", "3 pipes into head -n", id="a pipe in bash -c"),
        pytest.param("eval 'seq 1 200000 | head -n 1'", "3 pipes into head -n", id="a pipe in eval"),
        # Under errexit, as the suite sets it, the trap's failed pipeline ends the shell with 141 when it fires.
        pytest.param("trap 'seq 1 200000 | head -n 1' EXIT", "3 pipes into head -n", id="a pipe in a trap"),
        pytest.param(
            "bash -c 'seq 1 200000 | echo $(head -n 1)'", "3 pipes into head -n", id="a fed substitution in bash -c"
        ),
    ],
)
def test_the_pipe_race_policy_finds_a_reader_by_its_documented_options_and_wrappers(
    tmp_path: Path, script: str, race: str
) -> None:
    assert races_in(tmp_path, script) == [race]


# A trap's action the grammar refuses runs nothing the pipe-race policy can see, as the same text given to eval does:
# the walk hands it on as eval's script, and the reader of a script the grammar refuses reads no pipe in it.
@pytest.mark.parametrize(
    ("word", "text"),
    [
        ("'seq 1 200000 | head -n 1; if true; then'", "seq 1 200000 | head -n 1; if true; then"),
        ("'seq 1 200000 | head -n 1; case x in'", "seq 1 200000 | head -n 1; case x in"),
    ],
)
def test_a_trap_action_the_grammar_refuses_is_read_as_eval_s_script_is(tmp_path: Path, word: str, text: str) -> None:
    assert (
        suite_discovery._shell_script(["trap", text, "EXIT"]) == suite_discovery._shell_script(["eval", text]) == text
    )
    assert races_in(tmp_path, f"trap {word} EXIT") == races_in(tmp_path, f"eval {word}") == []
    assert races_in(tmp_path, f"seq 1 200000 | trap {word} EXIT") == races_in(tmp_path, f"seq 1 200000 | eval {word}")


def test_a_race_in_unquoted_backticks_is_named_once(tmp_path: Path) -> None:
    assert races_in(tmp_path, "x=`printf a | head -1`") == ["3 pipes into head -1"]
    assert races_in(tmp_path, "x=`printf a | head -1`; printf a | grep -q a") == [
        "3 pipes into grep -q",
        "3 pipes into head -1",
    ]


@pytest.mark.parametrize(
    "setting",
    [
        pytest.param("set -o pipefail", id="set -o"),
        pytest.param("set -e -o pipefail", id="set -e -o"),
        pytest.param("set -euo pipefail", id="set in a cluster"),
        pytest.param("shopt -so pipefail", id="shopt -so"),
        pytest.param("shopt -s -o pipefail", id="shopt -s -o"),
        pytest.param("builtin set -o pipefail", id="builtin set"),
        pytest.param("command set -o pipefail", id="command set"),
        pytest.param("eval 'set -o pipefail'", id="eval"),
        pytest.param("if true; then set -o pipefail; fi", id="in a compound"),
        # The & of a redirection's >& or <& stays in its command, so the option after it is still set's.
        pytest.param("set 2>&1 -o pipefail", id="after a >& redirection"),
        pytest.param("set 0<&0 -o pipefail", id="after a <& redirection"),
        pytest.param("x=$(set -o pipefail)", id="in a substitution"),
        pytest.param("bash -o pipefail -c 'printf a | head -n 1'", id="a shell started with it"),
        # A shell named by a path or with .exe is the shell its file name names.
        pytest.param("/usr/bin/bash -o pipefail -c 'printf a | head -n 1'", id="a shell named by a path"),
        pytest.param("bash.exe -o pipefail -c 'printf a | head -n 1'", id="a shell named with .exe"),
        pytest.param("env SHELLOPTS=pipefail bash -c 'printf a | head -n 1'", id="SHELLOPTS"),
        pytest.param("SHELLOPTS=braceexpand:pipefail bash -c 'printf a | head -n 1'", id="SHELLOPTS listing it"),
    ],
)
def test_each_way_bash_turns_pipefail_on_has_its_suite_examined(tmp_path: Path, setting: str) -> None:
    write_fixture_tree(tmp_path, {"tests/test_race.sh": f"#!/usr/bin/env bash\n{setting}\nprintf a | head -n 1\n"})
    assert [problem.partition(" ")[0] for problem in pipe_race_problems(tmp_path)][-1:] == ["tests/test_race.sh:3"]


def test_a_suite_that_never_turns_pipefail_on_is_not_examined(tmp_path: Path) -> None:
    script = "#!/usr/bin/env bash\nset -eu\n# pipefail is off\nprintf a | head -n 1\necho set -o pipefail\n"
    write_fixture_tree(tmp_path, {"tests/test_race.sh": script})
    assert pipe_race_problems(tmp_path) == []


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("case x in head|grep) printf a;; esac", id="a case's patterns"),
        pytest.param("printf a | case x in (head) cat;; esac", id="a pattern named head"),
        pytest.param("printf a | while read -r x; do :; done; head -n 1 file", id="after a loop"),
        pytest.param("printf a | if true; then cat; fi; head -n 1 file", id="after an if"),
        pytest.param("for head in a; do :; done", id="a loop's name"),
        pytest.param("printf a | for x in head; do cat; done", id="a loop's words"),
        pytest.param("f() { head -n 1 file; }; printf a | cat; f", id="a function the pipe does not run"),
        pytest.param("printf a | bash script.sh", id="a script by its file"),
        pytest.param("printf a | bash -c 'cat'", id="a shell reading to the end"),
        pytest.param("printf a | env -S 'cat -u'", id="env -S reading to the end"),
        pytest.param("f() { f; }; printf a | f", id="a function calling itself"),
        pytest.param("f() { bash -c 'g() { f; }; g'; }; printf a | f", id="a function a shell's script calls back"),
        # A stated limit: the reader follows a command NESTING_LIMIT functions deep, so a chain far longer than any
        # suite writes ends there rather than at Python's recursion limit.
        pytest.param(
            " ".join(f"f{index}() {{ f{index + 1}; }};" for index in range(1000))
            + " f1000() { head -n 1; }; printf a | f0",
            id="a reader past the nesting limit",
        ),
        # A substitution reads the standard input of its own command, which no pipe feeds here.
        pytest.param("printf a | cat; echo $(head -n 1 file)", id="a substitution after the pipeline"),
        # A >( ) substitution reads what its command writes into it, never the pipe.
        pytest.param("printf a | tee >(head -n 1 >/dev/null)", id="a >( ) substitution in a fed command"),
        pytest.param("echo $(head -n 1 file) | cat", id="a substitution in the writer"),
        pytest.param("printf a | cat && x=$(head -n 1 file)", id="a substitution after &&"),
        pytest.param('printf a | { cat; }; echo "`head -n 1 file`"', id="quoted backticks after a fed group"),
        pytest.param("printf a | cat\necho $(head -n 1 file)", id="a substitution on the next line"),
        pytest.param("printf a | cat |\n  cat; echo $(head -n 1 file)", id="a substitution after a continued pipe"),
        pytest.param("f() { echo $(head -n 1 file); }; printf a | cat; f", id="a function no pipe runs"),
        pytest.param("f() { :; }; g() { echo $(head -n 1 file); }; printf a | f", id="another function's substitution"),
        pytest.param("bash -c 'echo $(head -n 1 file)'; printf a | cat", id="a shell no pipe feeds"),
        pytest.param("printf a | eval 'cat; echo' ; eval 'echo $(head -n 1 file)'", id="an eval no pipe feeds"),
    ],
)
def test_the_pipe_race_policy_passes_each_bash_form_that_reads_to_the_end(tmp_path: Path, script: str) -> None:
    assert races_in(tmp_path, script) == []


# A substitution the grammar cannot read leaves the suite unread, since Bash runs the lines before a syntax error: Bash
# refuses the first and the last as a file, and the backquoted one when it runs it.
@pytest.mark.parametrize(
    "script",
    [
        pytest.param('seq 1 200000 | echo "$(head -n \'x)"', id="an unreadable substitution"),
        pytest.param('seq 1 200000 | echo "`head -n \'x`"', id="unreadable backticks"),
        pytest.param('f() { echo "$(head -n \'x)"; }; seq 1 200000 | f', id="an unreadable one in a function"),
    ],
)
def test_the_pipe_race_policy_names_a_suite_whose_substitution_bash_cannot_read(tmp_path: Path, script: str) -> None:
    assert races_in(tmp_path, script) == [
        "tests/test_race.sh cannot be read as Bash to find a pipe into a reader that ends early: a single quote is not "
        "closed"
    ]


@pytest.mark.parametrize(
    "script",
    [
        pytest.param("x=${ printf a | grep -q a; }", id="a command substitution run in the shell itself"),
        pytest.param("x=${| printf a | head -n 1; }", id="one that sets REPLY"),
        pytest.param("cat <<EOF\n${ printf a | head -n 1; }\nEOF", id="one in a here-document's body"),
        pytest.param("f() { x=${ printf a | grep -q a; }; }", id="one in a function"),
    ],
)
def test_the_pipe_race_policy_names_a_suite_holding_a_form_the_grammar_refuses(tmp_path: Path, script: str) -> None:
    # Bash 5.3 runs the pipe in the form, which bash_syntax refuses, so the suite is unread rather than passed.
    [problem] = races_in(tmp_path, script)
    assert problem.startswith(
        "tests/test_race.sh cannot be read as Bash to find a pipe into a reader that ends early: Bash 5.3 runs the "
        "command after `${"
    )


def test_a_here_document_s_substitution_bash_cannot_read_runs_nothing_and_the_rest_of_the_body_is_read(
    tmp_path: Path,
) -> None:
    # The shell expands an unquoted body's substitutions from the fed command's standard input, so a readable one is
    # a race. One it cannot read runs nothing: Bash 5.3 reports an unexpected end and Bash 3.2 a bad substitution, and
    # neither starts its head, so it is no reader of the pipe, as bash_syntax reads such a body.
    assert races_in(tmp_path, "seq 1 200000 | cat <<EOF\n$(head -n x\nEOF") == []
    assert races_in(tmp_path, "seq 1 200000 | cat <<EOF\n$(head -n 1) $(head -n x\nEOF") == ["4 pipes into head -n"]


def test_a_suite_nested_deeper_than_the_reader_follows_is_named_as_unread(tmp_path: Path) -> None:
    unread = (
        "tests/test_race.sh cannot be read as Bash to find a pipe into a reader that ends early: it nests deeper than "
        "the reader follows"
    )
    # The grammar refuses nesting past Python's recursion limit, and the walk over its tree does too.
    assert races_in(tmp_path, "x=" + "$(" * 2000 + ")" * 2000) == [unread]
    with mock.patch.object(suite_discovery._BashReading, "read_list", side_effect=RecursionError):
        assert races_in(tmp_path, "printf a | head -n 1") == [unread]


def test_the_pipe_race_policy_reads_each_function_of_ones_that_call_each_other_once(tmp_path: Path) -> None:
    # A walk that kept the functions it was in for each path read them once per order of calls: nine functions that
    # each call the rest took 876,809 reads. Read once per question, and again only from a shallower depth, each
    # function's calls are read at most once for each depth, so the reads stay below the cube of the functions.
    count = 9
    script = " ".join(
        f"f{index}() {{ {'; '.join(f'f{other}' for other in range(count) if other != index)}; }};"
        for index in range(count)
    )
    reads = 0
    program = suite_discovery._early_exit_program

    def counted(words: list[str]) -> str:
        nonlocal reads
        reads += 1
        if reads > count**3:
            raise AssertionError(f"the pipe-race policy read more than {count**3} commands")
        return program(words)

    with mock.patch.object(suite_discovery, "_early_exit_program", counted):
        assert races_in(tmp_path, f"{script} printf a | f0") == []


def test_the_pipe_race_policy_reads_a_function_again_from_a_shallower_call(tmp_path: Path) -> None:
    # g is first read at the end of a chain as deep as the nesting limit, which leaves its call of h unread; a then
    # calls g directly, and that shallower read finds the head in h, as a walk per path did.
    chain = " ".join(f"c{index}() {{ c{index + 1}; }};" for index in range(1, 15))
    script = f"{chain} c15() {{ g; }}; h() {{ head -n 1; }}; g() {{ h; }}; a() {{ c1; g; }}; seq 1 200000 | a"
    assert races_in(tmp_path, script) == ["3 pipes into head -n"]


# Each table of functions and each script is its own. The reader keeps every function a shell's script defines in
# the table its commands are read under, so a call of `f` before the script redefines it reads the script's `f`: here
# its grep -q, found before the head after it. A table lost, or a read kept across tables, reads the outer `f`, which
# reads to the end, and finds the head instead.
@pytest.mark.parametrize(
    ("script", "race"),
    [
        pytest.param(
            "f() { cat; }; printf a | bash -c 'f; head -n 1; f() { grep -q x; }'",
            "3 pipes into grep -q",
            id="a function a shell's script defines",
        ),
        pytest.param(
            "f() { cat; }; h() { f; bash -c 'f; head -n 1; f() { grep -q x; }'; }; printf a | h",
            "3 pipes into grep -q",
            id="a name read in two tables",
        ),
        pytest.param(
            "f() { cat; }; h() { bash -c f; bash -c 'bash -c f; head -n 1; f() { grep -q x; }'; }; printf a | h",
            "3 pipes into grep -q",
            id="one script under two tables",
        ),
        # A shell's script read at the depth limit is read, not taken as read before.
        pytest.param(
            " ".join(f"f{index}() {{ f{index + 1}; }};" for index in range(1, 15))
            + " f15() { bash -c 'head -n 1'; }; printf a | f1",
            "3 pipes into head -n",
            id="a shell's script at the nesting limit",
        ),
    ],
)
def test_the_pipe_race_policy_reads_each_table_of_functions_and_each_script_apart(
    tmp_path: Path, script: str, race: str
) -> None:
    assert races_in(tmp_path, script) == [race]


def test_the_pipe_race_policy_reads_a_script_once_at_a_depth_and_again_only_shallower(tmp_path: Path) -> None:
    # g's two runs of the same script are at one depth, so the second is not read; f's run of it is shallower than
    # the one g reached first, so it is read again. An unreadable script runs nothing the reader can see.
    reads: list[str] = []
    program = suite_discovery._early_exit_program

    def counted(words: list[str]) -> str:
        reads.append(" ".join(words))
        return program(words)

    script = 'g() { bash -c cat; bash -c cat; }; f() { g; bash -c cat; }; printf a | { f; bash -c "echo \'x"; }'
    with mock.patch.object(suite_discovery, "_early_exit_program", counted):
        assert races_in(tmp_path, script) == []
    assert reads == ["f", "g", "bash -c cat", "cat", "bash -c cat", "bash -c cat", "cat", "bash -c echo 'x"]


class PytestBaseFolderTests(unittest.TestCase):
    """Every pytest the runner starts keeps its temporary folders in a base folder of its own, never in pytest's
    default base, `pytest-of-<user>` in the temporary directory, which every other pytest process shares with its
    `pytest-current` link: a process that ends removes the links there whose target is gone, so one that removes the
    link between another's listing and its removal ends that other's session in a FileNotFoundError after its tests
    all passed."""

    def started(self, job: Callable[[], None]) -> tuple[list[str], Path]:
        """The command `job` gives run_process and the base folder it names, checked while the job runs: a folder in a
        folder of its own, empty, that the job made under the temporary directory and removes once it is done."""
        commands: list[list[str]] = []

        def run(arguments: list[str], environment: dict[str, str] | None = None, cwd: Path = REPOSITORY_ROOT) -> None:
            commands.append(list(arguments))
            self.assertEqual(1, arguments.count("--basetemp"), arguments)
            base = Path(arguments[arguments.index("--basetemp") + 1])
            self.assertTrue(base.parent.is_dir(), base)
            self.assertEqual([], list(base.parent.iterdir()))
            self.assertTrue(base.parent.parent.samefile(tempfile.gettempdir()), base)
            self.assertEqual(suite_discovery.PYTEST_ENVIRONMENT, environment)

        with mock.patch.object(suite_discovery, "run_process", side_effect=run):
            job()
        [command] = commands
        base = Path(command[command.index("--basetemp") + 1])
        self.assertFalse(base.parent.exists(), base)
        return command, base

    def test_a_whole_suite_and_each_shard_start_pytest_with_a_base_folder_of_their_own(self) -> None:
        suite = REPOSITORY_ROOT / "tests" / "test_a.py"
        whole, whole_base = self.started(functools.partial(suite_discovery.run_test_script, suite))
        first, first_base = self.started(functools.partial(suite_discovery.run_shard, suite, 0, 2, {"A::test_a": 9}))
        second, second_base = self.started(functools.partial(suite_discovery.run_shard, suite, 1, 2, {}))
        start = [sys.executable, "-B", "-m", "pytest", "tests/test_a.py", "--basetemp"]
        self.assertEqual([*start, str(whole_base)], whole)
        shard = ["--suite-shard", "1/2", "--test-seconds", "A::test_a=9"]
        self.assertEqual([*start, str(first_base), *shard], first)
        self.assertEqual([*start, str(second_base), "--suite-shard", "2/2"], second)
        self.assertEqual(3, len({whole_base.parent, first_base.parent, second_base.parent}))

    def test_two_tmp_path_suites_run_at_once_each_keep_their_folders_in_their_own_base(self) -> None:
        # pytest makes its default base, pytest-of-<user>, in the folder PYTEST_DEBUG_TEMPROOT names, in place of the
        # temporary directory, so a run that used the shared base would leave it in `shared`.
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as shared:
            suites = []
            for name in ("first", "second"):
                (Path(temporary) / name).mkdir()
                suites.append(Path(temporary) / name / "test_temporary.py")
                suites[-1].write_bytes(TMP_PATH_SUITE)

            def run(suite: Path) -> subprocess.CompletedProcess[str]:
                return run_pytest(suite, "-s", environment={"PYTEST_DEBUG_TEMPROOT": shared})

            with ThreadPoolExecutor(max_workers=len(suites)) as pool:
                runs = list(pool.map(run, suites))
            for ran in runs:
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
            self.assertEqual([], sorted(entry.name for entry in Path(shared).iterdir()))
            folders = [
                line.split("TMP_PATH ", 1)[1] for ran in runs for line in ran.stdout.splitlines() if "TMP_PATH " in line
            ]
            self.assertEqual(2, len(set(folders)), folders)
            for folder in folders:
                self.assertIn("pytest-base-", folder)


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

    def test_a_file_in_place_of_tests_and_a_folder_named_like_a_suite_are_passed_over(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {name: text for name, text in self.FILES.items() if not name.startswith("tests/")}
            # tests is a file, where a suite root folder is expected, and test_folder.py is a folder, not a suite.
            write_fixture_tree(root, {**files, "tests": "", "skills/alpha/scripts/test_folder.py/notes.txt": ""})
            self.assertEqual(
                [
                    ".claude/skills/local/scripts/local.test.ps1",
                    "skills/alpha/scripts/test_run.py",
                    "skills/beta/scripts/test-tool.sh",
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
        missing = "is imported by no Python regression suite; add a test that imports it"
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

    def test_every_suite_name_counts_and_a_file_that_is_no_running_suite_does_not(self) -> None:
        missing = "is imported by no Python regression suite; add a test that imports it"
        modules = ("by_suffix", "by_dash", "by_dotted", "by_upper", "in_fixture", "by_helper", "by_shell")
        self.assertEqual(
            [f"deployer/{name}.py {missing}" for name in ("by_helper", "by_shell", "in_fixture")],
            self.problems(
                {
                    **{f"deployer/{name}.py": "" for name in modules},
                    # Each name the shared rule gives a suite runs, so its imports count.
                    "tests/deployer/widget_test.py": "from deployer import by_suffix\n",
                    "tests/deployer/widget-test.py": "from deployer import by_dash\n",
                    "tests/deployer/widget.test.py": "from deployer import by_dotted\n",
                    "tests/deployer/TEST_widget.py": "from deployer import by_upper\n",
                    # A suite under tests/fixtures/ never runs, a helper is no suite, and a shell suite imports nothing.
                    "tests/fixtures/repo/test_fixture.py": "from deployer import in_fixture\n",
                    "tests/deployer/support.py": "from deployer import by_helper\n",
                    "tests/deployer/test_shell.sh": "python -c 'from deployer import by_shell'\n",
                }
            ),
        )

    def test_a_module_counts_as_tested_only_by_an_import_that_loads_its_own_file(self) -> None:
        missing = "is imported by no Python regression suite; add a test that imports it"
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
        pinned = '[tool.pytest.ini_options]\naddopts = ["-p", "no:cacheprovider", "--capture=no"]\nstrict = true\n'
        self.assertEqual({"addopts": ["-p", "no:cacheprovider", "--capture=no"], "strict": True}, PYTEST_CONFIGURATION)
        self.assertEqual([], self.problems({"pyproject.toml": pinned}))
        deselects = pinned.replace('"--capture=no"]', '"--capture=no", "--deselect", "tests/test_a.py"]')
        self.assertEqual(
            [
                "pyproject.toml: [tool.pytest] must be ini_options = "
                f"{PYTEST_CONFIGURATION!r}, not {{'ini_options': {{'addopts': "
                "['-p', 'no:cacheprovider', '--capture=no', '--deselect', 'tests/test_a.py'], 'strict': True}}"
            ],
            self.problems({"pyproject.toml": deselects}),
        )
        # Strict mode off, or only the options pytest 8 had, which still allow a duplicate parametrization id.
        for loosened in (
            pinned.replace("strict = true", "strict = false"),
            pinned.replace("strict = true", "strict_markers = true\nstrict_config = true"),
        ):
            with self.subTest(configuration=loosened):
                self.assertEqual(1, len(self.problems({"pyproject.toml": loosened})))
        self.assertEqual(
            [
                "pytest.ini is a pytest configuration file beside pyproject.toml; keep the configuration in "
                "[tool.pytest.ini_options] alone",
                "setup.cfg is a pytest configuration file beside pyproject.toml; keep the configuration in "
                "[tool.pytest.ini_options] alone",
                f"pyproject.toml: [tool.pytest] must be ini_options = {PYTEST_CONFIGURATION!r}, not {{}}",
            ],
            self.problems({"pyproject.toml": "[tool.ruff]\n", "pytest.ini": "[pytest]\n", "setup.cfg": ""}),
        )

    def test_each_configuration_file_at_the_root_beside_pyproject_toml_fails(self) -> None:
        # pytest.toml, from pytest 9, takes precedence over pyproject.toml even when empty.
        pinned = '[tool.pytest.ini_options]\naddopts = ["-p", "no:cacheprovider", "--capture=no"]\nstrict = true\n'
        for name in ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "tox.ini", "setup.cfg"):
            with self.subTest(name=name):
                self.assertEqual(
                    [
                        f"{name} is a pytest configuration file beside pyproject.toml; keep the configuration in "
                        "[tool.pytest.ini_options] alone"
                    ],
                    self.problems({"pyproject.toml": pinned, name: ""}),
                )

    def test_a_configuration_file_in_a_suite_s_folder_or_between_it_and_the_root_fails(self) -> None:
        pinned = '[tool.pytest.ini_options]\naddopts = ["-p", "no:cacheprovider", "--capture=no"]\nstrict = true\n'
        suites = {
            "pyproject.toml": pinned,
            "tests/deployer/test_a.py": "def test_a():\n    pass\n",
            "skills/a/SKILL.md": "",
            "skills/a/scripts/test_b.py": "def test_b():\n    pass\n",
        }
        self.assertEqual([], self.problems(suites))
        for path, suite in (
            ("tests/deployer/pytest.ini", "tests/deployer/test_a.py"),
            ("tests/pytest.toml", "tests/deployer/test_a.py"),
            ("tests/deployer/.pytest.toml", "tests/deployer/test_a.py"),
            ("skills/a/scripts/pyproject.toml", "skills/a/scripts/test_b.py"),
            ("skills/a/tox.ini", "skills/a/scripts/test_b.py"),
            ("skills/setup.cfg", "skills/a/scripts/test_b.py"),
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    [
                        f"{path} is a pytest configuration file pytest may read for {suite} instead of the root's "
                        "pyproject.toml; keep the configuration there"
                    ],
                    self.problems({**suites, path: ""}),
                )
        # A folder no suite sits in or below is never searched.
        self.assertEqual([], self.problems({**suites, "docs/pytest.ini": ""}))

    def test_the_walk_up_from_a_suite_stops_below_the_root(self) -> None:
        pinned = '[tool.pytest.ini_options]\naddopts = ["-p", "no:cacheprovider", "--capture=no"]\nstrict = true\n'
        beside = (
            "pytest.ini is a pytest configuration file beside pyproject.toml; keep the configuration in "
            "[tool.pytest.ini_options] alone"
        )
        files = {"pyproject.toml": pinned, "pytest.ini": "", "tests/test_a.py": "def test_a():\n    pass\n"}
        # The root's own file is reported once, as the root's, never as a folder above the suite's.
        self.assertEqual([beside], self.problems(files))
        self.assertEqual(
            [
                beside,
                "tests/pytest.ini is a pytest configuration file pytest may read for tests/test_a.py instead of the "
                "root's pyproject.toml; keep the configuration there",
            ],
            self.problems({**files, "tests/pytest.ini": ""}),
        )
        # A suite in the root itself, or outside it, has no folder of its own to search.
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(temporary)
            write_fixture_tree(root, {"pyproject.toml": pinned, "pytest.ini": ""})
            write_fixture_tree(Path(elsewhere), {"pytest.ini": "", "test_b.py": ""})
            suites = [root / "test_a.py", Path(elsewhere) / "test_b.py"]
            with mock.patch.object(suite_discovery, "regression_suites", return_value=suites):
                self.assertEqual([beside], pytest_configuration_problems(root))


class PytestRootPolicy(unittest.TestCase):
    """A pytest a suite starts from the repository on a suite outside it pins its root, in each form the repository's
    suites use, or is named."""

    # What each fixture suite starts with: the modules it uses and the repository's root, as tests/test_conftest.py
    # spells it.
    HEADER = (
        "import subprocess\nimport sys\nimport tempfile\nfrom pathlib import Path\n\n"
        "from suite_discovery import pytest_command\nfrom tools import runner\n\n"
        "REPOSITORY_ROOT = Path(__file__).resolve().parents[1]\n\n\n"
    )
    # A repository module that starts pytest on a suite its caller gives it, from the repository, as
    # tools/python_diagnostics.py does: one suite, each suite of a list, and one under a folder of its own.
    RUNNER = textwrap.dedent(
        """\
        import subprocess
        import tempfile
        from pathlib import Path

        from suite_discovery import pytest_command
        from validation_support import REPOSITORY_ROOT


        def run_suite(suite: str) -> int:
            return subprocess.run(pytest_command(suite), cwd=REPOSITORY_ROOT, check=False).returncode


        def run(suites: list[str]) -> list[int]:
            codes = []
            for suite in suites:
                codes.append(run_suite(suite))
            return codes


        def run_temporary() -> int:
            with tempfile.TemporaryDirectory() as temporary:
                return subprocess.run(pytest_command(str(Path(temporary) / "test_x.py")), check=False).returncode
        """
    )
    PINNING = (
        "write a pytest.ini beside the suite before starting it, or pass --confcutdir or --rootdir naming its folder, "
        "so pytest collects no folder above it"
    )

    def problems(self, body: str) -> list[str]:
        return self.reading({"tests/test_a.py": self.HEADER + textwrap.dedent(body)})[0]

    def reading(self, files: Mapping[str, str]) -> tuple[list[str], list[str]]:
        """pytest_root_reading over a fixture tree holding the runner module and files."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tools/runner.py": self.RUNNER, **files})
            return suite_discovery.pytest_root_reading(root)

    def line(self, body: str) -> int:
        """The line of the fixture suite that `# start` marks."""
        lines = (self.HEADER + textwrap.dedent(body)).splitlines()
        return next(number for number, text in enumerate(lines, 1) if text.endswith("# start"))

    def unpinned(self, body: str) -> str:
        return (
            f"tests/test_a.py:{self.line(body)} starts pytest from the repository on a suite under the temporary "
            f"folder and pins no root; {self.PINNING}"
        )

    def configured_unpinned(self, body: str) -> str:
        return (
            f"tests/test_a.py:{self.line(body)} starts pytest from the repository on a suite under the temporary "
            "folder, naming its configuration file, and pins no root; pass --confcutdir naming the suite's folder, "
            "since a run that names its configuration file reads no pytest.ini beside the suite, so pytest collects no "
            "folder above it"
        )

    def unplaced(self, body: str, why: str) -> str:
        return (
            f"tests/test_a.py:{self.line(body)} starts pytest from the repository on a suite this policy cannot place "
            f"in the repository or see pinned ({why}); build the suite's path from a tempfile folder in the function "
            "that starts it, and write a pytest.ini beside it or pass --confcutdir or --rootdir naming its folder"
        )

    def test_a_start_on_a_suite_under_a_temporary_folder_with_no_root_pinned_fails(self) -> None:
        cases = {
            "pytest_command": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        suite.write_text("def test_a():\\n    pass\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(suite)), cwd=REPOSITORY_ROOT, check=True)  # start
                """,
            "an argument list holding -m pytest": """\
                def test_runs() -> None:
                    folder = tempfile.mkdtemp()
                    suite = Path(folder) / "test_child.py"
                    subprocess.run([sys.executable, "-B", "-m", "pytest", "-q", str(suite)], check=True)  # start
                """,
            "a pytest.ini written only after the start": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        subprocess.run(pytest_command(str(suite)), check=True)  # start
                        (suite.parent / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                """,
            "a pytest.ini and a --confcutdir in another folder": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
                        suite = Path(temporary) / "test_child.py"
                        (Path(other) / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(suite), "--confcutdir", other), check=True)  # start
                """,
            "a --rootdir above the suite's folder": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        command = [*pytest_command(str(suite)), "--rootdir", str(Path(temporary).parent)]  # start
                        subprocess.run(command, check=True)
                """,
            # tests/tools/test_python_diagnostics.py before it pinned its fixture folders: it gave
            # python_diagnostics.run_suite a suite under a temporary folder with nothing beside it.
            "a repository function that starts pytest on the suite it is given": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_sleeping.py"
                        runner.run_suite(str(suite))  # start
                """,
            "a repository function given the suite as a keyword argument": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        runner.run_suite(suite=str(Path(temporary) / "test_child.py"))  # start
                """,
            "a name that is no literal joined to a temporary folder": """\
                NAMES = ("test_child.py",)


                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        for name in NAMES:
                            subprocess.run(pytest_command(str(Path(temporary) / name)), check=True)  # start
                """,
            "a repository function that starts pytest on each suite of a list": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        runner.run([str(Path(temporary) / "test_child.py")])  # start
                """,
            "the suite's own function that starts pytest on the suite it is given": """\
                def run_child(suite: Path) -> int:
                    return subprocess.run(pytest_command(str(suite)), check=False).returncode


                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        run_child(Path(temporary) / "test_child.py")  # start
                """,
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual([self.unpinned(body)], self.problems(body))

    def test_a_run_that_names_its_configuration_file_is_pinned_by_confcutdir_alone(self) -> None:
        cases = {
            "a pytest.ini beside the suite of a run given --config-file and its path apart": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        (Path(temporary) / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                        named = str(REPOSITORY_ROOT / "pyproject.toml")
                        subprocess.run(pytest_command(str(suite), "--config-file", named), check=True)  # start
                """,
            "a pytest.ini beside the suite of a run given --config-file=<path> as one literal": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        (Path(temporary) / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(suite), "--config-file=pyproject.toml"), check=True)  # start
                """,
            "a pytest.ini beside the suite of a run that names another configuration file": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        (Path(temporary) / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                        configuration = str(REPOSITORY_ROOT / "pyproject.toml")
                        subprocess.run(pytest_command(str(suite), "-c", configuration), check=True)  # start
                """,
            "a --rootdir naming the suite's folder in a run that names another configuration file": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        named = REPOSITORY_ROOT / "pyproject.toml"
                        command = [*pytest_command(str(suite), "--rootdir", temporary), f"-c{named}"]  # start
                        subprocess.run(command, check=True)
                """,
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual([self.configured_unpinned(body)], self.problems(body))

    def test_a_dot_step_neither_climbs_nor_descends(self) -> None:
        # Near the root, where a "." read as a step down would keep `..` inside it, and one read as a step up would
        # climb out of it.
        climbing = """\
            def test_runs() -> None:
                subprocess.run(pytest_command(str(REPOSITORY_ROOT / "." / "." / ".." / "x.py")))  # start
            """
        why = (
            "str(REPOSITORY_ROOT / '.' / '.' / '..' / 'x.py') is neither in the repository nor under a temporary folder"
        )
        self.assertEqual([self.unplaced(climbing, why)], self.problems(climbing))
        to_the_root = """\
            def test_runs() -> None:
                subprocess.run(pytest_command(str(REPOSITORY_ROOT / "tests" / "." / ".." / "x.py")), check=True)
            """
        self.assertEqual([], self.problems(to_the_root))

    def test_a_step_up_or_across_in_a_literal_is_read_against_the_root(self) -> None:
        climbing = """\
            def test_runs() -> None:
                subprocess.run(pytest_command(str(REPOSITORY_ROOT / ".." / "elsewhere" / "test_x.py")))  # start
            """
        why = (
            "str(REPOSITORY_ROOT / '..' / 'elsewhere' / 'test_x.py') is neither in the repository nor under a "
            "temporary folder"
        )
        self.assertEqual([self.unplaced(climbing, why)], self.problems(climbing))
        staying = """\
            def test_runs() -> None:
                subprocess.run(pytest_command(str(REPOSITORY_ROOT / "tests" / "." / "tests" / "x.py")), check=True)
                subprocess.run(pytest_command("tests/../tests/x.py"), check=True)  # start
            """
        why = "'tests/../tests/x.py' is neither in the repository nor under a temporary folder"
        self.assertEqual([self.unplaced(staying, why)], self.problems(staying))

    def test_a_module_found_under_a_root_given_unresolved_is_read_against_it(self) -> None:
        # RepositoryModules resolves the modules it finds, so a root spelled through `..`, as one under macOS's /var
        # or a Windows 8.3 short name is spelled unresolved, must be compared resolved too, or a followed module's
        # cwd from its own resolved __file__ reads as outside the repository and its start is not held.
        runner = textwrap.dedent(
            """\
            import subprocess
            from pathlib import Path

            from suite_discovery import pytest_command

            ROOT = Path(__file__).resolve().parents[1]


            def run_suite(suite: str) -> int:
                return subprocess.run(pytest_command(suite), cwd=ROOT, check=False).returncode
            """
        )
        body = """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    runner.run_suite(str(Path(temporary) / "test_x.py"))  # start
            """
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "x").mkdir()
            root = Path(temporary) / "x" / ".."
            write_fixture_tree(
                root, {"tools/runner.py": runner, "tests/test_a.py": self.HEADER + textwrap.dedent(body)}
            )
            self.assertEqual(
                ([self.unpinned(body)], ["tests/test_a.py:" + str(self.line(body))]),
                suite_discovery.pytest_root_reading(root),
            )

    def test_each_form_the_repository_pins_a_root_in_passes(self) -> None:
        cases = {
            # tests/test_conftest.py's run_child.
            "a pytest.ini beside the suite a helper is given": """\
                def run_child(suite: Path, plugin: bool = True) -> int:
                    (suite.parent / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                    plugin_options = ["-p", "conftest"] if plugin else []
                    command = [sys.executable, "-B", "-m", "pytest", "-q", *plugin_options, str(suite)]
                    return subprocess.run(command, cwd=REPOSITORY_ROOT, check=False).returncode


                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        assert run_child(Path(temporary) / "test_child.py") == 0
                """,
            # tests/validation/test_suite_discovery.py's run_pytest.
            "a pytest.ini beside the suite pytest_command is given": """\
                def run_pytest(suite: Path, *options: str) -> subprocess.CompletedProcess[str]:
                    (suite.parent / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                    return subprocess.run(
                        [*pytest_command(str(suite), *options), "-p", "conftest"], cwd=REPOSITORY_ROOT, check=False
                    )
                """,
            # tests/validation/test_job_pool.py's run of a suite under the repository's configuration.
            "a --confcutdir naming the suite's folder": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_left_attached.py"
                        command = pytest_command(
                            str(suite), "-c", str(REPOSITORY_ROOT / "pyproject.toml"), "--confcutdir", temporary
                        )
                        subprocess.run(command, check=True)
                """,
            "a pytest.ini written by the name suite_discovery gives it, or one bound to the literal": """\
                from suite_discovery import PYTEST_INI

                CONFIGURATION = "pytest.ini"


                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
                        suite = Path(temporary) / "test_child.py"
                        (Path(temporary) / PYTEST_INI).write_text("[pytest]\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(suite)), check=True)
                        elsewhere = Path(other) / "test_child.py"
                        (Path(other) / CONFIGURATION).write_text("[pytest]\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(elsewhere)), check=True)
                """,
            # tests/tools/test_python_diagnostics.py's suite_folder.
            "a folder a helper makes holding a pytest.ini": """\
                def suite_folder() -> Path:
                    folder = Path(tempfile.mkdtemp(prefix="fixture-"))
                    (folder / "pytest.ini").write_bytes(b"[pytest]\\n")
                    return folder


                def test_runs() -> None:
                    folder = suite_folder()
                    suite = folder / "test_clean.py"
                    subprocess.run([*pytest_command(str(suite)), "--collect-only"], cwd=REPOSITORY_ROOT, check=True)
                    runner.run_suite(str(suite))
                """,
            "a --rootdir naming the suite's folder, as two arguments or one": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = Path(temporary) / "test_child.py"
                        subprocess.run(pytest_command(str(suite), "--rootdir", str(suite.parent)), check=True)
                        subprocess.run([*pytest_command(str(suite)), f"--rootdir={temporary}"], check=True)
                """,
            "a suite in the repository": """\
                from validation_support import relative


                def test_runs() -> None:
                    subprocess.run(pytest_command("tests/test_b.py"), check=True)
                    subprocess.run(pytest_command(str(REPOSITORY_ROOT / "tests" / "test_b.py")), check=True)
                    subprocess.run(pytest_command(relative(REPOSITORY_ROOT / "tests" / "test_b.py")), check=True)
                    runner.run([str(REPOSITORY_ROOT / "tests" / "test_b.py")])
                """,
            # tests/tools/test_mutation_gate.py's reach runner, and an expected command a test compares.
            "a pytest started in another folder, or a command an assertion compares": """\
                def test_runs() -> None:
                    def reach(tree: Path, suite: str) -> None:
                        subprocess.run([sys.executable, "-B", "-m", "pytest", suite, "-q"], cwd=tree, check=True)

                    command = ["python", "-m", "pytest", "x"]
                    target = command[-1]
                    assert [sys.executable, "-B", "-m", "pytest", target] == command
                    reach(Path("tree"), "tests/x.py")


                class Case(unittest.TestCase):
                    def test_compares(self) -> None:
                        target = ["x"][0]
                        self.assertEqual(pytest_command(target), [sys.executable, "-B", "-m", "pytest", target])
                """,
        }
        for name, body in cases.items():
            with self.subTest(name):
                self.assertEqual([], self.problems(body))

    def test_a_start_whose_suite_it_cannot_place_or_see_pinned_is_named(self) -> None:
        cases = {
            "str(suite) is neither in the repository nor under a temporary folder": """\
                SUITES = {"a": Path("x")}


                def test_runs() -> None:
                    suite = SUITES["a"]
                    subprocess.run(pytest_command(str(suite)), check=True)  # start
                """,
            "str(suite) comes from a parameter of a method or a nested function": """\
                class Runner:
                    def run(self, suite: Path) -> None:
                        subprocess.run(pytest_command(str(suite)), check=True)  # start
                """,
            "str(suite) names a path bound more than once": """\
                def test_runs(flag: bool) -> None:
                    with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
                        if flag:
                            suite = Path(temporary) / "test_child.py"
                        else:
                            suite = Path(other) / "test_child.py"
                        (suite.parent / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                        subprocess.run(pytest_command(str(suite)), check=True)  # start
                """,
            "pytest_command is given no one suite": """\
                def test_runs(arguments: list[str]) -> None:
                    subprocess.run(pytest_command(*arguments), check=True)  # start
                """,
            "run's suites is not one it can read": """\
                def test_runs(suites: list[str]) -> None:
                    runner.run(suites)  # start
                """,
            "run_temporary starts pytest at line 22 on a suite it does not pin: the suite lies under the temporary "
            "folder and its root is not pinned": """\
                def test_runs() -> None:
                    runner.run_temporary()  # start
                """,
            "str(tmp_path) comes from a parameter pytest gives the test or fixture test_runs": """\
                def test_runs(tmp_path: Path) -> None:
                    subprocess.run(pytest_command(str(tmp_path)), cwd=REPOSITORY_ROOT, check=True)  # start
                """,
            "str(suite) comes from a parameter pytest gives the test or fixture started": """\
                import pytest


                @pytest.fixture
                def started(suite: Path) -> int:
                    return subprocess.run(pytest_command(str(suite)), check=False).returncode  # start
                """,
            "a starred item of run's suites": """\
                SUITES = ["tests/test_b.py"]


                def test_runs() -> None:
                    runner.run([*SUITES])  # start
                """,
            "str(REPOSITORY_ROOT.parents[1] / 'elsewhere' / 'test_x.py') is neither in the repository nor under a "
            "temporary folder": """\
                def test_runs() -> None:
                    subprocess.run(pytest_command(str(REPOSITORY_ROOT.parents[1] / "elsewhere" / "test_x.py")))  # start
                """,
        }
        for why, body in cases.items():
            with self.subTest(why):
                self.assertEqual([self.unplaced(body, why)], self.problems(body))

    def test_a_suite_that_starts_no_pytest_reads_no_start_and_passes(self) -> None:
        body = "def test_runs() -> None:\n    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
        self.assertEqual(([], []), self.reading({"tests/test_a.py": self.HEADER + body}))

    def test_a_suite_or_a_module_it_follows_that_cannot_be_read_is_named(self) -> None:
        unreadable_suite = self.HEADER + "def test_runs(:\n    pytest_command('x')\n"
        problems, _ = self.reading({"tests/test_a.py": unreadable_suite})
        self.assertEqual(["tests/test_a.py"], [problem.split(" ", 1)[0] for problem in problems], problems)
        calling = self.HEADER + "def test_runs() -> None:\n    runner.run_suite('tests/test_b.py')\n"
        problems, _ = self.reading(
            {"tests/test_a.py": calling, "tools/runner.py": "def run_suite(:\n    pytest_command\n"}
        )
        self.assertEqual(["tools/runner.py"], [problem.split(" ", 1)[0] for problem in problems], problems)

    def test_a_call_through_a_local_binding_looks_up_no_module_file(self) -> None:
        # A call through a name the module binds itself resolves to a placeholder such as `<local>.helper.run_suite`,
        # which names no module: looking it up as one would stat `tests/<local>.py`, a name Windows refuses.
        body = """\
            def make() -> object:
                return object()


            def test_runs() -> None:
                helper = make()
                with tempfile.TemporaryDirectory() as temporary:
                    helper.run_suite(str(Path(temporary) / "test_child.py"))
            """
        looked_up: list[list[str]] = []
        module_file = suite_discovery.RepositoryModules._module_file

        def recorded(base: Path, parts: list[str]) -> Path | None:
            looked_up.append(parts)
            return module_file(base, parts)

        with mock.patch.object(suite_discovery.RepositoryModules, "_module_file", staticmethod(recorded)):
            self.assertEqual([], self.problems(body))
        self.assertTrue(looked_up)
        self.assertEqual([], [parts for parts in looked_up if any("<" in part or ">" in part for part in parts)])

    def test_a_module_a_suite_loads_by_its_path_is_not_followed(self) -> None:
        # A stated limit: a call cannot name a function of a module loaded by its path, so the start reader asks
        # RepositoryModules for the modules a suite imports by name alone.
        loading = textwrap.dedent(
            """\
            import importlib.util
            import tempfile
            from pathlib import Path

            REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
            SPEC = importlib.util.spec_from_file_location("runner", REPOSITORY_ROOT / "tools" / "runner.py")


            def test_runs() -> None:
                runner = importlib.util.module_from_spec(SPEC)
                with tempfile.TemporaryDirectory() as temporary:
                    runner.run_suite(str(Path(temporary) / "test_child.py"))
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {"tools/runner.py": self.RUNNER, "tests/test_a.py": loading})
            modules = suite_discovery.RepositoryModules(root)
            suite = root / "tests" / "test_a.py"
            loaded = (root / "tools" / "runner.py").resolve()
            # Asked by name first, then with loads by path, each answer kept apart.
            self.assertNotIn(loaded, modules.imports(suite, by_path=False))
            self.assertIn(loaded, modules.imports(suite))
            self.assertNotIn(loaded, modules.imports(suite, by_path=False))
            self.assertEqual(([], []), suite_discovery.pytest_root_reading(root))


class PytestScriptRootPolicy(unittest.TestCase):
    """A suite that runs a repository script whose own pytest takes the suites on its command line, as
    tests/tools/test_python_diagnostics.py's run_tool runs tools/python_diagnostics.py, pins each suite's root too."""

    # A script that starts pytest on the suites its command line gives it, through tools/runner.py, with an option that
    # takes a value and a flag, as tools/python_diagnostics.py's parser holds.
    TOOL = textwrap.dedent(
        """\
        import argparse
        from pathlib import Path

        from tools import runner


        def main(arguments: list[str] | None = None) -> int:
            parser = argparse.ArgumentParser()
            parser.add_argument("--summary", type=Path)
            parser.add_argument("--quiet", action="store_true")
            parser.add_argument("suites", nargs="*")
            namespace = parser.parse_args(arguments)
            return max(runner.run(namespace.suites), default=0)
        """
    )
    # What each fixture suite starts with: tests/tools/test_python_diagnostics.py's TOOL, run_tool, and suite_folder.
    HEADER = textwrap.dedent(
        """\
        import shutil
        import subprocess
        import sys
        import tempfile
        import unittest
        from pathlib import Path

        REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
        TOOL = REPOSITORY_ROOT / "tools" / "tool.py"


        def run_tool(*arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-B", str(TOOL), *arguments], env=environment, cwd=REPOSITORY_ROOT, check=False
            )


        def suite_folder() -> Path:
            folder = Path(tempfile.mkdtemp(prefix="fixture-"))
            (folder / "pytest.ini").write_bytes(b"[pytest]\\n")
            return folder


        """
    )

    def reading(self, body: str, tool: str | None = None) -> tuple[list[str], list[str]]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "tools/runner.py": PytestRootPolicy.RUNNER,
                    "tools/tool.py": self.TOOL if tool is None else tool,
                    "tests/test_a.py": self.HEADER + textwrap.dedent(body),
                },
            )
            return suite_discovery.pytest_root_reading(root)

    def line(self, body: str) -> int:
        lines = (self.HEADER + textwrap.dedent(body)).splitlines()
        return next(number for number, text in enumerate(lines, 1) if text.endswith("# start"))

    def unpinned(self, body: str) -> str:
        return (
            f"tests/test_a.py:{self.line(body)} starts pytest from the repository on a suite under the temporary "
            f"folder and pins no root; {PytestRootPolicy.PINNING}"
        )

    def unplaced(self, body: str, why: str) -> str:
        return (
            f"tests/test_a.py:{self.line(body)} starts pytest from the repository on a suite this policy cannot place "
            f"in the repository or see pinned ({why}); build the suite's path from a tempfile folder in the function "
            "that starts it, and write a pytest.ini beside it or pass --confcutdir or --rootdir naming its folder"
        )

    def test_a_script_given_a_suite_under_a_temporary_folder_with_no_root_pinned_fails(self) -> None:
        cases = {
            # tests/tools/test_python_diagnostics.py's CleanRunTests before it pinned its fixture folders.
            "run_tool given one suite": """\
                class CleanRunTests(unittest.TestCase):
                    def test_a_run_whose_every_suite_passes_exits_0(self) -> None:
                        with tempfile.TemporaryDirectory() as temporary:
                            suite = Path(temporary) / "test_clean.py"
                            completed = run_tool(str(suite))  # start
                """,
            # Its FixtureRunTests, whose suites a dict holds, spread after an option and its value.
            "run_tool given an option's value and suites spread from a folder the function makes": """\
                class FixtureRunTests(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls) -> None:
                        folder = Path(tempfile.mkdtemp(prefix="python-diagnostics-test-"))
                        cls.addClassCleanup(shutil.rmtree, folder)
                        cls.suites = {}
                        for name in ("test_a.py", "test_b.py"):
                            cls.suites[name] = str(folder / name)
                        summary = folder / "summary.md"
                        cls.completed = run_tool("--summary", str(summary), *cls.suites.values())  # start
                """,
            "the script started as a program, by a literal path, given a flag and a suite": """\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = str(Path(temporary) / "test_child.py")
                        subprocess.run([sys.executable, "-B", "tools/tool.py", "--quiet", suite], check=True)  # start
                """,
        }
        for name, body in cases.items():
            with self.subTest(name):
                problems, _ = self.reading(body)
                self.assertEqual([self.unpinned(body)], problems)

    def test_a_script_given_suites_whose_roots_are_pinned_or_in_the_repository_passes(self) -> None:
        # tests/tools/test_python_diagnostics.py's forms since its fixture folders hold a pytest.ini.
        body = """\
            class FixtureRunTests(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    folder = suite_folder()
                    cls.addClassCleanup(shutil.rmtree, folder)
                    cls.suites = {}
                    for name in ("test_a.py", "test_b.py"):
                        cls.suites[name] = str(folder / name)
                    summary = folder / "summary.md"
                    cls.completed = run_tool("--summary", str(summary), *cls.suites.values())  # read


            class CleanRunTests(unittest.TestCase):
                def test_a_run_whose_every_suite_passes_exits_0(self) -> None:
                    folder = suite_folder()
                    suite = folder / "test_clean.py"
                    completed = run_tool(str(suite))  # read


            class UsageTests(unittest.TestCase):
                def test_a_run_given_no_suite_or_one_in_the_repository(self) -> None:
                    run_tool(environment={})  # read
                    run_tool("tests/no_such_suite.py", "tests")  # read
                    with tempfile.TemporaryDirectory() as temporary:
                        summary = Path(temporary) / "summary.md"
                        run_tool("--summary", str(summary), "tests/test_b.py")  # read
            """
        problems, read = self.reading(body)
        self.assertEqual([], problems)
        # Each run_tool call is read as a start, and so is the command run_tool itself starts the script with.
        lines = (self.HEADER + textwrap.dedent(body)).splitlines()
        marked = [number for number, text in enumerate(lines, 1) if text.endswith("# read") or "*arguments]" in text]
        self.assertEqual(sorted(f"tests/test_a.py:{number}" for number in marked), sorted(read))

    def test_a_script_start_this_policy_cannot_read_is_named(self) -> None:
        cases = {
            "a starred item of the script's arguments, in a function that makes no folder": """\
                SUITES = ["x"]


                def test_runs() -> None:
                    run_tool(*SUITES)  # start
                """,
            "str(suite) is neither in the repository nor under a temporary folder": """\
                SUITES = {"a": Path("x")}


                def test_runs() -> None:
                    suite = SUITES["a"]
                    run_tool(str(suite))  # start
                """,
        }
        for why, body in cases.items():
            with self.subTest(why):
                problems, _ = self.reading(body)
                self.assertEqual([self.unplaced(body, why)], problems)

    # A suite with no run_tool of its own, so the pre-check reads only what its body holds.
    BARE = textwrap.dedent(
        """\
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
        TOOL = REPOSITORY_ROOT / "tools" / "tool.py"


        """
    )

    def tree_reading(self, files: Mapping[str, str], root: Path | None = None) -> tuple[list[str], list[str]]:
        """pytest_root_reading over a tree holding the runner, the tool, and files, at root or a new folder."""
        with tempfile.TemporaryDirectory() as temporary:
            tree = root if root is not None else Path(temporary)
            write_fixture_tree(tree, {"tools/runner.py": PytestRootPolicy.RUNNER, "tools/tool.py": self.TOOL, **files})
            return suite_discovery.pytest_root_reading(tree)

    def problem(self, name: str, source: str, why: str | None = None) -> str:
        """The problem for the start `# start` marks in source, unpinned, or unplaced with why."""
        line = next(number for number, text in enumerate(source.splitlines(), 1) if text.endswith("# start"))
        if why is None:
            return (
                f"{name}:{line} starts pytest from the repository on a suite under the temporary folder and pins no "
                f"root; {PytestRootPolicy.PINNING}"
            )
        return (
            f"{name}:{line} starts pytest from the repository on a suite this policy cannot place in the repository or "
            f"see pinned ({why}); build the suite's path from a tempfile folder in the function that starts it, and "
            "write a pytest.ini beside it or pass --confcutdir or --rootdir naming its folder"
        )

    def test_an_interpreter_named_through_an_import_or_a_binding_is_read(self) -> None:
        spellings = {
            "from sys import executable": ("from sys import executable\n", "executable"),
            "import sys as system": ("import sys as system\n", "system.executable"),
            "a name bound to sys.executable": ("PYTHON = sys.executable\n", "PYTHON"),
        }
        for name, (binding, interpreter) in spellings.items():
            source = (
                self.BARE
                + binding
                + textwrap.dedent(
                    f"""\


                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = str(Path(temporary) / "test_child.py")
                        subprocess.run([{interpreter}, str(TOOL), suite], check=True)  # start
                """
                )
            )
            with self.subTest(name):
                problems, _ = self.tree_reading({"tests/test_a.py": source})
                self.assertEqual([self.problem("tests/test_a.py", source)], problems)

    def test_a_script_a_folder_above_the_suite_holds_is_read(self) -> None:
        # The suite lies a folder below the one that holds the script, which the pre-check finds from any folder
        # between the suite's and the root.
        source = textwrap.dedent(
            """\
            import subprocess
            import sys
            import tempfile
            from pathlib import Path

            TOOL = Path(__file__).resolve().parents[1] / "tool.py"


            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, str(TOOL), suite], check=True)  # start
            """
        )
        problems, _ = self.tree_reading({"tests/sub/test_a.py": source, "tests/tool.py": self.TOOL})
        self.assertEqual([self.problem("tests/sub/test_a.py", source)], problems)

    def test_the_interpreters_options_are_skipped_and_a_module_or_program_is_no_script(self) -> None:
        valued = self.BARE + textwrap.dedent(
            """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, "-X", "dev", "-W", "error", str(TOOL), suite], check=True)  # start
            """
        )
        problems, _ = self.tree_reading({"tests/test_a.py": valued})
        self.assertEqual([self.problem("tests/test_a.py", valued)], problems)
        for program in ('"-m", str(TOOL)', '"-c", str(TOOL)', '"-m", "tools.tool"', '"-c", "pass", str(TOOL)'):
            # Read beside run_tool, so the pre-check passes the suite and the script reader decides.
            source = self.HEADER + textwrap.dedent(
                f"""\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = str(Path(temporary) / "test_child.py")
                        subprocess.run([sys.executable, {program}, suite], check=True)
                """
            )
            with self.subTest(program):
                problems, _ = self.tree_reading({"tests/test_a.py": source})
                self.assertEqual([], problems)

    def test_an_options_value_and_a_dash_led_f_string_are_no_suite_and_a_flags_next_argument_is(self) -> None:
        tool = self.TOOL.replace(
            '    parser.add_argument("--quiet", action="store_true")\n',
            '    parser.add_argument("--quiet", action="store_true")\n'
            '    parser.add_argument("--include", action="append")\n'
            '    parser.add_argument("--extend", action="extend", nargs="+")\n'
            '    parser.add_argument("--off", nargs=0)\n',
        )
        # Each argument the script's parser takes as an option or its value is one this policy would name as unplaced
        # if it read it as a suite, so only the suite after the flag is judged, and found unpinned.
        body = """\
            NAMES = {"one": Path("x"), "two": Path("y"), "three": Path("z")}


            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    one, two, suite = str(NAMES["one"]), str(NAMES["two"]), str(Path(temporary) / "test_c.py")
                    run_tool(f"--summary={NAMES['three']}", "--include", one, "--extend", two, "--off", suite)  # start
            """
        problems, _ = self.reading(body, tool)
        self.assertEqual([self.unpinned(body)], problems)

    def test_the_interpreters_options_are_read_from_its_documented_catalogue_and_an_unknown_one_is_named(
        self,
    ) -> None:
        # Python's command-line documentation: -c, -m, -W, -X, and --check-hash-based-pycs take a separate value.
        self.assertEqual(frozenset({"-W", "-X", "--check-hash-based-pycs"}), suite_discovery.INTERPRETER_VALUE_OPTIONS)
        self.assertEqual(("-m", "-c"), suite_discovery.INTERPRETER_PROGRAMS)
        for options in ('"--check-hash-based-pycs", "always"', '"-IB"', '"-Wdefault", "-Xdev"', '"-OO", "--version"'):
            source = self.BARE + textwrap.dedent(
                f"""\
                def test_runs() -> None:
                    with tempfile.TemporaryDirectory() as temporary:
                        suite = str(Path(temporary) / "test_child.py")
                        subprocess.run([sys.executable, {options}, str(TOOL), suite], check=True)  # start
                """
            )
            with self.subTest(options):
                problems, _ = self.tree_reading({"tests/test_a.py": source})
                self.assertEqual([self.problem("tests/test_a.py", source)], problems)
        unknown = self.BARE + textwrap.dedent(
            """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, "--frobnicate", "x", str(TOOL), suite], check=True)  # start
                    subprocess.run([sys.executable, "--frobnicate", "tests/test_b.py"], check=True)
            """
        )
        why = "the interpreter's option --frobnicate, which this policy does not know, comes before the script"
        problems, _ = self.tree_reading({"tests/test_a.py": unknown})
        self.assertEqual([self.problem("tests/test_a.py", unknown, why)], problems)

    def test_a_script_path_built_through_steps_up_and_dots_is_read_and_one_windows_refuses_is_never_looked_up(
        self,
    ) -> None:
        stepped = self.BARE.replace(
            'TOOL = REPOSITORY_ROOT / "tools" / "tool.py"',
            'TOOL = Path(__file__).resolve().parent / "missing" / ".." / ".." / "tools" / "." / "tool.py"',
        ) + textwrap.dedent(
            """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, str(TOOL), suite], check=True)  # start
            """
        )
        problems, _ = self.tree_reading({"tests/test_a.py": stepped})
        self.assertEqual([self.problem("tests/test_a.py", stepped)], problems)
        # Read beside run_tool, whose command the pre-check finds, so the commands below reach the script reader.
        refused = self.HEADER + textwrap.dedent(
            """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, "tools/<tool>.py", suite], check=True)
                    subprocess.run([sys.executable, str(REPOSITORY_ROOT / "tools" / "<tool>.py"), suite], check=True)
            """
        )
        asked: list[str] = []
        is_present = file_presence.is_present

        def recorded(path: Path, *arguments: object, **options: object) -> bool:
            asked.append(str(path))
            return is_present(path, *arguments, **options)  # type: ignore[arg-type]  # the arguments it was given

        with mock.patch.object(file_presence, "is_present", recorded):
            problems, _ = self.tree_reading({"tests/test_a.py": refused})
        self.assertEqual([], problems)
        self.assertTrue(asked)
        self.assertEqual([], [path for path in asked if "<" in path or ">" in path])

    def test_a_starred_parameter_no_caller_it_reads_gives_is_named(self) -> None:
        why = "*arguments comes from a parameter no caller it reads gives"
        cases = {
            "a method": """\
                class Runner:
                    def run(self, *arguments: str) -> None:
                        subprocess.run([sys.executable, "-B", str(TOOL), *arguments], check=False)  # start
                """,
            "a nested function": """\
                def test_runs() -> None:
                    def run(*arguments: str) -> None:
                        subprocess.run([sys.executable, "-B", str(TOOL), *arguments], check=False)  # start

                    run("tests/test_b.py")
                """,
        }
        for name, body in cases.items():
            source = self.BARE + textwrap.dedent(body)
            with self.subTest(name):
                problems, _ = self.tree_reading({"tests/test_a.py": source})
                self.assertEqual([self.problem("tests/test_a.py", source, why)], problems)

    def test_a_script_that_is_missing_or_lies_outside_the_root_is_not_read(self) -> None:
        # Each read beside run_tool, so the pre-check passes the suite and the script reader decides.
        missing = self.HEADER + textwrap.dedent(
            """\
            MISSING = REPOSITORY_ROOT / "tools" / "missing.py"


            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    subprocess.run([sys.executable, str(MISSING), str(Path(temporary) / "test_child.py")], check=True)
            """
        )
        problems, _ = self.tree_reading({"tests/test_a.py": missing})
        self.assertEqual([], problems)
        outside = self.HEADER + textwrap.dedent(
            """\
            BESIDE = Path(__file__).resolve().parents[2] / "tool.py"


            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    subprocess.run([sys.executable, str(BESIDE), str(Path(temporary) / "test_child.py")], check=True)
            """
        )
        with tempfile.TemporaryDirectory() as temporary:
            # A script beside the root, which names a start itself, so only its place keeps it unread.
            (Path(temporary) / "tool.py").write_bytes(
                (self.TOOL + "\nfrom suite_discovery import pytest_command\n").encode("utf-8")
            )
            problems, _ = self.tree_reading({"tests/test_a.py": outside}, Path(temporary) / "repository")
            self.assertEqual([], problems)

    def test_a_pytest_ini_written_after_the_run_pins_no_starred_argument(self) -> None:
        body = """\
            class FixtureRunTests(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    folder = Path(tempfile.mkdtemp())
                    cls.suites = {"a": str(folder / "test_a.py")}
                    run_tool(*cls.suites.values())  # start
                    (folder / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
            """
        problems, _ = self.reading(body)
        self.assertEqual([self.unpinned(body)], problems)

    def test_a_pytest_ini_written_before_the_run_pins_a_starred_argument(self) -> None:
        body = """\
            class FixtureRunTests(unittest.TestCase):
                @classmethod
                def setUpClass(cls) -> None:
                    folder = Path(tempfile.mkdtemp())
                    (folder / "pytest.ini").write_text("[pytest]\\n", encoding="utf-8")
                    cls.suites = {"a": str(folder / "test_a.py")}
                    run_tool(*cls.suites.values())
            """
        self.assertEqual([], self.reading(body)[0])

    def test_a_script_given_a_working_directory_of_its_own_is_still_held(self) -> None:
        # tools/python_diagnostics.py starts its pytest from the repository's root whatever folder it is started in.
        source = self.BARE + textwrap.dedent(
            """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    suite = str(Path(temporary) / "test_child.py")
                    subprocess.run([sys.executable, str(TOOL), suite], cwd=temporary, check=True)  # start
            """
        )
        problems, _ = self.tree_reading({"tests/test_a.py": source})
        self.assertEqual([self.problem("tests/test_a.py", source)], problems)

    def test_a_script_that_starts_no_pytest_or_takes_no_suite_is_not_a_start(self) -> None:
        body = """\
            def test_runs() -> None:
                with tempfile.TemporaryDirectory() as temporary:
                    run_tool(str(Path(temporary) / "test_child.py"))
            """
        quiet = textwrap.dedent(
            """\
            import argparse


            def main() -> int:
                argparse.ArgumentParser().add_argument("names", nargs="*")
                return 0
            """
        )
        self.assertEqual(([], []), self.reading(body, quiet))
        options_only = self.TOOL.replace('parser.add_argument("suites", nargs="*")', 'parser.add_argument("--suite")')
        self.assertEqual(([], []), self.reading(body, options_only))


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

    def test_a_change_through_any_lookup_by_name_is_reported(self) -> None:
        # Each form of the one catalogue attribute_lookups reads beyond getattr, by a literal name and a computed one,
        # and a store into a namespace, which replaces the attribute it names.
        alpha = (
            "import inspect\n"  # 1
            "import operator\n"  # 2
            "import os\n"  # 3
            "import sys\n"  # 4
            "\n"  # 5
            "def check(name):\n"  # 6
            "    inspect.getattr_static(os, 'environ')['A'] = 'b'\n"  # 7
            "    object.__getattribute__(os, 'chdir')('..')\n"  # 8
            "    os.__getattribute__('environ').pop('A')\n"  # 9
            "    operator.attrgetter('environ')(os).update({})\n"  # 10
            "    vars(os)['environ']['A'] = 'b'\n"  # 11
            "    os.__dict__['chdir']('..')\n"  # 12
            "    vars(sys)['stdout'] = None\n"  # 13
            "    vars(os)[name]('..')\n"  # 14
            "    operator.attrgetter('environ', 'sep')(os)[0].clear()\n"  # 15: a tuple of attributes is not followed
        )
        problems = self.problems({"tests/validation/alpha.py": alpha})
        self.assertEqual(
            [
                "tests/validation/alpha.py:7 changes inspect.getattr_static(os, 'environ')['A']",
                "tests/validation/alpha.py:8 calls os.chdir",
                "tests/validation/alpha.py:9 calls os.environ.pop",
                "tests/validation/alpha.py:10 calls os.environ.update",
                "tests/validation/alpha.py:11 changes vars(os)['environ']['A']",
                "tests/validation/alpha.py:12 calls os.chdir",
                "tests/validation/alpha.py:13 changes vars(sys)['stdout']",
                "tests/validation/alpha.py:14 may change a standard stream, os.environ, or the working directory "
                "through `vars(os)[name]`",
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

    def test_a_policy_module_of_the_wrong_kind_is_named_so_never_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # A module of the wrong kind is reported and the modules after it are still read.
            write_fixture_tree(
                root,
                {
                    "tests/run_validation.py": self.RUNNER,
                    "tests/validation/alpha.py/inner.py": "",
                    "tests/validation/beta.py": "import os\n\n\ndef f():\n    os.chdir('.')\n",
                },
            )
            problems = pool_shared_state_problems(root)
        self.assertEqual(2, len(problems), problems)
        self.assertEqual(
            "tests/validation/alpha.py: a policy module tests/run_validation.py runs is not a file", problems[0]
        )
        self.assertTrue(problems[1].startswith("tests/validation/beta.py:5 calls os.chdir"), problems)

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


# A suite with a test each leak below leaves out, which leaves a file behind when it runs, and a plugin that keeps only
# the test that leaves none.
LEAKED_SUITE = (
    "import unittest\nfrom pathlib import Path\n\n\ndef test_kept():\n    pass\n\n\n"
    "def test_dropped():\n    Path('dropped.ran').touch()\n\n\n"
    "class Case(unittest.TestCase):\n    def test_unittest(self):\n        Path('unittest.ran').touch()\n"
)
HIDING_PLUGIN = "def pytest_collection_modifyitems(items):\n    items[:] = [i for i in items if i.name == 'test_kept']"
# Each way the user's environment reaches pytest: the variables it sets, whether an installed package registers the
# hiding plugin, and the test the leak would leave out.
LEAKS = {
    "a deselection in PYTEST_ADDOPTS": ({"PYTEST_ADDOPTS": "--deselect=test_leak.py::test_dropped"}, False, "dropped"),
    "a disabled plugin in PYTEST_ADDOPTS": ({"PYTEST_ADDOPTS": "-p no:unittest"}, False, "unittest"),
    "a plugin in PYTEST_PLUGINS": ({"PYTEST_PLUGINS": "hiding"}, False, "dropped"),
    "a plugin an installed package registers": ({}, True, "dropped"),
}


@contextlib.contextmanager
def leaked_environment(variables: Mapping[str, str], registered: bool) -> Iterator[Path]:
    """A folder holding LEAKED_SUITE and the hiding plugin, which a dist-info folder registers as an installed
    package's pytest plugin when `registered`, with this process's environment holding only `variables` of pytest's."""
    with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, variables):
        root = Path(temporary)
        for name in {"PYTEST_ADDOPTS", "PYTEST_PLUGINS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"} - set(variables):
            os.environ.pop(name, None)
        files = {"pytest.ini": "[pytest]\n", "test_leak.py": LEAKED_SUITE, "hiding.py": HIDING_PLUGIN}
        if registered:
            files["hiding-1.0.dist-info/METADATA"] = "Metadata-Version: 2.1\nName: hiding\nVersion: 1.0\n"
            files["hiding-1.0.dist-info/entry_points.txt"] = "[pytest11]\nhiding = hiding\n"
        write_fixture_tree(root, files)
        yield root


class PytestEnvironmentFixtures(unittest.TestCase):
    """The runner starts pytest with none of the user's PYTEST_ADDOPTS, PYTEST_PLUGINS, or autoloaded plugins, so a
    test the environment would leave out still runs."""

    def test_a_suite_runs_every_test_whatever_the_environment_leaves_out(self) -> None:
        for name, (variables, registered, left_out) in LEAKS.items():
            with self.subTest(name), leaked_environment(variables, registered) as root:
                with (
                    mock.patch.object(validation_support, "REPOSITORY_ROOT", root),
                    mock.patch.object(
                        suite_discovery, "run_process", functools.partial(job_pool.run_process, cwd=root)
                    ),
                ):
                    suite_discovery.run_test_script(root / "test_leak.py")
                self.assertTrue((root / f"{left_out}.ran").is_file())

    def test_a_shard_starts_pytest_with_the_same_environment_as_a_whole_suite(self) -> None:
        with mock.patch.object(suite_discovery, "run_process") as run:
            suite_discovery.run_shard(REPOSITORY_ROOT / "tests" / "test_a.py", 0, 2, {})
            suite_discovery.run_test_script(REPOSITORY_ROOT / "tests" / "test_a.py")
        expected = {"PYTEST_ADDOPTS": "", "PYTEST_PLUGINS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        self.assertEqual([expected, expected], [call.args[1] for call in run.call_args_list])
