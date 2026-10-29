"""Regression suite for tools/hub_guard.py: the hub guard hook, its Bash and PowerShell readers, and its contract."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement
import git_client
import git_worktrees
from bounded_process import Finished, run_bounded
from markdown_sections import CODE_SPAN, markdown_section
from worktree_fixture import GUARD_SCRIPT, HUB, REPOSITORY_ROOT, TREE, HubTestCase, isolated_environment

from deployer import platform_support
from tools import bash_syntax, hub_guard, worktrees


def guard_in_process(stdin: str, cwd: Path, environment: Mapping[str, str]) -> tuple[int, str, str]:
    """The guard's status, stdout, and stderr for one event, as its own process started in cwd with only environment
    would give them: no answer an earlier event cached about a hub outlives the call."""
    stdout, stderr = io.StringIO(), io.StringIO()
    cached = (hub_guard.guarded_hub_at, hub_guard.guarded_hub_of_repository)
    for function in cached:
        function.cache_clear()
    try:
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            contextlib.chdir(cwd),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            code = hub_guard.guard(stdin)
    finally:
        for function in cached:
            function.cache_clear()
    return code, stdout.getvalue(), stderr.getvalue()


class GuardTestCase(HubTestCase):
    """A test that hands the hook one event, as Claude Code does, and reads the guard's decision.

    The guard runs in this process unless the class sets `end_to_end`, which starts `tools/hub_guard.py` as the hook
    does; `EndToEndTests` holds that both give the same decisions.
    """

    script = GUARD_SCRIPT
    end_to_end = False

    def setUp(self) -> None:
        super().setUp()
        # Bash checks every text a test hands the Bash reader at once, when the test ends, rather than one at a time.
        self.bash_texts: list[str] = []
        self.addCleanup(lambda: bash_agreement.assert_reader_agrees(self.bash_texts))

    def decision(self, event: Mapping[str, object] | str, environment: dict[str, str] | None = None) -> str | None:
        if (
            not isinstance(event, str)
            and hub_guard.SHELL_READERS.get(str(event.get("tool_name"))) is hub_guard.read_bash
        ):
            tool_input = event.get("tool_input")
            if isinstance(tool_input, Mapping) and isinstance(tool_input.get("command"), str):
                self.bash_texts.append(tool_input["command"])
        stdin = event if isinstance(event, str) else json.dumps(event)
        if self.end_to_end:
            result = self.run_script([], cwd=self.root, stdin=stdin, environment=environment)
            code, stdout, stderr = result.returncode, result.stdout, result.stderr
        else:
            code, stdout, stderr = guard_in_process(stdin, self.root, environment or self.environment)
        self.assertEqual(0, code, stderr)
        self.stderr = stderr
        self.reason = ""
        if not stdout.strip():
            return None
        output = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual("PreToolUse", output["hookEventName"])
        self.reason = output["permissionDecisionReason"]
        self.assertIn("tools/worktrees.py new", self.reason)
        decision: str = output["permissionDecision"]
        return decision

    def edit(
        self,
        path: Path | str,
        cwd: Path | None = None,
        tool: str = "Edit",
        environment: dict[str, str] | None = None,
    ) -> str | None:
        key = "notebook_path" if tool == "NotebookEdit" else "file_path"
        event = {"tool_name": tool, "tool_input": {key: str(path)}, "cwd": str(cwd or self.root)}
        return self.decision(event, environment)

    def bash(self, command: str, cwd: Path, environment: dict[str, str] | None = None) -> str | None:
        return self.decision({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}, environment)

    def powershell(self, command: str, cwd: Path, environment: dict[str, str] | None = None) -> str | None:
        event = {"tool_name": "PowerShell", "tool_input": {"command": command}, "cwd": str(cwd)}
        return self.decision(event, environment)


class EnvironmentConfigurationTests(unittest.TestCase):
    def test_a_count_of_whole_ascii_digits_reads_its_pairs(self) -> None:
        values = {"GIT_CONFIG_COUNT": " 1 ", "GIT_CONFIG_KEY_0": "Core.Worktree", "GIT_CONFIG_VALUE_0": "x"}
        configuration = hub_guard.environment_configuration(values)
        self.assertEqual(([("core.worktree", "x")], True), (configuration.entries, configuration.settled))

    def test_a_count_git_would_not_read_leaves_the_configuration_unsettled(self) -> None:
        # A digit str.isdigit() passes and int() refuses, a count past the interpreter's digit limit, and others.
        for count in ("\u00b2", "1" * 5000, "", "-1", "x", None):
            with self.subTest(count=(count or "")[:8]):
                configuration = hub_guard.environment_configuration({"GIT_CONFIG_COUNT": count})
                self.assertEqual(([], False), (configuration.entries, configuration.settled))

    def test_a_count_of_more_than_six_digits_is_not_read_so_no_loop_runs_long(self) -> None:
        pair = {"GIT_CONFIG_KEY_0": "a.b", "GIT_CONFIG_VALUE_0": "x"}
        read = hub_guard.environment_configuration({"GIT_CONFIG_COUNT": "000001", **pair})
        self.assertEqual(([("a.b", "x")], True), (read.entries, read.settled))
        unread = hub_guard.environment_configuration({"GIT_CONFIG_COUNT": "0000001", **pair})
        self.assertEqual(([], False), (unread.entries, unread.settled))


class PowerShellReaderStartTests(unittest.TestCase):
    """pwsh starts as every process a repository tool starts does: through ejryan89-skill-core's run_bounded, without
    git's repository-selecting variables, in a process tree, and with its output bounded."""

    def test_pwsh_starts_bounded_and_without_the_hooks_repository_variables(self) -> None:
        environment = isolated_environment() | {
            "GIT_DIR": str(Path("elsewhere") / ".git"),
            "GIT_WORK_TREE": "elsewhere",
            "GIT_INDEX_FILE": "elsewhere-index",
        }
        reading = hub_guard.Reading(environment, text="git commit -m x")
        with (
            mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as start,
            mock.patch.object(hub_guard, "run_bounded", wraps=run_bounded) as bounded,
        ):
            events = hub_guard.powershell_events("git commit -m x", reading, 0)
        self.assertEqual([], reading.unread)
        self.assertTrue(events, "pwsh read the command")
        [started] = start.call_args_list
        self.assertTrue(Path(started.args[0][0]).stem.lower().startswith("pwsh"))
        handed = {name.upper() for name in started.kwargs["env"]}
        self.assertFalse(handed & {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}, handed)
        self.assertEqual("elsewhere", environment["GIT_WORK_TREE"], "the hook's own environment is left as it was")
        [call] = bounded.call_args_list
        self.assertEqual(15, call.args[1])
        self.assertIsNotNone(call.kwargs["tree"])
        self.assertEqual(4 * 1024 * 1024, call.kwargs["output_limit"])

    def test_pwsh_gets_what_is_left_of_the_reading_time(self) -> None:
        reading = hub_guard.Reading(isolated_environment(), text="git commit -m x")
        reading.deadline = hub_guard.reading_clock() + 5
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, b'{"events": []}', b"")) as bounded,
        ):
            hub_guard.powershell_events("git commit -m x", reading, 0)
        [call] = bounded.call_args_list
        self.assertGreater(call.args[1], 0)
        self.assertLessEqual(call.args[1], 5)

    def test_pwsh_that_does_not_read_in_the_time_left_leaves_a_command_the_guard_cannot_read(self) -> None:
        reading = hub_guard.Reading(isolated_environment(), text="git commit -m x")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", side_effect=subprocess.TimeoutExpired("pwsh", 15)),
        ):
            self.assertIsNone(hub_guard.powershell_events("git commit -m x", reading, 0))
        self.assertEqual(["pwsh did not read the command in what was left of the guard's 15 seconds"], reading.unread)

    def test_output_that_is_not_utf8_is_read_with_replacement_characters_and_a_repeated_key_is_not(self) -> None:
        # A console code page can reach the reader's output; a byte it cannot decode costs only that character.
        cases: tuple[tuple[bytes, list[dict[str, Any]] | None, list[str]], ...] = (
            (b'{"events": [], "detail": "caf\xe9"}', [], []),
            (b'{"events": [], "events": []}', None, ["pwsh could not read the command ()"]),
        )
        for stdout, events, notes in cases:
            reading = hub_guard.Reading(isolated_environment(), text="Get-Item x")
            with (
                self.subTest(stdout=stdout),
                mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
                mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, stdout, b"")),
            ):
                self.assertEqual(events, hub_guard.powershell_events("Get-Item x", reading, 0))
                self.assertEqual(notes, reading.notes)


class FileGuardTests(GuardTestCase):
    end_to_end = True

    def test_edits_inside_the_hub_are_denied(self) -> None:
        for tool, path, cwd in (
            ("Edit", self.hub / "README.md", None),
            ("Write", self.hub / "new" / "nested" / "file.txt", None),
            ("NotebookEdit", self.hub / "notebook.ipynb", None),
            ("Edit", "README.md", self.hub),
            ("Edit", self.hub / "README.md", self.tree),
        ):
            with self.subTest(tool=tool, path=str(path), cwd=str(cwd)):
                self.assertEqual("deny", self.edit(path, cwd, tool))

    def test_edits_in_a_nested_worktree_outside_the_repository_or_to_ignored_hub_files_are_allowed(self) -> None:
        for path in (
            self.tree / "README.md",
            self.tree / "new.txt",
            self.worktrees / "not-a-worktree" / "file.txt",
            self.root / "elsewhere.txt",
            self.hub / "local.json",
        ):
            with self.subTest(path=str(path)):
                self.assertIsNone(self.edit(path))

    if platform_support.WINDOWS:

        def test_git_bash_paths_are_understood(self) -> None:
            drive, rest = str(self.hub / "README.md").replace("\\", "/").split(":", 1)
            self.assertEqual("deny", self.edit(f"/{drive.lower()}{rest}"))

    else:

        def test_a_path_is_read_as_it_is_written(self) -> None:
            # Off Windows /c/... names a directory of its own, never a drive.
            self.assertEqual("deny", self.edit(str(self.hub / "README.md")))
            self.assertIsNone(self.edit(f"/c{self.hub / 'README.md'}"))

    def test_guard_is_inert_unless_this_clone_opts_in(self) -> None:
        for value in (None, "false"):
            with self.subTest(value=value):
                self.enable(value)
                self.assertIsNone(self.edit(self.hub / "README.md"))
                self.assertIsNone(self.bash("git switch feat/demo", self.hub))


