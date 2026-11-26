"""Regression tests for local_checks.py: what it runs, with what environment, and what it prints of each run."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))
# The folder removal the repository-skill and tools suites share.
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "tests" / "tools"))

import local_checks
from bounded_process import Finished
from fixture_template import remove_tree
from git_client import GitClient, GitError, GitResult, subprocess_runner

GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})
# The literals the script's lines are pinned against, independent of the constants that build them.
PRE_PUSH_COMMAND = "python -B tests/run_validation.py --pre-push"
SKILL_JOB = "static type check (mypy skills/alpha/scripts)"
SHARED_JOB = "static type check (mypy deployer, tools, deploy.py, tests, conftest.py)"
STRICT_TESTS_JOB = "static type check (mypy --strict tests)"
PYPROJECT = (
    '[tool.validation]\nmypy-strict = ["tests"]\n\n'
    '[tool.mypy]\nmypy_path = ["$MYPY_CONFIG_FILE_DIR/skills/core/scripts"]\n'
)
FILES = (
    "skills/alpha/SKILL.md",
    "skills/alpha/scripts/run.py",
    "skills/core/SKILL.md",
    "skills/core/scripts/shared.py",
    "tests/unit/test_gate.py",
    "docs/guide.md",
)


def git(directory: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


class Run:
    """One command the script started: what it ran, from where, with which environment, and where its output went."""

    def __init__(self, command: Sequence[str], cwd: Path | None, env: Mapping[str, str] | None, stdout: object) -> None:
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env or {})
        self.stdout = stdout


class LocalChecksTests(unittest.TestCase):
    """A clone of a bare origin whose main holds a skill, a shared skill mypy_path lists, a suite, and a document; each
    test changes some of them and runs the script with the runner's process start replaced."""

    git_config = GIT_CONFIG

    @override
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp(prefix="local-checks-test-")).resolve()
        self.addCleanup(remove_tree, self.temporary)
        origin = self.temporary / "origin.git"
        git(self.temporary, "init", "--quiet", "--bare", str(origin))
        self.root = self.temporary / "work tree"
        git(self.temporary, "clone", "--quiet", str(origin), str(self.root))
        self.write("pyproject.toml", PYPROJECT)
        for name in FILES:
            self.write(name, f"# {name}\n")
        git(self.root, "add", "--all")
        git(self.root, "commit", "--quiet", "-m", "base")
        git(self.root, "push", "--quiet", "origin", "main")
        git(self.root, "switch", "--quiet", "-c", "feat/change")
        self.runs: list[Run] = []
        self.statuses: list[int | None] = []

    def write(self, name: str, text: str) -> None:
        path = self.root.joinpath(*name.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def fake_run(self, command: Sequence[str], timeout: float, **options: Any) -> Finished:
        """Record the run, and end it with the next of self.statuses, 0 once they run out, or as a timeout for None."""
        self.assertGreater(timeout, 0)
        self.runs.append(Run(command, options.get("cwd"), options.get("env"), options.get("stdout")))
        self.assertIs(options.get("stdout"), options.get("stderr"))
        self.assertIsNotNone(options.get("tree"))
        status = self.statuses.pop(0) if self.statuses else 0
        if status is None:
            raise subprocess.TimeoutExpired(list(command), timeout)
        return Finished(status, b"", b"")

    def run_script(self, *arguments: str, git_client: GitClient | None = None) -> tuple[int, list[str]]:
        output = io.StringIO()
        with (
            mock.patch.object(local_checks, "run_bounded", side_effect=self.fake_run),
            mock.patch.object(local_checks, "output_stream", return_value=io.BytesIO()),
            contextlib.redirect_stdout(output),
        ):
            code = local_checks.main(["--repository", str(self.root), "--base", "origin/main", *arguments], git_client)
        return code, output.getvalue().splitlines()

    def base_line(self) -> str:
        return f"BASE origin/main {git(self.root, 'rev-parse', 'origin/main')}"

    def test_a_python_change_runs_the_changed_selection_then_its_type_checks_as_on_windows(self) -> None:
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        git(self.root, "commit", "--quiet", "-am", "change")
        self.write("tests/unit/test_new.py", "")
        windows = f"python -B tests/run_validation.py -k '{SKILL_JOB}' -k '{SHARED_JOB}' -k '{STRICT_TESTS_JOB}'"
        self.assertEqual(
            (
                0,
                [
                    self.base_line(),
                    f"RUN pre-push {PRE_PUSH_COMMAND}",
                    "PASSED pre-push",
                    f"RUN windows-types VALIDATION_MYPY_PLATFORM=win32 {windows}",
                    "PASSED windows-types",
                    "READY",
                ],
            ),
            self.run_script(),
        )
        changed, types = self.runs
        self.assertEqual([sys.executable, "-B", "tests/run_validation.py", "--pre-push"], changed.command)
        self.assertEqual(
            [
                sys.executable,
                "-B",
                "tests/run_validation.py",
                "-k",
                SKILL_JOB,
                "-k",
                SHARED_JOB,
                "-k",
                STRICT_TESTS_JOB,
            ],
            types.command,
        )
        self.assertEqual({self.root}, {changed.cwd, types.cwd})
        # The runner compares with origin/<branch>, as GITHUB_BASE_REF names it in continuous integration.
        self.assertEqual("main", changed.env["GITHUB_BASE_REF"])
        self.assertNotIn("VALIDATION_MYPY_PLATFORM", changed.env)
        self.assertEqual("win32", types.env["VALIDATION_MYPY_PLATFORM"])

    def test_a_shared_module_runs_every_type_check_as_on_windows(self) -> None:
        self.write("skills/core/scripts/shared.py", "# changed\n")
        code, lines = self.run_script()
        self.assertEqual(0, code)
        self.assertIn(
            "RUN windows-types VALIDATION_MYPY_PLATFORM=win32 python -B tests/run_validation.py -k 'static type check'",
            lines,
        )
        self.assertEqual(
            [sys.executable, "-B", "tests/run_validation.py", "-k", "static type check"], self.runs[1].command
        )

    def test_a_change_no_type_check_reads_runs_the_changed_selection_alone(self) -> None:
        self.write("docs/guide.md", "# changed\n")
        self.assertEqual(
            (
                0,
                [
                    self.base_line(),
                    f"RUN pre-push {PRE_PUSH_COMMAND}",
                    "PASSED pre-push",
                    "SKIPPED windows-types no changed file is one a type check reads",
                    "READY",
                ],
            ),
            self.run_script(),
        )
        self.assertEqual(1, len(self.runs))

    def test_an_inherited_platform_never_reaches_the_native_run(self) -> None:
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        with mock.patch.dict("os.environ", {"VALIDATION_MYPY_PLATFORM": "linux", "GITHUB_BASE_REF": "other"}):
            self.assertEqual(0, self.run_script()[0])
        self.assertNotIn("VALIDATION_MYPY_PLATFORM", self.runs[0].env)
        self.assertEqual("main", self.runs[0].env["GITHUB_BASE_REF"])
        self.assertEqual("win32", self.runs[1].env["VALIDATION_MYPY_PLATFORM"])

    def test_the_title_and_body_files_are_checked_in_the_changed_run(self) -> None:
        files = self.temporary / "files"
        files.mkdir()
        title, body = files / "title.txt", files / "body.md"
        title.write_text("feat: x\n", encoding="utf-8")
        body.write_text("Body\n", encoding="utf-8")
        code, lines = self.run_script("--title-file", str(title), "--body-file", str(body))
        self.assertEqual(0, code)
        self.assertIn(f'RUN pre-push {PRE_PUSH_COMMAND} --pr-title "{title}" --pr-body "{body}"', lines)
        self.assertEqual(
            [
                sys.executable,
                "-B",
                "tests/run_validation.py",
                "--pre-push",
                "--pr-title",
                str(title),
                "--pr-body",
                str(body),
            ],
            self.runs[0].command,
        )

    def test_each_run_that_fails_is_named_with_its_status_and_every_run_still_runs(self) -> None:
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        self.statuses = [1, None]
        code, lines = self.run_script()
        self.assertEqual(1, code)
        self.assertEqual(
            ["NOT_PASSED pre-push 1", "NOT_PASSED windows-types timeout", "NOT_READY 2"],
            [line for line in lines if line.startswith(("NOT_", "PASSED", "READY"))],
        )
        self.assertEqual(2, len(self.runs))

    def test_a_runner_that_cannot_start_is_a_failure_to_report(self) -> None:
        self.write("docs/guide.md", "# changed\n")

        def missing(command: Sequence[str], timeout: float, **options: Any) -> Finished:
            raise FileNotFoundError(2, "No such file or directory", command[0])

        output = io.StringIO()
        with (
            mock.patch.object(local_checks, "run_bounded", side_effect=missing),
            mock.patch.object(local_checks, "output_stream", return_value=io.BytesIO()),
            contextlib.redirect_stdout(output),
        ):
            code = local_checks.main(["--repository", str(self.root), "--base", "origin/main"])
        lines = output.getvalue().splitlines()
        self.assertEqual(1, code)
        self.assertRegex(lines[-1], r"^FAILED cannot start the validation runner: .*No such file or directory")

    def test_a_base_that_cannot_be_fetched_runs_nothing(self) -> None:
        def runner(command: Sequence[str], timeout: float) -> GitResult:
            if "fetch" in command:
                raise GitError("fatal: unable to access the remote", kind="git", returncode=128)
            return subprocess_runner(command, timeout)

        code, lines = self.run_script(git_client=GitClient(runner=runner))
        self.assertEqual(1, code)
        self.assertEqual(["FAILED cannot fetch main from origin: fatal: unable to access the remote"], lines)
        self.assertEqual([], self.runs)

    def test_a_base_on_another_remote_is_a_usage_error(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as exited:
            local_checks.main(["--repository", str(self.root), "--base", "upstream/main"])
        self.assertEqual(2, exited.exception.code)
        self.assertIn("the validation runner compares with origin, so give origin/<branch>", errors.getvalue())


class OutputTests(unittest.TestCase):
    def test_each_run_writes_to_this_process_s_standard_output(self) -> None:
        stream: IO[bytes] = local_checks.output_stream()
        self.assertIs(sys.stdout.buffer, stream)
