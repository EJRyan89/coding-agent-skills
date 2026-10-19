"""Regression suite for tools/runtime_canary.py, with every runtime replaced by a fake.

No test starts Claude Code, Codex, or Copilot CLI: setUp replaces the real process runner with one that fails the
test. The fakes stand in for a runtime by running, or not running, the deployed skill's command themselves with the
environment the canary gave them, so the deployment, the recorder, and the fixture's marker are all real.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))


from deployer import discovery, platform_support, runtime_support
from deployer import source as deploy_source
from deployer.paths import Paths
from tools import runtime_canary
from tools.runtime_canary import Completed

# Runs no test of its own; the test functions below check through its assertions, as the classes do.
CHECK = unittest.TestCase()

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
# The real runner, kept before setUp replaces it, for the cases that start a stub program through it.
RUN_PROCESS = runtime_canary.run_process

FIXTURE = "runtime-canary-probe"
HIDDEN_REASON = "A hidden dependency no runtime starts."
# What the canary says of a skill the deployer gave no adapter, pinned here rather than read from the tool.
NO_ADAPTER = "the deployer gave it no runtime adapter, so deploy.py verify expects no runtime to list it"
# What a RAN line adds for a Python script that exits 0: the recorder sees a Python script end only from 3.12.
PYTHON_EXIT = " exit=0" if sys.version_info >= (3, 12) else ""
# The platform's Python command, which the fixture renders and Copilot is granted, written here as a literal.
PYTHON = "python" if sys.platform == "win32" else "python3"
# A line Codex 0.160 wrote to stderr when its execution policy refused the canary's first command.
CODEX_REJECTION = (
    "2026-10-03T22:35:23.161504Z ERROR codex_core::tools::router: error=exec_command failed: CreateProcess { "
    'message: "Rejected(\\"`\\\\\\"C:\\\\\\\\pwsh.exe\\\\\\" -Command \\\\\\"Get-Content -LiteralPath '
    '\'C:\\\\\\\\x\\\\\\\\SKILL.md\'\\\\\\"` rejected: blocked by policy\\")" }\n'
)


def forward(path: Path | str) -> str:
    return str(path).replace("\\", "/")


# What each fake runtime prints for --version, as each real one words it.
VERSIONS = {
    "claude": "2.1.291 (Claude Code)",
    "codex": "codex-cli 0.160.0",
    "copilot": "GitHub Copilot CLI 1.0.98.\nRun 'copilot update' to check for updates.",
}


def split(output: str) -> tuple[list[str], list[str]]:
    """The run's lines, and the record block it printed after them, RECORD_BEGIN to RECORD_END, or none."""
    lines = output.splitlines()
    if "RECORD_BEGIN" not in lines:
        return lines, []
    start = lines.index("RECORD_BEGIN")
    return lines[:start], lines[start:]