class EndToEndTests(GuardTestCase):
    """tools/hub_guard.py started as its own process, as the hook starts it, decides each kind of event as the
    in-process call the other tests make does, with the same reason and the same notes."""

    def both(self, event: Mapping[str, object] | str, environment: dict[str, str] | None = None) -> str | None:
        decisions = []
        for end_to_end in (True, False):
            self.end_to_end = end_to_end
            decisions.append((self.decision(event, environment), self.reason, self.stderr))
        self.assertEqual(decisions[0], decisions[1])
        return decisions[0][0]

    def test_each_kind_of_event_is_decided_alike_in_its_own_process_and_in_this_one(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        for event, environment, expected in (
            ({"tool_name": "Edit", "tool_input": {"file_path": str(self.hub / "README.md")}}, None, "deny"),
            ({"tool_name": "Write", "tool_input": {"file_path": str(self.tree / "new.txt")}}, None, None),
            # With no cwd in the event, a relative path is read from where the hook starts.
            ({"tool_name": "Edit", "tool_input": {"file_path": f"{HUB}/README.md"}}, None, "deny"),
            ({"tool_name": "Read", "tool_input": {"file_path": str(self.hub / "README.md")}}, None, None),
            (
                {"tool_name": "Bash", "tool_input": {"command": "git switch feat/demo"}, "cwd": str(self.hub)},
                None,
                "deny",
            ),
            ({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(self.hub)}, None, None),
            (
                {"tool_name": "Bash", "tool_input": {"command": 'cd "$UNSET_HERE" && git commit'}, "cwd": str(outside)},
                None,
                None,
            ),
            # Only the environment the hook was given names the hub, as the work tree a worktree's reset writes.
            (
                {"tool_name": "Bash", "tool_input": {"command": "git reset --hard"}, "cwd": str(self.tree)},
                {**self.environment, "GIT_WORK_TREE": str(self.hub)},
                "deny",
            ),
            ({"tool_name": "Monitor", "tool_input": {"command": "git stash"}, "cwd": str(self.hub)}, None, "deny"),
            ({"tool_name": "PowerShell", "tool_input": {"command": "git stash"}, "cwd": str(self.hub)}, None, "deny"),
            ({"tool_name": "PowerShell", "tool_input": {"command": "git commit"}, "cwd": str(self.tree)}, None, None),
            ("{not json", None, None),
        ):
            with self.subTest(event=event):
                self.assertEqual(expected, self.both(event, environment))
        self.assertIn("hub guard skipped", self.stderr)

    def test_an_event_that_names_no_command_or_file_is_allowed_with_a_note_never_unseen(self) -> None:
        # The contract allows an event the guard cannot read, so it cannot block every tool, but never in silence.
        self.enable("true")
        for event, note in (
            ({"tool_name": "Bash", "tool_input": {}, "cwd": str(self.hub)}, "the Bash event names no command"),
            ({"tool_name": "PowerShell", "cwd": str(self.hub)}, "the PowerShell event names no command"),
            ({"tool_name": "Monitor", "tool_input": {"command": 7}, "cwd": str(self.hub)}, "the Monitor event names"),
            ({"tool_name": "Edit", "tool_input": {}, "cwd": str(self.hub)}, "the Edit event names no file path"),
            ({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": None}}, "the NotebookEdit event names no"),
        ):
            with self.subTest(event=event):
                self.assertIsNone(self.both(event))
                self.assertIn(f"hub guard skipped: {note}", self.stderr)

    def test_an_opt_in_git_cannot_read_is_allowed_with_a_note_never_read_as_unguarded_unseen(self) -> None:
        self.enable("maybe")
        # git's own message for this value, on one line, is what the note names.
        said = subprocess.run(
            ["git", "-C", str(self.hub), "config", "--local", "--type=bool", "--get", "coding-agent-skills.hubGuard"],
            env=self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertNotIn(said.returncode, (0, 1))
        event = {"tool_name": "Bash", "tool_input": {"command": "git switch feat/demo"}, "cwd": str(self.hub)}
        self.assertIsNone(self.both(event))
        self.assertIn(
            f"hub guard skipped: the opt-in of {self.hub} could not be read: {' '.join(said.stderr.split())}\n",
            self.stderr,
        )
        with mock.patch.object(hub_guard, "git", return_value=git_client.GitResult(3, "", "")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertFalse(hub_guard.guard_enabled(self.hub))
        self.assertEqual(
            f"hub guard skipped: the opt-in of {self.hub} could not be read: git config exited with 3\n",
            stderr.getvalue(),
            "a failure git explains nothing about still names its exit code",
        )
        self.enable(None)
        self.assertIsNone(self.both(event))
        self.assertEqual("", self.stderr, "a clone that has not opted in is unguarded, with nothing to note")

    def test_a_checkout_git_cannot_read_is_noted_never_read_as_outside_every_repository(self) -> None:
        # Rewritten in place: git for Windows hides the file, and Windows refuses to create a hidden file anew.
        with (self.tree / ".git").open("r+", encoding="utf-8") as dot_git:
            dot_git.write(f"gitdir: {self.root / 'gone'}\n")
            dot_git.truncate()
        for command, expected in (("git status", None), (f'git -C "{self.hub}" commit -m x', "deny")):
            with self.subTest(command=command):
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.tree)}
                self.assertEqual(expected, self.both(event))
                self.assertIn(f"hub guard could not look up the hub of {self.tree}: git could not read", self.stderr)

    def test_an_opt_in_changed_between_events_is_read_afresh_as_by_a_new_process(self) -> None:
        for event in (
            {"tool_name": "Bash", "tool_input": {"command": "git switch feat/demo"}, "cwd": str(self.hub)},
            {"tool_name": "Edit", "tool_input": {"file_path": str(self.hub / "README.md")}},
        ):
            for value, expected in ((None, None), ("true", "deny"), ("false", None)):
                with self.subTest(tool=event["tool_name"], value=value):
                    self.enable(value)
                    self.assertEqual(expected, self.both(event))

    def test_bash_nested_deeper_than_the_reader_follows_is_refused_alike_in_its_own_process(self) -> None:
        # Bash reads this text, and the reader does not (tests/tools/test_bash_syntax.py holds both), so the event is
        # handed over as text, past the check that Bash and the reader agree.
        nested = "$( " * 300 + "git commit -m x" + " )" * 300
        for command, expected in ((nested, "deny"), ("git status", None)):
            with self.subTest(command=command[:30]):
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.hub)}
                self.assertEqual(expected, self.both(json.dumps(event)))
        self.assertNotIn("hub guard skipped", self.stderr)


class ReaderFailureTests(GuardTestCase):
    """A reader or a judgment that fails leaves a command the guard cannot read: refused when it names git while a
    guarded hub is in view, and allowed with a note otherwise, as a part the readers cannot settle is."""

    def outside(self) -> Path:
        outside = self.root / "not-a-repository"
        outside.mkdir(exist_ok=True)
        return outside

    def shell(self, tool: str, command: str, cwd: Path) -> str | None:
        # Handed over as text: a reader made to fail cannot agree with Bash.
        return self.decision(json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}))

    def test_any_error_inside_a_reader_is_refused_when_the_command_names_git(self) -> None:
        for tool, target, name, error in (
            ("Bash", bash_syntax, "parse", ValueError("the reader broke")),
            ("Bash", bash_syntax, "parse", RecursionError("maximum recursion depth exceeded")),
            ("Monitor", hub_guard.BashReader, "run_list", KeyError("variables")),
            ("PowerShell", hub_guard, "powershell_events", KeyError("events")),
        ):
            with (
                self.subTest(tool=tool, failing=name, error=type(error).__name__),
                mock.patch.object(target, name, side_effect=error),
            ):
                self.assertEqual("deny", self.shell(tool, "git commit -m x", self.hub))
                self.assertIn(f"The guard failed reading this command ({type(error).__name__}: ", self.reason)
                self.assertIsNone(self.shell(tool, "echo done", self.hub))
                self.assertIsNone(self.shell(tool, "git commit -m x", self.outside()))
                self.assertIn(f"hub guard skipped: {type(error).__name__}: ", self.stderr)

    def test_an_error_before_the_guarded_hubs_in_view_are_known_is_allowed_with_a_note(self) -> None:
        with mock.patch.object(hub_guard, "hubs_in_view", side_effect=RuntimeError("no hubs")):
            for tool in ("Bash", "PowerShell"):
                with self.subTest(tool=tool):
                    self.assertIsNone(self.shell(tool, "git commit -m x", self.hub))
                    self.assertEqual("hub guard skipped: RuntimeError: no hubs\n", self.stderr)

    def test_an_error_reading_a_shell_alias_is_a_part_of_the_command_the_guard_cannot_read(self) -> None:
        self.git(self.hub, "config", "alias.save", "!git commit -m x")
        with mock.patch.object(hub_guard, "read_bash", side_effect=RecursionError("maximum recursion depth exceeded")):
            self.assertEqual("deny", self.shell("Bash", "git save", self.tree))
        self.assertIn("The guard failed reading this command (RecursionError: ", self.reason)

    def test_an_error_judging_a_command_is_refused_while_a_guarded_hub_is_in_view(self) -> None:
        with mock.patch.object(hub_guard, "denied", side_effect=RuntimeError("the judge broke")):
            for tool in ("Bash", "PowerShell"):
                with self.subTest(tool=tool):
                    self.assertEqual("deny", self.shell(tool, "git commit -m x", self.hub))
                    self.assertEqual(
                        "The guard failed reading this command (RuntimeError: the judge broke), and it names git, so "
                        "it is refused while a hub is guarded. Rewrite it so each git command is written out. "
                        f"{hub_guard.DENY_REASON}",
                        self.reason,
                    )
                    self.assertIsNone(self.shell(tool, "echo done", self.hub))
                    self.assertIn("hub guard skipped: RuntimeError: the judge broke", self.stderr)
                    self.assertIsNone(self.shell(tool, "git commit -m x", self.outside()))
                    self.assertIn("hub guard skipped: RuntimeError: the judge broke", self.stderr)


def nested_loops(tool: str, body: str, depth: int) -> str:
    """body inside depth loops, each of which the guard reads twice."""
    if tool == "Bash":
        return "while true; do " * depth + body + "; done" * depth
    return "while ($true) { " * depth + body + " }" * depth


class ReadingBoundTests(GuardTestCase):
    """The guard reads loops to a stated depth and reads each command within a stated time, both well inside the hook's
    own timeout, past which Claude Code would let the command run. What it cannot read in time is refused when it names
    git while a guarded hub is in view."""

    def shell(self, tool: str, command: str, cwd: Path) -> str | None:
        return self.bash(command, cwd) if tool == "Bash" else self.powershell(command, cwd)

    def test_loops_nested_past_the_limit_are_refused_quickly_when_they_name_git(self) -> None:
        # Each level doubles the reading, so without the limit these would outlast the hook's timeout many times over.
        for tool in ("Bash", "PowerShell"):
            for body, cwd in (("git commit -m x", self.hub), (f'git -C "{self.hub}" switch main', self.tree)):
                with self.subTest(tool=tool, body=body):
                    started = time.monotonic()
                    self.assertEqual("deny", self.shell(tool, nested_loops(tool, body, 30), cwd))
                    self.assertLess(time.monotonic() - started, 10)
                    self.assertIn("nest more than 8 deep", self.reason)

    def test_loops_are_read_to_the_limit_and_refused_one_level_past_it(self) -> None:
        # A computed word after git starts pwsh, which a plain git status never waits for.
        for tool, status in (("Bash", "git status"), ("PowerShell", "git status $y")):
            with self.subTest(tool=tool):
                self.assertIsNone(self.shell(tool, nested_loops(tool, status, 8), self.hub))
                self.assertEqual("deny", self.shell(tool, nested_loops(tool, "git commit -m x", 8), self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
                self.assertEqual("deny", self.shell(tool, nested_loops(tool, status, 9), self.hub))
                self.assertIn("nest more than 8 deep", self.reason)

    def test_loops_that_name_no_git_or_follow_one_another_are_allowed(self) -> None:
        for tool, status in (("Bash", "git status"), ("PowerShell", "git status $y")):
            with self.subTest(tool=tool):
                self.assertIsNone(self.shell(tool, nested_loops(tool, "echo done", 30), self.hub))
                # Loops one after another nest no deeper than one.
                self.assertIsNone(self.shell(tool, "; ".join([nested_loops(tool, status, 1)] * 12), self.hub))

    def test_after_loops_nested_past_the_limit_neither_the_directory_nor_a_variable_is_settled(self) -> None:
        loops = nested_loops("Bash", "echo done", 9)
        reading = hub_guard.read_command(
            "Bash",
            f'x="{self.tree}"; cd "{self.tree}"; {loops}; git status; git -C "$x" status',
            self.hub,
            self.environment,
        )
        self.assertEqual(
            [(None, "status"), (None, "status")], [(call.directory, call.subcommand) for call in reading.calls]
        )
        loops = nested_loops("PowerShell", "echo done", 9)
        reading = hub_guard.read_command(
            "PowerShell",
            f"$env:X = '{self.tree}'; Set-Location '{self.tree}'; {loops}; git status $y; git -C $env:X status $y",
            self.hub,
            self.environment,
        )
        self.assertEqual(
            [(None, "status"), (None, "status")], [(call.directory, call.subcommand) for call in reading.calls]
        )

    def test_a_command_not_read_within_the_reading_time_is_refused_when_it_names_git(self) -> None:
        with mock.patch.object(hub_guard, "READING_SECONDS", 0):
            self.assertEqual("deny", self.decision(json.dumps(self.event("git status", self.hub))))
            self.assertEqual(
                "The guard failed reading this command (ReadingTimeout: the guard did not finish reading it within 0 "
                "seconds), and it names git, so it is refused while a hub is guarded. Rewrite it so each git command "
                f"is written out. {hub_guard.DENY_REASON}",
                self.reason,
            )
            self.assertIsNone(self.decision(json.dumps(self.event("echo done", self.hub))))
            outside = self.root / "not-a-repository"
            outside.mkdir()
            self.assertIsNone(self.decision(json.dumps(self.event("git status", outside))))
            self.assertIn("hub guard skipped: ReadingTimeout: the guard did not finish reading it", self.stderr)

    def event(self, command: str, cwd: Path, tool: str = "Bash") -> dict[str, object]:
        return {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}

    def test_a_slow_reading_is_refused_once_it_passes_the_reading_time(self) -> None:
        # Each look at the clock is a second later, so a command read in more than READING_SECONDS looks is too slow.
        for tool, step, last in (("Bash", "true; ", "git status"), ("PowerShell", "echo a; ", "git status $y")):
            for repeats, expected in ((2, None), (20, "deny")):
                with (
                    self.subTest(tool=tool, repeats=repeats),
                    mock.patch.object(hub_guard, "reading_clock", side_effect=itertools.count()),
                ):
                    self.assertEqual(
                        expected, self.decision(json.dumps(self.event(step * repeats + last, self.hub, tool)))
                    )
                    if expected is not None:
                        self.assertIn("within 15 seconds", self.reason)

    def test_an_alias_expansion_is_read_within_the_same_reading_time(self) -> None:
        self.git(self.hub, "config", "alias.look", "!git status")
        reading = hub_guard.read_command("Bash", "git look", self.tree, self.environment)
        self.assertEqual(["look"], [call.subcommand for call in reading.calls])
        # The judge's first look at the clock is in time; the next, inside the alias's expansion, is not.
        looks = iter([reading.deadline - 1])
        with (
            mock.patch.object(hub_guard, "reading_clock", side_effect=lambda: next(looks, reading.deadline + 1)),
            self.assertRaises(hub_guard.ReadingTimeout),
        ):
            hub_guard.denied(reading, {self.hub})

    def test_an_alias_of_an_alias_is_read_within_the_same_reading_time(self) -> None:
        self.git(self.hub, "config", "alias.look", "peek")
        self.git(self.hub, "config", "alias.peek", "!git status")
        reading = hub_guard.read_command("Bash", "git look", self.tree, self.environment)
        # Both judges' looks at the clock are in time; the next, inside the second alias's expansion, is not.
        looks = iter([reading.deadline - 1, reading.deadline - 1])
        with (
            mock.patch.object(hub_guard, "reading_clock", side_effect=lambda: next(looks, reading.deadline + 1)),
            self.assertRaises(hub_guard.ReadingTimeout),
        ):
            hub_guard.denied(reading, {self.hub})

    def test_the_reading_time_ends_at_the_deadline_and_not_a_moment_before(self) -> None:
        reading = hub_guard.Reading({}, deadline=100.0)
        for now, left in ((99.0, 1.0), (99.5, 0.5), (99.875, 0.125)):
            with self.subTest(now=now), mock.patch.object(hub_guard, "reading_clock", return_value=now):
                self.assertEqual(left, reading.time_left())
        for now in (100.0, 100.5):
            with (
                self.subTest(now=now),
                mock.patch.object(hub_guard, "reading_clock", return_value=now),
                self.assertRaises(hub_guard.ReadingTimeout),
            ):
                reading.time_left()


class NestedAliasTests(GuardTestCase):
    """An alias's expansion is judged by a judge of its own, which inherits the guarded hubs, the environment, the
    reading time, and the depth of the one that found it, and keeps its notes to itself."""

    def test_an_alias_chain_is_followed_through_each_judge_to_the_call_it_runs(self) -> None:
        # Three aliases deep stays inside the nesting limit only when each judge goes one level deeper.
        self.git(self.hub, "config", "alias.one", "two")
        self.git(self.hub, "config", "alias.two", "three")
        self.git(self.hub, "config", "alias.three", f'!git -C "{self.hub}" commit -m x')
        reading = hub_guard.read_command("Bash", "git one", self.tree, self.environment)
        reason = hub_guard.denied(reading, {self.hub})
        assert reason is not None
        self.assertIn(WRITES.format("commit"), reason)
        self.assertEqual(3, reason.count("is an alias for"))
        self.assertNotIn("nests more than", reason)

    def test_a_note_inside_an_alias_with_no_guarded_hub_in_view_is_kept_by_its_own_judge(self) -> None:
        self.git(self.hub, "config", "alias.save", '!git -C "$UNSET_HERE" commit -m x')
        reading = hub_guard.read_command("Bash", "git save", self.tree, self.environment)
        self.assertIsNone(hub_guard.denied(reading, set()))
        self.assertEqual([], reading.notes)


class HookCommandTests(GuardTestCase):
    """The hook command in the tracked .claude/settings.json, run in Git Bash as Claude Code runs it.

    A `python` shim first on PATH records its arguments, so a test sees whether the command would start Python at
    all, and with what, without depending on which Python the machine has.
    """

    def run_hook(self, variables: Mapping[str, str] | None = None, note: str = "") -> tuple[int, list[str] | None]:
        bash = platform_support.find_bash()
        if bash is None:
            raise AssertionError("Git Bash was not found; it is required for repository validation.")
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [group] = settings["hooks"]["PreToolUse"]
        [hook] = group["hooks"]
        shims = self.root / "shims"
        shims.mkdir(exist_ok=True)
        record = self.root / "python-arguments.txt"
        record.unlink(missing_ok=True)
        (shims / "python").write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SHIM_RECORD"\n', encoding="utf-8")
        (shims / "python").chmod(0o755)  # Bash runs a program off Windows only when it is executable
        environment = {
            **self.environment,
            "CLAUDE_PROJECT_DIR": str(self.hub),
            "SHIM_RECORD": str(record),
            "PATH": str(shims) + os.pathsep + self.environment["PATH"],
            **(variables or {}),
        }
        event = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}, "cwd": str(self.hub)})
        completed = subprocess.run(
            [bash, "-c", hook["command"]],
            cwd=self.hub,
            input=event,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        self.assertEqual(note, completed.stderr)
        arguments = record.read_text(encoding="utf-8").splitlines() if record.exists() else None
        return completed.returncode, arguments

    def test_a_clone_that_has_not_opted_in_starts_no_python(self) -> None:
        for value in (None, "false", "0"):
            with self.subTest(value=value):
                self.enable(value)
                self.assertEqual((0, None), self.run_hook())

    def test_an_opted_in_clone_runs_the_guard_under_the_project_directory(self) -> None:
        for value in ("true", "yes", "1"):
            with self.subTest(value=value):
                self.enable(value)
                code, arguments = self.run_hook()
                self.assertEqual(0, code)
                self.assertEqual(["-B", f"{self.hub}/tools/hub_guard.py"], arguments)

    def test_an_opt_in_git_cannot_read_is_allowed_with_a_note_that_names_the_failure(self) -> None:
        def not_a_boolean() -> None:
            self.enable("maybe")

        def malformed_configuration() -> None:
            with (self.hub / ".git" / "config").open("a", encoding="utf-8") as configuration:
                configuration.write("[not closed\n")

        # A value that is no boolean, then a configuration file git cannot parse, with the opt-in set in it.
        for name, breaks in (("not a boolean", not_a_boolean), ("malformed", malformed_configuration)):
            with self.subTest(name):
                self.enable("true")
                breaks()
                reading = ["config", "--local", "--type=bool", "--get", "coding-agent-skills.hubGuard"]
                said = subprocess.run(
                    ["git", "-C", str(self.hub), *reading],
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    check=False,
                )
                self.assertNotIn(said.returncode, (0, 1))
                # git's own message comes first, as git prints it, and the note that names its exit status after it.
                note = (
                    f"hub guard skipped: the opt-in of {self.hub} could not be read: "
                    f"git config exited with {said.returncode}\n"
                )
                self.assertEqual((0, None), self.run_hook(note=said.stderr + note))

    def test_an_inherited_git_dir_or_git_config_cannot_switch_the_opt_in_off(self) -> None:
        other = self.other_repository()
        self.git(other, "config", "coding-agent-skills.hubGuard", "false")
        # Windows reads an environment name in any case, so git there takes git_dir as GIT_DIR.
        for name, value in (
            ("GIT_DIR", ".git"),
            ("GIT_COMMON_DIR", ".git"),
            ("GIT_CONFIG", ".git/config"),
            ("git_dir", ".git"),
            ("Git_Common_Dir", ".git"),
            ("git_config", ".git/config"),
            ("Git_Config", ".git/config"),
        ):
            with self.subTest(name=name):
                code, arguments = self.run_hook({name: str(other / value)})
                self.assertEqual((0, ["-B", f"{self.hub}/tools/hub_guard.py"]), (code, arguments))

    def test_inherited_command_line_configuration_cannot_switch_the_opt_in_off(self) -> None:
        # git config --get reads command-line configuration, which outranks the repository's own file.
        for variables in (
            {"GIT_CONFIG_PARAMETERS": "'coding-agent-skills.hubGuard=false'"},
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "coding-agent-skills.hubGuard",
                "GIT_CONFIG_VALUE_0": "false",
            },
        ):
            with self.subTest(variables=variables):
                code, arguments = self.run_hook(variables)
                self.assertEqual((0, ["-B", f"{self.hub}/tools/hub_guard.py"]), (code, arguments))


