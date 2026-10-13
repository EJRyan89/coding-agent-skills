"""Every tool the deployer runs has a deadline: a tool that hangs is ended with every process it started, and the run
stops as any failure does, with the lock released and the journal as recovery expects."""

from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import job_tree
from harness import DeployerTestCase, Result, forward
from test_job_tree import QUICK_LAUNCHER, PausedTree, SignalledTree, ended

from deployer import errors, plan, platform_support

# Seconds a stub tool's processes get to be reported gone once ended; SignalledTree bounds its start-up.
GONE_SECONDS = 10.0
# The deadline the tests give a stub that hangs; the tree holding it starts the clock only once the stub is up.
SHORT_DEADLINE = 2
RECONCILING = "Deployment failed; reconciling the current journal before exit..."
# Starts a program that runs on, as a tool a hung tool started does, writes its own and that program's process IDs to
# the file named by its first argument, and then runs on itself.
HANGING_PROGRAM = textwrap.dedent("""
    import os, subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    with open(sys.argv[1] + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()} {child.pid}")
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
""")


def timed_out_lines(tool: str, seconds: float) -> list[str]:
    """What the deployer prints for a tool that ran out of time, written out here rather than built by the code."""
    return [
        f"ERROR: {Path(tool).name} did not finish within {seconds:g} seconds, the deadline TOOL_DEADLINE_SECONDS in "
        "deployer/platform_support.py sets, so it was ended with every process it started.",
        f"The tool was {forward(Path(tool))}. Run it by itself to see whether it finishes, then retry.",
        'See "A tool that does not finish" in docs/recovery.md.',
    ]


class Stubs:
    """Stand-in tools under a test's folder that run Python as a launcher does: a batch file on Windows, and a shell
    script elsewhere."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.pids = root / "pids.txt"
        self.program = root / "hang.py"
        self.program.write_text(HANGING_PROGRAM, encoding="utf-8")

    def tool(self, name: str, command: str) -> str:
        if platform_support.WINDOWS:
            path = self.root / f"{name}.cmd"
            path.write_text(f"@{command}\r\n", encoding="utf-8")
            return str(path)
        path = self.root / name
        path.write_bytes(f"#!/bin/sh\n{command}\n".encode())
        path.chmod(0o755)
        return str(path)

    def hanging(self, name: str) -> str:
        return self.tool(name, f'"{sys.executable}" "{self.program}" "{self.pids}"')

    def slow(self, name: str) -> str:
        return self.tool(name, f'"{sys.executable}" -c "import time; time.sleep(1)"')

    def started(self) -> list[int]:
        return [int(pid) for pid in self.pids.read_text(encoding="utf-8").split()]


def run_with_deadline(stub: str, seconds: float) -> Any:
    """run_tool, giving the stub `seconds` and every other tool the deployer's own deadline."""
    real = platform_support.run_tool

    def run(arguments: list[str], environment: dict[str, str] | None = None) -> platform_support.ToolResult:
        return real(arguments, environment, timeout=seconds if arguments[0] == stub else None)

    return mock.patch.object(platform_support, "run_tool", side_effect=run)


def found(name: str, path: str) -> Any:
    """find_executable, finding `path` for `name` and every other tool where it is."""
    real = platform_support.find_executable
    return mock.patch.object(
        platform_support, "find_executable", side_effect=lambda wanted: path if wanted == name else real(wanted)
    )


