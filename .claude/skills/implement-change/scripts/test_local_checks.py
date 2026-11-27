"""Regression tests for local_checks.py: what it runs, with what environment, and what it prints of each run."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
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
# The repository's top, for the deployer's Bash lookup.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

import local_checks
from bounded_process import Finished
from fixture_template import remove_tree
from git_client import GitClient, GitError, GitResult, subprocess_runner

# The one Git Bash lookup. This folder's type check runs mypy from the folder, where the deployer is no package.
from deployer import platform_support  # type: ignore[import-not-found]  # checked from this folder: no deployer

GIT_CONFIG: Mapping[str, str] = MappingProxyType({"init.defaultBranch": "main"})
# The literals the script's lines are pinned against, independent of the constants that build them. The interpreter is
# this one, as Bash reads it quoted; the round trip through Bash holds the quoting itself.
INTERPRETER = shlex.quote(sys.executable)
RUNNER = f"{INTERPRETER} -B tests/run_validation.py"
PRE_PUSH_COMMAND = f"GITHUB_BASE_REF=main VALIDATION_MYPY_PLATFORM='' {RUNNER} --pre-push"
WINDOWS_VARIABLES = "GITHUB_BASE_REF=main VALIDATION_MYPY_PLATFORM=win32"
# A stand-in for the runner that prints, as JSON, the command line it was started with, the interpreter that runs it,
# and the variables the script gives a run.
RUNNER_STAND_IN = """import json, os, sys
names = ("GITHUB_BASE_REF", "VALIDATION_MYPY_PLATFORM")
seen = {"argv": sys.orig_argv, "executable": sys.executable, "variables": {n: os.environ.get(n) for n in names}}
print(json.dumps(seen))
"""
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


def bash() -> str:
    """The Bash the printed commands are for: Git Bash on Windows, as validation requires."""
    found: str | None = platform_support.find_bash()
    if found is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash reruns the printed commands")
    return found


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

    def run_script(
        self, *arguments: str, git_client: GitClient | None = None, base: str = "origin/main"
    ) -> tuple[int, list[str]]:
        output = io.StringIO()
        with (
            mock.patch.object(local_checks, "run_bounded", side_effect=self.fake_run),
            mock.patch.object(local_checks, "output_stream", return_value=io.BytesIO()),
            contextlib.redirect_stdout(output),
        ):
            code = local_checks.main(["--repository", str(self.root), "--base", base, *arguments], git_client)
        return code, output.getvalue().splitlines()

    def base_line(self) -> str:
        return f"BASE origin/main {git(self.root, 'rev-parse', 'origin/main')}"

    def test_a_python_change_runs_the_changed_selection_then_its_type_checks_as_on_windows(self) -> None:
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        git(self.root, "commit", "--quiet", "-am", "change")
        self.write("tests/unit/test_new.py", "")
        windows = f"{RUNNER} -k '{SKILL_JOB}' -k '{SHARED_JOB}' -k '{STRICT_TESTS_JOB}'"
        self.assertEqual(
            (
                0,
                [
                    self.base_line(),
                    f"RUN pre-push {PRE_PUSH_COMMAND}",
                    "PASSED pre-push",
                    f"RUN windows-types {WINDOWS_VARIABLES} {windows}",
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
        # Empty, which the runner reads as unset: the platform it runs on.
        self.assertEqual("", changed.env["VALIDATION_MYPY_PLATFORM"])
        self.assertEqual("win32", types.env["VALIDATION_MYPY_PLATFORM"])

    def test_a_shared_module_runs_every_type_check_as_on_windows(self) -> None:
        self.write("skills/core/scripts/shared.py", "# changed\n")
        code, lines = self.run_script()
        self.assertEqual(0, code)
        self.assertIn(
            f"RUN windows-types {WINDOWS_VARIABLES} {RUNNER} -k 'static type check'",
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
        self.assertEqual("", self.runs[0].env["VALIDATION_MYPY_PLATFORM"])
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
        # Quoted as the system's temporary folder needs, since it may hold a space.
        words = f"--pr-title {shlex.quote(str(title))} --pr-body {shlex.quote(str(body))}"
        self.assertIn(f"RUN pre-push {PRE_PUSH_COMMAND} {words}", lines)
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

    def test_a_relative_title_or_body_is_read_from_the_caller_s_folder_not_the_top_of_the_repository(self) -> None:
        caller = self.temporary / "caller folder"
        caller.mkdir()
        (caller / "title.txt").write_text("feat: x\n", encoding="utf-8")
        (caller / "body.md").write_text("Body\n", encoding="utf-8")
        with contextlib.chdir(caller):
            code, lines = self.run_script("--title-file", "title.txt", "--body-file", "body.md")
        self.assertEqual(0, code)
        title, body = caller / "title.txt", caller / "body.md"
        # The caller's folder holds a space, so each path is in single quotes.
        self.assertIn(f"RUN pre-push {PRE_PUSH_COMMAND} --pr-title '{title}' --pr-body '{body}'", lines)
        self.assertEqual(["--pr-title", str(title), "--pr-body", str(body)], self.runs[0].command[-4:])

    def test_each_printed_command_rerun_in_bash_starts_the_run_again_with_its_words_and_variables(self) -> None:
        # A base other than main, inherited values of both variables, and paths Bash would expand or split unquoted.
        git(self.root, "push", "--quiet", "origin", "main:release/staging")
        self.write("skills/alpha/scripts/run.py", "# changed\n")
        files = self.temporary / "files $HOME"
        files.mkdir()
        title, body = files / "title $USER.txt", files / "body 'quoted' `x`.md"
        title.write_text("feat: x\n", encoding="utf-8")
        body.write_text("Body\n", encoding="utf-8")
        inherited = {"GITHUB_BASE_REF": "other", "VALIDATION_MYPY_PLATFORM": "linux"}
        with mock.patch.dict("os.environ", inherited):
            code, lines = self.run_script(
                "--title-file", str(title), "--body-file", str(body), base="origin/release/staging"
            )
        self.assertEqual(0, code)
        printed = [line.split(" ", 2) for line in lines if line.startswith("RUN ")]
        self.assertEqual(["pre-push", "windows-types"], [name for _, name, _ in printed])
        self.assertEqual(
            [["--pre-push", "--pr-title", str(title), "--pr-body", str(body)], "release/staging"],
            [self.runs[0].command[3:], self.runs[0].env["GITHUB_BASE_REF"]],
        )
        stand_in = self.temporary / "rerun"
        (stand_in / "tests").mkdir(parents=True)
        (stand_in / "tests" / "run_validation.py").write_text(RUNNER_STAND_IN, encoding="utf-8")
        for (_, name, command), run in zip(printed, self.runs, strict=True):
            with self.subTest(run=name):
                rerun = subprocess.run(
                    [bash(), "-c", command],
                    cwd=stand_in,
                    env={**os.environ, **inherited, "USER": "someone", "HOME": "/elsewhere"},
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                    timeout=60,
                )
                self.assertEqual(0, rerun.returncode, rerun.stderr)
                seen = json.loads(rerun.stdout)
                self.assertEqual(run.command[1:], seen["argv"][1:])
                self.assertTrue(Path(run.command[0]).samefile(seen["executable"]), seen["executable"])
                self.assertEqual({variable: run.env.get(variable) for variable in seen["variables"]}, seen["variables"])

    def test_a_command_no_run_line_can_carry_is_a_failure_before_any_run(self) -> None:
        self.write("docs/guide.md", "# changed\n")
        code, lines = self.run_script("--title-file", str(self.temporary / "title\nnext.txt"))
        self.assertEqual(1, code)
        self.assertEqual(
            "FAILED the pre-push run's command holds a line break or a control character, which its RUN line could "
            f"not carry: {self.temporary / 'title next.txt'}",
            lines[-1],
        )
        self.assertEqual([], self.runs)

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

    def test_a_pyproject_the_type_checks_cannot_read_is_a_failure_before_any_run(self) -> None:
        # The base holds the configuration, since a changed pyproject.toml reaches every type check unread.
        pyproject = self.root / "pyproject.toml"
        for text, reason in (
            ("[tool.mypy\n", f'FAILED cannot read "{pyproject}": '),
            (
                '[tool.mypy]\nmypy_path = "skills"\n',
                "FAILED pyproject.toml: [tool.mypy] mypy_path is not a list of paths",
            ),
            (
                '[tool.validation]\nmypy-strict = "tests"\n\n[tool.mypy]\nmypy_path = []\n',
                "FAILED pyproject.toml: [tool.validation] mypy-strict is not a list of paths",
            ),
        ):
            with self.subTest(text=text):
                self.runs.clear()
                self.write("pyproject.toml", text)
                git(self.root, "commit", "--quiet", "-am", "configuration")
                git(self.root, "push", "--quiet", "origin", "HEAD:main")
                self.write("skills/alpha/scripts/run.py", f"# changed for {text!r}\n")
                code, lines = self.run_script()
                self.assertEqual(1, code)
                self.assertEqual(self.base_line(), lines[0])
                self.assertTrue(lines[-1].startswith(reason), lines)
                self.assertEqual([], self.runs)

    def test_a_job_name_no_single_quotes_can_hold_is_a_failure_before_any_run(self) -> None:
        self.write("skills/al'pha/SKILL.md", "# al'pha\n")
        self.write("skills/al'pha/scripts/run.py", "# changed\n")
        code, lines = self.run_script()
        self.assertEqual(1, code)
        self.assertEqual(
            "FAILED a type-check job's name holds a single quote: static type check (mypy skills/al'pha/scripts)",
            lines[-1],
        )
        self.assertEqual([], self.runs)

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
