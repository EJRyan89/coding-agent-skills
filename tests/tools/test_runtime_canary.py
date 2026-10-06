"""Regression suite for tools/runtime_canary.py, with every runtime replaced by a fake.

No test starts Claude Code, Codex, or Copilot CLI: setUp replaces the real process runner with one that fails the
test. The fakes stand in for a runtime by running, or not running, the deployed skill's command themselves with the
environment the canary gave them, so the deployment, the recorder, and the fixture's marker are all real.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Callable
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT))

from deployer import discovery, platform_support
from tools import runtime_canary
from tools.runtime_canary import Completed

FIXTURE = "runtime-canary-probe"
# A line Codex 0.160 wrote to stderr when its execution policy refused the canary's first command.
CODEX_REJECTION = (
    "2026-10-03T22:35:23.161504Z ERROR codex_core::tools::router: error=exec_command failed: CreateProcess { "
    'message: "Rejected(\\"`\\\\\\"C:\\\\\\\\pwsh.exe\\\\\\" -Command \\\\\\"Get-Content -LiteralPath '
    '\'C:\\\\\\\\x\\\\\\\\SKILL.md\'\\\\\\"` rejected: blocked by policy\\")" }\n'
)


def forward(path: Path | str) -> str:
    return str(path).replace("\\", "/")


class FakeRuntimes:
    """Answers each runtime's skill listing, through talk, and stands in for its model run, when called."""

    def __init__(self) -> None:
        self.listings: dict[str, list[tuple[str, str]]] = {}
        self.actions: dict[str, Callable[[list[str], Path, dict[str, str]], Completed]] = {}
        self.calls: list[tuple[str, list[str], Path, dict[str, str]]] = []

    def talk(
        self,
        arguments: list[str],
        cwd: Path,
        environment: dict[str, str],
        requests: list[str],
        answered: Callable[[str], bool] | None,
        timeout: float,
    ) -> list[str]:
        """The listing each runtime prints: Codex names a skill by its SKILL.md file, Copilot by its directory."""
        runtime = Path(arguments[0]).name
        self.calls.append(("listing", arguments, cwd, environment))
        skills = self.listings.get(runtime, [])
        if runtime == "codex":
            entries = [{"name": name, "path": f"{path}/SKILL.md", "enabled": True} for name, path in skills]
            return [json.dumps({"id": discovery.CODEX_LIST_ID, "result": {"data": [{"skills": entries}]}})]
        return [json.dumps([{"name": name, "path": path, "enabled": True} for name, path in skills])]

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Completed:
        runtime = Path(arguments[0]).name
        self.calls.append(("run", arguments, cwd, environment))
        action = self.actions.get(runtime)
        return action(arguments, cwd, environment) if action else Completed(0, "", "")


def run_python(*arguments: str) -> Callable[[list[str], Path, dict[str, str]], Completed]:
    """A runtime that runs `python -B <arguments>` the way a shell tool would, then exits 0."""

    def action(command: list[str], cwd: Path, environment: dict[str, str]) -> Completed:
        process = subprocess.run(
            [sys.executable, "-B", *arguments], cwd=cwd, env=environment, capture_output=True, text=True, check=False
        )
        return Completed(0, json.dumps({"stdout": process.stdout}) + "\n", process.stderr)

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


class RuntimeCanaryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="runtime-canary-test.")
        self.root = Path(self._temporary.name).resolve()
        self.home = self.root / "Canary Home (1)"
        self.home.mkdir()

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

    def make_source(self, user_only: bool = False) -> Path:
        """A source whose alpha runs its own scripts/tool.py, whose beta runs alpha's, declared in skill_deps, and
        whose gamma runs its own scripts/tool.sh with Bash."""
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
                f'Run `{command} "{script}"`.\n'.encode("utf-8")
            )
            (source / "deploy-meta" / f"{name}.json").write_text(json.dumps(metadata), encoding="utf-8")
            if command == "bash":
                (source / "skills" / name / "scripts" / "tool.sh").write_bytes(
                    b"#!/usr/bin/env bash\nprintf 'tool %s\\n' \"$*\"\nexit 3\n"
                )
            else:
                (source / "skills" / name / "scripts" / "tool.py").write_text("print('tool')\n", encoding="utf-8")
        return source

    def canary(
        self,
        runner: FakeRuntimes,
        skills: list[str] = (FIXTURE,),
        runtimes: tuple[str, ...] = ("codex",),
        sources: list[Path] | None = None,
        environment: dict[str, str] | None = None,
        installed: tuple[str, ...] = runtime_canary.RUNTIMES,
        discovery_only: bool = False,
        **options: str,
    ) -> list[str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = runtime_canary.canary(
                list(skills),
                list(runtimes),
                self.home,
                sources=sources if sources is not None else [runtime_canary.FIXTURE_SOURCE],
                runner=runner,
                talk=runner.talk,
                which=lambda name: forward(self.root / "bin" / name) if name in installed else None,
                base_environment=dict(os.environ) if environment is None else environment,
                discovery_only=discovery_only,
                timeout=60,
                **options,
            )
        self.assertEqual(0, code, output.getvalue())
        return output.getvalue().splitlines()

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

    def test_the_probe_records_where_it_ran_from_its_own_location(self) -> None:
        self.assertEqual(0, runtime_canary.deploy(self.home, runtime_canary.FIXTURE_SOURCE)[0])
        elsewhere = self.root / "Somewhere Else"
        elsewhere.mkdir()
        script = self.home / ".claude" / "skills" / FIXTURE / "scripts" / "canary_probe.py"
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
            [{"script": forward(script), "cwd": forward(elsewhere), "arguments": ["--help", "two words"]}], records
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


class RunTests(RuntimeCanaryTestCase):
    def test_a_runtime_that_runs_the_fixture_is_reported_with_the_marker_it_wrote(self) -> None:
        runner = FakeRuntimes()
        script = f"{self.skill_dir()}/scripts/canary_probe.py"
        runner.actions["codex"] = run_python(script, "--help")
        lines = self.canary(runner)
        self.assertIn(f'RUNTIME codex {FIXTURE} RAN "{script}" "{forward(self.home)}" {self.kept("codex")}', lines)
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
            f'RUNTIME copilot alpha RAN "{self.skill_dir("alpha")}/scripts/tool.py" "{forward(self.home)}" '
            f"{self.kept('copilot')}",
            lines,
        )

    def test_a_script_from_a_declared_sibling_counts_and_an_undeclared_one_does_not(self) -> None:
        sibling = f"{self.skill_dir('alpha')}/scripts/tool.py"
        runner = FakeRuntimes()
        runner.actions["codex"] = run_python(sibling, "--help")
        lines = self.canary(runner, ["beta", "alpha"], sources=[runtime_canary.FIXTURE_SOURCE, self.make_source()])
        self.assertIn(f'RUNTIME codex beta RAN "{sibling}" "{forward(self.home)}" {self.kept("codex")}', lines)
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
            lines = self.canary(runner, runtimes=("codex", "copilot"))
        probe = forward(self.home / ".runtime-canary" / "probe.jsonl")
        self.assertIn(
            f'RUNTIME codex {FIXTURE} FAILED "cannot read what the run recorded in {probe}: '
            f'Permission denied" {self.kept("codex")}',
            lines,
        )
        self.assertTrue([line for line in lines if line.startswith(f"RUNTIME copilot {FIXTURE} ")])

    def test_a_run_without_any_script_is_a_never_ran_failure_with_its_cause(self) -> None:
        cases = (
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
        for runtime, (arguments, cwd, environment) in runs.items():
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
                "--allow-tool=shell(python:*)",
                "--allow-tool=shell(bash:*)",
                "--deny-tool=write",
                "--deny-tool=url",
                "--deny-tool=memory",
                "-p",
                f"/{FIXTURE} {runtime_canary.INSTRUCTION}",
            ],
            copilot[1:],
        )

    def test_codex_is_given_the_windows_sandbox_mode_its_ignored_configuration_would_set(self) -> None:
        for sandbox in ("elevated", "unelevated"):
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
                self.assertEqual(["-c", f"windows.sandbox={sandbox}"], codex[codex.index("-c") : codex.index("-c") + 2])
                self.assertIn(
                    f"Codex ran with windows.sandbox={sandbox}, so",
                    next(line for line in lines if line.startswith("RUNTIME codex ")),
                )
        # Discovery runs no model, so nothing is supplied.
        lines = self.canary(FakeRuntimes(), discovery_only=True)
        self.assertFalse([line for line in lines if line.startswith("SUPPLIED ")])

    def test_the_codex_sandbox_option_defaults_to_elevated_and_takes_only_known_modes(self) -> None:
        for arguments, expected in (([], "elevated"), (["--codex-sandbox", "unelevated"], "unelevated")):
            with (
                self.subTest(arguments=arguments),
                mock.patch.object(runtime_canary, "create_home", return_value=self.home),
                mock.patch.object(runtime_canary, "canary", return_value=0) as canary,
            ):
                self.assertEqual(0, runtime_canary.main(["--discovery-only", *arguments]))
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
                    f'RUNTIME {runtime} gamma RAN "{self.tool()}" "{forward(self.home)}" {self.kept(runtime)}', lines
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
        self.assertEqual([{"argv": [forward(script)], "cwd": forward(self.home)}], started)
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
        self.assertEqual([], runtime_canary.read_records(log))
        self.assertEqual((3, "tool two words (1)\n", ""), bash(self.tool(), "two words", "(1)"))
        self.assertEqual([{"argv": [self.tool()], "cwd": forward(self.home)}], runtime_canary.read_records(log))
        # A log it cannot write leaves the script as it was.
        self.assertEqual(
            (3, "tool x\n", ""),
            bash(
                self.tool(),
                "x",
                env={**environment, runtime_canary.SCRIPT_LOG: str(self.home / ".runtime-canary" / "scripts")},
            ),
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


class DiscoveryAndSkipTests(RuntimeCanaryTestCase):
    def test_codex_listing_reports_the_canary_path_and_every_shadow(self) -> None:
        runner = FakeRuntimes()
        adapters = forward(self.home / ".agents" / "skills")
        runner.listings["codex"] = [
            ("alpha", "C:/Users/YourName/.agents/skills/alpha"),
            ("alpha", f"{adapters}/alpha"),
            (FIXTURE, f"{adapters}/{FIXTURE}"),
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

    def test_both_runtimes_list_a_user_only_skill(self) -> None:
        runner = FakeRuntimes()
        alpha = str(self.home / ".agents" / "skills" / "alpha")
        runner.listings = {"copilot": [("alpha", alpha)], "codex": [("alpha", alpha)]}
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

        runner.talk = fail
        lines = self.canary(runner, discovery_only=True)
        self.assertIn('DISCOVERY_FAILED codex "cannot read its skill list: no answer within 60 seconds"', lines)

    def test_a_runtime_not_on_path_is_skipped_and_the_others_run_without_tokens(self) -> None:
        names = {name for name in os.environ if "TOKEN" in name or name.endswith("API_KEY")}
        runner = FakeRuntimes()
        lines = self.canary(
            runner,
            runtimes=runtime_canary.RUNTIMES,
            installed=("codex", "copilot"),
            environment={key: value for key, value in os.environ.items() if key not in names},
        )
        self.assertIn('SKIPPED claude "claude is not on PATH"', lines)
        self.assertEqual({"codex", "copilot"}, {Path(call[1][0]).name for call in runner.calls if call[0] == "run"})

    def test_output_starts_with_the_home_and_each_deployment(self) -> None:
        lines = self.canary(FakeRuntimes(), discovery_only=True)
        self.assertEqual([f'HOME "{forward(self.home)}"', "DEPLOYED test/runtime-canary"], lines[:2])

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


if __name__ == "__main__":
    unittest.main()