class DeadlineCase(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.stubs = Stubs(self.root)

    def assert_ended(self, pids: list[int]) -> None:
        for pid in pids:
            self.assertTrue(ended(pid, GONE_SECONDS), f"process {pid} the tool started still runs")

    def held_until_started(self) -> Any:
        return mock.patch.object(platform_support, "process_tree", return_value=SignalledTree(self.stubs.pids))


class RunToolTests(DeadlineCase):
    def test_the_deadline_is_ten_minutes(self) -> None:
        self.assertEqual(600, platform_support.TOOL_DEADLINE_SECONDS)

    def test_a_slow_tool_within_its_deadline_finishes_with_its_output_in_order(self) -> None:
        program = "import sys, time; print('out', flush=True); time.sleep(1); print('err', file=sys.stderr)"
        result = platform_support.run_tool([sys.executable, "-c", program], timeout=30)
        self.assertEqual((0, ["out", "err"]), (result.returncode, result.output.splitlines()))

    def test_the_environment_is_added_to_the_deployer_s_own(self) -> None:
        program = "import os; print(os.environ['ADDED'], bool(os.environ.get('PATH')))"
        result = platform_support.run_tool([sys.executable, "-c", program], {"ADDED": "a value"})
        self.assertEqual((0, ["a value True"]), (result.returncode, result.output.splitlines()))

    def test_a_program_a_tool_starts_and_leaves_before_it_is_held_is_ended_when_the_tool_returns(self) -> None:
        started = self.root / "child.pid"
        with mock.patch.object(platform_support, "process_tree", return_value=PausedTree(started)):
            result = platform_support.run_tool([sys.executable, "-c", QUICK_LAUNCHER, str(started)])
        self.assertEqual(0, result.returncode, result.output)
        child = int(started.read_text(encoding="utf-8"))
        self.assertTrue(ended(child, 0), f"process {child} the tool started still runs once run_tool has returned")

    def test_a_hung_tool_is_ended_with_what_it_started_and_named_with_its_deadline(self) -> None:
        stub = self.stubs.hanging("shellcheck")
        with self.held_until_started(), self.assertRaises(platform_support.ToolTimedOut) as raised:
            platform_support.run_tool([stub, "--version"], timeout=SHORT_DEADLINE)
        self.assertEqual(timed_out_lines(stub, SHORT_DEADLINE), list(raised.exception.lines))
        self.assertIsInstance(raised.exception, errors.DeployError)
        self.assertEqual(1, raised.exception.exit_code)
        self.assert_ended(self.stubs.started())

    def test_the_deadline_is_read_when_the_tool_runs(self) -> None:
        stub = self.stubs.hanging("pwsh")
        with (
            self.held_until_started(),
            mock.patch.object(platform_support, "TOOL_DEADLINE_SECONDS", SHORT_DEADLINE),
            self.assertRaises(platform_support.ToolTimedOut) as raised,
        ):
            platform_support.run_tool([stub])
        self.assertEqual(SHORT_DEADLINE, raised.exception.seconds)
        self.assert_ended(self.stubs.started())

    if platform_support.WINDOWS:

        def test_a_tool_that_cannot_be_held_with_what_it_starts_is_ended_and_refused(self) -> None:
            refusal = OSError("AssignProcessToJobObject failed: Access is denied. (Windows error 5)")
            with (
                mock.patch.object(job_tree, "job_holding", side_effect=refusal),
                self.assertRaises(errors.DeployError) as raised,
            ):
                platform_support.run_tool([sys.executable, "-c", "import time; time.sleep(120)"])
            self.assertEqual(
                [
                    f"ERROR: {Path(sys.executable).name} could not be run with an end to every process it starts: it "
                    "could not join a Windows job object: AssignProcessToJobObject failed: Access is denied. (Windows "
                    "error 5)."
                ],
                list(raised.exception.lines),
            )

    else:

        def test_a_tool_that_cannot_be_held_with_what_it_starts_is_ended_and_refused(self) -> None:
            # Started without a session of its own, the tool shares the deployer's process group, so ending a group
            # could not end what it starts without ending the deployer.
            with (
                mock.patch.object(job_tree.GroupTree, "start_options", return_value={}),
                self.assertRaises(errors.DeployError) as raised,
            ):
                platform_support.run_tool([sys.executable, "-c", "import time; time.sleep(120)"])
            self.assertEqual(
                [
                    f"ERROR: {Path(sys.executable).name} could not be run with an end to every process it starts: it "
                    "was not started as the leader of a process group of its own."
                ],
                list(raised.exception.lines),
            )


class DeploymentDeadlineTests(DeadlineCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.lock = self.home / ".claude" / "deployer" / ".deploy.lock.d"
        self.staging = self.home / ".claude" / "deployer" / "staging"

    def make_script_skill(self, content: str) -> None:
        directory = self.make_skill("alpha", content)
        self.write(directory / "run.sh", '#!/usr/bin/env bash\nprintf "%s\\n" "alpha"\n')

    def assert_lines_in_order(self, expected: list[str], result: Result) -> None:
        lines = result.output.splitlines()
        start = lines.index(expected[0]) if expected[0] in lines else -1
        self.assertEqual(expected, lines[start : start + len(expected)], result.output)

    def test_a_validator_that_hangs_refuses_before_any_change_with_the_lock_released_and_no_journal(self) -> None:
        self.make_script_skill("Alpha")
        self.make_config()
        stub = self.stubs.hanging("shellcheck")
        with self.held_until_started(), found("shellcheck", stub), run_with_deadline(stub, SHORT_DEADLINE):
            # deploy_fails compares the home before and after: a refusal before mutation changes nothing at all.
            result = self.deploy_fails("--all", pattern="did not finish within")
        self.assertEqual(1, result.code)
        self.assert_lines_in_order([*timed_out_lines(stub, SHORT_DEADLINE), "", RECONCILING], result)
        self.assertNotIn("Rendered and validated.", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.lock.exists())
        self.assertEqual([], sorted(path.name for path in self.staging.iterdir()))
        self.assertFalse(self.manifest_file.exists())
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assert_ended(self.stubs.started())

    def test_a_tool_that_hangs_after_the_first_change_rolls_back_through_the_journal(self) -> None:
        self.make_skill("alpha", "Original alpha")
        self.make_config()
        self.deploy_ok("--all")
        installed = self.manifest()
        self.make_skill("alpha", "Updated alpha")
        stub = self.stubs.hanging("git")
        carry_out = plan.carry_out
        changed: list[str] = []

        def carry_out_then_hang(*arguments: Any) -> None:
            carry_out(*arguments)
            changed.append(arguments[-1].name)
            platform_support.run_tool([stub, "rev-parse", "HEAD"])

        with (
            self.held_until_started(),
            run_with_deadline(stub, SHORT_DEADLINE),
            mock.patch.object(plan, "carry_out", side_effect=carry_out_then_hang),
        ):
            # The rollback puts every item back, so the home ends as it began: no changes_home is stated.
            result = self.deploy_fails("--all", pattern="did not finish within")
        self.assertEqual(["alpha"], changed, "the tool ran only after the deployment replaced alpha")
        self.assertEqual(1, result.code)
        self.assert_lines_in_order([*timed_out_lines(stub, SHORT_DEADLINE), "", RECONCILING], result)
        self.assertIn("Original alpha", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertEqual(installed, self.manifest())
        self.assertFalse(self.lock.exists())
        self.assertEqual([], sorted(path.name for path in self.staging.iterdir()))
        self.assert_ended(self.stubs.started())

    def test_a_slow_validator_within_the_deadline_lets_the_deployment_finish(self) -> None:
        self.make_script_skill("Alpha")
        self.make_config()
        with found("shellcheck", self.stubs.slow("shellcheck")):
            result = self.deploy_ok("--all")
        self.assertIn("Rendered and validated.", result.output)
        self.assertIn("alpha", self.owned("skills"))
        self.assertFalse(self.lock.exists())


class ReadOnlyCommandDeadlineTests(DeadlineCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def test_check_stops_at_a_version_probe_that_hangs(self) -> None:
        stub = self.stubs.hanging("shellcheck")
        with self.held_until_started(), found("shellcheck", stub), run_with_deadline(stub, SHORT_DEADLINE):
            result = self.run_cli("check")
        self.assertEqual(1, result.code, result.output)
        for line in timed_out_lines(stub, SHORT_DEADLINE):
            self.assertIn(line, result.output.splitlines())
        self.assertNotIn("Traceback", result.output)
        self.assert_ended(self.stubs.started())

    def test_verify_stops_at_a_version_probe_that_hangs(self) -> None:
        self.deploy_ok("--all")
        stub = self.stubs.hanging("codex")
        with self.held_until_started(), found("codex", stub), run_with_deadline(stub, SHORT_DEADLINE):
            result = self.run_cli("verify")
        self.assertEqual(1, result.code, result.output)
        for line in timed_out_lines(stub, SHORT_DEADLINE):
            self.assertIn(line, result.output.splitlines())
        self.assertNotIn("Traceback", result.output)
        self.assert_ended(self.stubs.started())


if __name__ == "__main__":
    unittest.main()