class HookCommandUnderPowerShellTests(GuardTestCase):
    """The tracked hook command run under PowerShell, as Copilot CLI runs a hook's command on Windows.

    Copilot reads .claude/settings.json as a hook source in a trusted folder and hands its shell calls to this hook as
    `Bash`, PowerShell commands included. PowerShell must parse the command and end it at its own `exit 0`, so a
    Copilot call is never denied by a hook that errored, and never judged by the Bash reader.
    """

    def run_hook(self, mode: str) -> tuple[subprocess.CompletedProcess[str], bool]:
        pwsh = platform_support.find_pwsh(self.environment["PATH"])
        if pwsh is None:
            raise AssertionError("pwsh was not found; it is required for repository validation.")
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
        shims = self.root / "shims"
        shims.mkdir(exist_ok=True)
        record = self.root / "started.txt"
        record.unlink(missing_ok=True)
        # Each program the command would start after its exit, under the name PowerShell would find it by.
        for program in ("python", "git", "env", "awk", "test"):
            (shims / f"{program}.cmd").write_text(f'@echo {program}>>"%SHIM_RECORD%"\r\n', encoding="utf-8")
            (shims / program).write_text(f'#!/bin/sh\necho {program} >> "$SHIM_RECORD"\n', encoding="utf-8")
            (shims / program).chmod(0o755)  # pwsh runs a program off Windows only when it is executable
        environment = {
            **self.environment,
            "CLAUDE_PROJECT_DIR": str(self.hub),
            "COPILOT_PROJECT_DIR": str(self.hub),
            "SHIM_RECORD": str(record),
            "PATH": str(shims) + os.pathsep + self.environment["PATH"],
        }
        # The payload Copilot CLI hands a .claude/settings.json hook for its powershell tool.
        event = json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "session_id": "probe",
                "timestamp": "2026-10-22T00:00:00.000Z",
                "cwd": str(self.hub),
                "tool_name": "Bash",
                "tool_input": {"command": "git checkout -b probe-x"},
            }
        )
        if mode == "-File":
            script = self.root / "hook.ps1"
            script.write_text(hook["command"], encoding="utf-8")
            arguments = [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)]
        else:
            arguments = [pwsh, "-NoProfile", "-NonInteractive", "-Command", hook["command"]]
        completed = subprocess.run(
            arguments,
            cwd=self.hub,
            input=event,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
            check=False,
        )
        return completed, record.exists()

    def test_powershell_parses_the_command_and_ends_it_before_it_starts_anything(self) -> None:
        # Unset, opted in, and an opt-in git cannot read: in each PowerShell ends at the command's own `exit 0`.
        for value in (None, "true", "maybe"):
            for mode in ("-Command", "-File"):
                with self.subTest(value=value, mode=mode):
                    self.enable(value)
                    completed, started = self.run_hook(mode)
                    outcome = (completed.returncode, completed.stdout, completed.stderr, started)
                    self.assertEqual((0, "", "", False), outcome)


UNPLACED = "could not tell where `git {}` runs"
WRITES = "`git {}` would move the hub's HEAD or write its index or tree."


def guard_section() -> str:
    """The "The guard" section of docs/parallel-sessions.md, its contract included."""
    document = (REPOSITORY_ROOT / "docs" / "parallel-sessions.md").read_text(encoding="utf-8")
    section = markdown_section(document, "## The guard")
    if section is None:
        raise ValueError("docs/parallel-sessions.md has no ## The guard section")
    return section


def contract_names(after: str, before: str) -> list[str]:
    """The code spans the guard's section names between two phrases, read with its lines joined."""
    text = " ".join(guard_section().split())
    start = text.index(after) + len(after)
    return CODE_SPAN.findall(text[start : text.index(before, start)])


def bash_prefix_programs() -> set[str]:
    """The programs and builtins whose command the Bash reader reads as the one it starts."""
    builtins = {name for name, reader in hub_guard.BASH_BUILTINS.items() if reader is hub_guard.bash_exec}
    return builtins | set(hub_guard.PREFIX_PROGRAMS) | hub_guard.ENV_NAMES