def answering(completed: Completed, calls: list[list[str]]) -> runtime_canary.Runner:
    """A runner that notes each command it is given and answers every one with `completed`."""

    def runner(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        calls.append(arguments)
        return completed

    return runner


class FakeRuntimes:
    """Answers each runtime's skill listing, through talk, and stands in for its model run, when called."""

    def __init__(self) -> None:
        # Each copy a runtime lists: the skill's name, its directory, and whether it is enabled.
        self.listings: dict[str, list[tuple[str, str, bool]]] = {}
        self.actions: dict[str, Callable[[list[str], Path, dict[str, str]], Completed]] = {}
        self.calls: list[tuple[str, list[str], Path, Mapping[str, str]]] = []
        # Each runtime asked for its version, which the fake answers apart from its runs, and the environment it got.
        self.versions: list[str] = []
        self.version_environments: list[Mapping[str, str]] = []

    def talk(
        self,
        arguments: list[str],
        cwd: Path,
        environment: Mapping[str, str],
        requests: list[str],
        answered: Callable[[str], bool] | None,
        timeout: float,
    ) -> list[str]:
        """The listing each runtime prints: Codex names a skill by its SKILL.md file, Copilot by its directory."""
        runtime = Path(arguments[0]).name
        self.calls.append(("listing", arguments, cwd, environment))
        skills = self.listings.get(runtime, [])
        if runtime == "codex":
            entries = [{"name": name, "path": f"{path}/SKILL.md", "enabled": on} for name, path, on in skills]
            return [json.dumps({"id": discovery.CODEX_LIST_ID, "result": {"data": [{"skills": entries}]}})]
        return [json.dumps([{"name": name, "path": path, "enabled": on} for name, path, on in skills])]

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        runtime = Path(arguments[0]).name
        if arguments[1:] == ["--version"]:
            self.versions.append(runtime)
            self.version_environments.append(environment)
            return Completed(0, f"{VERSIONS[Path(runtime).stem]}\n", "")
        self.calls.append(("run", arguments, cwd, environment))
        action = self.actions.get(runtime)
        return action(arguments, cwd, environment) if action else Completed(0, "", "")


def run_python(*arguments: str, timed_out: bool = False) -> Callable[[list[str], Path, dict[str, str]], Completed]:
    """A runtime that runs `python -B <arguments>` the way a shell tool would, then exits 0 or runs out of time."""

    def action(command: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
        process = subprocess.run(
            [sys.executable, "-B", *arguments], cwd=cwd, env=environment, capture_output=True, text=True, check=False
        )
        stdout = json.dumps({"stdout": process.stdout}) + "\n"
        return Completed(-1 if timed_out else 0, stdout, process.stderr, timed_out=timed_out)

    return action


def git_bash() -> str:
    found = platform_support.find_bash()
    if found is None:
        raise AssertionError("Git Bash is required, as tests/run_validation.py states")
    return found


def run_bash(*arguments: str) -> Callable[[list[str], Path, dict[str, str]], Completed]:
    """A runtime that starts Git Bash by its full path with these arguments, as Codex does, then exits 0."""

    def action(command: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
        process = subprocess.run(
            [git_bash(), *arguments], cwd=cwd, env=environment, capture_output=True, text=True, check=False
        )
        return Completed(0, json.dumps({"stdout": process.stdout, "code": process.returncode}) + "\n", process.stderr)

    return action


def stub_program(directory: Path, name: str) -> Path:
    """A runtime program that exits 0 at once, started by its path as the canary starts a runtime: a .cmd file on
    Windows, as npm installs Codex, and an executable shell script elsewhere."""
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        program = directory / f"{name}.cmd"
        program.write_bytes(b"@exit /b 0\r\n")
    else:
        program = directory / name
        program.write_bytes(b"#!/bin/sh\nexit 0\n")
        program.chmod(0o755)
    return program


def launcher(directory: Path) -> tuple[list[str], Path]:
    """A runtime that starts a child and waits for it, as a launcher script starts the runtime proper, and the file
    the child writes its process ID to before it sleeps for a minute, outliving any limit a case sets. The child writes
    the ID beside the file and renames it into place, so the file never exists without the whole ID."""
    started = directory / "child.pid"
    child = directory / "child.py"
    child.write_text(
        "import os, pathlib, time\n"
        f"pending = pathlib.Path({str(started) + '.pending'!r})\n"
        "pending.write_text(str(os.getpid()))\n"
        f"os.replace(pending, {str(started)!r})\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    program = directory / "launcher.py"
    program.write_text(
        f"import subprocess, sys\nsubprocess.run([sys.executable, {str(child)!r}], check=False)\n", encoding="utf-8"
    )
    return [sys.executable, "-B", str(program)], started


# How long a case waits for the launcher's child to write its process ID.
PID_WAIT_SECONDS = 30.0


def child_pid(pid_file: Path, wait: float = PID_WAIT_SECONDS) -> int:
    """The process ID the launcher's child wrote, once the file exists and holds it, or an AssertionError naming the
    wait when it holds none within `wait` seconds."""
    deadline = time.monotonic() + wait
    while True:
        written = pid_file.read_text(encoding="utf-8").strip() if pid_file.is_file() else ""
        if written:
            return int(written)
        if time.monotonic() >= deadline:
            raise AssertionError(f"the launcher's child wrote no process ID to {pid_file} within {wait:g}s")
        time.sleep(0.05)


def running(pid_file: Path) -> bool:
    return platform_support.process_status(child_pid(pid_file)).alive


class RuntimeCanaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="runtime-canary-test.")
        self.root = Path(self._temporary.name).resolve()
        self.home = self.root / "Canary Home (1)"
        self.home.mkdir()
        # The record block the last run printed, which outcome splits from its lines.
        self.recorded: list[str] = []

        def refuse(*_: object, **__: object) -> Completed:
            raise AssertionError("a test started a real runtime")

        for patcher in (
            mock.patch.object(runtime_canary, "run_process", refuse),
            mock.patch.object(discovery, "converse", refuse),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def make_source(self, user_only: bool = False, hidden: bool = False) -> Path:
        """A source whose alpha runs its own scripts/tool.py, whose beta runs alpha's, declared in skill_deps, and
        whose gamma runs its own scripts/tool.sh with Bash. Each declares the runtime support its frontmatter implies.
        With hidden, alpha also declares core, a hidden dependency that declares none for every runtime, as
        code-review-core does, so the deployer gives it no adapter.
        """
        support = runtime_support.declaration({"user-only-start"} if user_only else set())
        source = self.root / "source"
        (source / "deploy-meta").mkdir(parents=True)
        (source / "source.json").write_text(json.dumps({"id": "test/shipped"}), encoding="utf-8")
        flags = "disable-model-invocation: true\n" if user_only else ""
        for name, command, script, metadata in (
            ("alpha", "python -B", "${CLAUDE_SKILL_DIR}/scripts/tool.py", {}),
            ("beta", "python -B", "${CLAUDE_SKILL_DIR}/../alpha/scripts/tool.py", {"skill_deps": ["alpha"]}),
            ("gamma", "bash", "${CLAUDE_SKILL_DIR}/scripts/tool.sh", {}),
        ):
            (source / "skills" / name / "scripts").mkdir(parents=True)
            scripts = f'{command} "{script.rsplit("/", 1)[0]}/*'
            allowed = json.dumps([f"Bash({scripts})", f"PowerShell({scripts})"])
            (source / "skills" / name / "SKILL.md").write_bytes(
                f'---\nname: {name}\ndescription: "Skill {name}"\nallowed-tools: {allowed}\n{flags}---\n\n'
                f'Run `{command} "{script}"`.\n'.encode()
            )
            (source / "deploy-meta" / f"{name}.json").write_text(
                json.dumps({**metadata, "runtime_support": support}), encoding="utf-8"
            )
            if command == "bash":
                (source / "skills" / name / "scripts" / "tool.sh").write_bytes(
                    b"#!/usr/bin/env bash\nprintf 'tool %s\\n' \"$*\"\nexit 3\n"
                )
            else:
                (source / "skills" / name / "scripts" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
        if hidden:
            (source / "skills" / "core").mkdir()
            (source / "skills" / "core" / "SKILL.md").write_bytes(
                b"---\nname: core\ndescription: Internal support.\ndisable-model-invocation: true\n"
                b"user-invocable: false\n---\n\n# Core\n"
            )
            none = {"level": "none", "needs": [], "reason": HIDDEN_REASON}
            (source / "deploy-meta" / "core.json").write_text(
                json.dumps({"selectable": False, "runtime_support": dict.fromkeys(runtime_support.RUNTIMES, none)}),
                encoding="utf-8",
            )
            alpha = source / "deploy-meta" / "alpha.json"
            alpha.write_text(
                json.dumps({**json.loads(alpha.read_text(encoding="utf-8")), "skill_deps": ["core"]}), encoding="utf-8"
            )
        return source

    def canary(
        self,
        runner: FakeRuntimes,
        skills: Sequence[str] = (FIXTURE,),
        runtimes: tuple[str, ...] = ("codex",),
        sources: list[Path] | None = None,
        environment: dict[str, str] | None = None,
        installed: tuple[str, ...] = runtime_canary.RUNTIMES,
        discovery_only: bool = False,
        which: Callable[[str], str | None] | None = None,
        **options: str,
    ) -> list[str]:
        code, lines = self.outcome(
            runner, skills, runtimes, sources, environment, installed, discovery_only, which, **options
        )
        # A deployment that stops exits 1 too, but it prints none of these lines, so it fails here.
        # A runtime is checked by a listing that found or missed the canary's copy, or by a run the canary started.
        checked = any(
            line.startswith(("DISCOVERED ", "UNDISCOVERED "))
            or (line.startswith("RUNTIME ") and line.split()[3] not in ("NOT_ATTEMPTED", "UNSUPPORTED"))
            for line in lines
        )
        failed = not checked or any(
            line.startswith(("UNDISCOVERED ", "DISCOVERY_FAILED "))
            or (line.startswith("MATRIX ") and " DISAGREES " in line)
            for line in lines
        )
        self.assertEqual(1 if failed else 0, code, "\n".join(lines))
        return lines

    def outcome(
        self,
        runner: FakeRuntimes,
        skills: Sequence[str] = (FIXTURE,),
        runtimes: tuple[str, ...] = ("codex",),
        sources: list[Path] | None = None,
        environment: dict[str, str] | None = None,
        installed: tuple[str, ...] = runtime_canary.RUNTIMES,
        discovery_only: bool = False,
        which: Callable[[str], str | None] | None = None,
        **options: str,
    ) -> tuple[int, list[str]]:
        """The canary's exit code and printed lines, for a test that states the code it expects."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime_canary.canary(
                list(skills),
                list(runtimes),
                self.home,
                sources=sources if sources is not None else [runtime_canary.FIXTURE_SOURCE],
                runner=runner,
                talk=runner.talk,
                which=which or (lambda name: forward(self.root / "bin" / name) if name in installed else None),
                base_environment=dict(os.environ) if environment is None else environment,
                discovery_only=discovery_only,
                timeout=60,
                **options,
            )
        lines, self.recorded = split(output.getvalue())
        # Every run that ends prints its record last.
        self.assertEqual(["RECORD_BEGIN", "RECORD_END"], [self.recorded[0], self.recorded[-1]], output.getvalue())
        return code, lines

    def skill_dir(self, name: str = FIXTURE) -> str:
        return forward(self.home / ".claude" / "skills" / name)

    def kept(self, runtime: str) -> str:
        return f"KEPT {json.dumps(runtime_canary.KEPT[runtime])}"


class DeploymentTests(RuntimeCanaryTestCase):
    def test_the_fixture_deploys_into_a_canary_home_with_an_adapter_that_names_its_directory(self) -> None:
        code, log = runtime_canary.deploy(self.home, runtime_canary.FIXTURE_SOURCE)
        self.assertEqual(0, code, log)
        self.assertTrue((self.home / ".claude" / "skills" / FIXTURE / "scripts" / "canary_probe.py").is_file())
        adapter = (self.home / ".agents" / "skills" / FIXTURE / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"`${{CLAUDE_SKILL_DIR}}` stands for `{self.skill_dir()}`", adapter)
        self.assertTrue((self.home / ".deploy-canary-home").is_file())
        # The probe is started by the platform's Python command, in its grants and its fence alike.
        skill = (self.home / ".claude" / "skills" / FIXTURE / "SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("{{", skill)
        for line in (
            f'allowed-tools: ["Bash({PYTHON} -B \\"${{CLAUDE_SKILL_DIR}}/scripts/canary_probe.py\\" *)", '
            f'"PowerShell({PYTHON} -B \\"${{CLAUDE_SKILL_DIR}}/scripts/canary_probe.py\\" *)"]',
            f'{PYTHON} -B "${{CLAUDE_SKILL_DIR}}/scripts/canary_probe.py"',
        ):
            self.assertIn(line, skill.splitlines())

    def test_the_repository_and_the_fixture_deploy_side_by_side(self) -> None:
        for source in (REPOSITORY_ROOT, runtime_canary.FIXTURE_SOURCE):
            code, log = runtime_canary.deploy(self.home, source)
            self.assertEqual(0, code, log)
        manifest = json.loads((self.home / ".claude" / "skills" / ".deploy-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({"ejryan89/coding-agent-skills", "test/runtime-canary"}, set(manifest["sources"]))
        self.assertEqual(
            forward(REPOSITORY_ROOT), forward(manifest["sources"]["ejryan89/coding-agent-skills"]["source_dir"])
        )
        # Every root, opt-in ones too, so any shipped skill can be canaried.
        self.assertTrue((self.home / ".claude" / "skills" / "dotnet-format" / "SKILL.md").is_file())
        # The hidden dependencies are deployed with no adapter, and they are the skills the canary asks no runtime
        # to list; every other skill has its adapter.
        unadapted = runtime_canary.without_adapter(self.home)
        self.assertLessEqual({"code-review-core", "skill-core"}, set(unadapted))
        self.assertEqual({NO_ADAPTER}, set(unadapted.values()))
        for name in ("code-review-core", "skill-core"):
            self.assertTrue((self.home / ".claude" / "skills" / name / "SKILL.md").is_file())
            self.assertFalse((self.home / ".agents" / "skills" / name).exists())
        for name in ("review-prs", "dotnet-format", FIXTURE):
            self.assertNotIn(name, unadapted)
            self.assertTrue((self.home / ".agents" / "skills" / name / "SKILL.md").is_file())

    def test_the_probe_records_where_it_ran_from_its_own_location(self) -> None:
        self.assertEqual(0, runtime_canary.deploy(self.home, runtime_canary.FIXTURE_SOURCE)[0])
        elsewhere = self.root / "Somewhere Else"
        elsewhere.mkdir()
        script = self.home / ".claude" / "skills" / FIXTURE / "scripts" / "canary_probe.py"
        (self.home / ".runtime-canary").mkdir()
        (self.home / ".runtime-canary" / "run-id").write_text("run one\n", encoding="utf-8")
        process = subprocess.run(
            [sys.executable, "-B", str(script), "--help", "two words"],
            cwd=elsewhere,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(f"RUNTIME_CANARY_PROBE RAN {forward(script)}\n", process.stdout)
        records = runtime_canary.read_records(self.home / ".runtime-canary" / "probe.jsonl")
        self.assertEqual(
            [
                {
                    "script": forward(script),
                    "cwd": forward(elsewhere),
                    "arguments": ["--help", "two words"],
                    "run": "run one",
                }
            ],
            records,
        )

    def test_unknown_skills_are_refused_before_a_home_is_made(self) -> None:
        output = io.StringIO()
        with (
            mock.patch("tempfile.mkdtemp", side_effect=AssertionError("made a home")),
            contextlib.redirect_stderr(output),
            self.assertRaises(SystemExit) as raised,
        ):
            runtime_canary.main(["review-prs", "no-such-skill"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("unknown skill: no-such-skill", output.getvalue())


class LookupTests(unittest.TestCase):
    def test_a_runtime_is_looked_up_where_check_and_verify_look_for_it(self) -> None:
        # skill-core's locator through the deployer, so a runtime only in WinGet's Links folder is checked too.
        self.assertIs(
            platform_support.find_executable, inspect.signature(runtime_canary.canary).parameters["which"].default
        )


# git's repository-selecting variables, written out here rather than read from skill-core.
REPOSITORY_SELECTING = (
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_CONFIG",
    "GIT_DIR",
    "GIT_GRAFT_FILE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_NAMESPACE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_OBJECT_DIRECTORY",
    "GIT_PREFIX",
    "GIT_REPLACE_REF_BASE",
    "GIT_SHALLOW_FILE",
    "GIT_WORK_TREE",
)


class EnvironmentTests(unittest.TestCase):
    def test_a_runtime_sees_no_repository_selecting_variable_the_shell_exported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            base = {**os.environ, **{name: "C:/elsewhere/.git" for name in REPOSITORY_SELECTING}}
            base.update({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.scope", "GIT_CONFIG_VALUE_0": "on"})
            environment = runtime_canary.environment(base, home / "scripts.jsonl", home)
            program = "import json, os; print(json.dumps(sorted(name.upper() for name in os.environ)))"
            completed = runtime_canary.run_process([sys.executable, "-c", program], home, environment, 60)
        self.assertEqual(0, completed.returncode, completed.stderr)
        seen = set(json.loads(completed.stdout))
        self.assertEqual(set(), set(REPOSITORY_SELECTING) & seen)
        self.assertLessEqual({"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "PYTHONPATH"}, seen)


class RunProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="runtime-canary-test.")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve()

    def test_a_runtime_started_through_a_launcher_ends_at_the_limit_with_everything_it_started(self) -> None:
        command, started = launcher(self.root)
        began = time.monotonic()
        completed = RUN_PROCESS(command, self.root, dict(os.environ), 5)
        self.assertLess(time.monotonic() - began, 30, "the run waited for the child the launcher started")
        self.assertTrue(completed.timed_out, completed)
        self.assertTrue(started.is_file(), "the launcher's child never started within the limit")
        self.assertFalse(running(started), "the child the launcher started outlived the run")

    def test_an_interrupted_run_ends_everything_the_runtime_started(self) -> None:
        command, started = launcher(self.root)
        limit = 120.0
        real_wait = subprocess.Popen.wait
        interrupted_runs: list[subprocess.Popen[bytes]] = []

        def interrupted(process: subprocess.Popen[bytes], timeout: float | None = None) -> int:
            """Ctrl+C while the canary waits for the run, once the launcher's child is running."""
            if timeout != limit:
                return real_wait(process, timeout=timeout)
            interrupted_runs.append(process)
            deadline = time.monotonic() + 30
            while not started.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            raise KeyboardInterrupt

        with mock.patch.object(subprocess.Popen, "wait", interrupted), self.assertRaises(KeyboardInterrupt):
            RUN_PROCESS(command, self.root, dict(os.environ), limit)
        self.assertTrue(started.is_file(), "the launcher's child never started")
        self.assertFalse(running(started), "the child the launcher started outlived the interrupted run")
        # The launcher went with it, so it is reported gone at once.
        self.assertEqual(1, len(interrupted_runs))
        real_wait(interrupted_runs[0], timeout=1)

    def test_an_empty_pid_file_is_a_named_wait_rather_than_a_parse_error(self) -> None:
        # The file a child has created and not yet written, which a non-atomic write leaves for a moment.
        started = self.root / "child.pid"
        started.write_text("", encoding="utf-8")
        began = time.monotonic()
        with self.assertRaisesRegex(AssertionError, r"^the launcher's child wrote no process ID to .+ within 0\.2s$"):
            child_pid(started, 0.2)
        self.assertGreaterEqual(time.monotonic() - began, 0.2, "the read did not wait for the ID")

    def test_a_program_that_cannot_start_is_a_run_that_says_why(self) -> None:
        missing = self.root / "bin" / "copilot.exe"
        completed = RUN_PROCESS([str(missing), "-p", "x"], self.root, dict(os.environ), 60)
        self.assertEqual(
            (-1, "", "", False), (completed.returncode, completed.stdout, completed.stderr, completed.timed_out)
        )
        self.assertRegex(completed.error, f"^cannot run {re.escape(forward(missing))}: .+")

    def test_output_reads_back_as_text_with_line_feeds(self) -> None:
        program = "import sys; sys.stdout.buffer.write(b'one\\r\\ntwo\\n\\xff'); sys.stderr.write('warn')"
        completed = RUN_PROCESS([sys.executable, "-c", program], self.root, dict(os.environ), 60)
        self.assertEqual(Completed(0, "one\ntwo\n\ufffd", "warn"), completed)


def remove_home(*arguments: str) -> tuple[int, str]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = runtime_canary.main(["--remove-home", *arguments])
    return code, output.getvalue()


class RemoveHomeTests(unittest.TestCase):
    # The skill deletes the home through the script, which its allowed-tools already grant, rather than through a
    # prefix grant on rm that would approve deleting any directory.
    def test_a_home_the_canary_made_is_removed(self) -> None:
        home = runtime_canary.create_home()
        runtime_canary.claim_home(home)
        (home / ".runtime-canary" / "transcripts").mkdir(parents=True)
        (home / ".runtime-canary" / "transcripts" / "claude.txt").write_text("a run\n", encoding="utf-8")
        code, output = remove_home(str(home))
        self.assertEqual((0, f'REMOVED "{home.as_posix()}"\n'), (code, output))
        self.assertFalse(home.exists())

    def test_any_other_directory_is_refused_and_kept(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-canary-") as directory:
            root = Path(directory)
            nested = root / "runtime-canary-nested"
            nested.mkdir()
            with tempfile.TemporaryDirectory(prefix="not-a-canary-") as other:
                for candidate in (nested, Path(other), root / "runtime-canary-missing"):
                    with self.subTest(candidate=candidate.name):
                        code, output = remove_home(str(candidate))
                        self.assertEqual(1, code)
                        self.assertRegex(output, r'^FAILED ".+ is not a home the canary made under .+"\n$')
                self.assertTrue(Path(other).is_dir())
            self.assertTrue(nested.is_dir())

    def test_a_home_whose_claim_cannot_be_judged_for_links_is_not_claimed_and_is_kept(self) -> None:
        home = runtime_canary.create_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        runtime_canary.claim_home(home)
        state = home / ".runtime-canary"
        real = os.lstat

        def lstat(target: Any, *rest: Any, **options: Any) -> os.stat_result:
            if Path(os.fsdecode(target)) == state:
                raise PermissionError(13, "Access is denied", os.fspath(state))
            return real(target, *rest, **options)

        with mock.patch.object(platform_support.os, "lstat", side_effect=lstat):
            self.assertFalse(runtime_canary.claimed(home))
            code, output = remove_home(str(home))
        self.assertEqual(1, code)
        self.assertRegex(
            output, r'^FAILED ".+ holds no \.runtime-canary/home\.json naming it, so the canary did not make it"\n$'
        )
        self.assertTrue((state / "home.json").is_file())

    def test_remove_home_takes_no_skill(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            runtime_canary.main(["--remove-home", "somewhere", "review-prs"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("--remove-home takes no SKILL", output.getvalue())


class RunTests(RuntimeCanaryTestCase):
    def test_a_runtime_that_runs_the_fixture_is_reported_with_the_marker_it_wrote(self) -> None:
        runner = FakeRuntimes()
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        runner.actions["codex"] = run_python(script, "--help")
        lines = self.canary(runner)
        self.assertIn(
            f'RUNTIME codex {FIXTURE} RAN "{script}" "{forward(self.home)}"{PYTHON_EXIT} {self.kept("codex")}', lines
        )
        transcript = self.home / ".runtime-canary" / "transcripts" / f"codex-{FIXTURE}.jsonl"
        self.assertIn(f'TRANSCRIPT codex {FIXTURE} "{forward(transcript)}"', lines)
        self.assertIn("RUNTIME_CANARY_PROBE RAN", transcript.read_text(encoding="utf-8"))

    def test_a_shipped_skill_is_reported_ran_from_the_python_recorder(self) -> None:
        runner = FakeRuntimes()
        runner.actions["copilot"] = run_python(f"{self.skill_dir('alpha')}/scripts/tool.py", "--help")
        lines = self.canary(
            runner, ["alpha"], ("copilot",), sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()]
        )
        self.assertIn(
            f'RUNTIME copilot alpha RAN "{self.skill_dir("alpha")}/scripts/tool.py" "{forward(self.home)}"'
            f"{PYTHON_EXIT} {self.kept('copilot')}",
            lines,
        )

    def test_a_script_from_a_declared_sibling_counts_and_an_undeclared_one_does_not(self) -> None:
        sibling = f"{self.skill_dir('alpha')}/scripts/tool.py"
        runner = FakeRuntimes()
        runner.actions["codex"] = run_python(sibling, "--help")
        lines = self.canary(runner, ["beta", "alpha"], sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()])
        self.assertIn(
            f'RUNTIME codex beta RAN "{sibling}" "{forward(self.home)}"{PYTHON_EXIT} {self.kept("codex")}', lines
        )
        self.assertEqual(
            f'FAILED "ran {sibling} instead of a script under {self.skill_dir()}"',
            runtime_canary.verdict(
                FIXTURE,
                self.home,
                probes=[],
                started=[{"argv": [sibling], "cwd": forward(self.home)}],
                completed=Completed(0, "", ""),
                denials=[],
                timeout=60,
            ),
        )

    def test_the_installed_copy_is_never_reported_ran(self) -> None:
        installed = self.root / "profile" / ".claude" / "skills"
        script = forward(installed / "alpha" / "scripts" / "tool.py")
        self.assertEqual(
            f'FAILED "ran the installed copy {script}, not the canary copy"',
            runtime_canary.verdict(
                "alpha",
                self.home,
                probes=[],
                started=[{"argv": [script], "cwd": "x"}],
                completed=Completed(0, "", ""),
                denials=[],
                timeout=60,
                installed=installed,
            ),
        )

    def test_a_script_run_from_another_path_is_a_wrong_path_failure(self) -> None:
        runner = FakeRuntimes()
        wrong = forward(self.root / "elsewhere" / FIXTURE / "scripts" / "canary_probe.py")
        # A script there that exists, since one that does not never started.
        Path(wrong).parent.mkdir(parents=True)
        Path(wrong).write_text("print('elsewhere')\n", encoding="utf-8")
        runner.actions["codex"] = run_python(wrong, "--help")
        lines = self.canary(runner)
        self.assertIn(
            f'RUNTIME codex {FIXTURE} FAILED "ran {wrong} instead of a script under {self.skill_dir()}" '
            f"{self.kept('codex')}",
            lines,
        )

    def test_the_right_script_without_its_marker_is_reported(self) -> None:
        reason = runtime_canary.verdict(
            FIXTURE,
            self.home,
            probes=[],
            started=[{"argv": [f"{self.skill_dir()}/scripts/canary_probe.py"], "cwd": forward(self.home)}],
            completed=Completed(0, "", ""),
            denials=[],
            timeout=60,
        )
        self.assertEqual(
            f'FAILED "ran {self.skill_dir()}/scripts/canary_probe.py but it wrote no marker; was the write denied?"',
            reason,
        )

    def test_a_ran_line_carries_the_recorded_exit_and_a_timeout_and_still_passes(self) -> None:
        script = f"{self.skill_dir('alpha')}/scripts/tool.py"
        start = {"argv": [script], "cwd": forward(self.home), "pid": 41}
        cases: tuple[tuple[list[dict], Completed, str], ...] = (
            ([start, {**start, "exit": 1}], Completed(0, "", ""), " exit=1"),
            ([start, {**start, "exit": 1}], Completed(-1, "", "", timed_out=True), " exit=1 timed-out"),
            # The session ran out of time while the script still ran, so no exit was recorded.
            ([start], Completed(-1, "", "", timed_out=True), " timed-out"),
            # An exit pairs only with the start record it repeats.
            ([start, {**start, "pid": 42, "exit": 1}], Completed(0, "", ""), ""),
        )
        for started, completed, suffix in cases:
            with self.subTest(suffix=suffix):
                outcome = runtime_canary.verdict(
                    "alpha", self.home, probes=[], started=started, completed=completed, denials=[], timeout=60
                )
                self.assertEqual(f'RAN "{script}" "{forward(self.home)}"{suffix}', outcome)
                # The pass rule is unchanged: the runtime started the right copy.
                declared = {"alpha": {"codex": runtime_support.Support("full")}}
                line = f'RUNTIME codex alpha {outcome} KEPT "k"'
                self.assertEqual(["MATRIX codex alpha AGREES full"], runtime_canary.compare([line], declared))

    def test_a_bash_script_that_exits_nonzero_before_a_timeout_is_ran_with_both(self) -> None:
        runner = FakeRuntimes()
        bash = run_bash(f"{self.skill_dir('gamma')}/scripts/tool.sh", "--help")

        def timed_out(command: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
            return Completed(-1, bash(command, cwd, environment).stdout, "", timed_out=True)

        runner.actions["codex"] = timed_out
        lines = self.canary(runner, ["gamma"], sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()])
        self.assertIn(
            f'RUNTIME codex gamma RAN "{self.skill_dir("gamma")}/scripts/tool.sh" "{forward(self.home)}" exit=3 '
            f"timed-out {self.kept('codex')}",
            lines,
        )
        self.assertIn("MATRIX codex gamma AGREES full", lines)

    def test_the_fixture_must_exit_0_beside_its_marker(self) -> None:
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        start = {"argv": [script, "--help"], "cwd": forward(self.home), "pid": 7}
        for status, outcome in (
            (2, f'FAILED "ran {script} but it exited with code 2"'),
            (0, f'RAN "{script}" "{forward(self.home)}" exit=0'),
            (None, f'RAN "{script}" "{forward(self.home)}"'),
        ):
            with self.subTest(status=status):
                started = [start] if status is None else [start, {**start, "exit": status}]
                self.assertEqual(
                    outcome,
                    runtime_canary.verdict(
                        FIXTURE,
                        self.home,
                        probes=[{"script": script, "cwd": forward(self.home)}],
                        started=started,
                        completed=Completed(0, "", ""),
                        denials=[],
                        timeout=60,
                    ),
                )

    def test_the_python_recorder_records_how_each_script_exited(self) -> None:
        self.canary(FakeRuntimes(), discovery_only=True)
        log = self.home / ".runtime-canary" / "scripts" / "direct.jsonl"
        environment = runtime_canary.environment(dict(os.environ), log, self.home)
        scripts = {
            "raises SystemExit(3).py": ("raise SystemExit(3)\n", 3),
            "fails at import.py": ("import runtime_canary_no_such_module\n", 1),
            "calls sys.exit(5).py": ("import sys\n\ndef main():\n    sys.exit(5)\n\nmain()\n", 5),
            "catches its own error.py": ("try:\n    raise KeyError\nexcept KeyError:\n    pass\nprint('ok')\n", 0),
            # Python opened it but never started it, so nothing unwound, and it exits 1 all the same.
            "does not compile.py": ("def broken(:\n", 1),
        }
        for name, (text, status) in scripts.items():
            with self.subTest(script=name):
                log.write_text("", encoding="utf-8")
                path = self.root / name
                path.write_text(text, encoding="utf-8")
                process = subprocess.run(
                    [sys.executable, "-B", str(path)], cwd=self.root, env=environment, capture_output=True, check=False
                )
                self.assertEqual(status, process.returncode)
                # sys.monitoring, which reports the exit, arrived in Python 3.12; before it the start alone is kept.
                recorded = status if sys.version_info >= (3, 12) else None
                self.assertEqual(
                    [(forward(path), forward(self.root), recorded)],
                    runtime_canary.recorded_scripts(runtime_canary.read_records(log)),
                )

    def test_the_python_recorder_counts_no_script_for_a_missing_file_a_folder_or_a_module(self) -> None:
        self.canary(FakeRuntimes(), discovery_only=True)
        log = self.home / ".runtime-canary" / "scripts" / "direct.jsonl"
        environment = runtime_canary.environment(dict(os.environ), log, self.home)
        scripts = Path(self.skill_dir()) / "scripts"
        (scripts / "a folder.py").mkdir()
        (scripts / "canary_module.py").write_text("print('module')\n", encoding="utf-8")
        cases = {
            "a missing file": ([str(scripts / "missing.py")], self.home, 2),
            "a folder": ([str(scripts / "a folder.py")], self.home, 1),
            # A module that runs from the skill's folder is still no script file, as a command string is not.
            "a module": (["-m", "canary_module"], scripts, 0),
            "a command string": (["-c", "print('c')"], scripts, 0),
        }
        for name, (arguments, cwd, status) in cases.items():
            with self.subTest(case=name):
                log.write_text("", encoding="utf-8")
                process = subprocess.run(
                    [sys.executable, "-B", *arguments], cwd=cwd, env=environment, capture_output=True, check=False
                )
                self.assertEqual(status, process.returncode, process.stderr)
                started = runtime_canary.read_records(log)
                self.assertEqual([], started)
                self.assertEqual(
                    'FAILED "never ran a script from the skill"',
                    runtime_canary.verdict(
                        FIXTURE,
                        self.home,
                        probes=[],
                        started=started,
                        completed=Completed(0, "", ""),
                        denials=[],
                        timeout=60,
                    ),
                )

    def test_a_marker_without_the_runs_identifier_does_not_count(self) -> None:
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        probe_log = self.home / ".runtime-canary" / "probe.jsonl"
        for name, extra in (("no identifier", {}), ("another run's identifier", {"run": "an earlier run"})):
            with self.subTest(case=name):
                # The runtime's shell appends a marker line itself and never starts the probe.
                forged = json.dumps({"script": script, "cwd": forward(self.home), "arguments": [], **extra})

                def append(command: list[str], cwd: Path, environment: dict[str, str], line: str = forged) -> Completed:
                    with probe_log.open("a", encoding="utf-8") as handle:
                        handle.write(line + "\n")
                    return Completed(0, "", "")

                runner = FakeRuntimes()
                runner.actions["codex"] = append
                code, lines = self.outcome(runner)
                self.assertEqual(1, code, lines)
                self.assertIn(
                    f'RUNTIME codex {FIXTURE} FAILED "never ran a script from the skill" {self.kept("codex")}', lines
                )
        # The probe itself copies the identifier the canary wrote for its run, which is new each run.
        runner = FakeRuntimes()
        runner.actions["codex"] = run_python(script, "--help")
        identifiers = []
        for _ in range(2):
            self.assertIn(f'RUNTIME codex {FIXTURE} RAN "{script}"', "\n".join(self.canary(runner)))
            identifiers.append(runtime_canary.read_records(probe_log)[0]["run"])
            self.assertEqual(identifiers[-1], (self.home / ".runtime-canary" / "run-id").read_text(encoding="utf-8"))
        self.assertNotEqual(identifiers[0], identifiers[1])

    # Codex is given a Windows sandbox mode only on Windows; the case holds on every platform as Windows runs it.
    @mock.patch.object(runtime_canary, "CODEX_HAS_WINDOWS_SANDBOX", True)
    def test_a_command_the_codex_policy_refused_is_blocked_not_failed(self) -> None:
        runner = FakeRuntimes()
        runner.actions["codex"] = lambda *_: Completed(
            0, "", "Reading additional input from stdin...\n" + CODEX_REJECTION
        )
        lines = self.canary(runner)
        self.assertIn(
            f'RUNTIME codex {FIXTURE} BLOCKED "blocked by policy; the runtime refused 1 command before any '
            f"script ran; Codex ran with windows.sandbox=elevated, so its Windows sandbox likely did not "
            f'start; see docs/codex-support.md#windows-sandbox-mode" {self.kept("codex")}',
            lines,
        )
        # The section the line points at exists.
        self.assertIn(
            "\n## Windows sandbox mode\n", (REPOSITORY_ROOT / "docs" / "codex-support.md").read_text(encoding="utf-8")
        )
        # A script that ran outranks a refusal, and other runtimes' output is never read as Codex's.
        self.assertEqual([], runtime_canary.rejections("copilot", Completed(0, "", CODEX_REJECTION)))
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        self.assertTrue(
            runtime_canary.verdict(
                FIXTURE,
                self.home,
                probes=[{"script": script, "cwd": "x"}],
                started=[],
                completed=Completed(0, "", ""),
                denials=[],
                timeout=60,
                rejected=["blocked by policy"],
            ).startswith("RAN ")
        )

    def test_the_canary_creates_each_record_file_empty_before_the_run_appends_to_it(self) -> None:
        # A file the Codex Windows sandbox creates belongs to its sandbox user, and the canary cannot read it back.
        state = self.home / ".runtime-canary"
        found: list[tuple[str, int]] = []

        def inspect(command: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
            for path in (state / "probe.jsonl", Path(environment["RUNTIME_CANARY_SCRIPT_LOG"])):
                found.append((path.name, path.stat().st_size))
            return run_python(f"{self.skill_dir()}/scripts/canary_probe.py", "--help")(command, cwd, environment)

        runner = FakeRuntimes()
        runner.actions["codex"] = inspect
        lines = self.canary(runner)
        self.assertIn(
            f'RUNTIME codex {FIXTURE} RAN "{self.skill_dir()}/scripts/canary_probe.py"',
            next(line for line in lines if line.startswith("RUNTIME codex ")),
        )
        # A later run starts from empty files again, whatever the last one left.
        self.assertTrue((state / "probe.jsonl").read_text(encoding="utf-8"))
        runtime_canary.run_skill("codex", "codex", FIXTURE, self.home, dict(os.environ), runner, 60, [FIXTURE])
        self.assertEqual([("probe.jsonl", 0), (f"codex-{FIXTURE}.jsonl", 0)] * 2, found)

    def test_a_record_the_canary_cannot_read_fails_that_run_and_the_next_runtime_still_runs(self) -> None:
        runner = FakeRuntimes()
        real = runtime_canary.read_records

        def denied(path: Path) -> list[dict]:
            if path.name == "probe.jsonl" and "codex" in str(runner.calls[-1][1][0]):
                raise PermissionError(13, "Permission denied", str(path))
            return real(path)

        with mock.patch.object(runtime_canary, "read_records", denied):
            code, lines = self.outcome(runner, runtimes=("codex", "copilot"))
        self.assertEqual(1, code, lines)
        probe = forward(self.home / ".runtime-canary" / "probe.jsonl")
        self.assertIn(
            f'RUNTIME codex {FIXTURE} FAILED "cannot read what the run recorded in {probe}: '
            f'Permission denied" {self.kept("codex")}',
            lines,
        )
        self.assertTrue([line for line in lines if line.startswith(f"RUNTIME copilot {FIXTURE} ")])

    def test_a_run_without_any_script_is_a_never_ran_failure_with_its_cause(self) -> None:
        cases: tuple[tuple[Completed, list[str], str], ...] = (
            (Completed(0, "", ""), [], "never ran a script from the skill"),
            (Completed(3, "", ""), [], "exited with code 3 before running a script from the skill"),
            (
                Completed(1, "", "", timed_out=True),
                [],
                "timed out after 60 seconds before running a script from the skill",
            ),
            (
                Completed(0, "", ""),
                ["Bash: python -B x.py"],
                "never ran a script from the skill; denied: Bash: python -B x.py",
            ),
        )
        for completed, denials, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(
                    f'FAILED "{reason}"',
                    runtime_canary.verdict(
                        FIXTURE,
                        self.home,
                        probes=[],
                        started=[{"argv": ["-c"], "cwd": "x"}],
                        completed=completed,
                        denials=denials,
                        timeout=60,
                    ),
                )

    def test_a_runtime_whose_program_vanishes_fails_that_skill_and_every_other_line_stands(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        program = stub_program(self.root / "bin", "copilot")
        runner = FakeRuntimes()
        runner.listings[program.name] = [(name, f"{adapters}/{name}", True) for name in (FIXTURE, "alpha")]

        def update_moves_it_away(arguments: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
            # Copilot's update renames its program once a session has finished, so the next start finds none.
            completed = RUN_PROCESS(arguments, cwd, environment, 60)
            program.unlink(missing_ok=True)
            return completed

        runner.actions[program.name] = update_moves_it_away
        lines = self.canary(
            runner,
            ["alpha"],
            ("copilot",),
            sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()],
            which=lambda name: forward(program) if name == "copilot" else None,
        )
        self.assertIn(f'DISCOVERED copilot alpha "{adapters}/alpha"', lines)
        self.assertIn(
            f'RUNTIME copilot {FIXTURE} FAILED "never ran a script from the skill" {self.kept("copilot")}', lines
        )
        failed = [line for line in lines if line.startswith("RUNTIME copilot alpha ")]
        self.assertEqual(1, len(failed), lines)
        self.assertRegex(failed[0], f'^RUNTIME copilot alpha FAILED "cannot run {re.escape(forward(program))}: .+"')
        self.assertIn(
            f'TRANSCRIPT copilot alpha "{forward(self.home)}/.runtime-canary/transcripts/copilot-alpha.jsonl"', lines
        )
        for skill in (FIXTURE, "alpha"):
            self.assertTrue([line for line in lines if line.startswith(f"MATRIX copilot {skill} DISAGREES ")], lines)

    def test_a_run_that_fails_unexpectedly_loses_no_line_already_made(self) -> None:
        runner = FakeRuntimes()
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        ran = run_python(script, "--help")

        def fails_on_the_second_skill(arguments: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
            if FIXTURE not in arguments[-1]:
                raise RuntimeError("stub runtime broke")
            return ran(arguments, cwd, environment)

        runner.actions["codex"] = fails_on_the_second_skill
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaisesRegex(RuntimeError, "stub runtime broke"):
            runtime_canary.canary(
                [FIXTURE, "alpha"],
                ["codex"],
                self.home,
                sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()],
                runner=runner,
                talk=runner.talk,
                which=lambda name: forward(self.root / "bin" / name),
                base_environment=dict(os.environ),
                timeout=60,
            )
        lines = output.getvalue().splitlines()
        self.assertTrue([line for line in lines if line.startswith(f"RUNTIME codex {FIXTURE} RAN ")], lines)
        self.assertTrue([line for line in lines if line.startswith(f"TRANSCRIPT codex {FIXTURE} ")], lines)

    def test_a_permission_denial_of_any_shape_is_named_from_what_it_holds(self) -> None:
        cases: tuple[tuple[object, list[str]], ...] = (
            (["Bash"], ['?: "Bash"']),
            ([{"tool_name": "Bash", "tool_input": "ls"}], ['Bash: "ls"']),
            ([{"tool_name": "Bash", "tool_input": {"command": 3}}], ['Bash: {"command": 3}']),
            ([{"tool_name": "Bash", "tool_input": {"command": "ls"}}], ["Bash: ls"]),
            ({"tool_name": "Bash"}, []),
        )
        for listed, denials in cases:
            with self.subTest(listed=listed):
                stream = json.dumps({"type": "result", "permission_denials": listed})
                self.assertEqual((None, denials), runtime_canary.claude_stream(stream))
        init = json.dumps({"type": "system", "subtype": "init", "skills": 5})
        self.assertEqual((set(), []), runtime_canary.claude_stream(init))

    def test_claude_discovery_and_permission_denials_come_from_its_stream(self) -> None:
        runner = FakeRuntimes()
        stream = "\n".join(
            json.dumps(event)
            for event in (
                {"type": "system", "subtype": "init", "skills": [FIXTURE, "other"], "slash_commands": []},
                {
                    "type": "result",
                    "subtype": "success",
                    "permission_denials": [
                        {"tool_name": "Bash", "tool_input": {"command": "python -B probe.py --help"}}
                    ],
                },
            )
        )
        runner.actions["claude"] = lambda *_: Completed(0, stream + "\n", "")
        lines = self.canary(runner, runtimes=("claude",))
        self.assertIn(f"DISCOVERED claude {FIXTURE}", lines)
        self.assertIn(
            f'RUNTIME claude {FIXTURE} FAILED "never ran a script from the skill; '
            f'denied: Bash: python -B probe.py --help" {self.kept("claude")}',
            lines,
        )

    # Codex is given a Windows sandbox mode only on Windows; the case holds on every platform as Windows runs it.
    @mock.patch.object(runtime_canary, "CODEX_HAS_WINDOWS_SANDBOX", True)
    def test_each_run_keeps_its_sign_in_and_drops_ambient_configuration(self) -> None:
        runner = FakeRuntimes()
        base = {**os.environ, "PYTHONPATH": "existing", "HOME": "real-home-left-alone"}
        self.canary(runner, runtimes=runtime_canary.RUNTIMES, environment=base)
        runs = {
            Path(arguments[0]).name: (arguments, cwd, environment)
            for kind, arguments, cwd, environment in runner.calls
            if kind == "run"
        }
        self.assertEqual(set(runtime_canary.RUNTIMES), set(runs))
        recorder = self.home / ".runtime-canary" / "recorder"
        for runtime, (_, cwd, environment) in runs.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(self.home, cwd)
                # The real configuration folders, and so the sign-ins, are left where they are.
                self.assertEqual("real-home-left-alone", environment["HOME"])
                for variable in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "COPILOT_HOME"):
                    self.assertEqual(base.get(variable), environment.get(variable))
                self.assertEqual(f"{recorder}{os.pathsep}existing", environment["PYTHONPATH"])
                self.assertEqual(forward(recorder / "bash_env.sh"), environment["BASH_ENV"])
                self.assertTrue(environment["RUNTIME_CANARY_SCRIPT_LOG"].startswith(str(self.home)))
        # Git Bash would read a carriage return as part of a command.
        self.assertNotIn(b"\r", (recorder / "bash_env.sh").read_bytes())
        claude, codex, copilot = (runs[name][0] for name in ("claude", "codex", "copilot"))
        self.assertEqual(
            [
                "-p",
                f"/{FIXTURE} {runtime_canary.INSTRUCTION}",
                "--output-format",
                "stream-json",
                "--verbose",
                "--setting-sources",
                "project,local",
                "--strict-mcp-config",
                "--no-session-persistence",
            ],
            claude[1:],
        )
        self.assertEqual(
            [
                "exec",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "workspace-write",
                "-c",
                "approval_policy=never",
                "-c",
                "windows.sandbox=elevated",
                "--json",
                "-C",
                str(self.home),
                f"${FIXTURE} {runtime_canary.INSTRUCTION}",
            ],
            codex[1:],
        )
        self.assertEqual(
            [
                "--no-custom-instructions",
                "--no-ask-user",
                "--no-remote",
                "--no-remote-export",
                "--disable-builtin-mcps",
                "--stream=off",
                "--output-format=json",
                f"--allow-tool=shell({PYTHON}:*)",
                "--allow-tool=shell(bash:*)",
                "--deny-tool=write",
                "--deny-tool=url",
                "--deny-tool=memory",
                "-p",
                f"/{FIXTURE} {runtime_canary.INSTRUCTION}",
            ],
            copilot[1:],
        )

    # Codex is given a Windows sandbox mode only on Windows; the case holds on every platform as Windows runs it.
    @mock.patch.object(runtime_canary, "CODEX_HAS_WINDOWS_SANDBOX", True)
    def test_codex_is_given_the_windows_sandbox_mode_its_ignored_configuration_would_set(self) -> None:
        for sandbox in ("mxc", "elevated", "unelevated"):
            with self.subTest(sandbox=sandbox):
                runner = FakeRuntimes()
                runner.actions["codex"] = lambda *_: Completed(0, "", CODEX_REJECTION)
                lines = self.canary(runner, runtimes=runtime_canary.RUNTIMES, codex_sandbox=sandbox)
                supplied = f'SUPPLIED codex "windows.sandbox={sandbox}"'
                self.assertEqual([supplied], [line for line in lines if line.startswith("SUPPLIED ")])
                # Once, before Codex's first run.
                self.assertLess(
                    lines.index(supplied),
                    next(index for index, line in enumerate(lines) if line.startswith("RUNTIME codex ")),
                )
                codex = next(
                    arguments
                    for kind, arguments, _, _ in runner.calls
                    if kind == "run" and Path(arguments[0]).name == "codex"
                )
                setting = codex.index(f"windows.sandbox={sandbox}")
                self.assertEqual("-c", codex[setting - 1])
                self.assertIn(
                    f"Codex ran with windows.sandbox={sandbox}, so",
                    next(line for line in lines if line.startswith("RUNTIME codex ")),
                )
        # Discovery runs no model, so nothing is supplied.
        lines = self.canary(FakeRuntimes(), discovery_only=True)
        self.assertFalse([line for line in lines if line.startswith("SUPPLIED ")])

    @mock.patch.object(runtime_canary, "CODEX_HAS_WINDOWS_SANDBOX", False)
    def test_off_windows_codex_is_given_no_windows_sandbox_and_a_refusal_names_no_cause(self) -> None:
        self.assertEqual([], runtime_canary.supplied("codex", "elevated"))
        self.assertEqual("", runtime_canary.blocked_cause("codex", "elevated"))
        codex = runtime_canary.run_command("codex", "codex", FIXTURE, self.home, "elevated")
        # The approval policy is set on every platform, as a non-interactive run should.
        self.assertEqual(
            ["approval_policy=never"], [codex[index + 1] for index, word in enumerate(codex) if word == "-c"]
        )
        self.assertFalse([word for word in codex if "windows.sandbox" in word])

    def test_the_codex_sandbox_option_defaults_to_elevated_and_takes_only_known_modes(self) -> None:
        for arguments, expected in (
            ([], "elevated"),
            (["--codex-sandbox", "unelevated"], "unelevated"),
            (["--codex-sandbox", "mxc"], "mxc"),
        ):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(runtime_canary, "create_home", return_value=self.home),
                mock.patch.object(runtime_canary, "canary", return_value=0) as canary,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(0, runtime_canary.main(["--discovery-only", "--keep-home", *arguments]))
                self.assertEqual(expected, canary.call_args.kwargs["codex_sandbox"])
        output = io.StringIO()
        with contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            runtime_canary.main(["--codex-sandbox", "none"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("invalid choice: 'none'", output.getvalue())

    def test_the_instruction_carries_the_marker_and_survives_cmd_quoting(self) -> None:
        # npm installs Codex as a .cmd file, which Windows runs through cmd.exe.
        for text in (
            runtime_canary.INSTRUCTION,
            *runtime_canary.supplied("codex", "elevated"),
            *runtime_canary.supplied("codex", "unelevated"),
            *runtime_canary.supplied("codex", "mxc"),
            runtime_canary.CODEX_APPROVAL_POLICY,
        ):
            self.assertFalse(set('"%&<>^|!()\r\n') & set(text), text)
        self.assertIn("--help", runtime_canary.INSTRUCTION)
        self.assertIn("RUNTIME-CANARY-RUN", runtime_canary.INSTRUCTION)


class BashTests(RuntimeCanaryTestCase):
    def tool(self) -> str:
        return f"{self.skill_dir('gamma')}/scripts/tool.sh"

    def test_a_bash_script_from_the_canary_copy_is_reported_ran_however_bash_is_started(self) -> None:
        runner = FakeRuntimes()
        # Claude Code's Bash tool runs `bash` from Git Bash's own PATH inside `bash -c`; Codex names Git Bash by its
        # full path from PowerShell; a relative path resolves against the home, which Git Bash calls /tmp/...
        runner.actions["claude"] = run_bash("-c", f'bash "{self.tool()}" --help')
        runner.actions["codex"] = run_bash(self.tool(), "--help")
        runner.actions["copilot"] = run_bash(".claude/skills/gamma/scripts/tool.sh", "--help")
        lines = self.canary(
            runner, ["gamma"], runtime_canary.RUNTIMES, sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()]
        )
        for runtime in runtime_canary.RUNTIMES:
            with self.subTest(runtime=runtime):
                self.assertIn(
                    f'RUNTIME {runtime} gamma RAN "{self.tool()}" "{forward(self.home)}" exit=3 {self.kept(runtime)}',
                    lines,
                )
                # The script ran as it would without the recorder: its output, its exit code, no stray errors.
                transcript = self.home / ".runtime-canary" / "transcripts" / f"{runtime}-gamma.jsonl"
                self.assertEqual(
                    {"stdout": "tool --help\n", "code": 3}, json.loads(transcript.read_text(encoding="utf-8"))
                )
                self.assertFalse(transcript.with_suffix(".stderr.txt").exists())

    def test_a_bash_script_from_the_installed_copy_is_never_reported_ran(self) -> None:
        installed = self.root / "profile" / ".claude" / "skills"
        script = installed / "gamma" / "scripts" / "tool.sh"
        script.parent.mkdir(parents=True)
        script.write_bytes(b"#!/usr/bin/env bash\nexit 0\n")
        runner = FakeRuntimes()
        runner.actions["codex"] = run_bash(str(script), "--help")
        self.canary(runner, ["gamma"], sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()])
        started = runtime_canary.read_records(self.home / ".runtime-canary" / "scripts" / "codex-gamma.jsonl")
        start = {"argv": [forward(script)], "cwd": forward(self.home), "pid": started[0].get("pid")}
        self.assertEqual([start, {**start, "exit": 0}], started)
        self.assertEqual(
            f'FAILED "ran the installed copy {forward(script)}, not the canary copy"',
            runtime_canary.verdict(
                "gamma",
                self.home,
                probes=[],
                started=started,
                completed=Completed(0, "", ""),
                denials=[],
                timeout=60,
                installed=installed,
            ),
        )

    def test_the_bash_recorder_records_only_scripts_and_never_stops_one(self) -> None:
        self.canary(
            FakeRuntimes(), ["gamma"], discovery_only=True, sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()]
        )
        log = self.home / ".runtime-canary" / "scripts" / "direct.jsonl"
        log.write_text("", encoding="utf-8")
        environment = runtime_canary.environment(dict(os.environ), log, self.home)

        def bash(*arguments: str, stdin: str = "", env: dict[str, str] = environment) -> tuple[int, str, str]:
            process = subprocess.run(
                [git_bash(), *arguments],
                cwd=self.home,
                env=env,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
            )
            return process.returncode, process.stdout, process.stderr

        # A command string or a script read from standard input is not a script file.
        self.assertEqual((0, "c", ""), bash("-c", "printf c"))
        self.assertEqual((0, "s\n", ""), bash("-s", stdin="echo s\n"))
        # Nor is a script Bash cannot open: Bash reads the recorder first, then refuses it.
        scripts = Path(self.tool()).parent
        (scripts / "a folder.sh").mkdir()
        self.assertEqual(127, bash(str(scripts / "missing.sh"))[0])
        self.assertEqual(126, bash(str(scripts / "a folder.sh"))[0])
        self.assertEqual([], runtime_canary.read_records(log))
        self.assertEqual((3, "tool two words (1)\n", ""), bash(self.tool(), "two words", "(1)"))
        started = runtime_canary.read_records(log)
        start = {"argv": [self.tool()], "cwd": forward(self.home), "pid": started[0].get("pid")}
        self.assertIsInstance(start["pid"], int)
        # The EXIT trap records the status the script ended with, after its own output.
        self.assertEqual([start, {**start, "exit": 3}], started)
        # A script that sets its own EXIT trap replaces the recorder's, so its end goes unrecorded, and a subshell
        # inherits no trap to record with.
        own = self.home / "own trap.sh"
        own.write_bytes(
            b"#!/usr/bin/env bash\nset -eu\ninside=$(printf sub)\ntrap 'echo \"own $inside\"' EXIT\nexit 4\n"
        )
        log.write_text("", encoding="utf-8")
        self.assertEqual((4, "own sub\n", ""), bash(str(own)))
        self.assertEqual([None], [record.get("exit") for record in runtime_canary.read_records(log)])
        # A log it cannot write leaves the script as it was.
        self.assertEqual(
            (3, "tool x\n", ""),
            bash(
                self.tool(),
                "x",
                env={**environment, runtime_canary.SCRIPT_LOG: str(self.home / ".runtime-canary" / "scripts")},
            ),
        )


class CopilotVersionTests(RuntimeCanaryTestCase):
    def test_every_copilot_start_turns_auto_update_off_so_the_recorded_version_is_the_one_that_ran(self) -> None:
        runner = FakeRuntimes()
        self.canary(runner, runtimes=("copilot",), environment={**os.environ, "COPILOT_AUTO_UPDATE": "true"})
        starts = [kind for kind, arguments, _, _ in runner.calls if Path(arguments[0]).name == "copilot"]
        self.assertEqual(["listing", "run"], starts)
        self.assertEqual(["copilot"], [Path(runtime).stem for runtime in runner.versions])
        self.assertEqual(
            ["false", "false", "false"],
            [
                environment.get("COPILOT_AUTO_UPDATE")
                for environment in [*runner.version_environments, *(call[3] for call in runner.calls)]
            ],
        )


class CopilotUserOnlyTests(RuntimeCanaryTestCase):
    def test_copilot_reports_a_user_only_skill_unsupported_and_spends_no_run_on_it(self) -> None:
        runner = FakeRuntimes()
        lines = self.canary(
            runner,
            ["alpha"],
            ("copilot", "codex"),
            sources=[runtime_canary.FIXTURE_SOURCE, self.make_source(user_only=True)],
        )
        self.assertIn(
            'RUNTIME copilot alpha UNSUPPORTED "copilot -p cannot start a user-only skill; '
            'see docs/copilot-support.md#headless-sessions"',
            lines,
        )
        self.assertFalse([line for line in lines if line.startswith("TRANSCRIPT copilot alpha ")])
        prompts = [
            arguments[-1]
            for kind, arguments, _, _ in runner.calls
            if kind == "run" and Path(arguments[0]).name == "copilot"
        ]
        self.assertEqual([f"/{FIXTURE} {runtime_canary.INSTRUCTION}"], prompts)
        # Codex can start a user-only skill by name, so it still runs it.
        self.assertTrue([line for line in lines if line.startswith("RUNTIME codex alpha FAILED ")])
        # The declared partial reached its limit on Copilot; on Codex, where it is full, the failed run disagrees.
        self.assertIn("MATRIX copilot alpha AGREES partial", lines)
        self.assertIn(
            'MATRIX codex alpha DISAGREES full "full expects RAN, but the run was FAILED"',
            lines,
        )
        # The section the line points at exists.
        self.assertIn(
            "\n## Headless sessions\n", (REPOSITORY_ROOT / "docs" / "copilot-support.md").read_text(encoding="utf-8")
        )

    def test_a_skill_without_a_readable_adapter_is_not_user_only(self) -> None:
        self.assertFalse(runtime_canary.user_only(self.home, "missing"))
        adapter = self.home / ".agents" / "skills" / "broken" / "SKILL.md"
        adapter.parent.mkdir(parents=True)
        adapter.write_text("no frontmatter\n", encoding="utf-8")
        self.assertFalse(runtime_canary.user_only(self.home, "broken"))
        self.assertEqual("", runtime_canary.unsupported("copilot", "broken", self.home))


class MatrixTests(RuntimeCanaryTestCase):
    def test_each_outcome_is_compared_with_what_full_partial_and_none_expect(self) -> None:
        full = runtime_support.Support("full")
        headless = runtime_support.Support("partial", ("user-only-start",), "Start it interactively.")
        inline = runtime_support.Support("partial", ("agent-delegation", "workflow"), "Reviews run inline.")
        none = runtime_support.Support("none", (), "Not on Codex.")
        declared = {
            "plain": {"claude-code": full, "codex": full, "copilot-cli": full},
            "user-only": {"claude-code": full, "codex": full, "copilot-cli": headless},
            "reviews": {"claude-code": full, "codex": none, "copilot-cli": inline},
            "undeclared": None,
        }
        lines = [
            'RUNTIME claude plain RAN "s" "c" KEPT "k"',
            'TRANSCRIPT claude plain "t"',
            'RUNTIME codex plain FAILED "never ran a script from the skill" KEPT "k"',
            'RUNTIME copilot plain BLOCKED "policy" KEPT "k"',
            'RUNTIME copilot user-only UNSUPPORTED "copilot -p cannot start a user-only skill"',
            'RUNTIME codex user-only RAN "s" "c" KEPT "k"',
            'RUNTIME copilot reviews RAN "s" "c" KEPT "k"',
            'RUNTIME codex reviews NOT_ATTEMPTED "Not on Codex."',
            "DISCOVERED copilot reviews",
            'RUNTIME claude undeclared RAN "s" "c" KEPT "k"',
        ]
        self.assertEqual(
            [
                "MATRIX claude plain AGREES full",
                'MATRIX codex plain DISAGREES full "full expects RAN, but the run was FAILED"',
                'MATRIX copilot plain DISAGREES full "full expects RAN, but the run was BLOCKED"',
                "MATRIX copilot user-only AGREES partial",
                "MATRIX codex user-only AGREES full",
                "MATRIX copilot reviews AGREES partial",
                "MATRIX codex reviews AGREES none",
                'MATRIX claude undeclared DISAGREES undeclared "it declares no runtime_support"',
            ],
            runtime_canary.compare(lines, declared),
        )
        # A partial that ran past a limit the canary can see, or stopped short of one it cannot, says more or less
        # than the run showed.
        self.assertEqual(
            [
                'MATRIX copilot user-only DISAGREES partial "partial expects UNSUPPORTED, but the run was RAN"',
                'MATRIX copilot reviews DISAGREES partial "partial expects RAN, but the run was FAILED"',
            ],
            runtime_canary.compare(
                [
                    'RUNTIME copilot user-only RAN "s" "c" KEPT "k"',
                    'RUNTIME copilot reviews FAILED "never ran a script from the skill" KEPT "k"',
                ],
                declared,
            ),
        )

    def test_a_runtime_a_skill_declares_none_for_is_never_started_for_it(self) -> None:
        source = self.make_source()
        metadata = source / "deploy-meta" / "gamma.json"
        document = json.loads(metadata.read_text(encoding="utf-8"))
        document["runtime_support"]["codex"] = {"level": "none", "reason": "No Bash on this runtime."}
        metadata.write_text(json.dumps(document), encoding="utf-8")
        runner = FakeRuntimes()
        runner.actions["codex"] = run_python(f"{self.skill_dir()}/scripts/canary_probe.py", "--help")

        lines = self.canary(runner, ["gamma"], sources=[runtime_canary.FIXTURE_SOURCE, source])

        self.assertIn('RUNTIME codex gamma NOT_ATTEMPTED "No Bash on this runtime."', lines)
        self.assertEqual(
            [f"MATRIX codex {FIXTURE} AGREES full", "MATRIX codex gamma AGREES none"],
            [line for line in lines if line.startswith("MATRIX ")],
        )
        prompts = [arguments[-1] for kind, arguments, _, _ in runner.calls if kind == "run"]
        self.assertEqual([f"${FIXTURE} {runtime_canary.INSTRUCTION}"], prompts)

    def test_a_full_skill_that_does_not_run_fails_the_canary(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime_canary.canary(
                [],
                ["codex"],
                self.home,
                sources=[runtime_canary.FIXTURE_SOURCE],
                runner=FakeRuntimes(),
                talk=FakeRuntimes().talk,
                which=lambda name: forward(self.root / "bin" / name),
                base_environment=dict(os.environ),
                timeout=60,
            )
        self.assertEqual(1, code)
        self.assertIn(
            f'MATRIX codex {FIXTURE} DISAGREES full "full expects RAN, but the run was FAILED"',
            output.getvalue().splitlines(),
        )

    def test_every_shipped_skill_and_the_fixture_declare_what_the_canary_compares(self) -> None:
        declared = runtime_canary.declarations([REPOSITORY_ROOT, runtime_canary.FIXTURE_SOURCE])
        self.assertEqual([], sorted(name for name, support in declared.items() if support is None))
        self.assertEqual(set(runtime_canary.DECLARED_RUNTIME.values()), set(runtime_support.RUNTIMES))
        # Pinned against literal outcomes, not read back from the declarations.
        self.assertEqual(
            {"claude": "RAN", "codex": "RAN", "copilot": "UNSUPPORTED"},
            {
                runtime: runtime_canary.expected((declared["repo-cleanup"] or {})[name])
                for runtime, name in runtime_canary.DECLARED_RUNTIME.items()
            },
        )
        self.assertEqual(
            {"claude": "RAN", "codex": "RAN", "copilot": "RAN"},
            {
                runtime: runtime_canary.expected((declared["review-prs"] or {})[name])
                for runtime, name in runtime_canary.DECLARED_RUNTIME.items()
            },
        )


class DiscoveryAndSkipTests(RuntimeCanaryTestCase):
    def test_codex_listing_reports_the_canary_path_and_every_shadow(self) -> None:
        runner = FakeRuntimes()
        adapters = forward(self.home / ".agents" / "skills")
        runner.listings["codex"] = [
            ("alpha", "C:/Users/YourName/.agents/skills/alpha", True),
            ("alpha", f"{adapters}/alpha", True),
            (FIXTURE, f"{adapters}/{FIXTURE}", True),
        ]
        lines = self.canary(
            runner,
            [FIXTURE, "alpha", "gamma"],
            discovery_only=True,
            sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()],
        )
        self.assertEqual(
            [
                f'DISCOVERED codex {FIXTURE} "{forward(self.home)}/.agents/skills/{FIXTURE}"',
                f'DISCOVERED codex alpha "{forward(self.home)}/.agents/skills/alpha"',
                'SHADOWED codex alpha "C:/Users/YourName/.agents/skills/alpha"',
                "UNDISCOVERED codex gamma",
            ],
            [line for line in lines if line.split()[0] in {"DISCOVERED", "SHADOWED", "UNDISCOVERED"}],
        )
        self.assertFalse([call for call in runner.calls if call[0] == "run"])
        listings = [call for call in runner.calls if call[0] == "listing"]
        self.assertEqual([[forward(self.root / "bin" / "codex"), "app-server"]], [call[1] for call in listings])
        self.assertEqual(self.home, listings[0][2])

    def test_a_hidden_dependency_is_not_attempted_for_discovery_and_the_canary_exits_0(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        core = forward(self.home / ".claude" / "skills" / "core")
        source = self.make_source(hidden=True)
        runner = FakeRuntimes()
        # Codex lists no copy of core; Copilot lists the home's own deployed copy as a project skill, since it runs in
        # the home, as Copilot CLI did in the v0.7.0 release canary.
        runner.listings["codex"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True)]
        runner.listings["copilot"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True), ("core", core, True)]
        for runtime in ("codex", "copilot"):
            runner.actions[runtime] = run_python(f"{self.skill_dir()}/scripts/canary_probe.py", "--help")

        lines = self.canary(runner, ["core"], ("codex", "copilot"), sources=[runtime_canary.FIXTURE_SOURCE, source])

        self.assertFalse([line for line in lines if line.startswith(("UNDISCOVERED ", "SHADOWED "))], lines)
        self.assertEqual(
            [
                f'DISCOVERY_NOT_ATTEMPTED codex core "{NO_ADAPTER}"',
                f'PROJECT_COPY copilot core "{core}"',
                f'DISCOVERY_NOT_ATTEMPTED copilot core "{NO_ADAPTER}"',
            ],
            [line for line in lines if " core " in line and not line.startswith(("RUNTIME ", "MATRIX "))],
        )
        for runtime in ("codex", "copilot"):
            self.assertIn(f'RUNTIME {runtime} core NOT_ATTEMPTED "{HIDDEN_REASON}"', lines)
            self.assertIn(f"MATRIX {runtime} core AGREES none", lines)
        # No model run is spent on core: only the fixture runs.
        prompts = [arguments[-1] for kind, arguments, _, _ in runner.calls if kind == "run"]
        self.assertEqual(2, len(prompts))
        self.assertFalse([prompt for prompt in prompts if "core" in prompt.split()[0]])

    def test_a_hidden_dependency_in_a_discovery_only_run_exits_0(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        runner = FakeRuntimes()
        runner.listings["codex"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime_canary.canary(
                ["core"],
                ["codex"],
                self.home,
                sources=[runtime_canary.FIXTURE_SOURCE, self.make_source(hidden=True)],
                runner=runner,
                talk=runner.talk,
                which=lambda name: forward(self.root / "bin" / name),
                base_environment=dict(os.environ),
                discovery_only=True,
                timeout=60,
            )
        self.assertEqual(0, code, output.getvalue())
        self.assertIn(f'DISCOVERY_NOT_ATTEMPTED codex core "{NO_ADAPTER}"', output.getvalue().splitlines())

    def test_a_skill_with_an_adapter_the_runtime_does_not_list_is_still_undiscovered(self) -> None:
        # A missing adapter the manifest records is a deployment problem, so a hidden dependency beside it does not
        # excuse it, and the project copy of the home's skill does not stand in for the adapter.
        runner = FakeRuntimes()
        alpha = forward(self.home / ".claude" / "skills" / "alpha")
        runner.listings["copilot"] = [("alpha", alpha, True)]
        lines = self.canary(
            runner,
            ["alpha", "core"],
            ("copilot",),
            discovery_only=True,
            sources=[runtime_canary.FIXTURE_SOURCE, self.make_source(hidden=True)],
        )
        self.assertIn(f'PROJECT_COPY copilot alpha "{alpha}"', lines)
        self.assertIn("UNDISCOVERED copilot alpha", lines)
        self.assertIn(f"UNDISCOVERED copilot {FIXTURE}", lines)
        self.assertIn(f'DISCOVERY_NOT_ATTEMPTED copilot core "{NO_ADAPTER}"', lines)
        self.assertNotIn("UNDISCOVERED copilot core", lines)

    def test_a_copy_the_runtime_lists_disabled_is_neither_discovered_nor_a_shadow(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        other = "C:/Users/YourName/.agents/skills/alpha"
        source = self.make_source()
        for runtime in ("codex", "copilot"):
            with self.subTest(runtime=runtime):
                runner = FakeRuntimes()
                runner.listings[runtime] = [
                    (FIXTURE, f"{adapters}/{FIXTURE}", False),
                    ("alpha", f"{adapters}/alpha", True),
                    ("alpha", other, False),
                ]
                lines = self.canary(
                    runner,
                    [FIXTURE, "alpha"],
                    (runtime,),
                    discovery_only=True,
                    sources=[runtime_canary.FIXTURE_SOURCE, source],
                )
                self.assertEqual(
                    [
                        f'DISABLED {runtime} {FIXTURE} "{adapters}/{FIXTURE}"',
                        f"UNDISCOVERED {runtime} {FIXTURE}",
                        f'DISCOVERED {runtime} alpha "{adapters}/alpha"',
                        f'DISABLED {runtime} alpha "{other}"',
                    ],
                    lines[3:],
                )

    def test_a_discovery_only_run_exits_1_on_any_undiscovered_skill(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        for listing, code in (([], 1), ([(FIXTURE, f"{adapters}/{FIXTURE}", True)], 0)):
            with self.subTest(listed=len(listing)):
                runner = FakeRuntimes()
                runner.listings["codex"] = listing
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    result = runtime_canary.canary(
                        [],
                        ["codex"],
                        self.home,
                        sources=[runtime_canary.FIXTURE_SOURCE],
                        runner=runner,
                        talk=runner.talk,
                        which=lambda name: forward(self.root / "bin" / name),
                        base_environment=dict(os.environ),
                        discovery_only=True,
                        timeout=60,
                    )
                self.assertEqual(code, result, output.getvalue())
                self.assertEqual(code == 1, f"UNDISCOVERED codex {FIXTURE}" in output.getvalue().splitlines())

    def test_both_runtimes_list_a_user_only_skill(self) -> None:
        runner = FakeRuntimes()
        alpha = str(self.home / ".agents" / "skills" / "alpha")
        runner.listings = {"copilot": [("alpha", alpha, True)], "codex": [("alpha", alpha, True)]}
        lines = self.canary(
            runner,
            ["alpha", "beta"],
            ("copilot", "codex"),
            discovery_only=True,
            sources=[runtime_canary.FIXTURE_SOURCE, self.make_source(user_only=True)],
        )
        for runtime in ("copilot", "codex"):
            self.assertIn(f'DISCOVERED {runtime} alpha "{forward(self.home)}/.agents/skills/alpha"', lines)
            self.assertIn(f"UNDISCOVERED {runtime} beta", lines)

    def test_a_listing_the_runtime_cannot_give_is_a_discovery_failure(self) -> None:
        runner = FakeRuntimes()

        def fail(*_: object) -> list[str]:
            raise discovery.ListingError("no answer within 60 seconds")

        with mock.patch.object(runner, "talk", fail):
            code, lines = self.outcome(runner, discovery_only=True)
        self.assertIn('DISCOVERY_FAILED codex "cannot read its skill list: no answer within 60 seconds"', lines)
        self.assertEqual(1, code, lines)

    def unreadable(self, runner: FakeRuntimes, runtime: str) -> Callable[..., list[str]]:
        """The runner's listing, except that the named runtime cannot give one."""
        listing: Callable[..., list[str]] = runner.talk

        def talk(arguments: list[str], *rest: object) -> list[str]:
            if Path(arguments[0]).name == runtime:
                raise discovery.ListingError("stub listing unavailable")
            return listing(arguments, *rest)

        return talk

    def test_a_listing_one_runtime_cannot_give_fails_the_run_and_the_other_runtime_is_still_reported(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        for discovery_only in (True, False):
            with self.subTest(discovery_only=discovery_only):
                runner = FakeRuntimes()
                runner.listings["copilot"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True)]
                runner.actions = {runtime: run_python(script, "--help") for runtime in ("codex", "copilot")}
                with mock.patch.object(runner, "talk", self.unreadable(runner, "codex")):
                    code, lines = self.outcome(runner, runtimes=("codex", "copilot"), discovery_only=discovery_only)
                self.assertEqual(1, code, lines)
                self.assertIn('DISCOVERY_FAILED codex "cannot read its skill list: stub listing unavailable"', lines)
                self.assertIn(f'DISCOVERED copilot {FIXTURE} "{adapters}/{FIXTURE}"', lines)
                self.assertFalse([line for line in lines if " DISAGREES " in line], lines)
                if not discovery_only:
                    for runtime in ("codex", "copilot"):
                        self.assertIn(f"MATRIX {runtime} {FIXTURE} AGREES full", lines)

    def test_a_run_whose_every_listing_is_readable_exits_0(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        for discovery_only in (True, False):
            with self.subTest(discovery_only=discovery_only):
                runner = FakeRuntimes()
                runner.listings = {
                    runtime: [(FIXTURE, f"{adapters}/{FIXTURE}", True)] for runtime in ("codex", "copilot")
                }
                runner.actions = {runtime: run_python(script, "--help") for runtime in ("codex", "copilot")}
                code, lines = self.outcome(runner, runtimes=("codex", "copilot"), discovery_only=discovery_only)
                self.assertEqual(0, code, lines)
                self.assertFalse([line for line in lines if line.startswith("DISCOVERY_FAILED ")], lines)

    def test_a_runtime_not_on_path_is_skipped_and_the_others_run_without_tokens(self) -> None:
        names = {name for name in os.environ if "TOKEN" in name or name.endswith("API_KEY")}
        runner = FakeRuntimes()
        lines = self.canary(
            runner,
            runtimes=runtime_canary.RUNTIMES,
            installed=("codex", "copilot"),
            environment={key: value for key, value in os.environ.items() if key not in names},
        )
        self.assertIn('SKIPPED claude "claude is not installed"', lines)
        self.assertEqual({"codex", "copilot"}, {Path(call[1][0]).name for call in runner.calls if call[0] == "run"})

    def test_a_run_that_skips_every_runtime_checked_nothing_and_fails(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        for installed, code in (((), 1), (("codex",), 0)):
            with self.subTest(installed=installed):
                runner = FakeRuntimes()
                runner.listings["codex"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True)]
                result, lines = self.outcome(
                    runner, runtimes=("codex", "copilot"), installed=installed, discovery_only=True
                )
                self.assertEqual(code, result, lines)
                self.assertIn('SKIPPED copilot "copilot is not installed"', lines)
                nothing = (
                    'NOTHING_CHECKED "no runtime was checked for discovery: '
                    'codex is not installed; copilot is not installed"'
                )
                self.assertEqual(code == 1, nothing in lines, lines)

    def test_a_discovery_only_run_whose_only_runtime_is_claude_code_checked_nothing_and_fails(self) -> None:
        claude = (
            "claude has no skill listing, so its discovery needs a model run, which --discovery-only does not start"
        )
        for runtimes, installed, reasons in (
            (("claude",), runtime_canary.RUNTIMES, claude),
            (runtime_canary.RUNTIMES, ("claude",), f"{claude}; codex is not installed; copilot is not installed"),
        ):
            with self.subTest(runtimes=runtimes, installed=installed):
                runner = FakeRuntimes()
                code, lines = self.outcome(runner, runtimes=runtimes, installed=installed, discovery_only=True)
                self.assertEqual(1, code, lines)
                self.assertIn(f'NOTHING_CHECKED "no runtime was checked for discovery: {reasons}"', lines)
                self.assertEqual([], runner.calls)

    def test_a_discovery_only_run_that_lists_one_runtime_exits_0_beside_claude_code(self) -> None:
        adapters = forward(self.home / ".agents" / "skills")
        for runtimes in (("codex",), ("claude", "codex")):
            with self.subTest(runtimes=runtimes):
                runner = FakeRuntimes()
                runner.listings["codex"] = [(FIXTURE, f"{adapters}/{FIXTURE}", True)]
                code, lines = self.outcome(runner, runtimes=runtimes, discovery_only=True)
                self.assertEqual(0, code, lines)
                self.assertIn(f'DISCOVERED codex {FIXTURE} "{adapters}/{FIXTURE}"', lines)
                self.assertFalse([line for line in lines if line.startswith(("NOTHING_CHECKED ", "RUNTIME "))], lines)
                self.assertFalse([line for line in lines if " claude " in line], lines)

    def test_a_discovery_only_run_whose_every_listing_failed_checked_nothing(self) -> None:
        runner = FakeRuntimes()

        def fail(*_: object) -> list[str]:
            raise discovery.ListingError("stub listing unavailable")

        with mock.patch.object(runner, "talk", fail):
            code, lines = self.outcome(runner, runtimes=("codex", "copilot"), discovery_only=True)
        self.assertEqual(1, code, lines)
        self.assertIn(
            "NOTHING_CHECKED \"no runtime was checked for discovery: codex's skill list could not be read; "
            "copilot's skill list could not be read\"",
            lines,
        )

    def test_a_run_whose_every_skill_was_never_started_checked_nothing(self) -> None:
        # The fixture declares full for every runtime and always runs first, so this takes a fixture that declares
        # none, as the hidden dependency does: its runs and its listing are both NOT_ATTEMPTED, and both agree.
        with mock.patch.object(runtime_canary, "FIXTURE_SKILL", "core"):
            code, lines = self.outcome(FakeRuntimes(), ["core"], ("codex",), sources=[self.make_source(hidden=True)])
        self.assertEqual(1, code, lines)
        self.assertIn(f'RUNTIME codex core NOT_ATTEMPTED "{HIDDEN_REASON}"', lines)
        self.assertIn("MATRIX codex core AGREES none", lines)
        self.assertIn(
            'NOTHING_CHECKED "no runtime was checked for discovery or run: '
            'codex was asked for no skill it could list or start"',
            lines,
        )

    def test_a_run_whose_only_outcome_is_unsupported_checked_nothing(self) -> None:
        # The fixture always runs first, and a listing that reads checks, so this takes a user-only fixture on Copilot
        # whose listing could not be read: its run is UNSUPPORTED, which started nothing.
        runner = FakeRuntimes()

        def fail(*_: object) -> list[str]:
            raise discovery.ListingError("stub listing unavailable")

        with mock.patch.object(runtime_canary, "FIXTURE_SKILL", "alpha"), mock.patch.object(runner, "talk", fail):
            code, lines = self.outcome(runner, ["alpha"], ("copilot",), sources=[self.make_source(user_only=True)])
        self.assertEqual(1, code, lines)
        self.assertIn(
            'RUNTIME copilot alpha UNSUPPORTED "copilot -p cannot start a user-only skill; '
            'see docs/copilot-support.md#headless-sessions"',
            lines,
        )
        self.assertIn(
            'NOTHING_CHECKED "no runtime was checked for discovery or run: copilot\'s skill list could not be read"',
            lines,
        )
        self.assertFalse([call for call in runner.calls if call[0] == "run"])

    def test_a_full_run_of_claude_code_alone_is_checked_by_its_run(self) -> None:
        # Claude Code has no listing, and a stream without its init event names no skill, so the RUNTIME line is the
        # only one that shows the run checked it.
        runner = FakeRuntimes()
        runner.actions["claude"] = run_python(f"{self.skill_dir()}/scripts/canary_probe.py", "--help")
        code, lines = self.outcome(runner, runtimes=("claude",))
        self.assertEqual(0, code, lines)
        self.assertTrue([line for line in lines if line.startswith(f"RUNTIME claude {FIXTURE} RAN ")], lines)
        self.assertIn(f"MATRIX claude {FIXTURE} AGREES full", lines)
        self.assertFalse(
            [line for line in lines if line.startswith(("NOTHING_CHECKED ", "DISCOVERED ", "UNDISCOVERED "))], lines
        )

    def test_output_starts_with_the_home_and_each_deployment(self) -> None:
        lines = self.canary(FakeRuntimes(), discovery_only=True)
        self.assertEqual([f'HOME "{forward(self.home)}"', "DEPLOYED test/runtime-canary"], lines[:2])
        # After the deployments, which take only an empty home or one a deployment marked, the run marks the home as
        # one it made, naming it, which --remove-home requires.
        marker = json.loads((self.home / ".runtime-canary" / "home.json").read_text(encoding="utf-8"))
        self.assertEqual({"made_by": "tools/runtime_canary.py", "home": forward(self.home)}, marker)
        self.assertTrue(runtime_canary.claimed(self.home))

    def test_a_failed_deployment_stops_before_any_runtime(self) -> None:
        broken = self.make_source()
        (broken / "deploy-meta" / "alpha.json").write_text("[]", encoding="utf-8")
        runner = FakeRuntimes()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime_canary.canary(
                [FIXTURE],
                ["codex"],
                self.home,
                sources=[broken],
                runner=runner,
                which=lambda name: name,
                base_environment=dict(os.environ),
                discovery_only=False,
                timeout=60,
            )
        self.assertEqual(1, code)
        log = self.home / ".runtime-canary" / "deploy-test-shipped.log"
        self.assertIn(f'DEPLOY_FAILED test/shipped "{forward(log)}"', output.getvalue())
        self.assertIn("invalid metadata shape", log.read_text(encoding="utf-8"))
        self.assertEqual([], runner.calls)
        # A home whose deployment failed is still one the canary made, so --remove-home may delete it.
        self.assertTrue(runtime_canary.claimed(self.home))

    def test_a_named_skill_with_a_source_that_cannot_be_read_reports_the_deployment_error(self) -> None:
        # A broken source names no skills, so the skill it holds is not refused as unknown; the deployment says why.
        broken = self.make_source()
        (broken / "deploy-meta" / "alpha.json").write_text("[]", encoding="utf-8")
        output = io.StringIO()
        with (
            mock.patch.object(runtime_canary, "REPOSITORY_ROOT", broken),
            mock.patch.object(runtime_canary, "create_home", return_value=self.home),
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(output),
        ):
            code = runtime_canary.main(["alpha", "--runtime", "codex"])
        self.assertEqual(1, code, output.getvalue())
        log = self.home / ".runtime-canary" / "deploy-test-shipped.log"
        self.assertIn(f'DEPLOY_FAILED test/shipped "{forward(log)}"', output.getvalue())
        self.assertNotIn("unknown skill", output.getvalue())
        self.assertIn("invalid metadata shape", log.read_text(encoding="utf-8"))
        # With every source readable, a name none of them holds is still the usage error.
        (broken / "deploy-meta" / "alpha.json").write_text(json.dumps({"required_vars": []}), encoding="utf-8")
        self.assertTrue(runtime_canary.discoverable(broken))
        stderr = io.StringIO()
        with (
            mock.patch.object(runtime_canary, "REPOSITORY_ROOT", broken),
            mock.patch("tempfile.mkdtemp", side_effect=AssertionError("made a home")),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            runtime_canary.main(["alpha", "no-such-skill"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("unknown skill: no-such-skill", stderr.getvalue())


class RecordTests(RuntimeCanaryTestCase):
    @mock.patch.object(runtime_canary, "CODEX_HAS_WINDOWS_SANDBOX", False)
    def test_the_record_holds_each_version_and_the_recorded_kinds_naming_no_path_of_this_machine(self) -> None:
        profile = self.root / "Profile Folder"
        adapters = forward(self.home / ".agents" / "skills")
        runner = FakeRuntimes()
        runner.listings["codex"] = [
            (FIXTURE, f"{adapters}/{FIXTURE}", True),
            (FIXTURE, f"{forward(profile)}/.agents/skills/{FIXTURE}", True),
        ]
        runner.actions["codex"] = run_python(f"{self.skill_dir()}/scripts/canary_probe.py", "--help")
        with mock.patch.object(platform_support, "home_directory", return_value=profile):
            lines = self.canary(runner, runtimes=("codex", "copilot"), installed=("codex",))
        script = f"<temp>/.claude/skills/{FIXTURE}/scripts/canary_probe.py"
        self.assertEqual(
            [
                "RECORD_BEGIN",
                "VERSION codex 0.160.0",
                f'DISCOVERED codex {FIXTURE} "<temp>/.agents/skills/{FIXTURE}"',
                f'SHADOWED codex {FIXTURE} "<home>/.agents/skills/{FIXTURE}"',
                f'RUNTIME codex {FIXTURE} RAN "{script}" "<temp>"{PYTHON_EXIT} {self.kept("codex")}',
                'SKIPPED copilot "copilot is not installed"',
                f"MATRIX codex {FIXTURE} AGREES full",
                "RECORD_END",
            ],
            self.recorded,
        )
        # The lines that name only this machine's throwaway files print above the record and stay out of it.
        for kind in ("HOME ", "DEPLOYED ", "TRANSCRIPT "):
            self.assertTrue([line for line in lines if line.startswith(kind)], kind)
        self.assertEqual(["codex"], runner.versions)

    def test_a_runtime_whose_version_cannot_be_read_is_recorded_as_unknown(self) -> None:
        cases = {
            "it exits otherwise than 0": Completed(1, "2.0.0\n", ""),
            "it prints no version": Completed(0, "Claude Code\n", ""),
            "it cannot start": Completed(-1, "", "", error="cannot run claude"),
        }
        for name, completed in cases.items():
            with self.subTest(case=name):
                calls: list[list[str]] = []
                runner = answering(completed, calls)
                version = runtime_canary.runtime_version("claude", runner, dict(os.environ), self.home)
                self.assertEqual(("unknown", [["claude", "--version"]]), (version, calls))

    def test_each_path_is_named_by_its_placeholder_as_far_as_the_end_of_its_name(self) -> None:
        profile = self.root / "Profile"
        home = profile / "Temp" / "runtime-canary-x"
        source = profile / "GitHub" / "repo"
        paths = {home: "<temp>", source: "<source>", profile: "<home>"}
        escaped = json.dumps(str(home))[1:-1]
        cases = {
            f'DISCOVERED codex a "{forward(home)}/.agents/skills/a"': 'DISCOVERED codex a "<temp>/.agents/skills/a"',
            f'SHADOWED codex a "{forward(profile)}/.agents/skills/a"': 'SHADOWED codex a "<home>/.agents/skills/a"',
            f'RUNTIME codex a FAILED "ran {forward(source)}/x.py"': 'RUNTIME codex a FAILED "ran <source>/x.py"',
            f'MATRIX "{forward(home).upper()}"': 'MATRIX "<temp>"',
            f'SKIPPED "{escaped}"': 'SKIPPED "<temp>"',
            f'SKIPPED "{forward(profile)}2/notes" {forward(home)}': f'SKIPPED "{forward(profile)}2/notes" <temp>',
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(expected, runtime_canary.placeholders(line, paths))


class FinishTests(unittest.TestCase):
    def finish(self, code: int, *arguments: str) -> tuple[Path, int, str]:
        """main's run with the canary itself replaced by one that exits with `code`, in a home the canary made."""
        home = runtime_canary.create_home()
        runtime_canary.claim_home(home)
        # Looked up when the cleanup runs, after any case that replaced it has put it back.
        self.addCleanup(lambda: shutil.rmtree(home, ignore_errors=True))
        output = io.StringIO()
        with (
            mock.patch.object(runtime_canary, "create_home", return_value=home),
            mock.patch.object(runtime_canary, "canary", return_value=code),
            contextlib.redirect_stdout(output),
        ):
            result = runtime_canary.main(["--runtime", "codex", *arguments])
        return home, result, output.getvalue()

    def test_the_home_is_removed_after_a_run_that_passed(self) -> None:
        home, code, output = self.finish(0)
        self.assertEqual((0, f'REMOVED "{home.as_posix()}"\n'), (code, output))
        self.assertFalse(home.exists())

    def test_the_home_is_kept_after_a_run_that_failed_or_with_keep_home(self) -> None:
        for code, arguments, why in (
            (1, (), "the run failed, so its transcripts stay for reading"),
            (0, ("--keep-home",), "--keep-home was given"),
            (1, ("--keep-home",), "--keep-home was given"),
        ):
            with self.subTest(code=code, arguments=arguments):
                home, result, output = self.finish(code, *arguments)
                self.assertEqual(
                    (code, f'HOME_KEPT "{home.as_posix()}" "{why}; remove it with --remove-home"\n'), (result, output)
                )
                self.assertTrue(runtime_canary.claimed(home))

    def test_a_home_that_cannot_be_removed_fails_a_run_that_passed(self) -> None:
        with mock.patch.object(runtime_canary.shutil, "rmtree", side_effect=OSError("held open")):
            home, code, output = self.finish(0)
        self.assertEqual((1, f'FAILED "cannot remove {home.as_posix()}: held open"\n'), (code, output))


class ChangedSkillsTests(RuntimeCanaryTestCase):
    def make_changing_source(self) -> Path:
        """make_source's skills, with core hidden behind alpha, an agent core declares, and a shared asset gamma
        declares."""
        source = self.make_source(hidden=True)
        (source / "agents").mkdir()
        (source / "agents" / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Reviews.\n---\n\nReview.\n", encoding="utf-8"
        )
        (source / "skills" / "notes.md").write_text("Shared notes.\n", encoding="utf-8")
        (source / "source.json").write_text(
            json.dumps({"id": "test/shipped", "shared_assets": {"notes.md": "owner"}}), encoding="utf-8"
        )
        for name, field, value in (("core", "agent_deps", ["reviewer"]), ("gamma", "shared_deps", ["notes.md"])):
            meta = source / "deploy-meta" / f"{name}.json"
            meta.write_text(
                json.dumps({**json.loads(meta.read_text(encoding="utf-8")), field: value}), encoding="utf-8"
            )
        return source

    def discovered(self, source: Path) -> deploy_source.Source:
        paths = Paths(source, source)
        return deploy_source.discover(paths, deploy_source.load_source_id(paths))

    def test_each_kind_of_changed_path_names_the_shipped_skills_it_reaches(self) -> None:
        src = self.discovered(self.make_changing_source())
        cases: dict[str, dict[str, str]] = {
            "skills/alpha/SKILL.md": {"alpha": "skills/alpha/SKILL.md"},
            "skills/beta/scripts/tool.py": {"beta": "skills/beta/scripts/tool.py"},
            "deploy-meta/gamma.json": {"gamma": "deploy-meta/gamma.json"},
            "skills/notes.md": {"gamma": "skills/notes.md, through the shared asset notes.md"},
            "skills/core/SKILL.md": {"alpha": "skills/core/SKILL.md, through the hidden dependency core"},
            "deploy-meta/core.json": {"alpha": "deploy-meta/core.json, through the hidden dependency core"},
            "agents/reviewer.md": {
                "alpha": "agents/reviewer.md, through the agent reviewer and the hidden dependency core"
            },
            "docs/skills.md": {},
            "skills/removed/SKILL.md": {},
            "deploy-meta/alpha.txt": {},
            "agents/nobody.md": {},
            "tests/fixtures/runtime-canary/skills/runtime-canary-probe/SKILL.md": {},
        }
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(expected, runtime_canary.changed_skills([path], src, lambda name: False))
        # Each skill is named once, by the first path in order that reaches it.
        self.assertEqual(
            {"alpha": "skills/alpha/SKILL.md", "beta": "deploy-meta/beta.json"},
            runtime_canary.changed_skills(
                ["skills/core/SKILL.md", "skills/alpha/SKILL.md", "deploy-meta/beta.json"], src, lambda name: False
            ),
        )

    def test_a_hidden_dependency_reaches_its_declarers_through_another_hidden_one(self) -> None:
        src = self.discovered(self.make_changing_source())
        inner = dataclasses.replace(src.skills["core"], name="inner", skill_deps=[])
        src.skills["inner"] = inner
        src.skills["core"] = dataclasses.replace(src.skills["core"], skill_deps=["inner"])
        self.assertEqual(
            {"alpha": "skills/inner/x.py, through the hidden dependency inner"},
            runtime_canary.changed_skills(["skills/inner/x.py"], src, lambda name: False),
        )

    def test_a_renderer_change_names_one_skill_of_each_kind_of_start_preferring_a_changed_one(self) -> None:
        src = self.discovered(self.make_changing_source())
        renderer = "deployer/render.py writes every adapter, so one skill"
        cases: tuple[tuple[list[str], dict[str, str]], ...] = (
            (
                ["deployer/render.py"],
                {"alpha": f"{renderer} the model may start", "gamma": f"{renderer} only the user may start"},
            ),
            (
                ["deployer/render.py", "skills/beta/SKILL.md"],
                {"beta": "skills/beta/SKILL.md", "gamma": f"{renderer} only the user may start"},
            ),
        )
        for paths, expected in cases:
            with self.subTest(paths=paths):
                self.assertEqual(expected, runtime_canary.changed_skills(paths, src, lambda name: name == "gamma"))

    def test_changed_since_reads_whether_a_skill_starts_only_by_the_user_from_its_source(self) -> None:
        source = self.make_changing_source()
        skill = source / "skills" / "gamma" / "SKILL.md"
        skill.write_bytes(skill.read_bytes().replace(b"---\n\n", b"disable-model-invocation: true\n---\n\n", 1))
        output = io.StringIO()
        with (
            mock.patch.object(runtime_canary, "changed_paths", return_value=("abc123", ["deployer/render.py"])),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, runtime_canary.changed_since("v1", root=source))
        renderer = "deployer/render.py writes every adapter, so one skill"
        self.assertEqual(
            [
                "SINCE v1 abc123",
                f'CHANGED alpha "{renderer} the model may start"',
                f'CHANGED gamma "{renderer} only the user may start"',
            ],
            output.getvalue().splitlines(),
        )

    def test_changed_since_reads_every_change_since_the_shared_commit_through_git(self) -> None:
        source = self.make_changing_source()

        def git(*arguments: str) -> str:
            return runtime_canary.GIT.output(list(arguments), directory=source)

        def listed(ref: str | None) -> tuple[int, list[str]]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = runtime_canary.changed_since(ref, root=source)
            return code, output.getvalue().splitlines()

        git("init", "-q")
        # With no tag to default to, git's refusal is the reason.
        code, lines = listed(None)
        self.assertEqual(1, code)
        self.assertRegex(lines[0], r'^FAILED ".+"$')
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("tag", "v1.0.0")
        base = git("rev-parse", "HEAD").strip()
        (source / "docs").mkdir()
        (source / "docs" / "notes.md").write_text("Notes.\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "documents")
        self.assertEqual(
            (0, [f"SINCE v1.0.0 {base}", 'NOTHING_CHANGED "no change since v1.0.0 reaches a shipped skill"']),
            listed(None),
        )
        tool = source / "skills" / "alpha" / "scripts" / "tool.py"
        tool.write_text("print('changed')\n", encoding="utf-8")
        git("commit", "-q", "-am", "alpha")
        # A rename names both sides: the file beta loses and the one gamma gains.
        git("mv", "skills/beta/scripts/tool.py", "skills/gamma/scripts/tool.py")
        # Unstaged, after the shared commit's tree.
        meta = source / "deploy-meta" / "core.json"
        meta.write_text(meta.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        (source / "agents" / "untracked.md").write_text("---\nname: untracked\n---\n", encoding="utf-8")
        self.assertEqual(
            (
                0,
                [
                    f"SINCE v1.0.0 {base}",
                    'CHANGED alpha "deploy-meta/core.json, through the hidden dependency core"',
                    'CHANGED beta "skills/beta/scripts/tool.py"',
                    'CHANGED gamma "skills/gamma/scripts/tool.py"',
                ],
            ),
            listed(None),
        )
        self.assertEqual((0, [f"SINCE HEAD {git('rev-parse', 'HEAD').strip()}", *listed(None)[1][1:]]), listed("HEAD"))
        code, lines = listed("no-such-ref")
        self.assertEqual(1, code)
        self.assertRegex(lines[0], r'^FAILED ".+"$')

    def test_changed_since_defaults_to_the_last_release_not_a_nearer_tag_of_another_name(self) -> None:
        source = self.make_changing_source()

        def git(*arguments: str) -> str:
            return runtime_canary.GIT.output(list(arguments), directory=source)

        def listed() -> tuple[int, list[str]]:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = runtime_canary.changed_since(None, root=source)
            return code, output.getvalue().splitlines()

        git("init", "-q")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("tag", "nightly")
        # A tag reaches HEAD, but no release tag does.
        self.assertEqual((1, ['FAILED "no release tag reaches HEAD; pass --changed-since REF"']), listed())
        git("tag", "v0.9.0")
        release = git("rev-parse", "HEAD").strip()
        tool = source / "skills" / "alpha" / "scripts" / "tool.py"
        tool.write_text("print('changed')\n", encoding="utf-8")
        git("commit", "-q", "-am", "alpha")
        git("tag", "v0.10.0-rc.1")
        meta = source / "deploy-meta" / "beta.json"
        meta.write_text(meta.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        git("commit", "-q", "-am", "beta")
        git("tag", "-a", "latest", "-m", "latest")
        # git describe names the annotated tag on HEAD; the change since the release reaches alpha and beta both.
        self.assertEqual("latest", git("describe", "--tags", "--abbrev=0", "HEAD").strip())
        self.assertEqual(
            (
                0,
                [
                    f"SINCE v0.9.0 {release}",
                    'CHANGED alpha "skills/alpha/scripts/tool.py"',
                    'CHANGED beta "deploy-meta/beta.json"',
                ],
            ),
            listed(),
        )


HOME_STATE = Path(".runtime-canary")


@pytest.fixture
def unclaimed_home() -> Iterator[Path]:
    """A folder named and placed as the canary names and places its homes, but made here."""
    home = Path(tempfile.mkdtemp(prefix="runtime-canary-")).resolve()
    yield home
    shutil.rmtree(home, ignore_errors=True)


@pytest.mark.parametrize(
    "prepare",
    [
        pytest.param(lambda home: None, id="no marker"),
        pytest.param(
            lambda home: (home / HOME_STATE / "home.json").write_text(
                json.dumps({"made_by": "tools/runtime_canary.py", "home": forward(home.parent / "elsewhere")}),
                encoding="utf-8",
            ),
            id="a marker naming another home",
        ),
        pytest.param(
            lambda home: (home / HOME_STATE / "home.json").write_text("{", encoding="utf-8"),
            id="a marker that is not JSON",
        ),
        pytest.param(lambda home: (home / HOME_STATE / "home.json").mkdir(), id="a marker that is a folder"),
    ],
)
def test_a_folder_named_like_a_home_without_the_marker_naming_it_is_refused_and_kept(
    unclaimed_home: Path, prepare: Callable[[Path], object]
) -> None:
    home = unclaimed_home
    (home / HOME_STATE).mkdir()
    (home / "notes.txt").write_text("someone's notes\n", encoding="utf-8")
    prepare(home)
    code, output = remove_home(str(home))
    CHECK.assertEqual(
        (
            1,
            f'FAILED "{home.as_posix()} holds no .runtime-canary/home.json naming it, so the canary did not make it"\n',
        ),
        (code, output),
    )
    CHECK.assertEqual("someone's notes\n", (home / "notes.txt").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--changed-since", "v1", "review-prs"], "--changed-since takes no SKILL"),
        (["--changed-since", "--keep-home"], "--changed-since makes no home, so it takes no other home option"),
        (["--remove-home", "x", "--keep-home"], "--remove-home makes no home, so it takes no other home option"),
        (["--remove-home", "x", "--changed-since"], "--remove-home makes no home, so it takes no other home option"),
        (["--changed-since=--all"], "--changed-since takes a commit, not an option: --all"),
    ],
    ids=[
        "changed-since with a skill",
        "changed-since with keep-home",
        "remove-home with keep-home",
        "remove-home with changed-since",
        "changed-since given an option",
    ],
)
def test_the_commands_that_make_no_home_take_no_skill_and_no_home_option(arguments: list[str], message: str) -> None:
    output = io.StringIO()
    with (
        mock.patch("tempfile.mkdtemp", side_effect=AssertionError("made a home")),
        contextlib.redirect_stderr(output),
        CHECK.assertRaises(SystemExit) as raised,
    ):
        runtime_canary.main(arguments)
    CHECK.assertEqual(2, raised.exception.code)
    CHECK.assertIn(message, output.getvalue())