class CommandGuardTests(GuardTestCase):
    def test_upstream_values_and_real_rename_options_reach_both_shell_guard_decisions(self) -> None:
        for shell in (self.bash, self.powershell):
            for options, expected in (
                ("-uorigin/main", None),
                ("-uorigin/MAIN", None),
                ("-quorigin/mM-feature", None),
                ("-u origin/main", None),
                ("--set-upstream-to=origin/main", None),
                ("-fm main renamed", "deny"),
                ("-qM main renamed", "deny"),
                ("-uorigin/main -m renamed", "deny"),
                ("--mov main renamed", "deny"),
            ):
                with self.subTest(shell=shell.__name__, options=options):
                    self.assertEqual(expected, shell(f"git branch {options}", self.hub))
                    self.assertEqual("", self.stderr)
        self.assertEqual("main\n", self.git(self.hub, "branch", "--show-current"))
        self.assertEqual("", self.git(self.hub, "status", "--porcelain"))

    def test_a_monitor_command_is_read_as_bash_in_the_hub(self) -> None:
        # Monitor runs its command in the shell the Bash tool uses, so a refused git call through it is refused too.
        def monitor(command: str, cwd: Path) -> str | None:
            event = {"tool_name": "Monitor", "tool_input": {"command": command, "timeout_ms": 60000}, "cwd": str(cwd)}
            return self.decision(event)

        self.assertEqual("deny", monitor("git switch feat/demo", self.hub))
        self.assertEqual("deny", monitor(f'cd "{self.hub}" && git commit -m "a message with spaces"', self.tree))
        self.assertIsNone(monitor("git status; git log --oneline -1", self.hub))
        self.assertIsNone(monitor("git switch -c other", self.tree))
        self.assertEqual("main\n", self.git(self.hub, "branch", "--show-current"))

    def test_commands_that_move_or_write_the_hub_are_denied(self) -> None:
        for command in (
            "git switch feat/demo",
            "git checkout -b other",
            "git checkout -- README.md",
            'git commit -m "a message with spaces"',
            "git add .",
            "git stash",
            "git stash pop",
            "git pull",
            "git merge feat/demo",
            "git reset --hard",
            "git restore README.md",
            "git rebase main",
            "git clean -fd",
            "git apply change.patch",
            "git update-ref refs/heads/main HEAD~1",
            "git symbolic-ref HEAD refs/heads/feat/demo",
            "git read-tree -u -m HEAD",
            "git update-index --add README.md",
            "git checkout-index -a -f",
            "git bisect start",
            "git sparse-checkout set docs",
            "git submodule update --init",
            "git submodule --quiet update",
            "git stage README.md",
            "git mergetool",
            "git merge-file ours base theirs",
            "git submodule add https://example.invalid/x.git vendor/x",
            "git submodule deinit vendor/x",
            "git submodule foreach git clean -fd",
            "git submodule set-url vendor/x https://example.invalid/y.git",
            "git branch -m renamed",
            "git branch -M feat/demo",
            "git branch -fm main renamed",
            "git branch --mov main renamed",
            "git branch --move=main renamed",
            "git difftool --dir-diff",
            "git merge-index git-merge-one-file -a",
            "git rerere",
            "git filter-branch --tree-filter true HEAD",
            "git fast-import --force",
            "git history reword HEAD --update-refs=head",
            "git replay --advance main origin/main..feat/demo",
            "git citool",
            "git gui",
            "git svn rebase",
            "echo ok && git commit -am message",
            "git status; git switch feat/demo",
            "GIT_EDITOR=true git rebase --continue",
            "git -c core.autocrlf=false commit -m x",
            "git --no-pager commit -m x 2>&1 | tail -1",
            "git commit -F - <<'EOF'\nIt's a message with an apostrophe\nEOF",
            f'cd "{self.tree}" && cd "{self.hub}" && git switch feat/demo',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))

    def test_read_only_and_hub_maintenance_commands_are_allowed(self) -> None:
        for command in (
            "git status",
            "git log --oneline -5",
            "git diff",
            "git fetch origin",
            "git fetch --prune origin",
            "git pull --ff-only",
            "git pull --ff-only origin main",
            "git pull -q --ff-only --no-rebase origin main",
            "git merge --ff-only origin/main",
            "git merge --quiet --ff-only origin/main",
            "git submodule status",
            "git submodule",
            "git submodule --quiet status --recursive",
            "git submodule summary",
            "git branch",
            "git branch --show-current",
            "git branch --merged main",
            "git branch -D feat/demo",
            "git branch -u origin/main",
            "git switch main",
            "git checkout -q main",
            "git switch main 2>&1",
            "git stash list",
            "git apply --check change.patch",
            "git worktree add ../x -b y main",
            "git worktree remove ../x",
            "git branch -d feat/demo",
            "echo git commit",
            "python -B tools/worktrees.py new fix thing",
            f'git -C "{self.tree}" commit -m x',
            f'cd "{self.tree}" && git commit -m x',
            "cd .claude/worktrees/feat-demo && git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))
                self.assertEqual("", self.stderr)

    def test_the_hub_fast_forwards_only_to_origin_main(self) -> None:
        for command, target in (
            ("git merge --ff-only feat/demo", "feat/demo"),
            ("git merge --ff-only main", "main"),
            ("git merge --ff-only", "its upstream"),
            ("git merge --ff-only origin/main feat/demo", "origin/main feat/demo"),
            ("git merge --ff-only --autostash origin/main", "--autostash origin/main"),
            ("git pull --ff-only origin feat/demo", "origin feat/demo"),
            ("git pull --ff-only upstream main", "upstream main"),
            ("git pull --ff-only origin", "origin"),
            ("git pull --ff-only --rebase origin main", "--rebase origin main"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(f"only to origin/main, not to {target}.", self.reason)
        self.assertIsNone(self.bash("git merge --ff-only feat/demo", self.tree))

    def test_aliases_are_judged_by_what_they_run(self) -> None:
        for name, expansion in (
            ("co", "checkout"),
            ("ff", "merge --ff-only"),
            ("up", "-c core.quotepath=false pull --ff-only"),
            ("sw", "!git switch"),
            ("chain", "co"),
            ("st", "status --short"),
            ("hop", f'!cd "{self.tree}" && git commit -m x'),
        ):
            self.git(self.hub, "config", f"alias.{name}", expansion)
        for command in (
            "git co feat/demo",
            "git CO feat/demo",
            "git ff feat/demo",
            "git up origin feat/demo",
            "git sw feat/demo",
            "git chain feat/demo",
            f'cd "{self.tree}" && git -C "{self.hub}" co feat/demo',
            "git -c alias.go=checkout go feat/demo",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn("is an alias", self.reason)
        for command in (
            "git co main",
            "git ff origin/main",
            "git up origin main",
            "git sw main",
            "git chain -q main",
            "git st",
            "git hop",
            "git co feat/demo && git status",
            "git -c alias.go=status go",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree if "&&" in command else self.hub))
        self.assertEqual("deny", self.powershell("git co feat/demo", self.hub))

    def test_an_alias_that_cannot_be_read_is_refused(self) -> None:
        for name, expansion in (
            ("unbalanced", "checkout 'feat/demo"),
            ("loop", "loop"),
            ("unsettled", '!git -C "$TARGET_UNSET_HERE" commit -m x'),
        ):
            self.git(self.hub, "config", f"alias.{name}", expansion)
        for command, reason in (
            ("git unbalanced", "could not be read"),
            ("git loop", "could not be read"),
            ("git unsettled", UNPLACED.format("commit")),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(reason, self.reason)
        # A worktree shares the hub's aliases, and its session has the hub in view.
        self.assertEqual("deny", self.bash("git unbalanced", self.tree))
        self.assertIn("could not be read", self.reason)

    def test_worktree_sessions_may_commit_but_not_reach_into_the_hub(self) -> None:
        self.assertIsNone(self.bash("git commit -m x && git rebase main", self.tree))
        self.assertIsNone(self.bash(f'git --work-tree="{self.tree}" add .', self.tree))
        self.assertEqual("deny", self.bash(f'git -C "{self.hub}" switch feat/demo', self.tree))
        self.assertEqual("deny", self.bash(f'cd "{self.hub}" && git commit -m x', self.tree))
        self.assertEqual("deny", self.bash(f'git --work-tree="{self.hub}" add .', self.tree))
        self.assertEqual("deny", self.bash(f'GIT_WORK_TREE="{self.hub}" git checkout -- .', self.tree))
        # Started in the hub, git writes the hub's own index whichever work tree it is given.
        self.assertEqual("deny", self.bash(f'git -C "{self.hub}" --work-tree="{self.tree}" add .', self.tree))
        self.assertIn(WRITES.format("add"), self.reason)

    if platform_support.WINDOWS:

        def test_a_location_variable_named_in_any_case_cannot_reach_into_the_hub(self) -> None:
            # Git on Windows reads an environment name in any case, so git_work_tree is GIT_WORK_TREE to it.
            for command in (
                f'git_work_tree="{self.hub}" git reset --hard',
                f'env git_work_tree="{self.hub}" git reset --hard',
                f'Git_Work_Tree="{self.hub}" git reset --hard',
            ):
                with self.subTest(command=command):
                    self.assertEqual("deny", self.bash(command, self.tree))

    else:

        def test_a_location_variable_not_in_upper_case_is_not_one_git_reads(self) -> None:
            for command in (
                f'git_work_tree="{self.hub}" git reset --hard',
                f'env Git_Work_Tree="{self.hub}" git reset --hard',
            ):
                with self.subTest(command=command):
                    self.assertIsNone(self.bash(command, self.tree))

    def test_a_guard_defect_or_a_command_outside_every_guarded_hub_is_allowed(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        self.assertIsNone(self.bash("git commit -m x", outside))
        self.assertIsNone(self.bash('cd "$SOMEWHERE_UNSET" && git commit -m x', outside))
        self.assertIn(UNPLACED.format("commit"), self.stderr)
        self.assertIsNone(
            self.decision({"tool_name": "Read", "tool_input": {"file_path": str(self.hub / "README.md")}})
        )
        result = self.run_script([], cwd=self.root, stdin="{not json")
        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("hub guard skipped", result.stderr)

    def test_a_location_variable_is_judged_by_the_checkout_it_names(self) -> None:
        other = self.other_repository() / ".git"
        for command, cwd, expected in (
            # git uses the other repository's HEAD and index and writes the directory it starts in.
            (f'GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'export GIT_DIR="{other}"; git reset --hard', self.hub, "deny"),
            (f'env GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'GIT_DIR="{other}" git reset --hard', self.tree, None),
            (f'set -a; GIT_DIR="{other}"; cd "{self.tree}"; git reset --hard', self.hub, None),
            # GIT_COMMON_DIR moves only the refs and objects every worktree shares.
            (f'export GIT_COMMON_DIR="{other}"; git reset --hard', self.hub, "deny"),
            (f'export GIT_COMMON_DIR="{other}"; git reset --hard', self.tree, None),
            (f'export GIT_WORK_TREE="{self.hub}"; cd "{self.tree}" && git reset --hard', self.hub, "deny"),
            (f"bash -c 'export GIT_WORK_TREE=\"{self.tree}\"'; git reset --hard", self.hub, "deny"),
        ):
            with self.subTest(command=command, cwd=cwd):
                self.assertEqual(expected, self.bash(command, cwd))
        inherited = {**self.environment, "GIT_DIR": str(other)}
        self.assertEqual("deny", self.bash("git reset --hard", self.hub, inherited))
        self.assertEqual("deny", self.powershell("git reset --hard", self.hub, inherited))
        self.assertIsNone(self.bash("git reset --hard", self.tree, inherited))
        self.assertEqual("main\n", self.git(self.hub, "branch", "--show-current"))
        self.assertEqual("", self.git(self.hub, "status", "--porcelain"))

    def test_an_inherited_location_variable_cannot_hide_the_hub_from_the_file_guard(self) -> None:
        other = self.other_repository() / ".git"
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            with self.subTest(name=name):
                event = {"tool_name": "Edit", "tool_input": {"file_path": str(self.hub / "README.md")}}
                self.assertEqual("deny", self.decision(event, {**self.environment, name: str(other)}))


def shown(directory: Path | None) -> str | None:
    return None if directory is None else os.path.normcase(os.path.normpath(directory))


class CommandReadingTests(GuardTestCase):
    """Each PowerShell case starts a real pwsh, as the hook does, and packs several forms into one command to keep that
    cheap. read gives each call's work tree, None when the text does not settle it; reach gives what each call
    reaches from the worktree unless told otherwise: the guarded hub, a place the text does not settle, or elsewhere.
    From the hub every call reaches the hub through its work tree whatever repository it names."""

    def reading(
        self, command: str, cwd: Path | None = None, tool: str = "PowerShell", environment: dict[str, str] | None = None
    ) -> hub_guard.Reading:
        if hub_guard.SHELL_READERS.get(tool) is hub_guard.read_bash:
            self.bash_texts.append(command)
        reading = hub_guard.read_command(tool, command, cwd or self.hub, environment or self.environment)
        self.notes, self.unread = reading.notes, reading.unread
        return reading

    def read(
        self, command: str, cwd: Path | None = None, tool: str = "PowerShell", environment: dict[str, str] | None = None
    ) -> list[tuple[str | None, str | None]]:
        reading = self.reading(command, cwd, tool, environment)
        return [(shown(call.directory), call.subcommand) for call in reading.calls]

    def reach(
        self, command: str, cwd: Path | None = None, tool: str = "PowerShell", environment: dict[str, str] | None = None
    ) -> list[tuple[str, str | None]]:
        reached = []
        for call in self.reading(command, cwd or self.tree, tool, environment).calls:
            hub, unknown = hub_guard.reached_hub(call.place)
            reached.append(("hub" if hub is not None else "unknown" if unknown else "elsewhere", call.subcommand))
        return reached

    def at(self, directory: Path | None, *subcommands: str) -> list[tuple[str | None, str | None]]:
        return [(shown(directory), subcommand) for subcommand in subcommands]

    def test_set_location_and_its_aliases_are_followed(self) -> None:
        forward = str(self.hub).replace("\\", "/")
        command = (
            f"Set-Location -Path '{self.tree}'; git commit -m x\n"
            f'sl -LiteralPath "{self.hub}" && git add .\n'
            f"cd -LP '{self.tree}' ; git rm x\n"
            f"chdir -Pat '{forward}' -PassThru; git mv a b\n"
            f"Set-Location -Path:'{self.tree}'; cd ..; git restore x\n"
            "cd; git reset\n"
            # A location that is not a directory fails and leaves the session where it was, or where it moved.
            f"cd '{self.root / 'no such folder'}'; git revert x\n"
            "cd -; git clean -fd"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit"),
                *self.at(self.hub, "add"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv"),
                *self.at(self.worktrees, "restore"),
                *self.at(Path.home(), "reset"),
                *self.at(None, "revert", "clean"),
            ],
            self.read(command),
        )

    def test_push_location_keeps_a_stack_and_a_pop_past_its_bottom_is_unknown(self) -> None:
        command = (
            f"Push-Location '{self.tree}'; git commit; pushd -Path '{self.hub}'; git add .; Pop-Location; git rm x;"
            " popd; git mv a b; Push-Location; git am; popd; popd; git reset"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit"),
                *self.at(self.hub, "add"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv", "am"),
                *self.at(None, "reset"),
            ],
            self.read(command),
        )

    def test_git_options_and_environment_variables_settle_the_directory_or_leave_it_unknown(self) -> None:
        environment = {**self.environment, "TASK_TREE": str(self.tree)}
        environment.pop("UNSET_TASK_TREE", None)
        command = (
            f"git -C '{self.tree}' commit; git \"--work-tree={self.tree}\" add .; git --work-tree '{self.tree}' rm x;"
            ' git --git-dir=.git mv a b; git -C $env:TASK_TREE reset; git -C "$env:TASK_TREE'
            + os.sep
            + '.." restore x;'
            " git -C \"$env:UNSET_TASK_TREE\" revert x; $env:GIT_WORK_TREE = 'elsewhere'; git clean -fd"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "commit", "add", "rm"),
                *self.at(self.hub, "mv"),
                *self.at(self.tree, "reset"),
                *self.at(self.worktrees, "restore"),
                *self.at(None, "revert"),
                *self.at(self.hub / "elsewhere", "clean"),
            ],
            self.read(command, environment=environment),
        )

    def test_quoting_comments_and_every_separator_are_read_as_powershell_reads_them(self) -> None:
        command = (
            'git commit -m @"\nIt\'s a "quoted" message; cd elsewhere\n"@\n'
            'git add `"odd name`" || git rm x | Out-Null && git mv a b # git reset in a comment\n'
            + (
                "& 'C:\\Program Files\\Git\\cmd\\git.exe' stash"
                if platform_support.WINDOWS
                else "& '/usr/bin/git' stash"
            )
        )
        self.assertEqual(self.at(self.hub, "commit", "add", "rm", "mv", "stash"), self.read(command))

    def test_commands_are_read_in_execution_order_and_nested_shells_are_followed(self) -> None:
        bash_hub = str(self.hub).replace("\\", "/")
        command = (
            f"git add (Set-Location '{self.tree}'); & {{ Set-Location '{self.hub}' }}; git commit;"
            f" iex 'Set-Location ''{self.tree}'''; git rm x;"
            f" pwsh -c \"Set-Location '{self.hub}'; git mv a b\"; git reset;"
            f" bash -c 'cd \"{bash_hub}\" && git clean -fd'; git restore x"
        )
        self.assertEqual(
            [
                *self.at(self.tree, "add"),
                *self.at(self.hub, "commit"),
                *self.at(self.tree, "rm"),
                *self.at(self.hub, "mv"),
                *self.at(self.tree, "reset"),
                *self.at(self.hub, "clean"),
                *self.at(self.tree, "restore"),
            ],
            self.read(command),
        )
        self.assertEqual(([], []), (self.notes, self.unread))

    def test_a_powershell_branch_loop_or_function_leaves_only_what_every_path_agrees_on(self) -> None:
        command = (
            f"if ($x) {{ Set-Location '{self.tree}' }}; git commit\n"
            f"Set-Location '{self.tree}'; foreach ($i in 1, 2) {{ git add .; Set-Location '{self.hub}' }}; git rm x\n"
            f"Set-Location '{self.tree}'; function Hop {{ Set-Location '{self.hub}' }}; git mv a b; Hop; git am\n"
            f"Set-Location '{self.tree}'; $block = {{ Set-Location '{self.hub}' }}; git reset\n"
            f"Set-Location '{self.tree}'; Test-Path x && Set-Location '{self.hub}'; git clean -fd"
        )
        self.assertEqual(
            [
                *self.at(None, "commit"),
                *self.at(self.tree, "add"),
                *self.at(None, "add", "rm"),
                *self.at(self.tree, "mv"),
                *self.at(self.hub, "am"),
                *self.at(None, "reset", "clean"),
            ],
            self.read(command),
        )

    def test_bash_follows_pwsh_and_git_work_tree_settings(self) -> None:
        command = (
            f'pwsh -Command "Set-Location \'{self.tree}\'; git switch x"; git --work-tree="{self.tree}" add .;'
            f' GIT_WORK_TREE="{self.tree}" git rm x; GIT_DIR=.git git commit;'
            f' bash -c "cd \\"{self.tree}\\" && git mv a b"; git reset'
        )
        self.assertEqual(
            [
                *self.at(self.tree, "switch", "add", "rm"),
                *self.at(self.hub, "commit"),
                *self.at(self.tree, "mv"),
                *self.at(self.hub, "reset"),
            ],
            self.read(command, tool="Bash"),
        )

    def test_bash_reads_git_inside_and_after_an_array_assignment(self) -> None:
        command = "a=($(git switch x)) b+=(1); OPTIONS=(--all) git commit; declare -a c=(`git add .`)"
        self.assertEqual(self.at(self.hub, "switch", "commit", "add"), self.read(command, tool="Bash"))
        self.assertEqual([], self.unread)
        command = "a[1]=($(git switch x)) b[k]+=(1) git commit; a[2]=(x); declare -a c[1]=(`git add .`)"
        self.assertEqual(self.at(self.hub, "switch", "commit", "add"), self.read(command, tool="Bash"))
        self.assertEqual([], self.unread)

    def test_bash_applies_location_variables_from_every_source_like_a_command_local_one(self) -> None:
        tree = f'"{self.tree}"'
        for command, expected in (
            (f"GIT_WORK_TREE={tree} git commit", self.at(self.tree, "commit")),
            (f"export GIT_WORK_TREE={tree}; git commit; git add .", self.at(self.tree, "commit", "add")),
            (f"env GIT_WORK_TREE={tree} git commit", self.at(self.tree, "commit")),
            (f"env -i GIT_WORK_TREE={tree} PATH=x git commit", self.at(self.tree, "commit")),
            (f"GIT_WORK_TREE={tree}; git commit", self.at(self.hub, "commit")),
            (f"GIT_WORK_TREE={tree}; export GIT_WORK_TREE; git commit", self.at(self.tree, "commit")),
            (f"declare -x GIT_WORK_TREE={tree}; git commit", self.at(self.tree, "commit")),
            (f"typeset -rx GIT_WORK_TREE={tree}; git commit", self.at(self.tree, "commit")),
            (f"set -a; GIT_WORK_TREE={tree}; set +a; git commit", self.at(self.tree, "commit")),
            (f"set -o allexport; GIT_WORK_TREE={tree}; git commit", self.at(self.tree, "commit")),
            (f"set -eu; GIT_WORK_TREE={tree}; git commit", self.at(self.hub, "commit")),
            (f"export GIT_WORK_TREE={tree}; unset GIT_WORK_TREE; git commit", self.at(self.hub, "commit")),
            (f"export GIT_WORK_TREE={tree}; export -n GIT_WORK_TREE; git commit", self.at(self.hub, "commit")),
            (f"export GIT_WORK_TREE={tree}; declare +x GIT_WORK_TREE; git commit", self.at(self.hub, "commit")),
            (f"GIT_WORK_TREE={tree} git --git-dir=.git commit", self.at(self.tree, "commit")),
            (
                f"(export GIT_WORK_TREE={tree}; git commit); git add .",
                [*self.at(self.tree, "commit"), *self.at(self.hub, "add")],
            ),
            (f"export GIT_WORK_TREE={tree}; bash -c 'git commit'", self.at(self.tree, "commit")),
            (f"bash -c 'export GIT_WORK_TREE={tree}'; git commit", self.at(self.hub, "commit")),
            (f"(cd {tree}); git commit", self.at(self.hub, "commit")),
            (
                f"echo $(cd {tree} && git commit) && git add .",
                [*self.at(self.tree, "commit"), *self.at(self.hub, "add")],
            ),
            (f"pushd {tree}; git commit; popd; git add .", [*self.at(self.tree, "commit"), *self.at(self.hub, "add")]),
            ("cd; git commit; cd -; git add .", [*self.at(Path.home(), "commit"), *self.at(None, "add")]),
            (f"env -C {tree} git commit", self.at(self.tree, "commit")),
            (f"env -C{tree} git commit", self.at(self.tree, "commit")),
            (f"env --chdir={tree} git commit", self.at(self.tree, "commit")),
            (f"env --chdir {tree} git commit", self.at(self.tree, "commit")),
            (f"env -v -C{tree} GIT_PAGER=cat git commit", self.at(self.tree, "commit")),
            # env changes directory once, to its last -C, read from where env starts.
            (f"env -C {tree} -C . git commit", self.at(self.hub, "commit")),
            (f"bash -lc 'cd {tree} && git commit'", self.at(self.tree, "commit")),
            (f"bash <<< 'cd {tree} && git commit'", self.at(self.tree, "commit")),
            (f"bash <<'EOF'\ncd {tree} && git commit\nEOF", self.at(self.tree, "commit")),
            (f"git -c core.worktree={tree} commit", self.at(self.tree, "commit")),
            (f"git -c core.worktree={tree} --git-dir=.git commit", self.at(self.tree, "commit")),
            (f'git --work-tree="{self.hub}" -c core.worktree={tree} commit', self.at(self.hub, "commit")),
            ("git -c core.worktree=relative commit", self.at(None, "commit")),
            ("git --config-env=core.worktree=TREE commit", self.at(None, "commit")),
            (f"GIT_CONFIG_PARAMETERS=\"'core.worktree={self.tree}'\" git commit", self.at(self.tree, "commit")),
            (
                f"GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.worktree GIT_CONFIG_VALUE_0={tree} git commit",
                self.at(self.tree, "commit"),
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.read(command, tool="Bash"))
                self.assertEqual(([], []), (self.notes, self.unread))

    def test_inherited_location_variables_apply_until_a_command_removes_them(self) -> None:
        inherited = {**self.environment, "GIT_WORK_TREE": str(self.tree)}
        for command, expected in (
            ("git commit", self.at(self.tree, "commit")),
            (f'git -C "{self.hub}" commit', self.at(self.tree, "commit")),
            ("env -i git commit", self.at(self.hub, "commit")),
            ("env -u GIT_WORK_TREE git commit", self.at(self.hub, "commit")),
            ("env --unset=GIT_WORK_TREE git commit", self.at(self.hub, "commit")),
            ("env --unset GIT_WORK_TREE git commit", self.at(self.hub, "commit")),
            ("env -uGIT_WORK_TREE git commit", self.at(self.hub, "commit")),
            ("env -uOTHER git commit", self.at(self.tree, "commit")),
            ("unset GIT_WORK_TREE; git commit", self.at(self.hub, "commit")),
            ('GIT_WORK_TREE="$ELSEWHERE_UNSET"; git commit', self.at(None, "commit")),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.read(command, tool="Bash", environment=inherited))
        allexport = {**self.environment, "SHELLOPTS": "braceexpand:allexport:hashall"}
        self.assertEqual(
            self.at(self.tree, "commit"),
            self.read(f'GIT_WORK_TREE="{self.tree}"; git commit', tool="Bash", environment=allexport),
        )
        self.assertEqual([], self.read("env -S 'git commit'", tool="Bash"))
        self.assertEqual(["could not read the option -S passed to env"], self.unread)
        # The refusal names the first option it could not read, past every one it reads.
        self.assertEqual([], self.read("env -uA -u B -CC -C D --unset=E --chdir=F -i -q git commit", tool="Bash"))
        self.assertEqual(["could not read the option -q passed to env"], self.unread)

    def test_powershell_applies_inherited_and_assigned_location_variables(self) -> None:
        inherited = {**self.environment, "GIT_WORK_TREE": str(self.tree)}
        self.assertEqual(
            [*self.at(self.tree, "commit"), *self.at(self.hub / "x", "add"), *self.at(None, "rm")],
            self.read(
                "git commit; $env:GIT_WORK_TREE = 'x'; git add .; [string]$env:GIT_WORK_TREE = 'y'; git rm y",
                environment=inherited,
            ),
        )
        command = (
            'pwsh -c "Set-Item env:GIT_DIR x; git rm x"; git restore x\n'
            "pwsh -c \"[Environment]::SetEnvironmentVariable('GIT_DIR', 'x'); git mv a b\"; git am\n"
            "pwsh -c \"New-Item -Path Env:\\GIT_DIR -Value x; bash -c 'git revert x'\"; git clean -fd\n"
            f"$env:GIT_WORK_TREE = '{self.hub}'; git commit; Set-Location '{self.tree}'; pwsh -c 'git add .'"
        )
        self.assertEqual(
            [
                ("unknown", "rm"),
                ("elsewhere", "restore"),
                ("unknown", "mv"),
                ("elsewhere", "am"),
                ("unknown", "revert"),
                ("elsewhere", "clean"),
                ("hub", "commit"),
                ("hub", "add"),
            ],
            self.reach(command),
        )
        self.assertEqual(([], []), (self.notes, self.unread))

    def test_powershell_reads_a_location_variable_each_env_drive_cmdlet_sets(self) -> None:
        # PowerShell's own binder places each argument, so a parameter abbreviated, aliased, given with a colon, or
        # left positional names the same path, and every element of an array of paths is read.
        for command in (
            "Set-Content env:GIT_DIR x",
            "Set-Content -Path Env:\\GIT_DIR -Value x",
            "Set-Content -LiteralPath ENV:/GIT_DIR x",
            "Set-Content -LP env:GIT_DIR x",
            "Set-Content env:NOTES, env:GIT_DIR x",
            "Set-Item -Value x -LiteralPath env:GIT_DIR",
            "si env:FOO, env:GIT_DIR x",
            "Rename-Item env:FOO GIT_DIR",
            "Rename-Item -NewName GIT_DIR -LiteralPath Env:\\FOO",
            "rni -Path env:FOO -NewName GIT_DIR",
            "Copy-Item env:FOO env:GIT_DIR",
            "Copy-Item -Path env:FOO -Destination ENV:\\GIT_DIR",
            "cpi env:FOO, env:BAR -Dest env:/GIT_DIR",
            "New-Item -Path env: -Name GIT_DIR -Value x",
            "ni Env:\\ -Name GIT_DIR -Value x",
            "Set-Item env:GIT_D* x",
        ):
            with self.subTest(command=command):
                self.assertEqual([("unknown", "commit")], self.reach(f"{command}; git commit"))
                self.assertEqual(([], []), (self.notes, self.unread))
        aimed = {**self.environment, "GIT_WORK_TREE": str(self.hub)}
        self.assertEqual(
            [("unknown", "commit")],
            self.reach(f"Set-Content env:GIT_WORK_TREE '{self.tree}'; git commit", self.tree, environment=aimed),
        )

    def test_a_script_pwsh_reads_from_its_standard_input_is_one_the_guard_cannot_read(self) -> None:
        for tool, command, unread in (
            ("Bash", "echo 'git commit -m x' | pwsh -NoProfile", ["could not read the command passed to pwsh"]),
            ("Bash", "echo 'git commit -m x' | pwsh -Command -", ["could not read the command passed to pwsh"]),
            ("Bash", "echo 'git commit -m x' | pwsh -f -", ["could not read the command passed to pwsh"]),
            ("PowerShell", "'git commit -m x' | pwsh -NoProfile", ["could not read the command passed to pwsh"]),
            # A pwsh that env or a prefix program starts is read as the one it starts.
            ("PowerShell", "'git commit -m x' | env pwsh -Command -", ["could not read the command passed to pwsh"]),
            ("PowerShell", "'git commit -m x' | nohup pwsh -File -", ["could not read the command passed to pwsh"]),
            ("Bash", "echo 'git commit -m x' | env pwsh -Command -", ["could not read the command passed to pwsh"]),
            ("Bash", "echo 'git commit -m x' | nice pwsh -File -", ["could not read the command passed to pwsh"]),
            ("PowerShell", "env pwsh -c 'git status'; git commit -m x", []),
            ("Bash", "pwsh -File build.ps1; git status", []),
            ("Bash", "pwsh -ExecutionPolicy Bypass build.ps1; git status", []),
            ("PowerShell", "pwsh -NoProfile -c 'git status'; git commit -m x", []),
        ):
            with self.subTest(tool=tool, command=command):
                self.read(command, tool=tool)
                self.assertEqual(unread, self.unread)

    def test_a_script_bash_or_sh_reads_from_its_standard_input_is_one_the_guard_cannot_read(self) -> None:
        for tool, command, unread in (
            ("Bash", "echo 'git commit -m x' | bash", ["could not read the command passed to bash"]),
            ("Bash", "printf 'git commit -m x' | sh -s -- one two", ["could not read the command passed to sh"]),
            ("PowerShell", "'git commit -m x' | bash", ["could not read the command passed to bash"]),
            ("Bash", "bash -s one <<< 'git commit -m x'", []),
            ("Bash", "bash script.sh; git status", []),
            # Only -s, alone or in a bundle of short options, reads the script from standard input.
            ("Bash", "bash -l script.sh; git status", []),
            ("Bash", "bash --posix script.sh; git status", []),
            ("Bash", "echo 'git commit -m x' | bash -ls one", ["could not read the command passed to bash"]),
            ("Bash", "bash < script.sh; git status", []),
        ):
            with self.subTest(tool=tool, command=command):
                self.read(command, tool=tool)
                self.assertEqual(unread, self.unread)

    def test_both_readers_leave_unknown_what_a_name_they_cannot_settle_may_set(self) -> None:
        # Bash's export "$v"=x may set any variable, and so may a cmdlet writing a provider path the text does not
        # settle; a path whose last part is literal text names that variable alone, which git does not read.
        for tool, command, expected in (
            ("Bash", 'export "$v"=x', "unknown"),
            ("Bash", 'unset "$v"', "unknown"),
            ("PowerShell", 'Set-Item "env:$v" x', "unknown"),
            ("PowerShell", "Set-Item $p x", "unknown"),
            ("PowerShell", "Set-Content -Path $p -Value x", "unknown"),
            ("PowerShell", "Set-Content $target x", "unknown"),
            ("PowerShell", 'Set-Item "$d\\GIT_DIR" x', "unknown"),
            ("PowerShell", "Copy-Item env:FOO $destination", "unknown"),
            ("PowerShell", "Remove-Item $p", "unknown"),
            ("PowerShell", "Rename-Item env:FOO $n", "unknown"),
            ("PowerShell", "New-Item -Path env: -Name $n -Value x", "unknown"),
            ("PowerShell", "New-Item $p -Value x", "unknown"),
            ("PowerShell", "Remove-Item -LiteralPath $p", "unknown"),
            ("PowerShell", "Rename-Item $p NEW", "unknown"),
            # A cmdlet that neither writes, removes, nor renames a variable reaches none, whatever path it reads.
            ("PowerShell", "Get-Content $p", "elsewhere"),
            ("Bash", 'printf x > "$d/out.txt"', "elsewhere"),
            ("PowerShell", 'Set-Content "$d\\out.txt" x', "elsewhere"),
            ("PowerShell", "New-Item -Path C:\\x -Name $n -ItemType Directory", "elsewhere"),
        ):
            with self.subTest(tool=tool, command=command):
                self.assertEqual([(expected, "commit")], self.reach(f"{command}; git commit", tool=tool))

    def test_powershell_reads_a_location_variable_set_through_the_provider_qualified_path(self) -> None:
        # Environment:: names the environment provider as the env: drive does, with or without its module.
        for command in (
            "Set-Item Environment::GIT_DIR x",
            "Set-Item -Path Microsoft.PowerShell.Core\\Environment::GIT_DIR -Value x",
            "Set-Content environment::GIT_DIR x",
            "New-Item -Path Environment::GIT_DIR -Value x",
            "New-Item -Path Environment:: -Name GIT_DIR -Value x",
            "ni Microsoft.PowerShell.Core\\Environment::\\ -Name GIT_DIR -Value x",
            "Copy-Item Environment::FOO Environment::GIT_DIR",
            "Copy-Item -Path env:FOO -Destination Microsoft.PowerShell.Core\\Environment::GIT_DIR",
            "Rename-Item Environment::FOO GIT_DIR",
            "Rename-Item -LiteralPath Microsoft.PowerShell.Core\\Environment::FOO -NewName GIT_DIR",
            "Set-Item \\Environment::GIT_DIR x",
            "Set-Content -Path \\ENVIRONMENT::GIT_DIR -Value x",
            "Copy-Item env:FOO \\Environment::GIT_DIR",
            "New-Item -Path Environment::\\ -Name GIT_DIR",
            "Rename-Item Environment::\\FOO GIT_DIR",
        ):
            with self.subTest(command=command):
                self.assertEqual([("unknown", "commit")], self.reach(f"{command}; git commit"))
                self.assertEqual(([], []), (self.notes, self.unread))
        for command in (
            "Copy-Item Environment::GIT_DIR env:SAVED",
            "New-Item -Path Environment::FOO -Name GIT_DIR",
            # PowerShell takes \Environment:: as Environment::, though not \Microsoft.PowerShell.Core\Environment::.
            "Set-Item \\Microsoft.PowerShell.Core\\Environment::GIT_DIR x",
            "Set-Item /Environment::GIT_DIR x",
            # After Environment:: PowerShell keeps a separator in the variable's name, which git never reads.
            "Set-Item Environment::\\GIT_DIR x",
            "Set-Item Environment::/GIT_DIR x",
            "Set-Content -Path Microsoft.PowerShell.Core\\Environment::\\GIT_DIR -Value x",
            "New-Item -Path \\Environment::/GIT_DIR -Value x",
            "Copy-Item env:FOO Environment::\\GIT_DIR",
            "${Environment::\\GIT_DIR} = 'x'",
        ):
            with self.subTest(command=command):
                self.assertEqual([("elsewhere", "commit")], self.reach(f"{command}; git commit"))

    def test_powershell_reads_a_location_variable_assigned_through_variable_notation(self) -> None:
        # Variable notation takes the paths a cmdlet does: ${env:\NAME} sets NAME, as Set-Item env:\NAME does, and a
        # plain assignment sets the value it gives.
        repository = self.hub / ".git"
        for command in (
            f"${{Environment::GIT_WORK_TREE}} = '{self.hub}'",
            f"${{Microsoft.PowerShell.Core\\Environment::GIT_DIR}} = '{repository}'",
            f"${{\\Environment::GIT_DIR}} = '{repository}'",
            f"${{env:GIT_DIR}} = '{repository}'",
            f'${{env:\\GIT_DIR}} = "{repository}"',
            f"$env:GIT_WORK_TREE = $env:HOMEDRIVE; $env:GIT_WORK_TREE = '{self.hub}'",
        ):
            with self.subTest(command=command):
                self.assertEqual([("hub", "reset")], self.reach(f"{command}; git reset --hard"))
                self.assertEqual(([], []), (self.notes, self.unread))
        # A compound assignment, a cast or attribute, parentheses, and a multiple assignment assign the variable they
        # wrap with a value the guard does not settle.
        for command in (
            "${ENV:/GIT_DIR} += 'x'",
            "[string]$env:GIT_DIR = 'x'",
            "[ValidateNotNull()][string]${Environment::GIT_DIR} = 'x'",
            f"(([string]${{Environment::GIT_DIR}})) = '{self.hub}'",
            "($env:GIT_DIR, $other) = 'x', 2",
            "([string]$env:GIT_DIR), $other = 'x', 2",
            "$other, $env:GIT_WORK_TREE = $values",
            "$env:GIT_DIR, $other = @(Get-Content values.txt)",
        ):
            with self.subTest(command=command):
                self.assertEqual([("unknown", "reset")], self.reach(f"{command}; git reset --hard"))
                self.assertEqual(([], []), (self.notes, self.unread))
        for command in (
            "${variable:GIT_DIR} = 'x'",
            "${GIT_DIR} = 'x'",
            "${Env::GIT_DIR} = 'x'",
            "($env:NOTES) = 'x'",
            "($GIT_DIR, $other) = 'x', 2",
            "$env:GIT_COMMON_DIR = 'x'",
        ):
            with self.subTest(command=command):
                self.assertEqual([("elsewhere", "reset")], self.reach(f"{command}; git reset --hard"))

    def test_powershell_passes_over_cmdlets_that_set_no_location_variable(self) -> None:
        inherited = {**self.environment, "GIT_WORK_TREE": str(self.tree)}
        for command in (
            "Set-Content notes.txt GIT_DIR",
            "Set-Content -Path notes.txt -Value env:GIT_DIR",
            "Set-Content env:NOTES GIT_DIR",
            # A computed path whose last part is literal text names only that variable.
            'Set-Content "$target\\notes.txt" x',
            "Rename-Item notes.txt GIT_DIR",
            # The provider takes -NewName as a bare name, so this sets a variable named env:GIT_DIR.
            "Rename-Item env:FOO env:GIT_DIR",
            "Copy-Item env:GIT_DIR env:SAVED",
            "Copy-Item notes.txt GIT_DIR",
            "New-Item -Path . -Name GIT_DIR",
            # The env: drive cannot seek within a variable, so Add-Content fails there.
            "Add-Content env:GIT_DIR x",
        ):
            with self.subTest(command=command):
                self.assertEqual([("elsewhere", "commit")], self.reach(f"{command}; git commit"))
                self.assertEqual(([], []), (self.notes, self.unread))
        self.assertEqual(
            self.at(self.tree, "commit"),
            self.read("Copy-Item env:GIT_WORK_TREE env:SAVED; git commit", environment=inherited),
        )

    def test_what_cannot_be_read_is_recorded_as_unread(self) -> None:
        self.assertEqual(
            [("elsewhere", None)], self.reach("& $git commit; git $verb x; pwsh -c $script; iex $text; pwsh -c")
        )
        self.assertIn("a git subcommand is computed at run time", self.notes)
        self.assertIn("could not read the command passed to pwsh", self.unread)
        self.assertIn("could not read the command passed to iex", self.unread)
        self.assertEqual([], self.read("git commit -m 'unterminated"))
        self.assertEqual(1, len(self.notes))
        self.assertIn("PowerShell would not parse the command, so none of it runs", self.notes[0])
        self.assertEqual([], self.read("git commit -m 'unterminated", tool="Bash"))
        self.assertIn("a single quote is not closed", self.unread[0])

    def test_pwsh_starts_only_for_text_that_could_hold_a_refused_git_call(self) -> None:
        # A PATH that names no existing directory: an empty or missing one would let Windows find pwsh anyway.
        unavailable = {**self.environment, "PATH": str(self.root / "no-such-directory")}
        missing = ["could not find pwsh to read a PowerShell command"]
        self.assertEqual([], self.read("git status; git log --oneline; Get-ChildItem", environment=unavailable))
        self.assertEqual(([], []), (self.notes, self.unread))
        for command in ("git commit -m x", "git fetch -u origin main:main", "git log $range", "git -c alias.x=y x"):
            with self.subTest(command=command):
                self.assertEqual([], self.read(command, environment=unavailable))
                self.assertEqual(missing, self.unread)
        self.git(self.hub, "config", "alias.co", "checkout")
        self.assertEqual([], self.read("git co feat/demo", environment=unavailable))
        self.assertEqual(missing, self.unread)
        self.assertEqual([], self.read("git cooperate; git log", environment=unavailable))
        self.assertEqual(([], []), (self.notes, self.unread))
        outside = self.root / "outside"
        outside.mkdir()
        self.assertEqual([], self.read("git cooperate", outside, environment=unavailable))
        self.assertEqual(missing, self.unread)


class PowerShellGuardTests(GuardTestCase):
    """End to end through the hook, which starts a real pwsh; the reading itself is covered by CommandReadingTests."""

    end_to_end = True

    def test_the_hub_refuses_powershell_git_writes_but_allows_maintenance_and_work_in_a_worktree(self) -> None:
        self.assertEqual("deny", self.powershell("git switch feat/demo", self.hub))
        self.assertIsNone(self.powershell("git status; git switch main; git pull --ff-only; git stash list", self.hub))
        self.assertIsNone(self.powershell(f"Set-Location '{self.tree}'; git commit -m \"it's done\"", self.hub))

    def test_a_worktree_session_cannot_reach_into_the_hub_through_either_shell(self) -> None:
        self.assertIsNone(self.powershell("git commit -m x", self.tree))
        self.assertEqual("deny", self.powershell(f"Set-Location '{self.hub}'; git commit -m x", self.tree))
        self.assertEqual("deny", self.bash(f"pwsh -c 'git -C \"{self.hub}\" switch feat/demo'", self.tree))

    def test_a_location_variable_set_where_the_text_does_not_settle_its_value_is_refused(self) -> None:
        for command in (
            "Rename-Item env:FOO GIT_WORK_TREE; git commit -m x",
            f"Set-Item Environment::GIT_WORK_TREE '{self.hub}'; git reset --hard",
            f"($env:GIT_WORK_TREE) = '{self.hub}'; $other, $env:GIT_DIR = 2, 'x'; git reset --hard",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
                self.assertIn("The guard could not tell where", self.reason)
        self.assertEqual("deny", self.powershell("Set-Content notes.txt GIT_WORK_TREE; git commit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_powershell_command_pwsh_cannot_read_is_refused_while_a_hub_is_guarded(self) -> None:
        git = shutil.which("git")
        if git is None:
            self.fail("git is not on PATH")
        folder = Path(git).parent
        if not platform_support.WINDOWS:
            # The system's folder holds pwsh beside git, so a folder of git alone stands in for it.
            folder = self.root / "git only"
            folder.mkdir()
            (folder / "git").symlink_to(git)
        path = os.pathsep.join([str(folder), str(self.root / "no-such-directory")])
        environment = {**self.environment, "PATH": path}
        for cwd in (self.hub, self.tree):
            with self.subTest(cwd=str(cwd)):
                self.assertEqual("deny", self.powershell("git switch feat/demo", cwd, environment))
                self.assertIn("could not find pwsh", self.reason)
        self.assertIsNone(self.powershell("git status", self.tree, environment))


class ContractTests(GuardTestCase):
    """One case for each construct the guard's contract in docs/parallel-sessions.md names, through the hook."""

    def nested(self, command: str, depth: int) -> str:
        for _ in range(depth):
            command = "iex '" + command.replace("'", "''") + "'"
        return command

    def test_remove_item_and_clear_item_of_an_inherited_location_variable_remove_it(self) -> None:
        # A git hook's environment names the worktree's own repository and work tree, wherever its command runs.
        repository = self.hub / ".git" / "worktrees" / "feat-demo"
        inherited = {**self.environment, "GIT_DIR": str(repository), "GIT_WORK_TREE": str(self.tree)}
        self.assertIsNone(self.powershell("git reset --hard", self.hub, inherited))
        for removal in (
            "Remove-Item env:GIT_WORK_TREE",
            "Remove-Item Environment::GIT_WORK_TREE",
            "Clear-Item env:GIT_WORK_TREE",
            "Clear-Item Environment::GIT_WORK_TREE",
            "ri env:GIT_*",
            "$env:GIT_WORK_TREE = $null",
            "[Environment]::SetEnvironmentVariable('GIT_WORK_TREE', $null)",
        ):
            with self.subTest(removal=removal):
                self.assertEqual("deny", self.powershell(f"{removal}; git reset --hard", self.hub, inherited))
                self.assertIn(WRITES.format("reset"), self.reason)

    def test_a_cmdlet_whose_binding_fails_or_that_runs_with_whatif_sets_nothing(self) -> None:
        for command in (
            "Set-Item -Path Environment::GIT_DIR -Name X -Value x; git commit -m x",
            "Set-Item env:GIT_DIR -Bogus x; git commit -m x",
            "Set-Item Environment::GIT_DIR x -WhatIf; git commit -m x",
            "Set-Content -Path env:GIT_DIR -Encoding utf8 x; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
                # Set, GIT_DIR would leave the call unplaced; PowerShell sets nothing, so it is the worktree's.
                self.assertIsNone(self.powershell(command, self.tree))

    def test_a_foreach_variable_psvariable_set_or_a_new_environment_drive_reaches_the_judgment(self) -> None:
        for command in (
            f"foreach ($env:GIT_WORK_TREE in '{self.hub}') {{ }}; git reset --hard",
            f"$ExecutionContext.SessionState.PSVariable.Set('env:GIT_WORK_TREE', '{self.hub}'); git reset --hard",
            "$ExecutionContext.SessionState.PSVariable.Set($name, 'x'); git reset --hard",
            "New-PSDrive -Name e -PSProvider Environment -Root '' | Out-Null; Set-Item e:GIT_WORK_TREE x;"
            " git reset --hard",
            "[Environment]::SetEnvironmentVariable($name, 'x'); git reset --hard",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
                self.assertIn("The guard could not tell where `git reset` runs", self.reason)

    def test_a_location_variable_counts_only_in_the_case_git_reads_it(self) -> None:
        command = f"$env:git_dir = '{self.hub / '.git'}'; git commit"
        reading = hub_guard.read_command("PowerShell", command, self.tree, self.environment)
        # Windows reads an environment name in any case; elsewhere git_dir is a variable git never reads.
        hub, unknown = hub_guard.reached_hub(reading.calls[0].place)
        self.assertEqual((platform_support.WINDOWS, False), (hub is not None, unknown))
        for command, expected in (
            # With no path, New-Item writes where the session is, on the file system.
            ("New-Item -Name GIT_DIR -Value x; git commit", (None, False)),
            # Every path of an array is written, so one on the environment provider sets the variable.
            ("New-Item -Path ., env: -Name GIT_DIR -Value x; git commit", (None, True)),
        ):
            with self.subTest(command=command):
                reading = hub_guard.read_command("PowerShell", command, self.tree, self.environment)
                self.assertEqual(expected, hub_guard.reached_hub(reading.calls[0].place))

    def test_bash_keywords_prefixes_and_backquotes_before_git_are_read(self) -> None:
        for command, subcommand in (
            ("if git diff --quiet; then echo clean; else git stash; fi", "stash"),
            ("{ git switch feat/demo; }", "switch"),
            ("for f in a b; do git add $f; done", "add"),
            ("! git commit -m x", "commit"),
            ("exec git switch feat/demo", "switch"),
            ("command git switch feat/demo", "switch"),
            ("time git commit -m x", "commit"),
            ("nohup git commit -m x", "commit"),
            ("timeout 5 git commit -m x", "commit"),
            ("nice git commit -m x", "commit"),
            ("stdbuf -o L git commit -m x", "commit"),
            ("select x in a; do git commit -m x; done", "commit"),
            ("$'git' $'commit' -m $'a\\nb'", "commit"),
            ("echo `git switch feat/demo`", "switch"),
            ("while true; do git reset --hard; done", "reset"),
            ("case x in x) git clean -fd;; esac", "clean"),
            ("f() { git commit -m x; }; f", "commit"),
            ("eval 'git commit -m x'", "commit"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(WRITES.format(subcommand), self.reason)
        self.assertIsNone(self.bash("command -v git", self.hub))

    def test_a_short_option_value_attached_or_separate_is_read_in_both_shells(self) -> None:
        for prefix in (
            "nice -n5",
            "nice -n 5",
            "nice --adjustment=5",
            "stdbuf -oL",
            "stdbuf -oL -eL",
            "timeout -k5 10",
        ):
            # Both readers share the reading of a prefix's options, so PowerShell takes the attached spellings only.
            for shell in (self.bash, self.powershell) if prefix in {"nice -n5", "stdbuf -oL"} else (self.bash,):
                with self.subTest(prefix=prefix, shell=shell.__name__):
                    self.assertEqual("deny", shell(f"{prefix} git commit -m x", self.hub))
                    self.assertIn(WRITES.format("commit"), self.reason)
                    self.assertIsNone(shell(f"{prefix} git commit -m x", self.tree))
                    self.assertIsNone(shell(f"{prefix} git status", self.hub))
        # A bundle of short options, or an option the guard does not know, stays unread, and a command that names git
        # with one is refused while a guarded hub is in view, from a worktree too.
        for command in ("nice -x5 git status", "timeout -vk5 10 git status", "stdbuf -oL --bogus git status"):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn("The guard could not read part of this command", self.reason)

    def test_env_and_the_prefix_programs_are_read_in_powershell(self) -> None:
        for command in (
            "env git commit -m x",
            f"env -C '{self.hub}' git commit -m x",
            "nohup git commit -m x",
            "time -p git commit -m x",
            "nice bash -c 'git commit -m x'",
            "env pwsh -c 'git commit -m x'",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub if "-C" not in command else self.tree))
                self.assertIn(WRITES.format("commit"), self.reason)
        self.assertIsNone(self.powershell("env git commit -m x", self.tree))

    def test_each_prefix_program_the_contract_names_is_read_as_the_command_it_starts(self) -> None:
        # One case for each program or builtin the contract names for each shell; ContractListTests holds the
        # contract's lists to the readers' own sets.
        cases = {
            "exec": "exec git commit -m x",
            "command": "command git commit -m x",
            "builtin": f"builtin cd '{self.hub}' && git commit -m x",
            "nohup": "nohup git commit -m x",
            "nice": "nice -n5 git commit -m x",
            "timeout": "timeout 5 git commit -m x",
            "stdbuf": "stdbuf -oL git commit -m x",
            "time": "time -p git commit -m x",
            "env": "env git commit -m x",
        }
        # In Bash `time` as a command's first word is the reserved word; after an assignment it is the program.
        bash_cases = {**cases, "time": "x=1 time -p git commit -m x"}
        for shell, named, shell_cases in (
            (self.bash, contract_names("Commands after", "are read as the commands they start"), bash_cases),
            (self.powershell, contract_names("A command after the programs", "is read as in Bash"), cases),
        ):
            self.assertLessEqual(set(named), set(shell_cases))
            for program in named:
                with self.subTest(program=program, shell=shell.__name__):
                    start = self.tree if program == "builtin" else self.hub
                    self.assertEqual("deny", shell(shell_cases[program], start))
                    self.assertIn(WRITES.format("commit"), self.reason)

    def test_env_dash_s_is_refused_whatever_follows_it(self) -> None:
        # pwsh starts only for a command that names a refused subcommand, so PowerShell takes the writing one alone.
        for shell, command in (
            (self.bash, "env -S 'git status'"),
            (self.bash, "env -S 'git commit -m x'"),
            (self.powershell, "env -S 'git commit -m x'"),
        ):
            with self.subTest(command=command, shell=shell.__name__):
                self.assertEqual("deny", shell(command, self.tree))
                self.assertIn("could not read the option -S passed to env", self.reason)

    def test_builtin_cd_a_cdpath_search_and_powershell_cd_plus_and_home_are_followed(self) -> None:
        self.assertEqual("deny", self.bash(f"builtin cd '{self.hub}' && git commit -m x", self.tree))
        self.assertIn(WRITES.format("commit"), self.reason)
        # Without CDPATH, `cd '<hub>'` from the worktree names a folder there is not, so it fails and git never runs;
        # with it, a search the guard does not follow.
        self.assertIsNone(self.bash(f"cd '{HUB}' && git commit -m x", self.tree))
        command = f"export CDPATH='{self.root}'; cd '{HUB}' && git commit -m x"
        self.assertEqual("deny", self.bash(command, self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell("cd +; git commit -m x", self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        home = {**self.environment, "HOME": str(self.root), "USERPROFILE": str(self.root)}
        self.assertEqual("deny", self.powershell(f"cd '~/{HUB}'; git commit -m x", self.tree, home))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertIsNone(self.powershell(f"cd '~/{HUB}/{TREE.as_posix()}'; git commit -m x", self.hub, home))

    def test_the_named_git_options_take_their_value_attached_or_as_the_next_word(self) -> None:
        for option in ("--namespace", "--super-prefix"):
            for spelling in (f"{option} x/", f"{option}=x/"):
                with self.subTest(spelling=spelling):
                    self.assertEqual("deny", self.bash(f"git {spelling} commit -m x", self.hub))
                    self.assertIn(WRITES.format("commit"), self.reason)
                    self.assertIsNone(self.bash(f"git {spelling} -C '{self.tree}' commit -m x", self.hub))

    def test_the_named_subcommands_are_refused_in_the_hub_and_stash_show_is_allowed(self) -> None:
        for subcommand in ("cherry-pick x", "archimport x", "cvsimport x", "p4 sync", "quiltimport", "svn rebase"):
            with self.subTest(subcommand=subcommand):
                self.assertEqual("deny", self.bash(f"git {subcommand}", self.hub))
                self.assertIn(WRITES.format(subcommand.split()[0]), self.reason)
        self.assertIsNone(self.bash("git stash show; git stash list", self.hub))
        self.assertEqual("deny", self.bash("git stash pop", self.hub))
        # gitk is a program of its own, which git does not run as a subcommand.
        self.assertNotIn("gitk", hub_guard.WRITING_SUBCOMMANDS)

    def test_powershell_dash_command_from_bash_is_read(self) -> None:
        self.assertEqual("deny", self.bash("powershell -Command 'git commit -m x'", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertIsNone(self.bash("powershell -Command 'git status'", self.hub))

    def test_a_git_call_after_any_assignment_bash_reads_before_it_is_judged_as_the_bare_call(self) -> None:
        tree = f'"{self.tree}"'
        for prefix in (
            "a[1]=x",
            "a[1]+=x",
            "x=1 a[1]=y b+=z c[k]+=w",
            'a[1]="v w"',
            "a[x y]=v",
            'a["k]"]=v',
            "a[1]=(x)",
            # Bash refuses an element's assignment before a command, sets nothing for it, and runs the command.
            f"GIT_WORK_TREE[0]={tree}",
            "env 'a[1]=x'",
            "env 'x y=1'",
        ):
            command = f"{prefix} git commit -m x"
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
                self.assertIsNone(self.bash(command, self.tree))
                self.assertIsNone(self.bash(f"{prefix} git status", self.hub))
        # On its own an element's assignment makes the variable an array, whose value the guard does not settle.
        command = f"export GIT_WORK_TREE={tree}; GIT_WORK_TREE[0]={tree}; git commit -m x"
        self.assertEqual("deny", self.bash(command, self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)

    def test_a_git_call_after_any_redirection_bash_reads_before_it_is_judged_as_the_bare_call(self) -> None:
        for template in (
            ">out {git}",
            "2>/dev/null {git}",
            "<in {git}",
            ">>out {git}",
            "&>out {git}",
            "&>>out {git}",
            "<<<'a b' {git}",
            "2>&1 {git}",
            ">&2 {git}",
            "2>&- {git}",
            "<>file {git}",
            ">|out {git}",
            "{{fd}}>out {git}",
            "<<EOF {git}\nbody\nEOF",
            '2>/dev/null >"a b" {git}',
            "2>/dev/null x=1 {git}",
            "x=1 2>/dev/null {git}",
            "x=1 2>/dev/null y=2 >out {git}",
            "2>/dev/null a[x y]=v {git}",
            "! 2>/dev/null {git}",
            "true && 2>/dev/null {git}",
        ):
            command = template.format(git="git commit -m x")
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
                self.assertIsNone(self.bash(command, self.tree))
                self.assertIsNone(self.bash(template.format(git="git status"), self.hub))
        # Where `time` is not a command's first word, or is quoted, Bash runs the program, which starts git.
        for command in (
            "2>/dev/null time git commit -m x",
            "x=1 time -p git commit -m x",
            "\\time -f %e -o out --append git commit -m x",
            "'time' --format=%e -- git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
                self.assertIsNone(self.bash(command, self.tree))
        self.assertEqual("deny", self.bash("\\time --bogus git commit -m x", self.hub))
        self.assertIn("could not read the options passed to time", self.reason)
        # An assignment after a leading redirection, or after a redirection that follows an assignment, still
        # reaches git, as Bash still assigns it.
        for prefix in ("2>/dev/null", "x=1 2>/dev/null"):
            command = f'{prefix} GIT_WORK_TREE="{self.hub}" git reset --hard'
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(WRITES.format("reset"), self.reason)

    def test_a_directory_change_that_runs_only_on_some_paths_leaves_the_directory_unknown(self) -> None:
        for command in (
            f'if [ -d x ]; then cd "{self.tree}"; fi; git commit -m x',
            f'test -d x && cd "{self.tree}"; git commit -m x',
            f'cd "{self.root / "no such folder"}"; git commit -m x',
            f'f() {{ cd "{self.tree}"; }}; git commit -m x',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
        self.assertIsNone(self.bash(f'cd "{self.tree}" || exit 1; git commit -m x', self.hub))
        self.assertIsNone(self.bash(f'f() {{ cd "{self.tree}"; }}; f; git commit -m x', self.hub))

    def test_a_set_location_powershell_cannot_bind_moves_nothing(self) -> None:
        for command in (
            f"cd -p '{self.tree}'; git commit -m x",
            f"Set-Location '{self.tree}' '{self.root}'; git commit -m x",
            f"cd -Path '{self.tree}' -LiteralPath '{self.tree}'; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_bash_word_holding_a_variable_the_text_does_not_set_is_not_settled(self) -> None:
        for command, subcommand in (
            ('cd "$HUB_NOT_SET_HERE" && git commit -m x', "commit"),
            ('git -C "$HUB_NOT_SET_HERE" switch feat/demo', "switch"),
            ('GIT_WORK_TREE="$HUB_NOT_SET_HERE" git reset --hard', "reset"),
            ('cd "$(git rev-parse --show-toplevel)/.." && git commit -m x', "commit"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(f"The guard could not tell where `git {subcommand}` runs", self.reason)
        self.assertEqual("deny", self.bash(f'HUB="{self.hub}"; cd "$HUB" && git commit -m x', self.tree))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertIsNone(self.bash(f'TREE="{self.tree}"; cd "$TREE" && git commit -m x', self.hub))
        self.assertIsNone(self.bash('cd "$PWD" && git commit -m x', self.tree))

    def test_pwsh_command_with_args_is_read_as_its_script(self) -> None:
        script = f'git -C "{self.hub}" switch feat/demo'
        self.assertEqual("deny", self.bash(f"pwsh -cwa '{script}'", self.tree))
        self.assertEqual("deny", self.bash(f"pwsh -CommandWithArgs '{script}' one two", self.tree))
        self.assertEqual("deny", self.powershell(f"pwsh -cwa '{script}'", self.tree))
        self.assertIsNone(self.bash("pwsh -CommandWithArgs 'git status $args' one", self.hub))

    def test_the_nesting_limits_and_the_reading_time_are_pinned(self) -> None:
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(
            (4, 8, 15, 10.0, 30),
            (
                hub_guard.NESTING_LIMIT,
                hub_guard.LOOP_NESTING_LIMIT,
                hub_guard.READING_SECONDS,
                worktrees.GUARD_GIT_TIMEOUT_SECONDS,
                hook["timeout"],
            ),
        )
        document = (REPOSITORY_ROOT / "docs" / "parallel-sessions.md").read_text(encoding="utf-8")
        self.assertIn("more than four deep", document)
        self.assertIn("more than eight deep", document)
        self.assertIn("within 15 seconds", document)
        # The PowerShell reader follows Invoke-Expression to the limit worktrees.py hands it.
        self.assertEqual("deny", self.powershell(self.nested("git commit -m x", 3), self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell(self.nested("git commit -m x", 4), self.hub))
        self.assertIn("could not read the command passed to iex", self.reason)

    def test_inherited_command_line_configuration_cannot_switch_the_opt_in_off(self) -> None:
        for variables in (
            {"GIT_CONFIG_PARAMETERS": "'coding-agent-skills.hubGuard=false'"},
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "coding-agent-skills.hubGuard",
                "GIT_CONFIG_VALUE_0": "false",
            },
        ):
            with self.subTest(variables=variables):
                environment = {**self.environment, **variables}
                self.assertEqual("deny", self.edit(self.hub / "README.md", environment=environment))
                self.assertEqual("deny", self.bash("git switch feat/demo", self.hub, environment))

    def test_fetch_with_update_head_ok_is_refused_in_the_hub(self) -> None:
        for command in (
            "git fetch --update-head-ok . feat/demo:main",
            "git fetch -u origin main:main",
            "git fetch -qu origin main:main",
            "git fetch --update-h origin main:main",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn("`git fetch --update-head-ok` would move the branch the hub's HEAD names.", self.reason)
                self.assertIsNone(self.bash(command, self.tree))
        for command in ("git fetch origin", "git fetch -j 2 origin", "git fetch -o u origin", "git fetch -- -u"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_attr_source_takes_the_next_word_as_its_value(self) -> None:
        for shell in (self.bash, self.powershell):
            with self.subTest(shell=shell.__name__):
                self.assertEqual("deny", shell("git --attr-source HEAD commit -m x", self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.bash("git --frobnicate commit -m x", self.hub))
        self.assertIn("could not tell which git subcommand runs", self.reason)

    def test_a_git_dir_naming_the_hubs_git_directory_writes_the_hub(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        git_dir = self.hub / ".git"
        for command, cwd in (
            (f'git --git-dir="{git_dir}" commit -m x', self.tree),
            (f'GIT_DIR="{git_dir}" git switch other', self.tree),
            (f'git --git-dir="{git_dir}" commit -m x', outside),
            (f'git --git-dir "{git_dir}" --work-tree "{self.tree}" reset --hard', self.tree),
        ):
            with self.subTest(command=command, cwd=str(cwd)):
                self.assertEqual("deny", self.bash(command, cwd))
                self.assertIn("would move the hub's HEAD", self.reason)
        self.assertEqual("deny", self.powershell(f"git --git-dir='{git_dir}' commit -m x", self.tree))
        other = self.other_repository() / ".git"
        self.assertIsNone(self.bash(f'git --git-dir="{other}" commit -m x', self.tree))
        self.assertIsNone(self.bash(f'GIT_DIR="{self.root / "no repository"}" git commit -m x', self.tree))

    def test_a_git_directory_git_cannot_read_is_refused_never_taken_for_no_hub(self) -> None:
        other = self.other_repository() / ".git"
        (other / "config").write_text("[core\nbogus\n", encoding="utf-8")
        self.assertEqual("deny", self.bash(f'git --git-dir="{other}" commit -m x', self.tree))
        self.assertIn("cannot place it while a hub is guarded", self.reason)
        self.assertEqual("deny", self.edit(other / "HEAD", tool="Write"))

    def test_a_here_document_body_is_data_not_commands(self) -> None:
        command = f"cat > run.sh <<EOF\ncd '{self.tree}'\ngit status\nEOF\ngit add run.sh"
        self.assertEqual("deny", self.bash(command, self.hub))
        self.assertIn(WRITES.format("add"), self.reason)
        self.assertIsNone(self.bash(f"cat <<'EOF'\ngit -C '{self.hub}' switch feat/demo\nEOF", self.tree))
        # An unquoted body expands, so a command substitution in it runs.
        self.assertEqual("deny", self.bash(f"cat <<EOF\n$(git -C '{self.hub}' switch feat/demo)\nEOF", self.tree))

    def test_cd_double_dash_changes_to_the_directory_after_it(self) -> None:
        for command in (
            f'cd -- "{self.hub}" && git commit -m x',
            f'cd -P -- "{self.hub}" && git commit -m x',
            f'pushd -- "{self.hub}" && git commit -m x',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_file_tool_writing_under_the_hubs_git_directory_is_refused(self) -> None:
        for path in ("HEAD", "config", "hooks/pre-commit", "info/exclude", "worktrees/feat-demo/HEAD"):
            with self.subTest(path=path):
                self.assertEqual("deny", self.edit(self.hub / ".git" / path, tool="Write"))
        self.assertEqual("deny", self.edit(self.tree / ".." / ".." / ".." / ".git" / "HEAD", tool="Write"))
        self.assertIsNone(self.edit(self.tree / ".git", tool="Write"))
        other = self.other_repository()
        self.assertIsNone(self.edit(other / ".git" / "config", tool="Write"))

    def test_a_hub_alias_is_read_when_powershell_starts_outside_the_repository(self) -> None:
        self.git(self.hub, "config", "alias.co", "checkout")
        outside = self.root / "outside"
        outside.mkdir()
        self.assertEqual("deny", self.powershell(f"Set-Location '{self.hub}'; git co feat/demo", outside))
        self.assertIn("is an alias", self.reason)
        self.assertEqual("deny", self.bash(f"cd '{self.hub}' && git co feat/demo", outside))

    def test_an_alias_is_read_wherever_its_call_runs(self) -> None:
        # A worktree shares the hub's repository configuration, so an alias the hub defines is defined there too.
        for name, expansion in (
            ("hop", f'!git -C "{self.hub}" commit -m x'),
            ("into", f'-C "{self.hub}" switch'),
            ("save", "!git commit -m x"),
            ("st", "status --short"),
            ("unbalanced", "checkout 'feat/demo"),
        ):
            self.git(self.hub, "config", f"alias.{name}", expansion)
        for shell in (self.bash, self.powershell):
            with self.subTest(shell=shell.__name__):
                self.assertEqual("deny", shell("git hop", self.tree))
                self.assertIn("is an alias", self.reason)
                self.assertIn(WRITES.format("commit"), self.reason)
                self.assertEqual("deny", shell("git into feat/demo", self.tree))
                self.assertIn(WRITES.format("switch"), self.reason)
                self.assertEqual("deny", shell("git unbalanced", self.tree))
                self.assertIn("could not be read", self.reason)
                self.assertIsNone(shell("git save", self.tree))
                self.assertIsNone(shell("git st", self.tree))
        # With no guarded hub in view, an alias that cannot be read is allowed with a note; one writing a hub is not.
        other = self.other_repository()
        self.git(other, "config", "alias.unbalanced", "checkout 'x")
        self.git(other, "config", "alias.hop", f'!git -C "{self.hub}" commit -m x')
        for shell in (self.bash, self.powershell):
            with self.subTest(shell=shell.__name__, cwd="other"):
                self.assertIsNone(shell("git unbalanced", other))
                self.assertIn("could not be read", self.stderr)
                self.assertEqual("deny", shell("git hop", other))
                self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_subcommand_quoted_in_part_is_the_word_each_shell_makes_of_it(self) -> None:
        for command in ("git com'mit' -m x", 'git co"mm"it -m x', "g'it' commit -m x"):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, self.hub))
                    self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell("git co`mmit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.bash("git co\\mmit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_variable_the_command_sets_is_read_with_the_value_it_sets(self) -> None:
        # The variable holds another place where the hook starts, so only the command's own value names the hub.
        environment = {**self.environment, "HUB_PLACE": str(self.root)}
        for bash, powershell in (
            (
                f'HUB_PLACE="{self.hub}"; cd "$HUB_PLACE";',
                f"$env:HUB_PLACE = '{self.hub}'; Set-Location $env:HUB_PLACE;",
            ),
            (
                f'export HUB_PLACE="{self.hub}"; cd "$HUB_PLACE/.";',
                f"${{env:HUB_PLACE}} = '{self.hub}'; Set-Location \"$env:HUB_PLACE/.\";",
            ),
            (
                f'export GIT_WORK_TREE="{self.hub}";',
                f"$env:GIT_WORK_TREE = '{self.hub}';",
            ),
            (
                f'export GIT_DIR="{self.hub / ".git"}";',
                f"${{Environment::GIT_DIR}} = '{self.hub / '.git'}';",
            ),
        ):
            for shell, command in ((self.bash, bash), (self.powershell, powershell)):
                with self.subTest(command=command):
                    self.assertEqual("deny", shell(f"{command} git reset --hard", self.tree, environment))
                    self.assertIn(WRITES.format("reset"), self.reason)
        for bash, powershell in (
            (f'GIT_WORK_TREE="{self.tree}" git commit -m x', f"$env:GIT_WORK_TREE = '{self.tree}'; git commit -m x"),
            (
                f'export GIT_DIR="{self.hub / ".git" / "worktrees" / "feat-demo"}"; git commit -m x',
                f"$env:GIT_DIR = '{self.hub / '.git' / 'worktrees' / 'feat-demo'}'; git commit -m x",
            ),
            (
                f'HUB_PLACE="{self.tree}"; cd "$HUB_PLACE" && git commit -m x',
                f"$env:HUB_PLACE = '{self.tree}'; Set-Location $env:HUB_PLACE; git commit -m x",
            ),
        ):
            for shell, command in ((self.bash, bash), (self.powershell, powershell)):
                with self.subTest(command=command):
                    self.assertIsNone(shell(command, self.tree, environment))
                    self.assertNotIn("could not", self.stderr)
        # A value the command removes, or sets where the text does not settle it, is not the one the hook was given.
        for command in (
            "Remove-Item env:HUB_PLACE; Set-Location $env:HUB_PLACE; git commit -m x",
            "Set-Item env:HUB_PLACE x; Set-Location $env:HUB_PLACE; git commit -m x",
            "$env:HUB_PLACE += 'x'; Set-Location $env:HUB_PLACE; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub, environment))
                self.assertIn("The guard could not tell where `git commit` runs", self.reason)

    def test_apply_check_after_a_double_dash_is_a_patch_name(self) -> None:
        for command, expected in (
            ("git apply change.patch -- --check", "deny"),
            ("git apply --check --apply change.patch", "deny"),
            ("git apply --check -- change.patch", None),
            ("git apply --check change.patch", None),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, self.hub))

    def test_two_spellings_of_a_location_variable_are_judged_by_the_upper_case_one(self) -> None:
        conflict = "GIT_WORK_TREE and git_work_tree name different values; git reads GIT_WORK_TREE"
        self.assertEqual("deny", self.bash(f'export GIT_WORK_TREE="{self.hub}"; git_work_tree=x git reset', self.tree))
        self.assertIn(WRITES.format("reset"), self.reason)
        command = f'export git_work_tree="{self.hub}"; GIT_WORK_TREE="{self.tree}" git reset --hard'
        self.assertIsNone(self.bash(command, self.tree))
        if platform_support.WINDOWS:
            self.assertIn(conflict, self.stderr)
        self.assertEqual("deny", self.bash('GIT_WORK_TREE="$UNSET_HERE" git_work_tree=x git reset', self.tree))

    def test_a_writing_call_the_guard_cannot_place_is_refused_while_a_hub_is_guarded(self) -> None:
        for shell in (self.bash, self.powershell):
            with self.subTest(shell=shell.__name__):
                command = 'git -C "$env:UNSET_HERE" commit -m x' if shell == self.powershell else 'cd "$UNSET_HERE"'
                if shell == self.bash:
                    command += " && git commit -m x"
                self.assertEqual("deny", shell(command, self.tree))
                self.assertIn("The guard could not tell where `git commit` runs", self.reason)
                self.assertIn("Write the directory into the command itself", self.reason)
        self.assertIsNone(self.bash('cd "$UNSET_HERE" && git status', self.tree))
        self.assertEqual("deny", self.bash("source .venv/bin/activate && git commit -m x", self.tree))
        # No alias can stand for a builtin, so a reading one is allowed wherever it runs.
        self.assertIsNone(self.bash("source .venv/bin/activate && git status", self.tree))
        self.assertEqual("deny", self.bash("git $VERB_UNSET_HERE x", self.hub))
        self.assertIn("could not tell which git subcommand runs", self.reason)
        # Whatever it is, a subcommand run in the worktree writes only the worktree.
        self.assertIsNone(self.bash("git $VERB_UNSET_HERE x", self.tree))
        self.assertEqual("deny", self.bash("git commit -m 'unterminated", self.tree))
        self.assertIn("a single quote is not closed", self.reason)
        self.assertIsNone(self.bash("echo 'unterminated", self.tree))


class EnvOptionTests(unittest.TestCase):
    def test_the_first_unread_option_is_found_past_each_value_given_as_the_next_word(self) -> None:
        for arguments, expected in (
            (["-u", "NAME"], "?"),
            (["-C", "dir"], "?"),
            (["-u", "NAME", "-q"], "-q"),
            (["-C", "dir", "-q", "git"], "-q"),
            (["-uNAME", "-q"], "-q"),
            (["-x"], "-x"),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, hub_guard.first_unread_env_option(list(arguments)))


class ProviderPathTests(unittest.TestCase):
    def test_a_computed_path_may_name_a_tracked_variable_unless_its_last_part_is_literal(self) -> None:
        for word, expected in (
            ({"text": "", "dynamic": True, "variable": "p"}, True),
            ({"text": "$d\\out.txt", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, False),
            ({"text": "$d/GIT_DIR", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, True),
            ({"text": "env:$n", "dynamic": True, "expansions": [{"token": "$n", "variable": "n"}]}, True),
            ({"text": "$d\\GIT_*", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, True),
            ({"text": "$d\\a`b", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, True),
            ({"text": "$d\\x:y", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, True),
            ({"text": "a$d", "dynamic": True, "expansions": [{"token": "$d", "variable": "d"}]}, True),
        ):
            with self.subTest(word=word["text"]):
                self.assertEqual(expected, hub_guard.may_name_tracked(word))


class HubsInViewTests(unittest.TestCase):
    def test_a_directory_git_cannot_read_is_noted_and_the_project_s_hub_still_counts(self) -> None:
        cwd, project = Path("session").absolute(), Path("project").absolute()
        failure = git_client.GitError("git could not read the checkout")
        stderr = io.StringIO()
        with (
            mock.patch.object(hub_guard, "checkout_of", side_effect=[failure, worktrees.Checkout(project, project)]),
            mock.patch.object(hub_guard, "guard_enabled", return_value=True),
            contextlib.redirect_stderr(stderr),
        ):
            found = hub_guard.hubs_in_view(cwd, {"CLAUDE_PROJECT_DIR": str(project)})
        self.assertEqual({project}, found)
        self.assertEqual(
            f"hub guard could not look up the hub of {cwd}: git could not read the checkout\n", stderr.getvalue()
        )


class ContractListTests(unittest.TestCase):
    """Each list of names the guard's section of docs/parallel-sessions.md gives is the set tools/hub_guard.py reads."""

    def test_the_refused_subcommands_are_the_writing_subcommands(self) -> None:
        lines = guard_section().split("\n")
        start = next(index for index, line in enumerate(lines) if "**Refused git commands.**" in line)
        end = next(index for index, line in enumerate(lines) if "**Still allowed in the hub.**" in line)
        listed = [name for line in lines[start:end] if line.startswith("  - ") for name in CODE_SPAN.findall(line)]
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(sorted(hub_guard.WRITING_SUBCOMMANDS), sorted(listed))

    def test_the_file_tools_are_the_ones_the_guard_judges_by_their_path(self) -> None:
        self.assertEqual(sorted(hub_guard.FILE_TOOLS), sorted(contract_names("**File tools.**", "is refused when")))

    def test_the_hook_matches_exactly_the_tools_the_guard_reads(self) -> None:
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        (group,) = settings["hooks"]["PreToolUse"]
        self.assertEqual(sorted({*hub_guard.FILE_TOOLS, *hub_guard.SHELL_READERS}), sorted(group["matcher"].split("|")))
        self.assertEqual(
            ["Bash", "Edit", "Monitor", "NotebookEdit", "PowerShell", "Write"], sorted(group["matcher"].split("|"))
        )

    def test_the_fast_forward_options_and_the_reading_submodule_subcommands_are_the_guard_s(self) -> None:
        self.assertEqual(
            sorted(hub_guard.FAST_FORWARD_OPTIONS), sorted(contract_names("`origin main`;", "and a fast-forward to"))
        )
        named = contract_names("`submodule` with no subcommand,", "; every other subcommand")
        self.assertEqual(hub_guard.READ_SUBMODULE, {None, *(name.removeprefix("submodule ") for name in named)})

    def test_git_s_options_with_a_value_are_the_ones_the_guard_reads(self) -> None:
        # The guard reads git_client's list, the one home of the options git reads a value after.
        self.assertEqual(sorted(git_client.GLOBAL_VALUE_OPTIONS), sorted(contract_names("or as the next word (", ")")))

    def test_the_prefix_programs_are_the_ones_each_reader_reads(self) -> None:
        bash = contract_names("Commands after", "are read as the commands they start")
        powershell = contract_names("A command after the programs", "is read as in Bash")
        self.assertEqual(sorted(bash_prefix_programs()), sorted(bash))
        self.assertEqual(sorted({*hub_guard.PREFIX_PROGRAMS, *hub_guard.ENV_NAMES}), sorted(powershell))

    def test_the_powershell_location_commands_are_the_ones_the_reader_follows(self) -> None:
        named = contract_names("makes every decision. The guard follows", "(bare goes home")
        self.assertEqual(sorted(hub_guard.LOCATION_NAMES), sorted(name.casefold() for name in named))


class WorkflowTests(GuardTestCase):
    """The commands docs/parallel-sessions.md and docs/implementing-changes.md tell a session to run stay allowed."""

    def test_the_documented_commands_are_allowed_from_a_worktree_in_both_shells(self) -> None:
        for command in (
            "python tools/worktrees.py new fix thing",
            "python tools/worktrees.py list",
            "git fetch origin main",
            "git rebase origin/main",
            "git merge origin/main",
            "git add -A",
            'git commit -m "fix(tools): a message"',
            "git commit -F - <<'EOF'\nfix(tools): a message\n\nWith a body.\nEOF",
            "git push -u origin HEAD",
            "git push",
            "git pull",
            "gh pr create --title 'refactor(tools): x' --body-file body.md",
            "gh pr update-branch",
            "gh pr merge --auto --squash",
            "python -m ruff format --check tools/worktrees.py",
            "python -m ruff check tools/worktrees.py",
            "python -B tests/run_validation.py -k worktrees",
            "python -B tests/run_validation.py --shard 1/4 --full",
            "git stash push -u -m my-tag",
            "git status --short && git diff --stat",
        ):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    if shell == self.powershell and "<<" in command:
                        continue
                    self.assertIsNone(shell(command, self.tree))
                    self.assertNotIn("could not", self.stderr)

    def test_the_documented_hub_commands_are_allowed_in_the_hub(self) -> None:
        for command in (
            "git pull --ff-only",
            "git fetch --prune origin",
            "python tools/worktrees.py list",
            "git worktree remove '.claude/worktrees/feat-demo'",
            "git branch -D 'feat/demo'",
            "git switch main",
        ):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertIsNone(shell(command, self.hub))


class GuardGitTests(unittest.TestCase):
    def runner(self) -> tuple[list[tuple[list[str], float]], Any]:
        """A runner for worktrees.CLIENT, which the guard runs git through, that records each command and its limit."""
        calls: list[tuple[list[str], float]] = []

        def run(command: Sequence[str], timeout: float) -> git_client.GitResult:
            calls.append((list(command), timeout))
            return git_client.GitResult(0, "", "")

        return calls, run

    def test_the_guards_git_calls_are_bounded_inside_the_hooks_timeout_and_new_and_list_by_the_clients(self) -> None:
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
        self.assertLess(worktrees.GUARD_GIT_TIMEOUT_SECONDS, hook["timeout"])
        calls, run = self.runner()
        with mock.patch.object(worktrees.CLIENT, "runner", run):
            hub_guard.guard_enabled(Path("a hub"))
            git_worktrees.list_worktrees(worktrees.CLIENT, Path("a hub"))
            git_worktrees.changed_paths(worktrees.CLIENT, Path("a checkout"))
        self.assertEqual(
            [
                worktrees.GUARD_GIT_TIMEOUT_SECONDS,
                git_client.DEFAULT_TIMEOUT_SECONDS,
                git_client.DEFAULT_TIMEOUT_SECONDS,
            ],
            [timeout for _, timeout in calls],
        )


@pytest.mark.parametrize(
    "arguments",
    [
        ["-uorigin/main"],
        ["-uorigin/MAIN"],
        ["-uorigin/mM-feature"],
        ["-quorigin/main"],
        ["-vvuorigin/MAIN"],
        ["-u", "origin/main"],
        ["-qu", "origin/MAIN"],
        ["-um"],
        ["-uM"],
        ["-u-mM"],
        ["-u", "-m"],
        ["-u", "-M"],
        ["-tm"],
        ["-tM"],
        ["--set-upstream-to=origin/main"],
        ["--set-upstream-to", "-m"],
        ["--set-upstream-t", "-M"],
        ["--", "-m"],
    ],
    ids=" ".join,
)
def test_upstream_and_tracking_values_are_not_rename_options(arguments: list[str]) -> None:
    assert not hub_guard.renames_a_branch(arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["-m", "renamed"],
        ["-M", "renamed"],
        ["-fm", "main", "renamed"],
        ["-qM", "main", "renamed"],
        ["-mM", "renamed"],
        ["-muorigin/main"],
        ["-Mtorigin/main"],
        ["-uorigin/main", "-m", "renamed"],
        ["-u", "origin/MAIN", "-M", "renamed"],
        ["-u", "--", "-m", "renamed"],
        ["-t", "-m", "renamed"],
        ["-tdirect", "-M", "renamed"],
        ["--mo", "main", "renamed"],
        ["--mov", "main", "renamed"],
        ["--move", "main", "renamed"],
        ["--move=main", "renamed"],
        ["--set-upstream-to", "origin/main", "--move", "renamed"],
        ["--set-upstream", "-m", "renamed"],
    ],
    ids=" ".join,
)
def test_rename_options_before_or_after_values_are_still_detected(arguments: list[str]) -> None:
    assert hub_guard.renames_a_branch(arguments)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        (None, None),
        ("git", "git"),
        ("GIT.EXE", "git"),
        ("/c/Program Files/Git/cmd/Git.Exe", "git"),
        ("codex.cmd", "codex.cmd"),
        # Only Windows separates a path with a backslash.
        *([("C:\\Program Files\\Git\\cmd\\git.exe", "git")] if platform_support.WINDOWS else []),
    ],
)
def test_a_command_is_named_lower_cased_without_its_directory_or_exe(word: str | None, expected: str | None) -> None:
    assert expected == hub_guard.command_name(word)
