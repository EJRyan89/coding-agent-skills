"""Regression suite for tools/hub_guard.py: the hub guard hook, its understood set, and its contract."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bash_agreement
import executable_locator
import git_client
import git_worktrees
import path_keys
from bounded_process import Finished, run_bounded
from guard_limits import PROCESS_SECONDS, process_limits_held
from markdown_sections import CODE_SPAN, markdown_section
from resident_pwsh import resident_reads
from worktree_fixture import GUARD_SCRIPT, HUB, REPOSITORY_ROOT, TREE, HubTestCase, isolated_environment, run_git

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
    does; `EndToEndTests` holds that both give the same decisions. In this process the guard reads a PowerShell command
    through this process's resident pwsh unless the class clears `resident_pwsh`, which leaves each read a pwsh start of
    its own, as the hook's is; tests/tools/test_hub_guard_pwsh.py holds that both read every node sample alike.
    """

    script = GUARD_SCRIPT
    end_to_end = False
    resident_pwsh = True

    def setUp(self) -> None:
        super().setUp()
        # The reading clock stands still unless a test moves it: a reading in this process ends with the reading time
        # only where a test says when, never because a loaded runner was slow to start git or pwsh.
        clock = mock.patch.object(hub_guard, "reading_clock", return_value=0.0)
        clock.start()
        self.addCleanup(clock.stop)
        # Each process a reading starts, git or pwsh, gets a limit no loaded runner reaches rather than what is left of
        # the reading time, so a slow start never ends a reading either; self.asked holds the limits the guard asked.
        self.asked = self.enterContext(process_limits_held())
        if self.resident_pwsh:
            self.enterContext(resident_reads())
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
        # Every refusal ends with its step: a worktree for a hub edit, or the remedy of one a worktree refuses too.
        if not self.reason.endswith("Run it from a terminal outside the clone."):
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

    def global_configuration(self, targets: Mapping[str, Path | str], name: str = "global.gitconfig") -> Path:
        """A git configuration file that includes the fixture's global one and sets each trace2 target given, a path
        written with forward slashes, which git reads on every platform."""
        config = self.root / name
        fixture = Path(self.environment["GIT_CONFIG_GLOBAL"]).as_posix()
        lines = [f'[include]\n\tpath = "{fixture}"\n', "[trace2]\n"]
        for key, value in targets.items():
            written = value.as_posix() if isinstance(value, Path) else value
            lines.append(f'\t{key} = "{written}"\n')
        config.write_text("".join(lines), encoding="utf-8")
        return config

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
        # The guard asks for its whole reading time; the real pwsh is handed the test's own limit, so a slow start on
        # a loaded runner is not read as pwsh failing to read the command.
        with (
            process_limits_held() as asked,
            mock.patch.object(subprocess, "Popen", wraps=subprocess.Popen) as start,
            mock.patch.object(hub_guard, "run_bounded", wraps=run_bounded) as bounded,
        ):
            events = hub_guard.powershell_events("git commit -m x", reading)
        self.assertEqual([], reading.unread)
        self.assertTrue(events, "pwsh read the command")
        [started] = start.call_args_list
        self.assertTrue(Path(started.args[0][0]).stem.lower().startswith("pwsh"))
        handed = {name.upper() for name in started.kwargs["env"]}
        self.assertFalse(handed & {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}, handed)
        self.assertEqual("elsewhere", environment["GIT_WORK_TREE"], "the hook's own environment is left as it was")
        [call] = bounded.call_args_list
        self.assertEqual([15], asked)
        self.assertEqual(PROCESS_SECONDS, call.args[1])
        self.assertIsNotNone(call.kwargs["tree"])
        self.assertEqual(4 * 1024 * 1024, call.kwargs["output_limit"])

    def test_each_git_call_is_handed_the_limit_the_guard_asks_for(self) -> None:
        # Outside the tests, process_seconds hands each process the limit the guard asks for, unchanged. No git runs.
        self.assertEqual(7.5, hub_guard.process_seconds(7.5))
        with (
            tempfile.TemporaryDirectory(prefix="hub-guard-limits.") as folder,
            mock.patch.object(hub_guard, "reading_clock", return_value=100.0),
            hub_guard.lookups_within(103.0),
            mock.patch.object(worktrees.CLIENT, "run", return_value=git_client.GitResult(128, "", "")) as run,
            mock.patch.object(worktrees.CLIENT, "output", return_value="false\n/x\n") as output,
        ):
            # A folder outside every checkout, so the unanswered git calls read as no repository's.
            place = Path(folder)
            hub_guard.git(place, "status")
            self.assertIsNone(hub_guard.checkout_of(place))
            self.assertIsNone(hub_guard.git_directory_holding(place))
        self.assertEqual([3.0, 3.0], [call.kwargs["timeout"] for call in run.call_args_list])
        self.assertEqual([3.0], [call.kwargs["timeout"] for call in output.call_args_list])

    def test_pwsh_gets_what_is_left_of_the_reading_time(self) -> None:
        # The clock is pinned, so the time handed to pwsh is exactly what is left, with no rounding between two looks.
        for now, left in ((100.0, 5.0), (103.0, 2.0)):
            reading = hub_guard.Reading(isolated_environment(), text="git commit -m x", deadline=105.0)
            with (
                self.subTest(now=now),
                mock.patch.object(hub_guard, "reading_clock", return_value=now),
                mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
                mock.patch.object(
                    hub_guard,
                    "run_bounded",
                    return_value=Finished(0, b'{"nodes": [], "commands": [], "statements": []}', b""),
                ) as bounded,
            ):
                hub_guard.powershell_events("git commit -m x", reading)
                [call] = bounded.call_args_list
                self.assertEqual(left, call.args[1])

    def test_pwsh_that_does_not_read_in_the_time_left_leaves_a_command_the_guard_does_not_read(self) -> None:
        reading = hub_guard.Reading(isolated_environment(), text="git commit -m x")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", side_effect=subprocess.TimeoutExpired("pwsh", 15)),
        ):
            self.assertIsNone(hub_guard.powershell_events("git commit -m x", reading))
        self.assertEqual(["pwsh did not read the command in what was left of the guard's 15 seconds"], reading.notes)
        self.assertEqual([], reading.unread)

    def test_a_command_with_no_json_form_is_one_the_guard_does_not_read_and_pwsh_never_starts(self) -> None:
        # A lone surrogate, which bytes that are not UTF-8 decode to, has no JSON form for the reader's payload.
        command = "Get-Item x\ud800"
        reading = hub_guard.Reading(isolated_environment(), text=command)
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded") as bounded,
        ):
            self.assertIsNone(hub_guard.powershell_events(command, reading))
        bounded.assert_not_called()
        self.assertEqual(
            ["could not hand pwsh the command (the value holds a lone surrogate U+D800, which UTF-8 cannot encode)"],
            reading.notes,
        )
        self.assertEqual([], reading.unread)

    def test_output_that_is_not_utf8_is_read_with_replacement_characters_and_a_repeated_key_is_not(self) -> None:
        # A console code page can reach the reader's output; a byte it cannot decode costs only that character.
        cases: tuple[tuple[bytes, dict[str, Any] | None, list[str]], ...] = (
            (b'{"nodes": [], "detail": "caf\xe9"}', {"nodes": [], "detail": "caf\ufffd"}, []),
            (b'{"nodes": [], "nodes": []}', None, ["pwsh could not read the command (exit code 0)"]),
        )
        for stdout, events, notes in cases:
            reading = hub_guard.Reading(isolated_environment(), text="Get-Item x")
            with (
                self.subTest(stdout=stdout),
                mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
                mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, stdout, b"")),
            ):
                self.assertEqual(events, hub_guard.powershell_events("Get-Item x", reading))
                self.assertEqual(notes, reading.notes)
                self.assertEqual([], reading.unread)

    def test_pwsh_whose_output_is_not_the_reader_s_names_its_reason_on_one_line_or_its_exit_code(self) -> None:
        for finished, reason in (
            (Finished(1, b"", b"ParserError:\r\n  at line 1\x1b[0m\n"), "ParserError: at line 1 [0m"),
            # A line separator U+2028 inside pwsh's message is flattened with the rest, never read as its end.
            (Finished(1, b"", b"first\xe2\x80\xa8second\n"), "first second"),
            (Finished(1, b"not json", b""), "exit code 1"),
            # A long message is cut to a bounded reason, as every failed command's is.
            (Finished(1, b"", b"x" * 600), "x" * 500 + "..."),
            (Finished(0, b"", b"  \n"), "exit code 0"),
        ):
            reading = hub_guard.Reading(isolated_environment(), text="Get-Item x")
            with (
                self.subTest(finished=finished),
                mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
                mock.patch.object(hub_guard, "run_bounded", return_value=finished),
            ):
                self.assertIsNone(hub_guard.powershell_events("Get-Item x", reading))
                self.assertEqual([f"pwsh could not read the command ({reason})"], reading.notes)
                self.assertEqual([], reading.unread)

    def test_only_a_line_whose_hash_follows_spaces_and_tabs_leaves_the_reader_as_a_comment(self) -> None:
        reader = Path(self.enterContext(tempfile.TemporaryDirectory())) / "reader.ps1"
        lines = ["# a comment", " \t# an indented comment", "\f# kept", "\x1c# kept", "$x = 1 # trailing", "$y = 2"]
        reader.write_text("\n".join(lines) + "\n", encoding="utf-8")
        hub_guard.powershell_reader.cache_clear()
        self.addCleanup(hub_guard.powershell_reader.cache_clear)
        with mock.patch.object(hub_guard, "POWERSHELL_READER", reader):
            self.assertEqual("\n".join(lines[2:]), hub_guard.powershell_reader())


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

    def test_a_guarded_hub_whose_path_holds_a_character_splitlines_breaks_on_is_protected(self) -> None:
        # git ends each path rev-parse prints with a newline alone; U+2028 and U+0085 also end a line to splitlines().
        for character in (chr(0x2028), chr(0x85)):
            hub = self.root / f"a{character}hub"
            hub.mkdir()
            self.git(hub, "init", "-q")
            (hub / "tracked.txt").write_text("x\n", encoding="utf-8")
            self.git(hub, "add", "tracked.txt")
            self.git(hub, "config", "coding-agent-skills.hubGuard", "true")
            with self.subTest(character=hex(ord(character))):
                self.assertEqual("deny", self.edit(hub / "tracked.txt"))
                self.assertEqual("deny", self.edit(hub / ".git" / "config"))

    def test_the_git_directory_of_another_repository_whose_path_holds_such_a_character_is_open(self) -> None:
        # Read with splitlines(), rev-parse's answer would be more than two lines, an error the edit is refused on.
        for character in (chr(0x2028), chr(0x85)):
            other = self.root / f"b{character}other"
            other.mkdir()
            self.git(other, "init", "-q")
            with self.subTest(character=hex(ord(character))):
                self.assertIsNone(self.edit(other / ".git" / "config"))
                self.assertEqual("", self.stderr)

    def test_guard_is_inert_unless_this_clone_opts_in(self) -> None:
        for value in (None, "false"):
            with self.subTest(value=value):
                self.enable(value)
                self.assertIsNone(self.edit(self.hub / "README.md"))
                self.assertIsNone(self.bash("git switch feat/demo", self.hub))


class UnreadableCheckoutFileTests(GuardTestCase):
    """A file event in a checkout git answers with an error for, as one whose ownership it doubts, is judged as a git
    directory git cannot read is: it may be a guarded hub's, so the edit is refused."""

    def test_a_hub_file_whose_check_ignore_git_answers_with_an_error_is_tracked(self) -> None:
        # check-ignore exits 0 for an ignored file and 1 for one that is not; any other answer is no answer, so an
        # ignored file git answered with an error for is refused, and one git never answered is allowed with a note.
        target = self.hub / "local.json"
        self.assertIsNone(self.edit(target))
        answered = git_client.GitError("git check-ignore could not read it", returncode=128)
        with mock.patch.object(worktrees.CLIENT, "quiet_output", side_effect=answered):
            self.assertEqual("deny", self.edit(target))
            self.assertEqual(hub_guard.DENY_REASON, self.reason)
        unanswered = git_client.GitError("git failed as timeout", kind="timeout")
        with mock.patch.object(worktrees.CLIENT, "quiet_output", side_effect=unanswered):
            self.assertIsNone(self.edit(target))
            self.assertIn("hub guard skipped: GitError: git failed as timeout", self.stderr)

    def test_an_edit_in_a_checkout_git_answers_with_an_error_for_is_refused(self) -> None:
        failure = git_client.GitError("git could not read the checkout", returncode=128)
        for tool in ("Edit", "Write", "NotebookEdit"):
            with self.subTest(tool=tool), mock.patch.object(hub_guard, "checkout_of", side_effect=failure):
                self.assertEqual("deny", self.edit(self.tree / "README.md", tool=tool))
                self.assertEqual(hub_guard.DENY_REASON, self.reason)
        with mock.patch.object(hub_guard, "checkout_of", side_effect=failure):
            self.assertTrue(hub_guard.hub_protected(self.root / "elsewhere.txt"))

    def test_an_edit_where_git_did_not_run_is_allowed_with_a_note(self) -> None:
        kinds: tuple[git_client.GitErrorKind, ...] = ("prerequisite", "execution", "timeout")
        for kind in kinds:
            failure = git_client.GitError(f"git failed as {kind}", kind=kind)
            with self.subTest(kind=kind), mock.patch.object(hub_guard, "checkout_of", side_effect=failure):
                self.assertIsNone(self.edit(self.hub / "README.md"))
                self.assertIn(f"hub guard skipped: GitError: git failed as {kind}", self.stderr)

    def test_an_answer_that_is_not_the_lines_asked_for_is_an_error_git_answered_with(self) -> None:
        answer = git_client.GitResult(0, "/a\nb\n/c\n", "")
        with mock.patch.object(worktrees, "git", return_value=answer):
            self.assertEqual("deny", self.edit(self.tree / "README.md"))
            self.assertEqual(hub_guard.DENY_REASON, self.reason)

    def test_an_edit_in_a_git_directory_is_refused_when_git_answers_with_an_error_and_allowed_when_it_did_not_run(
        self,
    ) -> None:
        target = self.other_repository() / ".git" / "config"
        answered = (
            git_client.GitError("git could not read the git directory", returncode=128),
            git_client.GitError("git rev-parse answered 3 lines for the git directory, not two"),
        )
        for failure in answered:
            with self.subTest(failure=str(failure)), mock.patch.object(worktrees.CLIENT, "output", side_effect=failure):
                self.assertEqual("deny", self.edit(target))
                self.assertEqual(hub_guard.DENY_REASON, self.reason)
        kinds: tuple[git_client.GitErrorKind, ...] = ("prerequisite", "execution", "timeout")
        for kind in kinds:
            failure = git_client.GitError(f"git failed as {kind}", kind=kind)
            with self.subTest(kind=kind), mock.patch.object(worktrees.CLIENT, "output", side_effect=failure):
                self.assertIsNone(self.edit(target))
                self.assertIn(f"hub guard skipped: GitError: git failed as {kind}", self.stderr)


@contextlib.contextmanager
def unstatable(*paths: Path) -> Iterator[None]:
    """Each of paths as one whose stat is refused looks to Python 3.14 and later: Path.stat, os.stat, and os.lstat
    raise a permission error naming it, and Path's and os.path's presence tests answer False. Every other path is read
    as it is."""
    keys = {os.path.normcase(os.path.normpath(path)) for path in paths}

    def refused(candidate: Any) -> bool:
        return isinstance(candidate, str | os.PathLike) and os.path.normcase(os.path.normpath(candidate)) in keys

    def raising(original: Any) -> Any:
        def call(candidate: Any, *args: Any, **kwargs: Any) -> Any:
            if refused(candidate):
                raise PermissionError(13, "Permission denied", str(candidate))
            return original(candidate, *args, **kwargs)

        return call

    def answering_false(original: Any) -> Any:
        def call(candidate: Any, *args: Any, **kwargs: Any) -> Any:
            return False if refused(candidate) else original(candidate, *args, **kwargs)

        return call

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", raising(Path.stat)))
        for name in ("stat", "lstat"):
            stack.enter_context(mock.patch.object(os, name, raising(getattr(os, name))))
        for name in ("exists", "is_dir", "is_file"):
            stack.enter_context(mock.patch.object(Path, name, answering_false(getattr(Path, name))))
        for name in ("exists", "lexists", "isdir", "isfile"):
            stack.enter_context(mock.patch.object(os.path, name, answering_false(getattr(os.path, name))))
        yield


class UnstatablePlaceTests(GuardTestCase):
    """A place whose stat fails is one the guard cannot look at, which may be a guarded hub's: it is never read as a
    place that is not there, passed over for the folder above it, or read as one that runs nothing, so a command naming
    git there is refused and an edit there is protected."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.place = self.root / "unreadable"
        self.place.mkdir()
        self.unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        hub_guard.guarded_hub_at.cache_clear()
        hub_guard.guarded_hub_of_repository.cache_clear()
        self.addCleanup(hub_guard.guarded_hub_at.cache_clear)
        self.addCleanup(hub_guard.guarded_hub_of_repository.cache_clear)

    def test_a_start_that_cannot_be_looked_at_is_never_passed_over_for_the_folder_above_it(self) -> None:
        with unstatable(self.place), self.assertRaises(git_client.GitError) as raised:
            worktrees.existing_directory(self.place / "below")
        self.assertIn("Permission denied", str(raised.exception))

    def test_a_git_mark_that_cannot_be_looked_at_is_never_read_as_none(self) -> None:
        with unstatable(self.place / ".git"), self.assertRaises(git_client.GitError):
            worktrees.below_a_checkout(self.place)

    def test_a_git_directory_and_the_folder_above_it_that_cannot_be_looked_at_are_no_place_without_a_hub(self) -> None:
        repository = self.hub / ".git"
        with unstatable(repository, self.hub), self.assertRaises(git_client.GitError) as raised:
            hub_guard.guarded_hub_of_repository(repository)
        self.assertTrue(raised.exception.answered)

    def test_a_start_that_cannot_be_looked_at_is_never_read_as_one_that_reads_no_alias(self) -> None:
        place = hub_guard.Place(self.place, self.place)
        with unstatable(self.place), self.assertRaises(git_client.GitError):
            hub_guard.alias_lookups(place)

    def test_a_command_naming_git_at_a_place_that_cannot_be_looked_at_is_refused(self) -> None:
        with unstatable(self.place):
            self.assertEqual("deny", self.bash(f'git -C "{self.place.as_posix()}" status', self.outside, self.unshared))
        self.assertIn("GitError", self.reason)

    def test_a_git_directory_that_cannot_be_looked_at_is_never_passed_over_for_the_checkout_lookup(self) -> None:
        # A .git the guard cannot stat may be a guarded hub's git directory, so the lookup of the hub protecting a path
        # inside it raises git's error rather than reading it as no git directory and asking from the path instead.
        repository = self.hub / ".git"
        with unstatable(repository), self.assertRaises(git_client.GitError) as raised:
            hub_guard.protecting_hub(repository / "config")
        self.assertIn("Permission denied", str(raised.exception))

    def test_an_edit_in_a_hub_git_directory_that_cannot_be_looked_at_is_refused(self) -> None:
        with unstatable(self.hub / ".git", self.hub):
            self.assertEqual("deny", self.edit(self.hub / ".git" / "config"))
        self.assertEqual(hub_guard.DENY_REASON, self.reason)


class AliasLookupPlaceTests(unittest.TestCase):
    """alias_lookups reads no alias at a place the text does not settle, or at one where no folder is, before it looks
    for the checkout there: each runs nothing, so it has no configuration to read."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_a_place_the_text_does_not_settle_reads_no_alias(self) -> None:
        self.assertEqual([], hub_guard.alias_lookups(hub_guard.Place(None, None)))

    def test_a_place_where_nothing_is_reads_no_alias(self) -> None:
        missing = self.root / "missing"
        self.assertEqual([], hub_guard.alias_lookups(hub_guard.Place(missing, missing)))

    def test_a_place_where_a_file_is_reads_no_alias(self) -> None:
        file = self.root / "file"
        file.write_text("", encoding="utf-8")
        self.assertEqual([], hub_guard.alias_lookups(hub_guard.Place(file, file)))


class EndToEndTests(GuardTestCase):
    """tools/hub_guard.py started as its own process, as the hook starts it, decides each kind of event as the
    in-process call the other tests make does, with the same reason and the same notes. The in-process call reads a
    PowerShell command in a pwsh start of its own here, as the hook's own process does."""

    resident_pwsh = False

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
            # A part the guard does not read is allowed, from the hub and from a worktree alike.
            ({"tool_name": "Bash", "tool_input": {"command": "eval x"}, "cwd": str(self.hub)}, None, None),
            (
                {"tool_name": "Bash", "tool_input": {"command": 'python3 -B "$P" check'}, "cwd": str(self.hub)},
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
        with mock.patch.object(hub_guard, "git", return_value=git_client.GitResult(3, "", "fatal: \x1b[31mbad\r\n")):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertFalse(hub_guard.guard_enabled(self.hub))
        self.assertEqual(
            f"hub guard skipped: the opt-in of {self.hub} could not be read: fatal: [31mbad\n",
            stderr.getvalue(),
            "a control character git prints is flattened, never handed on to the terminal",
        )
        self.enable(None)
        self.assertIsNone(self.both(event))
        self.assertEqual("", self.stderr, "a clone that has not opted in is unguarded, with nothing to note")

    def test_a_checkout_git_cannot_read_is_noted_never_read_as_outside_every_repository(self) -> None:
        # Rewritten in place: git for Windows hides the file, and Windows refuses to create a hidden file anew.
        with (self.tree / ".git").open("r+", encoding="utf-8") as dot_git:
            dot_git.write(f"gitdir: {self.root / 'gone'}\n")
            dot_git.truncate()
        # git status starts in that checkout, a place whose lookup git answers with an error for, which may be a
        # guarded hub's, so it is refused as a command naming git.
        for command in ("git status", f'git -C "{self.hub}" commit -m x'):
            with self.subTest(command=command):
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.tree)}
                self.assertEqual("deny", self.both(event))
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

    def test_bash_nested_deeper_than_the_reader_follows_is_allowed_with_a_note_alike_in_its_own_process(self) -> None:
        # Bash reads this text, and the reader does not (tests/tools/test_bash_syntax.py holds both), so the event is
        # handed over as text, past the check that Bash and the reader agree. What lies past the nesting limit is a
        # part the guard does not read.
        nested = "$( " * 300 + "git commit -m x" + " )" * 300
        for command in (nested, "git status"):
            with self.subTest(command=command[:30]):
                event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(self.hub)}
                self.assertIsNone(self.both(json.dumps(event)))
                self.assertNotIn("hub guard skipped", self.stderr)
        event = {"tool_name": "Bash", "tool_input": {"command": nested}, "cwd": str(self.hub)}
        self.both(json.dumps(event))
        self.assertIn("nests deeper than the reader follows", self.stderr)


class ReaderFailureTests(GuardTestCase):
    """A reader or a judgment that fails leaves a command the guard does not read: refused while a guarded hub is in
    view, and allowed with a note otherwise, as a part the readers do not read is."""

    def outside(self) -> Path:
        outside = self.root / "not-a-repository"
        outside.mkdir(exist_ok=True)
        return outside

    def shell(self, tool: str, command: str, cwd: Path) -> str | None:
        # Handed over as text: a reader made to fail cannot agree with Bash.
        return self.decision(json.dumps({"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}))

    def test_any_error_inside_a_reader_is_refused_while_a_hub_is_in_view(self) -> None:
        for tool, target, name, error in (
            ("Bash", bash_syntax, "parse", ValueError("the reader broke")),
            ("Bash", bash_syntax, "parse", RecursionError("maximum recursion depth exceeded")),
            ("Monitor", hub_guard.BashReader, "read_list", KeyError("variables")),
            ("PowerShell", hub_guard, "powershell_events", KeyError("events")),
        ):
            with (
                self.subTest(tool=tool, failing=name, error=type(error).__name__),
                mock.patch.object(target, name, side_effect=error),
            ):
                self.assertEqual("deny", self.shell(tool, "git commit -m x", self.hub))
                self.assertIn(f"The guard failed reading this command ({type(error).__name__}: ", self.reason)
                self.assertEqual("deny", self.shell(tool, "echo done", self.hub))
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

    def test_a_refusal_whose_reason_json_cannot_carry_still_refuses_with_the_fixed_reason(self) -> None:
        # A reason can quote an error naming a path whose name decoded to a lone surrogate, which json_writer
        # refuses; the guard must still print its refusal, since an error would let the command through.
        with mock.patch.object(hub_guard, "denied", side_effect=OSError("cannot read /hub/x\udcff")):
            self.assertEqual("deny", self.shell("Bash", "git commit -m x", self.hub))
        self.assertEqual(
            f"The guard refused this command for a reason that holds what JSON cannot carry. {hub_guard.DENY_REASON}",
            self.reason,
        )

    def test_an_error_judging_a_command_is_refused_while_a_guarded_hub_is_in_view(self) -> None:
        with mock.patch.object(hub_guard, "denied", side_effect=RuntimeError("the judge broke")):
            for tool in ("Bash", "PowerShell"):
                with self.subTest(tool=tool):
                    self.assertEqual("deny", self.shell(tool, "git commit -m x", self.hub))
                    self.assertEqual(
                        "The guard failed reading this command (RuntimeError: the judge broke), so it is refused "
                        f"while a hub is guarded. {hub_guard.REWRITE_IT} {hub_guard.DENY_REASON}",
                        self.reason,
                    )
                    self.assertIsNone(self.shell(tool, "git commit -m x", self.outside()))
                    self.assertIn("hub guard skipped: RuntimeError: the judge broke", self.stderr)


class ReadingBoundTests(GuardTestCase):
    """The guard reads each command within a stated time, well inside the hook's own timeout, past which Claude Code
    blocks the command. What it cannot read in time is refused while a guarded hub is in view."""

    def shell(self, tool: str, command: str, cwd: Path) -> str | None:
        return self.bash(command, cwd) if tool == "Bash" else self.powershell(command, cwd)

    def test_a_reading_in_this_process_takes_its_time_from_the_tests_clock_and_never_from_the_wall(self) -> None:
        # Each look at the wall clock is a thousand seconds later, past any reading time; the readings, which start
        # pwsh and look places up with git, are read and judged all the same, by the commit rule.
        # Every process the readings start is handed the test's own limit, never a real part of the reading time, which
        # a slow start on a loaded runner could run out: pwsh through the guard's run_bounded, git through the client's.
        wall = types.SimpleNamespace(monotonic=itertools.count(step=1000).__next__)
        handed: list[tuple[str, float]] = []

        def recorded(command: Sequence[str], timeout: float, **options: Any) -> Finished:
            handed.append((Path(command[0]).stem.lower(), timeout))
            return run_bounded(command, timeout, **options)

        for tool, command in (("Bash", "git commit -m x"), ("PowerShell", "git commit -m x")):
            with (
                self.subTest(tool=tool),
                mock.patch.object(hub_guard, "time", wall),
                mock.patch.object(hub_guard, "run_bounded", side_effect=recorded),
                mock.patch.object(git_client, "run_bounded", side_effect=recorded),
            ):
                self.assertEqual("deny", self.shell(tool, command, self.hub))
                self.assertIn(WRITES.format("commit"), self.reason)
        self.assertTrue({"git", "pwsh"} <= {program for program, _ in handed}, handed)
        self.assertEqual({PROCESS_SECONDS}, {timeout for _, timeout in handed}, handed)

    def test_a_command_not_read_within_the_reading_time_is_refused_while_a_hub_is_in_view(self) -> None:
        with mock.patch.object(hub_guard, "READING_SECONDS", 0):
            self.assertEqual("deny", self.decision(json.dumps(self.event("git status", self.hub))))
            self.assertEqual(
                "The guard failed reading this command (ReadingTimeout: the guard did not finish reading it within 0 "
                f"seconds), so it is refused while a hub is guarded. {hub_guard.REWRITE_IT} {hub_guard.DENY_REASON}",
                self.reason,
            )
            self.assertEqual("deny", self.decision(json.dumps(self.event("echo done", self.hub))))
            # A reading cut short before the lookups of the places it names, in either reader, with no hub of the
            # session's own in view, is allowed with a note: no place it names was looked up as a hub's.
            outside = self.root / "not-a-repository"
            outside.mkdir()
            environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
            for tool in ("Bash", "PowerShell"):
                with self.subTest(tool=tool):
                    command = f'git -C "{self.hub}" commit -m x'
                    self.assertIsNone(self.decision(json.dumps(self.event(command, outside, tool)), environment))
                    self.assertIn("hub guard skipped: ReadingTimeout: the guard did not finish reading it", self.stderr)
                    self.assertEqual("deny", self.decision(json.dumps(self.event(command, self.hub, tool))))

    def event(self, command: str, cwd: Path, tool: str = "Bash") -> dict[str, object]:
        return {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}

    def test_a_slow_reading_is_refused_once_it_passes_the_reading_time(self) -> None:
        # Each look at the clock is a second later, so a command read in more than READING_SECONDS looks is too slow.
        for tool, step, last in (("Bash", "true; ", "git status"), ("PowerShell", "echo a; ", "git status")):
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

    def test_the_hub_lookups_after_reading_end_with_the_reading_time(self) -> None:
        # Each place the command names is looked up after reading; each lookup here costs a simulated second, and the
        # clock moves only then, so a command naming more places than the reading time has seconds is refused at the
        # deadline, after no more lookups than that.
        places = []
        for index in range(40):
            place = self.root / f"place-{index}"
            place.mkdir()
            places.append(place)
        command = "".join(f'git -C "{place.as_posix()}" status; ' for place in places) + "git commit -m x"
        clock = [0.0]
        looked_up: list[tuple[float, float]] = []
        find = worktrees.checkout_of

        def slow_checkout_of(path: Path, timeout: float = worktrees.GUARD_GIT_TIMEOUT_SECONDS) -> object:
            # The limit the guard asked for this lookup's git call; git itself is handed the test's own.
            looked_up.append((clock[0], self.asked[-1]))
            found = find(path, timeout)
            clock[0] += 1
            return found

        with (
            mock.patch.object(hub_guard, "reading_clock", side_effect=lambda: clock[0]),
            mock.patch.object(worktrees, "checkout_of", side_effect=slow_checkout_of),
        ):
            self.assertEqual("deny", self.decision(self.event(command, self.tree)))
        self.assertIn("within 15 seconds", self.reason)
        self.assertLessEqual(clock[0], hub_guard.READING_SECONDS)
        self.assertLessEqual(len(looked_up), hub_guard.READING_SECONDS)
        for started, timeout in looked_up:
            self.assertLessEqual(timeout, hub_guard.READING_SECONDS - started)

    def test_a_lookup_the_reading_time_cuts_short_is_refused_with_no_hub_in_view_of_the_session(self) -> None:
        # The session starts outside every repository and has no project, so only a place the command names can bring
        # a guarded hub into view. Each lookup costs a simulated second; the hub is named first, so it is found before
        # the time runs out, or last, so it is never looked up. Either way a place not looked up may be a hub's.
        outside = self.root / "outside"
        outside.mkdir()
        places = []
        for index in range(40):
            place = self.root / f"place-{index}"
            place.mkdir()
            places.append(place)
        named = "".join(f'git -C "{place.as_posix()}" status; ' for place in places)
        hub_call = f'git -C "{self.hub.as_posix()}" commit -m x; '
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        find = worktrees.checkout_of
        clock = [0.0]

        def slow_checkout_of(path: Path, timeout: float = worktrees.GUARD_GIT_TIMEOUT_SECONDS) -> object:
            found = find(path, timeout)
            clock[0] += 1
            return found

        for label, command, expected in (
            ("hub first", hub_call + named + "echo done", "deny"),
            ("hub last", named + hub_call + "echo done", "deny"),
            ("no hub", named + "echo done", "deny"),
            ("in time", hub_call.replace("commit -m x", "status") + "echo done", None),
        ):
            clock[0] = 0.0
            with (
                self.subTest(label),
                mock.patch.object(hub_guard, "reading_clock", side_effect=lambda: clock[0]),
                mock.patch.object(worktrees, "checkout_of", side_effect=slow_checkout_of),
            ):
                self.assertEqual(expected, self.decision(self.event(command, outside), environment))
                if expected is not None:
                    self.assertIn("ReadingTimeout: the guard did not finish reading it within 15 seconds", self.reason)

    def test_a_reading_time_that_runs_out_after_the_lookups_finish_with_no_hub_in_view_is_allowed_with_a_note(
        self,
    ) -> None:
        # Every place was looked up and none is a guarded hub's, so a timeout in judging leaves no hub in view.
        outside = self.root / "outside"
        outside.mkdir()
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        timeout = hub_guard.ReadingTimeout("the guard did not finish reading it within 15 seconds")
        with mock.patch.object(hub_guard, "denied", side_effect=timeout):
            self.assertIsNone(self.decision(self.event("git status", outside), environment))
        self.assertIn("hub guard skipped: ReadingTimeout: the guard did not finish reading it", self.stderr)

    def test_a_lookup_while_judging_ends_with_the_reading_time(self) -> None:
        # The judge's look at the clock before the call is in time; the hub lookup the call then makes is not.
        reading = hub_guard.read_command("Bash", "git status", self.hub, self.environment)
        looks = iter([reading.deadline - 1])
        with (
            mock.patch.object(hub_guard, "reading_clock", side_effect=lambda: next(looks, reading.deadline + 1)),
            self.assertRaises(hub_guard.ReadingTimeout),
        ):
            hub_guard.denied(reading, {self.hub})

    def test_a_lookup_while_reading_ends_with_the_reading_time(self) -> None:
        # pwsh is handed what is left of the reading time, which is over.
        reading = hub_guard.start_reading("git status", self.hub, self.environment)
        with (
            mock.patch.object(hub_guard, "reading_clock", return_value=reading.deadline + 1),
            self.assertRaises(hub_guard.ReadingTimeout),
        ):
            hub_guard.read_into(reading, "PowerShell", self.hub, self.environment)

    def test_a_lookups_git_call_gets_what_is_left_of_the_reading_time(self) -> None:
        # self.asked holds the limit the guard asks for each git call; git itself is handed the test's own, which
        # PowerShellReaderStartTests holds is the one asked for outside the tests.
        with mock.patch.object(hub_guard, "reading_clock", return_value=100.0):
            hub_guard.lookup_timeout()
            self.assertEqual([worktrees.GUARD_GIT_TIMEOUT_SECONDS], self.asked)
            with hub_guard.lookups_within(103.0):
                self.asked.clear()
                hub_guard.lookup_timeout()
                self.assertEqual([3.0], self.asked)
                self.asked.clear()
                with (
                    mock.patch.object(worktrees, "git", wraps=worktrees.git) as git,
                    mock.patch.object(worktrees.CLIENT, "output", wraps=worktrees.CLIENT.output) as output,
                ):
                    hub_guard.guarded_hub_at.cache_clear()
                    hub_guard.guarded_hub_of_repository.cache_clear()
                    with mock.patch.dict(os.environ, self.environment, clear=True):
                        self.assertEqual(self.hub, hub_guard.guarded_hub_at(self.hub / "README.md"))
                        self.assertEqual(self.hub, hub_guard.guarded_hub_of_repository(self.hub / ".git"))
                    hub_guard.guarded_hub_at.cache_clear()
                    hub_guard.guarded_hub_of_repository.cache_clear()
                # The checkout's lookup, the opt-in's, and the git directory's each ask for what is left.
                self.assertTrue(git.call_args_list)
                self.assertEqual(len(git.call_args_list) + len(output.call_args_list), len(self.asked))
                self.assertEqual({3.0}, set(self.asked))
            self.asked.clear()
            with hub_guard.lookups_within(120.0):
                hub_guard.lookup_timeout()
            with hub_guard.lookups_within(100.0), self.assertRaises(hub_guard.ReadingTimeout):
                hub_guard.guard_enabled(self.hub)
            hub_guard.lookup_timeout()
            self.assertEqual([worktrees.GUARD_GIT_TIMEOUT_SECONDS] * 2, self.asked)

    def test_the_hubs_a_command_names_are_the_guarded_hubs_its_settled_places_reach(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        with mock.patch.dict(os.environ, self.environment, clear=True):
            command = f'git -C "{self.hub.as_posix()}" status; git -C "{self.tree.as_posix()}" status; git status'
            reading = hub_guard.read_command("Bash", command, outside, self.environment)
            self.assertEqual({self.hub}, hub_guard.reached_hubs(reading))
            self.assertEqual(set(), hub_guard.reached_hubs(hub_guard.read_command("Bash", "git status", outside, {})))

    def test_the_hubs_in_view_are_found_whatever_the_reading_time(self) -> None:
        # Which hubs are in view decides whether a command not read in time is refused, so finding them never ends
        # with the reading time.
        with mock.patch.object(hub_guard, "READING_SECONDS", 0):
            reading = hub_guard.start_reading("git status", self.tree, self.environment)
        self.assertEqual({self.hub}, {Path(os.path.realpath(hub)) for hub in reading.context_hubs})


class PlaceLookupTests(GuardTestCase):
    """A place the command names whose hub lookup git answers with an error for, or does not finish within the reading
    time, may be a guarded hub's: it is never read as a place outside every repository, the hubs the other places
    reach stay in view, and a command naming git is refused. A start inside a git directory is looked up as the
    repository git acts on there."""

    def setUp(self) -> None:
        super().setUp()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}

    def erroring_place(self) -> Path:
        """A directory below a checkout, whose `.git` file points nowhere, which git answers with an error for."""
        place = self.root / "broken checkout"
        place.mkdir()
        (place / ".git").write_text(f"gitdir: {self.root / 'gone'}\n", encoding="utf-8")
        return place

    def miscounted_place(self) -> Path:
        """A directory whose lookup git answers successfully with lines other than the two asked for."""
        place = self.root / "miscounted"
        place.mkdir()
        return place

    def test_a_place_git_answers_with_an_error_for_refuses_a_command_naming_git_and_keeps_the_hubs_found(
        self,
    ) -> None:
        hub_call = f'git -C "{self.hub.as_posix()}" commit -m x'
        find = worktrees.git
        miscounted = self.miscounted_place()

        def git(cwd: Path, *arguments: str, timeout: float | None = None) -> git_client.GitResult:
            if same(cwd, miscounted) and "--show-toplevel" in arguments:
                return git_client.GitResult(0, "/a\nb\n/c\n", "")
            return find(cwd, *arguments, timeout=timeout)

        for label, place in (("error", self.erroring_place()), ("line count", miscounted)):
            other = f'git -C "{place.as_posix()}" status'
            for order, command in (("hub first", f"{hub_call}; {other}"), ("hub last", f"{other}; {hub_call}")):
                with self.subTest(label, order=order), mock.patch.object(worktrees, "git", side_effect=git):
                    self.assertEqual("deny", self.bash(command, self.outside, self.unshared))
                    self.assertIn("GitError", self.reason)
                    self.assertNotIn("hub guard skipped", self.stderr)
            with self.subTest(label, order="alone"), mock.patch.object(worktrees, "git", side_effect=git):
                self.assertEqual("deny", self.bash(other, self.outside, self.unshared))
            # Every place is looked up, before the failing one and after it, before the error is raised.
            for order, command in (("found first", f"{hub_call}; {other}"), ("found last", f"{other}; {hub_call}")):
                reading = hub_guard.read_command("Bash", command, self.outside, self.unshared)
                found: set[Path] = set()
                with self.subTest(label, order=order):
                    with (
                        mock.patch.dict(os.environ, self.unshared, clear=True),
                        mock.patch.object(worktrees, "git", side_effect=git),
                        self.assertRaises(git_client.GitError),
                    ):
                        hub_guard.reached_hubs(reading, found)
                    self.assertEqual({self.hub}, {Path(os.path.realpath(hub)) for hub in found})
        self.assertIsNone(self.bash(f'git -C "{self.tree.as_posix()}" status', self.outside, self.unshared))

    def test_a_lookup_git_call_the_reading_time_cuts_short_refuses_a_command_naming_git(self) -> None:
        # The reading starts at 0 and its deadline is READING_SECONDS later; every later look at the clock leaves half
        # a millisecond, so the lookup's own git call is the one that runs out of time, not a look between calls. The
        # call given that half millisecond times out here as git_client times one out, since a real git can finish
        # within it on a fast runner; the calls given their whole time run git, under the test's own limit.
        looks = iter([0.0])
        timeouts: list[float] = []
        find = worktrees.git

        def git(cwd: Path, *arguments: str, timeout: float | None = None) -> git_client.GitResult:
            asked = self.asked[-1]
            timeouts.append(asked)
            if asked < worktrees.GUARD_GIT_TIMEOUT_SECONDS:
                raise git_client.GitError(
                    f"git {' '.join(arguments)} did not finish within {asked:g} seconds", kind="timeout"
                )
            return find(cwd, *arguments, timeout=timeout)

        with (
            mock.patch.object(
                hub_guard, "reading_clock", side_effect=lambda: next(looks, hub_guard.READING_SECONDS - 0.0005)
            ),
            mock.patch.object(worktrees, "git", side_effect=git),
        ):
            self.assertEqual("deny", self.bash(f'git -C "{self.hub}" commit -m x', self.outside, self.unshared))
        self.assertIn("GitError", self.reason)
        self.assertIn("did not finish", self.reason)
        self.assertIn(0.0005, [round(timeout, 4) for timeout in timeouts])

    def test_a_start_inside_a_hub_s_git_directory_is_the_hub_s(self) -> None:
        git_directory = (self.hub / ".git").as_posix()
        commands = (
            f'git -C "{git_directory}" symbolic-ref HEAD refs/heads/x',
            f'git -C "{git_directory}" update-ref refs/heads/main HEAD~1',
            f'git -C "{git_directory}/refs" symbolic-ref HEAD refs/heads/x',
            f'cd "{git_directory}" && git symbolic-ref HEAD refs/heads/x',
        )
        for cwd in (self.hub, self.tree, self.outside):
            for command in commands:
                with self.subTest(cwd=cwd.name, command=command):
                    self.assertEqual("deny", self.bash(command, cwd, self.unshared))
        # The session's own directory inside the hub's git directory brings the hub into view.
        self.assertEqual("deny", self.bash("git symbolic-ref HEAD refs/heads/x", self.hub / ".git", self.unshared))
        with mock.patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual({self.hub}, hub_guard.hubs_in_view(self.hub / ".git" / "refs", self.unshared))
        # Another repository's git directory, and a worktree's own, are no guarded hub's.
        other = (self.other_repository() / ".git").as_posix()
        self.assertIsNone(self.bash(f'git -C "{other}" symbolic-ref HEAD refs/heads/x', self.outside, self.unshared))
        tree_git_directory = self.git(self.tree, "rev-parse", "--path-format=absolute", "--git-dir").strip()
        command = f'git -C "{tree_git_directory}" symbolic-ref HEAD refs/heads/x'
        self.assertIsNone(self.bash(command, self.outside, self.unshared))

    def test_the_git_directory_holding_a_path_is_found_only_inside_one(self) -> None:
        with mock.patch.dict(os.environ, self.environment, clear=True):
            for path in (self.hub / ".git", self.hub / ".git" / "refs" / "heads", self.hub / ".git" / "missing"):
                with self.subTest(path=path):
                    found = hub_guard.git_directory_holding(path)
                    self.assertTrue(found is not None and same(found, self.hub / ".git"))
            for path in (self.hub, self.tree / "README.md", self.outside, self.root / "missing" / "deeper"):
                with self.subTest(path=path):
                    self.assertIsNone(hub_guard.git_directory_holding(path))
            answers = (("true\n", 1), ("true\n/a\n/b\n", 3))
            for answer, count in answers:
                with (
                    self.subTest(answer=answer),
                    mock.patch.object(worktrees.CLIENT, "output", return_value=answer),
                    self.assertRaisesRegex(
                        git_client.GitError, f"answered {count} lines for the git directory holding"
                    ),
                ):
                    hub_guard.git_directory_holding(self.hub / ".git")
            failure = git_client.GitError("git could not read it", returncode=128)
            with (
                mock.patch.object(worktrees.CLIENT, "output", side_effect=failure),
                self.assertRaises(git_client.GitError),
            ):
                hub_guard.git_directory_holding(self.hub / ".git")
            # Its git call asks for what is left of the reading time, as every lookup's does.
            self.asked.clear()
            with (
                mock.patch.object(hub_guard, "reading_clock", return_value=100.0),
                hub_guard.lookups_within(103.0),
                mock.patch.object(worktrees.CLIENT, "output", wraps=worktrees.CLIENT.output) as output,
            ):
                hub_guard.git_directory_holding(self.hub / ".git")
            self.assertEqual([3.0], self.asked)
            self.assertEqual([PROCESS_SECONDS], [call.kwargs["timeout"] for call in output.call_args_list])


def same(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


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

    A shim first on PATH for each Python name the test gives, `python` unless it says otherwise, records its
    arguments, so a test sees whether the command would start Python at all, by which name, and with what, without
    depending on which Python the machine has. A test that holds a name absent runs the command with a PATH of
    shims alone, `git`, `env`, and `awk` among them each running the program Bash finds, so no Python of the
    machine's is found by any name.
    """

    resident_pwsh = False

    def start_hook(
        self,
        variables: Mapping[str, str] | None = None,
        status: int = 0,
        pythons: Sequence[str] = ("python",),
        machine_path: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, list[str]]]:
        """Run the tracked hook command in Bash; return its result and the arguments each Python shim was given."""
        bash = platform_support.find_bash()
        if bash is None:
            raise AssertionError("Git Bash was not found; it is required for repository validation.")
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [group] = settings["hooks"]["PreToolUse"]
        [hook] = group["hooks"]
        shims = self.root / ("shims" if machine_path else "shims-alone")
        shutil.rmtree(shims, ignore_errors=True)
        shims.mkdir()
        records = self.root / "python-arguments"
        shutil.rmtree(records, ignore_errors=True)
        records.mkdir()
        programs = {
            name: f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SHIM_RECORDS/{name}"\nexit {status}\n' for name in pythons
        }
        if not machine_path:
            found = subprocess.run(
                [bash, "-c", "command -v git env awk"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=self.environment,
                check=True,
            ).stdout.splitlines()
            for name, program in zip(("git", "env", "awk"), found, strict=True):
                programs[name] = f"#!/bin/sh\nexec '{program}' \"$@\"\n"
        for name, text in programs.items():
            (shims / name).write_text(text, encoding="utf-8")
            (shims / name).chmod(0o755)  # Bash runs a program off Windows only when it is executable
        environment = {
            **self.environment,
            "CLAUDE_PROJECT_DIR": str(self.hub),
            "SHIM_RECORDS": str(records),
            "PATH": str(shims) + (os.pathsep + self.environment["PATH"] if machine_path else ""),
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
        started = {record.name: record.read_text(encoding="utf-8").splitlines() for record in records.iterdir()}
        return completed, started

    def run_hook(
        self, variables: Mapping[str, str] | None = None, note: str = "", status: int = 0
    ) -> tuple[int, list[str] | None]:
        """Run the hook with a `python` shim; return its status and the shim's arguments, None when it did not run."""
        completed, started = self.start_hook(variables, status)
        self.assertEqual(note, completed.stderr)
        self.assertLessEqual(started.keys(), {"python"})
        return completed.returncode, started.get("python")

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

    def test_an_opted_in_clone_with_only_python3_runs_the_guard_with_python3(self) -> None:
        # Homebrew's Python on macOS, and Linux without python-is-python3, name Python `python3` alone, and a hook that
        # started `python` alone blocked every tool call there.
        self.enable("true")
        completed, started = self.start_hook(pythons=("python3",), machine_path=False)
        outcome = (completed.returncode, completed.stderr, started)
        self.assertEqual((0, "", {"python3": ["-B", f"{self.hub}/tools/hub_guard.py"]}), outcome)

    def test_python_is_run_before_python3_where_both_resolve(self) -> None:
        # Windows installers name Python `python`, and a `python3` there may be the Microsoft Store's alias.
        self.enable("true")
        for machine_path in (True, False):
            with self.subTest(machine_path=machine_path):
                completed, started = self.start_hook(pythons=("python", "python3"), machine_path=machine_path)
                outcome = (completed.returncode, completed.stderr, started)
                self.assertEqual((0, "", {"python": ["-B", f"{self.hub}/tools/hub_guard.py"]}), outcome)

    def test_a_clone_that_has_not_opted_in_needs_no_python_by_any_name(self) -> None:
        for value in (None, "false"):
            with self.subTest(value=value):
                self.enable(value)
                completed, started = self.start_hook(pythons=(), machine_path=False)
                self.assertEqual((0, "", "", {}), (completed.returncode, completed.stdout, completed.stderr, started))

    def test_an_opted_in_clone_with_no_python_by_either_name_fails_the_hook_naming_python3(self) -> None:
        # The hook declares onFailure: block, so a guarded clone with no Python is blocked, never left unguarded.
        self.enable("true")
        completed, started = self.start_hook(pythons=(), machine_path=False)
        self.assertEqual({}, started)
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("python3", completed.stderr)

    def test_the_hook_blocks_on_a_failure_only_where_the_clone_has_opted_in(self) -> None:
        # The hook declares onFailure: block, so Claude Code blocks a command when the hook fails or times out; the
        # opt-in check exits 0 on every path where the clone has not opted in, so only a guarded clone can block.
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(("block", 30), (hook["onFailure"], hook["timeout"]))
        for value in (None, "false"):
            with self.subTest(value=value):
                self.enable(value)
                self.assertEqual((0, None), self.run_hook(status=3))
        self.enable("true")
        code, arguments = self.run_hook(status=3)
        self.assertEqual(3, code, "a guard that fails in a guarded clone fails the hook, which Claude Code blocks")
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

    def test_an_inherited_trace_or_output_file_has_the_hook_s_own_git_write_nothing(self) -> None:
        # The hook's git config call would append a trace to a hub file the session's environment names before the
        # guard starts, whatever the command; Windows reads an environment name in any case.
        readme = self.hub / "README.md"
        before = readme.read_bytes()
        self.enable("true")
        for name in ("GIT_TRACE", "GIT_TRACE2_PERF", "GIT_TRACE2_EVENT", "GIT_REDIRECT_STDOUT", "GIT_REDIRECT_STDERR"):
            with self.subTest(name=name):
                code, arguments = self.run_hook({name: str(readme)})
                self.assertEqual((0, ["-B", f"{self.hub}/tools/hub_guard.py"]), (code, arguments))
                self.assertEqual(before, readme.read_bytes())
        # So would the trace2 targets the global configuration names, which only the variables outrank.
        targets = {"normalTarget": readme, "perfTarget": readme, "eventTarget": readme}
        configured = self.global_configuration(targets)
        code, arguments = self.run_hook({"GIT_CONFIG_GLOBAL": str(configured)})
        self.assertEqual((0, ["-B", f"{self.hub}/tools/hub_guard.py"]), (code, arguments))
        self.assertEqual(before, readme.read_bytes())
        # Real git: without the variables, the hook's own call would have written there.
        run_git(self.hub, {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}, "config", "--get", "x.y")
        self.assertNotEqual(before, readme.read_bytes())
        self.git(self.hub, "checkout", "--", "README.md")


class HookCommandUnderPowerShellTests(GuardTestCase):
    """The tracked hook command run under PowerShell, as Copilot CLI runs a hook's command on Windows.

    Copilot reads .claude/settings.json as a hook source in a trusted folder and hands its shell calls to this hook as
    `Bash`, PowerShell commands included. PowerShell must parse the command and end it at its own `exit 0`, so a
    Copilot call is never denied by a hook that errored, and never judged by the Bash reader.
    """

    resident_pwsh = False

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
        for program in ("python", "python3", "git", "env", "awk", "test", "command", "exec"):
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
WRITES = "`git {}` is not a read-only form the hub allows"


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


class CommandGuardTests(GuardTestCase):
    def test_upstream_and_rename_options_of_branch_are_refused_in_the_hub_through_both_shells(self) -> None:
        # An upstream set in the hub is one a bare pull would follow, so none is set, origin/main included.
        for shell in (self.bash, self.powershell):
            for options in (
                "-uorigin/main",
                "-uorigin/MAIN",
                "-quorigin/mM-feature",
                "-u origin/main",
                "-u feat/demo",
                "--set-upstream-to=origin/main",
                "--set-upstream-to=feat/demo",
                "--set-upstream-t feat/demo",
                "--unset-upstream",
                "-t origin/main",
                "-fm main renamed",
                "-qM main renamed",
                "-uorigin/main -m renamed",
                "--mov main renamed",
                "-m renamed",
                "renamed",
                "-c main copy",
                "-f other main",
            ):
                with self.subTest(shell=shell.__name__, options=options):
                    self.assertEqual("deny", shell(f"git branch {options}", self.hub))
                    self.assertIn(WRITES.format("branch"), self.reason)
                    self.assertIsNone(shell(f"git branch {options}", self.tree))
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
            "time -p git switch feat/demo",
            "time -- git switch feat/demo",
            "time -p -- git switch feat/demo",
            "git status; git switch feat/demo",
            "GIT_EDITOR=true git rebase --continue",
            "GIT_EDITOR=vim git rebase --continue",
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
            "git pull --ff-only origin main",
            "git pull -q --ff-only --no-rebase origin main",
            "git merge --ff-only origin/main",
            "git merge --quiet --ff-only origin/main",
            "git branch",
            "git branch --show-current",
            "git branch --merged main",
            "git branch -D feat/demo",
            "git branch -vv",
            "git branch --list 'feat/*'",
            "git switch main",
            "git checkout -q main",
            "git switch main 2>&1",
            "git stash list",
            "git apply --check change.patch",
            "git worktree add ../x -b fix/y main",
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
            ("git pull --ff-only origin feat/demo", "origin feat/demo"),
            ("git pull --ff-only upstream main", "upstream main"),
            ("git pull --ff-only origin", "origin"),
            # A bare pull follows the upstream a setting names, which branch -u or a worktree may change.
            ("git pull --ff-only", "its upstream"),
            ("git pull -q --ff-only", "its upstream"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(f"only to origin/main, not to {target}.", self.reason)
        # Any option no row declares is refused, so the last value of --ff-only is the one given.
        for command in (
            "git merge --ff-only --autostash origin/main",
            "git merge --ff-only --no-ff origin/main",
            "git merge --ff-only --ff origin/main",
            "git merge --ff-on origin/main",
            "git pull --ff-only --rebase origin main",
            "git pull --ff-only -r origin main",
            "git pull --ff-only --ff origin main",
            "git pull --ff-only --set-upstream origin main",
            "git merge origin/main",
            "git pull origin main",
            "git merge --ff-only -- origin/main",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn("is not a read-only form the hub allows", self.reason)
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
            ("unread", "!eval x"),
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
        # A shell alias is read as Bash, and a part of it the guard does not read is allowed, as in any command.
        self.assertIsNone(self.bash("git unread", self.hub))
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
            for name in ("git_work_tree", "Git_Work_Tree"):
                with self.subTest(name=name):
                    environment = {**self.environment, name: str(self.hub)}
                    self.assertEqual("deny", self.bash("git reset --hard", self.tree, environment))

    else:

        def test_a_location_variable_not_in_upper_case_is_not_one_git_reads(self) -> None:
            for name in ("git_work_tree", "Git_Work_Tree"):
                with self.subTest(name=name):
                    environment = {**self.environment, name: str(self.hub)}
                    self.assertIsNone(self.bash("git reset --hard", self.tree, environment))

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
            # git uses the other repository's HEAD and index and writes the directory it starts in. An assignment
            # before git, or one env hands it, is a part of the git call the guard does not settle, refused while a
            # hub is in view; export is not read, so the reset is placed in the hub.
            (f'GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'export GIT_DIR="{other}"; git reset --hard', self.hub, "deny"),
            (f'env GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'env GIT_DIR="{other}" git reset --hard', self.tree, "deny"),
            (f'GIT_DIR="{other}" git reset --hard', self.tree, "deny"),
        ):
            with self.subTest(command=command, cwd=cwd):
                self.assertEqual(expected, self.bash(command, cwd))
        inherited = {**self.environment, "GIT_DIR": str(other)}
        self.assertEqual("deny", self.bash("git reset --hard", self.hub, inherited))
        self.assertEqual("deny", self.powershell("git reset --hard", self.hub, inherited))
        self.assertIsNone(self.bash("git reset --hard", self.tree, inherited))
        # GIT_COMMON_DIR moves only the refs and objects every worktree shares, so a worktree call given another is
        # refused for those, not for its checkout.
        common = {**self.environment, "GIT_COMMON_DIR": str(other)}
        self.assertEqual("deny", self.bash("git reset --hard", self.hub, common))
        self.assertEqual("deny", self.bash("git reset --hard", self.tree, common))
        self.assertIn("shares with its worktrees", self.reason)
        self.assertEqual("main\n", self.git(self.hub, "branch", "--show-current"))
        self.assertEqual("", self.git(self.hub, "status", "--porcelain"))

    def index_file(self, value: Path | str) -> dict[str, str]:
        return {**self.environment, "GIT_INDEX_FILE": str(value)}

    def test_an_index_file_a_guarded_hub_holds_refuses_every_git_call_unless_it_is_the_call_s_own(self) -> None:
        # A hook or an editor's terminal may hand the session the hub's GIT_INDEX_FILE. Every git call rewrites the
        # index it is given, even a form that only reads the hub: status refreshes it, and a fast-forward or
        # `worktree add -b`, whose `reset --hard` inherits the variable, writes a tree into it out of step with the
        # hub's HEAD. So none runs, wherever it starts.
        (self.tree / "new.txt").write_text("new\n", encoding="utf-8")
        for index in (self.hub / ".git" / "index", self.hub / ".git" / "other-index", self.hub / "README.md"):
            environment = self.index_file(index)
            for command in (
                "git add new.txt",
                "git reset",
                "git read-tree HEAD",
                "git update-index --refresh",
                "git checkout -- .",
                "git commit -m x",
                "git status",
                "git diff",
                "git log -1",
                "git merge --ff-only origin/main",
                "git pull --ff-only origin main",
                "git worktree add -b task/x ../x",
                "gh pr checkout 12",
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(index=str(index), command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, self.tree, environment))
                        self.assertIn(f"GIT_INDEX_FILE={index}, an index the guarded hub", self.reason)
        # In the hub its read-only forms are refused too, given its own index as much as any other: `worktree add -b`
        # starts a `reset --hard`, and `worktree remove` a clean check, in another checkout, each inheriting the
        # variable, so the hub's index gets the new worktree's tree while the hub's HEAD stays where it was.
        for index in (self.hub / ".git" / "index", self.hub / ".git" / "other-index"):
            for command in (
                "git status",
                f'git worktree add -b task/x "{self.worktrees / "task-x"}" origin/main',
                f'git worktree remove "{self.tree}"',
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(index=str(index), command=command, shell=shell.__name__, cwd="hub"):
                        self.assertEqual("deny", shell(command, self.hub, self.index_file(index)))
                        self.assertIn(f"GIT_INDEX_FILE={index}, an index the guarded hub", self.reason)
        self.assertEqual("", self.git(self.hub, "status", "--porcelain"))
        # One no guarded hub holds changes nothing, nor does the worktree's own, which lies in the hub's git directory.
        # git answers with forward slashes on Windows, so the answer is compared as a path, never as text.
        own = Path(self.git(self.tree, "rev-parse", "--path-format=absolute", "--git-path", "index").strip())
        self.assertTrue(own.is_relative_to(self.hub / ".git"), own)
        for index in (self.root / "elsewhere" / "index", own):
            environment = self.index_file(index)
            with self.subTest(index=str(index)):
                self.assertIsNone(self.bash("git add . && git commit -m x && git status", self.tree, environment))
                self.assertIsNone(self.powershell("git reset", self.tree, environment))
                # In the hub the call writes the hub's tree whichever index it stages into.
                self.assertEqual("deny", self.bash("git checkout -- .", self.hub, environment))
        # The worktree's own index is the call's own only where the guard can tell where it starts.
        self.assertEqual("deny", self.bash('cd "$SOMEWHERE_UNSET" && git add .', self.tree, self.index_file(own)))
        self.assertIn("an index the guarded hub", self.reason)

    def test_a_relative_index_file_is_refused_while_a_hub_is_guarded(self) -> None:
        # git reads a relative path from the directory each process it starts works in, so `worktree add`'s
        # `reset --hard` reads it from the new worktree, where this one reaches the hub's index.
        relative = os.path.relpath(self.hub / ".git" / "index", self.tree / "deep" / "er" / "nn")
        for value in (relative, "staged-index"):
            environment = self.index_file(value)
            for command in ("git worktree add -b task/x deep/er/nn", "git add .", "git log -1"):
                for shell in (self.bash, self.powershell):
                    with self.subTest(value=value, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, self.tree, environment))
                        self.assertIn("a GIT_INDEX_FILE the guard cannot place", self.reason)
        # With no guarded hub in view it is allowed, with a note.
        outside = self.root / "outside"
        outside.mkdir()
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        self.assertIsNone(self.bash("git add .", outside, {**unshared, "GIT_INDEX_FILE": "staged-index"}))
        self.assertIn("could not tell where the index of `git add` lies", self.stderr)

    def test_an_index_file_the_text_does_not_settle_is_refused_while_a_hub_is_guarded(self) -> None:
        # An assignment before git, or one env hands it, is a part of the git call the guard does not settle. A
        # variable a command sets for a later git call, by export or PowerShell's $env:, is not read, which the
        # contract states.
        for command in (
            'GIT_INDEX_FILE="$x" git add .',
            "GIT_INDEX_FILE=/x git add .",
            "env GIT_INDEX_FILE=/x git add .",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(UNREAD, self.reason)
        for command in ("export GIT_INDEX_FILE=/x; git add .",):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))
        for command in ("$env:GIT_INDEX_FILE = '/x'; git add .", "$env:GIT_INDEX_FILE = $x; git add ."):
            with self.subTest(command=command):
                self.assertIsNone(self.powershell(command, self.tree))
        # A call read with options the text does not show, as an alias's is where nothing settles them, may be given
        # any index.
        place = hub_guard.GitOptions.initial(self.tree, {}, opaque=True).place()
        self.assertTrue(place.index_named)
        self.assertIsNone(place.index)
        call = hub_guard.GitCall(hub_guard.Place(self.tree, self.tree, index_named=True), "add", ["."])
        notes: list[str] = []
        reason = hub_guard.Judge({self.hub}, self.environment, notes, math.inf).judge(call, 0)
        self.assertIn("a GIT_INDEX_FILE the guard cannot place", reason or "")
        self.assertIsNone(hub_guard.Judge(set(), self.environment, notes, math.inf).index_refusal(call))
        self.assertIn("could not tell where the index of `git add` lies", notes)

    def test_an_index_file_git_cannot_place_is_refused_while_a_hub_is_guarded(self) -> None:
        failure = git_client.GitError("git could not read it", returncode=128)
        environment = self.index_file(self.root / "elsewhere" / "index")
        with mock.patch.object(hub_guard, "protecting_hub", side_effect=failure):
            for shell in (self.bash, self.powershell):
                with self.subTest(shell=shell.__name__):
                    self.assertEqual("deny", shell("git add .", self.tree, environment))
                    self.assertIn("a GIT_INDEX_FILE the guard cannot place", self.reason)

    def test_own_paths_are_where_git_keeps_the_call_s_repository_and_fail_closed(self) -> None:
        # git answers with forward slashes on Windows, so each answer is compared as a path, never as text.
        asked = ["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", "--git-path", "objects"]
        git_dir, common_dir, objects = (Path(line) for line in self.git(self.tree, *asked).splitlines())
        outside = self.root / "outside"
        outside.mkdir()
        place = hub_guard.Place(self.tree, self.tree)
        own = hub_guard.own_paths(place)
        assert own is not None
        for found, expected in ((own.git_dir, git_dir), (own.common_dir, common_dir), (own.objects, objects)):
            self.assertTrue(same(found, expected), (found, expected))
        self.assertTrue(own.linked)
        hub_own = hub_guard.own_paths(hub_guard.Place(self.hub, self.hub))
        assert hub_own is not None
        self.assertFalse(hub_own.linked, "a hub's git directory is its common one")
        # A repository named with --git-dir or GIT_DIR is the one asked, wherever git starts.
        named = hub_guard.own_paths(hub_guard.Place(outside, outside, git_dir, True))
        assert named is not None
        self.assertTrue(same(named.git_dir, git_dir))
        # A start in no repository, one that is no folder, none at all, and a repository named but not settled have
        # no repository of their own.
        for start, repository, is_named in (
            (outside, None, False),
            (self.root / "missing", None, False),
            (None, None, False),
            (self.tree, None, True),
        ):
            with self.subTest(start=str(start), named=is_named):
                self.assertIsNone(hub_guard.own_paths(hub_guard.Place(start, start, repository, is_named)))
        # git's answer is read as the paths it names, however they are spelled: forward slashes, as git writes on
        # Windows, and a segment that names the folder it is in.
        spelled = "".join(f"{path.parent.as_posix()}/./{path.name}\n" for path in (git_dir, common_dir, objects))
        with mock.patch.object(worktrees.CLIENT, "output", return_value=spelled):
            answered = hub_guard.own_paths(place)
        assert answered is not None
        self.assertTrue(same(answered.git_dir, git_dir))
        # An answer of other than three lines is an error, never a match.
        with (
            mock.patch.object(worktrees.CLIENT, "output", return_value=f"{git_dir}\n{common_dir}\n"),
            self.assertRaisesRegex(git_client.GitError, "answered 2 lines for the repository of"),
        ):
            hub_guard.own_paths(place)

    def second_worktree(self) -> Path:
        """Another task worktree of the hub, beside self.tree."""
        second = self.worktrees / "feat-second"
        self.git(self.hub, "worktree", "add", "-q", "-b", "feat/second", str(second), "main")
        return second

    def test_any_file_in_the_call_s_own_worktree_git_directory_is_its_index_and_no_other_call_s(self) -> None:
        # A hook or an editor git starts mid-operation hands on the lock file git writes the new index into, or the
        # next-index lock of a commit given paths; each lies in the worktree's own git directory, which git writes for
        # the worktree's calls anyway.
        own = Path(self.git(self.tree, "rev-parse", "--path-format=absolute", "--git-dir").strip())
        self.assertTrue(own.is_relative_to(self.hub / ".git"), own)
        files = [own / "index", own / "index.lock", own / "next-index-4242.lock"]
        for index in files:
            environment = self.index_file(index)
            for command in ("git add .", "git status", "git commit -m x"):
                for shell in (self.bash, self.powershell):
                    with self.subTest(index=index.name, command=command, shell=shell.__name__):
                        self.assertIsNone(shell(command, self.tree, environment))
        # The same files are no other call's: not one the hub runs, not even one of its read-only forms, and not one
        # another worktree runs, though each lies in the hub's git directory.
        second = self.second_worktree()
        for index in files:
            environment = self.index_file(index)
            for cwd, command in (
                (self.hub, "git status"),
                (self.hub, "git log -1"),
                (self.hub, "git pull --ff-only origin main"),
                (second, "git status"),
                (second, "git add ."),
                (self.tree, f'git -C "{self.hub}" status'),
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(index=index.name, cwd=cwd.name, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(f"GIT_INDEX_FILE={index}, an index the guarded hub", self.reason)

    def test_a_call_that_acts_on_the_hub_given_any_index_store_or_alternates_is_refused(self) -> None:
        # A fast-forward in the hub moves its HEAD and tree while the new tree goes into the index it is given, so the
        # hub's own index is left at the old commit; a store, alternates, a shallow or graft file, or replacements other
        # than its own leave its refs naming objects its own store lacks.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        for name, value in (
            ("GIT_INDEX_FILE", elsewhere / "index"),
            ("GIT_OBJECT_DIRECTORY", elsewhere),
            ("GIT_COMMON_DIR", self.other_git_directory()),
            ("GIT_ALTERNATE_OBJECT_DIRECTORIES", elsewhere),
            ("GIT_SHALLOW_FILE", elsewhere / "shallow"),
            ("GIT_GRAFT_FILE", elsewhere / "grafts"),
            ("GIT_REPLACE_REF_BASE", "refs/elsewhere/"),
        ):
            environment = {**self.environment, name: str(value)}
            for cwd, command in (
                (self.hub, "git merge --ff-only origin/main"),
                (self.hub, "git pull --ff-only origin main"),
                (self.hub, "git status"),
                (self.tree, f'git -C "{self.hub}" log -1'),
                (self.tree, 'cd "$SOMEWHERE_UNSET" && git status'),
            ):
                for shell in (self.bash, self.powershell):
                    if shell is self.powershell and "SOMEWHERE" in command:
                        continue
                    with self.subTest(name=name, cwd=cwd.name, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(f"given {name} from the session's environment", self.reason)
                        self.assertIn(f"without {name}", self.reason)
            # A call in a worktree writes no hub's HEAD, index, or tree, and its index is its own to write.
            if name == "GIT_INDEX_FILE":
                with self.subTest(name=name, cwd="tree"):
                    self.assertIsNone(self.bash("git add . && git status", self.tree, environment))
        # The hub's own store and common directory are its own.
        own = hub_guard.own_paths(hub_guard.Place(self.hub, self.hub))
        assert own is not None
        for name, value in (("GIT_OBJECT_DIRECTORY", own.objects), ("GIT_COMMON_DIR", own.common_dir)):
            with self.subTest(name=name, own=True):
                self.assertIsNone(self.bash("git status", self.hub, {**self.environment, name: str(value)}))
        # Real git: the fast-forward leaves the hub's own index at the old commit, which status shows as staged.
        self.git(self.tree, "commit", "-q", "--allow-empty", "-m", "ahead")
        (self.tree / "added.txt").write_text("added\n", encoding="utf-8")
        self.git(self.tree, "add", "added.txt")
        self.git(self.tree, "commit", "-q", "-m", "added")
        branch = self.git(self.tree, "branch", "--show-current").strip()
        run = run_git(
            self.hub, {**self.environment, "GIT_INDEX_FILE": str(elsewhere / "index")}, "merge", "--ff-only", branch
        )
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertIn("added.txt", self.git(self.hub, "status", "--porcelain"))

    def other_git_directory(self) -> Path:
        """The git directory of a throwaway repository outside the hub, made once."""
        other = self.root / "other repository"
        if not other.exists():
            self.other_repository()
        return other / ".git"

    def test_an_inherited_store_a_guarded_hub_holds_is_refused_unless_it_is_the_call_s_own(self) -> None:
        (self.tree / "new.txt").write_text("new\n", encoding="utf-8")
        docs = self.hub / "docs"
        docs.mkdir()
        second = self.second_worktree()
        second_git = Path(self.git(second, "rev-parse", "--path-format=absolute", "--git-dir").strip())
        for name, value in (
            ("GIT_OBJECT_DIRECTORY", docs),
            ("GIT_OBJECT_DIRECTORY", self.hub / ".git" / "objects" / "pack"),
            ("GIT_COMMON_DIR", second_git),
            ("GIT_COMMON_DIR", docs),
        ):
            environment = {**self.environment, name: str(value)}
            for command in ("git add new.txt", "git status", "gh pr checkout 12"):
                for shell in (self.bash, self.powershell):
                    with self.subTest(name=name, value=str(value), command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, self.tree, environment))
                        self.assertIn(f"{name}={value}, in the guarded hub", self.reason)
        # A worktree's own store is the hub's, which git writes for it anyway; one no hub holds is no hub's, though a
        # worktree call given one is refused for the refs it shares with its hub.
        own = hub_guard.own_paths(hub_guard.Place(self.tree, self.tree))
        assert own is not None
        for name, value in (("GIT_OBJECT_DIRECTORY", own.objects), ("GIT_COMMON_DIR", own.common_dir)):
            with self.subTest(name=name, value=str(value)):
                self.assertIsNone(self.bash("git add new.txt", self.tree, {**self.environment, name: str(value)}))
        for name, value in (
            ("GIT_OBJECT_DIRECTORY", self.root / "elsewhere"),
            ("GIT_COMMON_DIR", self.other_git_directory()),
        ):
            with self.subTest(name=name, value=str(value)):
                self.assertEqual(
                    "deny", self.bash("git add new.txt", self.tree, {**self.environment, name: str(value)})
                )
                self.assertIn("shares with its worktrees", self.reason)
        # From another repository the hub's own store is the hub's: a commit there writes its objects into it.
        other = self.other_git_directory().parent
        hub_store = {**self.environment, "GIT_COMMON_DIR": str(self.hub / ".git")}
        self.assertEqual("deny", self.bash("git add . && git commit -m x", other, hub_store))
        # A relative store is read from wherever git works, so the guard cannot place it.
        for name in ("GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"):
            with self.subTest(name=name, relative=True):
                self.assertEqual("deny", self.bash("git status", self.tree, {**self.environment, name: "objects"}))
                self.assertIn("a store the guard cannot place", self.reason)
        # Real git: the variable sends a worktree's new objects into the folder it names.
        (self.tree / "stored.txt").write_text("stored\n", encoding="utf-8")
        run = run_git(self.tree, {**self.environment, "GIT_OBJECT_DIRECTORY": str(docs)}, "add", "stored.txt")
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertTrue(any(path.is_dir() for path in docs.iterdir()), "git wrote no object into the named store")
        # And GIT_COMMON_DIR from another repository sends its commit's objects into the hub's store.
        (other / "file.txt").write_text("other\n", encoding="utf-8")
        for arguments in (["add", "file.txt"], ["commit", "-q", "-m", "other"]):
            run = run_git(other, hub_store, *arguments)
            self.assertEqual(0, run.returncode, run.stderr)
        commit = run_git(other, hub_store, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual("commit\n", self.git(self.hub, "cat-file", "-t", commit))

    def test_a_trace_or_output_file_the_environment_names_in_a_hub_is_refused(self) -> None:
        readme = self.hub / "README.md"
        docs = self.hub / "docs"
        docs.mkdir()
        for name, value in (
            ("GIT_TRACE", readme),
            ("GIT_TRACE_PACKET", readme),
            ("GIT_TRACE2", readme),
            ("GIT_TRACE2_EVENT", self.hub / ".git" / "trace"),
            ("GIT_TRACE2_PERF", docs),
            ("GIT_REDIRECT_STDOUT", readme),
            ("GIT_REDIRECT_STDERR", readme),
        ):
            environment = {**self.environment, name: str(value)}
            # The variable reaches every git a command starts, a program's own and a gh subcommand's among them, which
            # the text does not show, so every command is refused, wherever it runs.
            for cwd, command in (
                (self.tree, "git status"),
                (self.tree, "gh pr checkout 12"),
                (self.root, "git init x"),
                (self.hub, "python tools/worktrees.py list"),
                (self.tree, "gh pr view"),
                (self.root, "ls"),
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(name=name, cwd=cwd.name, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(
                            f"holds {name}={value}, and every git a command starts, a program's own included, writes "
                            "there, into a file of a guarded hub",
                            self.reason,
                        )
                        self.assertIn(f"without {name}", self.reason)
            # An edit runs no git.
            with self.subTest(name=name, tool="Edit"):
                self.assertIsNone(self.edit(self.tree / "notes.txt", self.tree, environment=environment))
        # A value that names no file, a relative trace, which git ignores, and a file no hub holds write no hub.
        for name, text in (
            ("GIT_TRACE", "1"),
            ("GIT_TRACE", "true"),
            ("GIT_TRACE", "relative.txt"),
            ("GIT_TRACE2_EVENT", "af_unix:/tmp/socket"),
            ("GIT_TRACE", str(self.root / "trace.txt")),
            ("GIT_TRACE", "/dev/null"),
            ("GIT_REDIRECT_STDOUT", "off"),
            ("GIT_REDIRECT_STDERR", "2>&1"),
            ("GIT_REDIRECT_STDOUT", str(self.root / "out.txt")),
        ):
            with self.subTest(name=name, text=text):
                self.assertIsNone(self.bash("git status", self.tree, {**self.environment, name: text}))
        # git for Windows opens a relative output file from where it starts, which the guard does not place.
        for command in ("git status", "ls"):
            with self.subTest(command=command, relative=True):
                environment = {**self.environment, "GIT_REDIRECT_STDOUT": "o"}
                self.assertEqual("deny", self.bash(command, self.tree, environment))
                self.assertIn("a file git writes that the guard cannot place", self.reason)
        self.assertEqual("", self.git(self.hub, "status", "--porcelain", "--untracked-files=no"))
        # Real git: a trace is appended to the absolute path the variable names, by a program's git too, and trace2
        # makes a file inside a directory it names.
        trace = self.root / "trace.txt"
        run_git(self.tree, {**self.environment, "GIT_TRACE": str(trace)}, "status")
        self.assertTrue(trace.stat().st_size > 0)
        program_trace = self.root / "program-trace.txt"
        listed = subprocess.run(
            [sys.executable, "-B", "-c", "import subprocess; subprocess.run(['git', 'status'], check=True)"],
            cwd=self.hub,
            env={**self.environment, "GIT_TRACE": str(program_trace)},
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        self.assertTrue(program_trace.stat().st_size > 0, "the program's git wrote no trace")
        folder = self.root / "traces"
        folder.mkdir()
        run_git(self.tree, {**self.environment, "GIT_TRACE2_PERF": str(folder)}, "status")
        self.assertTrue(list(folder.iterdir()))

    def test_a_trace2_target_the_configuration_names_in_a_hub_is_refused_for_every_command(self) -> None:
        # trace2 reads its targets from the system and global configuration, which GIT_CONFIG_GLOBAL,
        # GIT_CONFIG_SYSTEM, HOME, and XDG_CONFIG_HOME choose, include.path followed, so every git a command starts
        # writes there; the variable of the same name outranks the setting.
        readme = self.hub / "README.md"
        docs = self.hub / "docs"
        docs.mkdir()
        home = self.root / "home"
        home.mkdir()
        xdg = self.root / "xdg"
        (xdg / "git").mkdir(parents=True)
        included = self.global_configuration({"perfTarget": docs}, name="included.gitconfig")
        unset = {name: value for name, value in self.environment.items() if name != "GIT_CONFIG_GLOBAL"}
        cases: list[tuple[str, dict[str, str], str, Path]] = []
        for key, target in (("eventTarget", readme), ("normalTarget", self.hub / ".git" / "trace")):
            configured = self.global_configuration({key: target}, name=f"{key}.gitconfig")
            cases.append((key, {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}, "global", target))
        system = self.global_configuration({"eventTarget": readme}, name="system.gitconfig")
        without_nosystem = {name: value for name, value in self.environment.items() if name != "GIT_CONFIG_NOSYSTEM"}
        cases.append(("system", {**without_nosystem, "GIT_CONFIG_SYSTEM": str(system)}, "system", readme))
        shutil.copy(self.global_configuration({"eventTarget": readme}, name="home.gitconfig"), home / ".gitconfig")
        cases.append(("home", {**unset, "HOME": str(home)}, "global", readme))
        shutil.copy(self.global_configuration({"eventTarget": readme}, name="xdg.gitconfig"), xdg / "git" / "config")
        cases.append(
            ("xdg", {**unset, "HOME": str(self.root / "no home"), "XDG_CONFIG_HOME": str(xdg)}, "global", readme)
        )
        including = self.root / "including.gitconfig"
        including.write_text(f'[include]\n\tpath = "{included.as_posix()}"\n', encoding="utf-8")
        cases.append(("include", {**self.environment, "GIT_CONFIG_GLOBAL": str(including)}, "global", docs))
        for label, environment, scope, target in cases:
            for cwd, command in (
                (self.tree, "git status"),
                (self.hub, "python tools/worktrees.py list"),
                (self.tree, "gh pr view"),
                (self.root, "ls"),
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(case=label, cwd=cwd.name, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(f"The session's {scope} git configuration sets trace2.", self.reason)
                        self.assertIn(f"to {target.as_posix()}, and every git a command starts", self.reason)
                        self.assertIn("from an environment that sets GIT_TRACE2", self.reason)
        self.assertEqual("", self.git(self.hub, "status", "--porcelain", "--untracked-files=no"))
        # A variable that holds a value outranks the setting; a target no hub holds, a descriptor, a relative path,
        # which trace2 ignores, and a target in the repository's own configuration, which trace2 never reads, write no
        # hub.
        configured = self.global_configuration({"eventTarget": readme})
        for label, environment in (
            ("off", {**self.environment, "GIT_CONFIG_GLOBAL": str(configured), "GIT_TRACE2_EVENT": "0"}),
            ("elsewhere", {**self.environment, "GIT_CONFIG_GLOBAL": str(configured), "GIT_TRACE2_EVENT": "/dev/null"}),
        ):
            with self.subTest(case=label):
                self.assertIsNone(self.bash("ls", self.root, environment))
        # With every target's variable holding a value, no trace2 target is judged, though the configuration is still
        # asked for the other files it names for git to write.
        every_off = {
            **self.environment,
            "GIT_CONFIG_GLOBAL": str(configured),
            **dict.fromkeys(("GIT_TRACE2", "GIT_TRACE2_PERF", "GIT_TRACE2_EVENT"), "0"),
        }
        with mock.patch.object(worktrees.CLIENT, "quiet_output", wraps=worktrees.CLIENT.quiet_output) as asked:
            self.assertIsNone(self.bash("ls", self.tree, every_off))
        self.assertTrue([call for call in asked.call_args_list if "--get-regexp" in call.args[0]])
        for label, harmless_target in (
            ("outside", self.root / "trace.json"),
            ("descriptor", "2"),
            ("relative", "trace.json"),
            ("socket", "af_unix:/tmp/socket"),
        ):
            with self.subTest(case=label):
                harmless = self.global_configuration({"eventTarget": harmless_target}, name=f"{label}.gitconfig")
                self.assertIsNone(
                    self.bash("git status", self.tree, {**self.environment, "GIT_CONFIG_GLOBAL": str(harmless)})
                )
        self.git(self.tree, "config", "--local", "trace2.eventTarget", readme.as_posix())
        self.assertIsNone(self.bash("git status", self.tree))
        self.git(self.tree, "config", "--local", "--unset", "trace2.eventTarget")
        # Real git: a trace2 target the global configuration names, include.path followed, is written by any git, and
        # the variable set to 0 turns it off.
        trace = self.root / "configured-trace.json"
        written = self.global_configuration({"eventTarget": trace}, name="written.gitconfig")
        wrapper = self.root / "wrapper.gitconfig"
        wrapper.write_text(f'[include]\n\tpath = "{written.as_posix()}"\n', encoding="utf-8")
        run_git(self.tree, {**self.environment, "GIT_CONFIG_GLOBAL": str(wrapper), "GIT_TRACE2_EVENT": "0"}, "status")
        self.assertFalse(trace.exists())
        run_git(self.tree, {**self.environment, "GIT_CONFIG_GLOBAL": str(wrapper)}, "status")
        self.assertTrue(trace.stat().st_size > 0)

    def test_a_file_an_include_if_entry_names_is_read_for_trace2_targets_and_refused_as_written(self) -> None:
        # An includeIf.<condition>.path names a further configuration file as include.path does. The guard asks git
        # from inside a repository, where a gitdir condition matching every git directory holds, so a trace2 target
        # in the file is read; and the file is one git reads its configuration from, whether or not it exists yet.
        readme = self.hub / "README.md"
        conditional = self.root / "conditional.gitconfig"
        configured = self.global_configuration({})
        with configured.open("a", encoding="utf-8") as stream:
            stream.write(f'[includeIf "gitdir:**"]\n\tpath = "{conditional.as_posix()}"\n')
        environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
        for shell, command in (
            (self.bash, f'printf x > "{conditional}"'),
            (self.bash, f'git config --file "{conditional}" trace2.eventTarget "{readme}" && git status'),
            (self.powershell, f"'x' >> '{conditional}'"),
        ):
            with self.subTest(command=command, shell=shell.__name__):
                self.assertEqual("deny", shell(command, self.tree, environment))
                self.assertIn(f"{conditional}, a file git reads its system or global configuration from", self.reason)
        conditional.write_text(f"[trace2]\n\teventTarget = {readme.as_posix()}\n", encoding="utf-8")
        for shell in (self.bash, self.powershell):
            with self.subTest(shell=shell.__name__, target="included"):
                self.assertEqual("deny", shell("ls", self.tree, environment))
                self.assertIn("The session's global git configuration sets trace2.eventtarget to", self.reason)
                self.assertIn("every git a command starts", self.reason)

    def test_a_command_that_writes_a_file_git_reads_its_configuration_from_is_refused(self) -> None:
        # The guard reads the configuration's trace2 targets before the command runs, so a command that writes one
        # into the system or global configuration, or a file it includes, and then runs git would write the hub.
        readme = self.hub / "README.md"
        included = self.root / "included.gitconfig"
        configured = self.global_configuration({})
        with configured.open("a", encoding="utf-8") as stream:
            stream.write(f'[include]\n\tpath = "{included.as_posix()}"\n')
        environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
        trace2 = f"[trace2]\\n\\teventTarget = {readme.as_posix()}\\n"
        for target in (configured, included):
            for shell, command in (
                (self.bash, f'git config --file "{target}" trace2.eventTarget "{readme}" && git status'),
                (self.bash, f"printf '{trace2}' >> \"{target}\" && git status"),
                (self.bash, f"printf '{trace2}' >> \"{target}\" && python tools/worktrees.py list"),
                (self.powershell, f"git config --file '{target}' trace2.eventTarget '{readme}'; git status"),
                (self.powershell, f"'x' >> '{target}'; git status"),
            ):
                with self.subTest(target=target.name, command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, self.tree, environment))
                    self.assertIn("a file git reads its system or global configuration from", self.reason)
        # The files HOME and XDG_CONFIG_HOME choose count whether or not they exist.
        home = self.root / "home"
        home.mkdir()
        xdg = self.root / "xdg"
        unset = {name: value for name, value in self.environment.items() if name != "GIT_CONFIG_GLOBAL"}
        located = {**unset, "HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}
        for target in (home / ".gitconfig", xdg / "git" / "config"):
            with self.subTest(target=str(target)):
                self.assertEqual("deny", self.bash(f'printf x > "{target}"', self.tree, located))
                self.assertIn("a file git reads its system or global configuration from", self.reason)
        # Any other file is judged as before, and with no hub in view nothing is asked.
        self.assertIsNone(self.bash(f'printf x > "{self.root / "notes.txt"}" && git status', self.tree, environment))
        outside = self.root / "outside"
        outside.mkdir()
        unshared = {name: value for name, value in environment.items() if name != "CLAUDE_PROJECT_DIR"}
        self.assertIsNone(self.bash(f'printf x >> "{configured}"', outside, unshared))
        # Where git cannot name the files, or one cannot be placed, every write is refused while a hub is guarded.
        failure = git_client.GitError("git could not read it", returncode=128)
        notes = f'printf x > "{self.root / "notes.txt"}"'
        with mock.patch.object(hub_guard, "configuration_files", side_effect=failure):
            self.assertEqual("deny", self.bash(notes, self.tree, environment))
            self.assertIn("could not tell which files git reads its system and global configuration from", self.reason)
        with mock.patch.object(hub_guard, "configuration_files", return_value=[None]):
            self.assertEqual("deny", self.bash(notes, self.tree, environment))
            self.assertIn("cannot place a file git reads its system or global configuration from", self.reason)
        self.assertEqual("", self.git(self.hub, "status", "--porcelain", "--untracked-files=no"))
        # Real git: a target written into an included file reaches the next git in the same command.
        included.write_text(f"[trace2]\n\teventTarget = {readme.as_posix()}\n", encoding="utf-8")
        before = readme.read_bytes()
        run_git(self.tree, environment, "status")
        self.assertNotEqual(before, readme.read_bytes())
        self.git(self.hub, "checkout", "--", "README.md")

    def test_included_files_are_read_as_git_reads_them(self) -> None:
        source = self.root / "configuration" / "global"
        home = self.root / "home"
        cases = (
            ("~/included", home / "included"),
            ("relative/included", source.parent / "relative" / "included"),
            (str(self.root / "absolute"), self.root / "absolute"),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(Path(os.path.realpath(expected)), hub_guard.included_file(value, source, home))
        self.assertIsNone(hub_guard.included_file("~/included", source, None))
        self.assertIsNone(hub_guard.included_file("relative", None, home))
        # git expands a ~user/ path and a %(prefix)/ path itself (interpolate_path), so the guard cannot place them.
        for value in ("~nobody/included", "%(prefix)/etc/included"):
            with self.subTest(value=value):
                self.assertIsNone(hub_guard.included_file(value, source, home))
        for value in ("~nobody/inc.gitconfig", "%(prefix)/etc/inc.gitconfig"):
            configured = self.global_configuration({}, name="interpolated.gitconfig")
            with configured.open("a", encoding="utf-8") as stream:
                stream.write(f'[include]\n\tpath = "{value}"\n')
            environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
            with self.subTest(value=value, written=True):
                notes = self.root / "notes.txt"
                self.assertEqual("deny", self.bash(f'printf x >> "{notes}" && git status', self.tree, environment))
                reasons = ["cannot place a file git reads its system or global configuration from"]
                if platform_support.WINDOWS and value.startswith("~"):
                    # git for Windows cannot expand a ~user/ path and refuses the whole configuration, so the guard
                    # refuses a command whose lookups git answers with an error.
                    reasons.append("The guard failed reading this command")
                self.assertTrue(any(reason in self.reason for reason in reasons), self.reason)

    def test_a_relative_location_of_the_configuration_is_a_trace2_target_the_guard_cannot_place(self) -> None:
        # git reads a relative GIT_CONFIG_GLOBAL, GIT_CONFIG_SYSTEM, HOME, or XDG_CONFIG_HOME from wherever each git
        # starts, never from where the guard asks, so a trace2 target the file there names cannot be placed.
        readme = self.hub / "README.md"
        (self.tree / "rel.gitconfig").write_text(f"[trace2]\n\teventTarget = {readme.as_posix()}\n", encoding="utf-8")
        for name in hub_guard.CONFIGURATION_LOCATIONS:
            environment = {**self.environment, name: "rel.gitconfig"}
            for shell in (self.bash, self.powershell):
                with self.subTest(name=name, shell=shell.__name__):
                    self.assertEqual("deny", shell("git status", self.tree, environment))
                    self.assertIn(f"holds {name}=rel.gitconfig, a relative path each git reads", self.reason)
                    self.assertIn(f"without {name}", self.reason)
        # With no hub in view it is noted.
        outside = self.root / "outside"
        outside.mkdir()
        unshared = {key: value for key, value in self.environment.items() if key != "CLAUDE_PROJECT_DIR"}
        self.assertIsNone(self.bash("ls", outside, {**unshared, "GIT_CONFIG_GLOBAL": "rel.gitconfig"}))
        self.assertIn("could not tell where GIT_CONFIG_GLOBAL has git write", self.stderr)
        # Real git: the relative file in the worktree is the one a git started there reads, and its target is written.
        before = readme.read_bytes()
        subprocess.run(
            ["git", "status"],
            cwd=self.tree,
            env={**self.environment, "GIT_CONFIG_GLOBAL": "rel.gitconfig"},
            capture_output=True,
            check=True,
        )
        self.assertNotEqual(before, readme.read_bytes())
        self.git(self.hub, "checkout", "--", "README.md")

    def test_an_assignment_before_git_to_a_name_git_reads_for_a_file_or_a_program_is_refused(self) -> None:
        # XDG_CONFIG_HOME chooses the global configuration git reads, a trace2 target in it among them.
        xdg = self.root / "xdg"
        for name in (*hub_guard.GIT_READ_NAMES, "GIT_CONFIG_GLOBAL", "GIT_DIR"):
            with self.subTest(name=name):
                self.assertEqual("deny", self.bash(f'{name}="{xdg}" git status', self.tree))
                self.assertIn(f"an assignment before git, {name}", self.reason)
        # Before any other program the guard does not read the assignment, as it does not read the program.
        for command in (
            f'XDG_CONFIG_HOME="{xdg}" gh pr view',
            f'XDG_CONFIG_HOME="{xdg}" python tools/worktrees.py list',
            f'LD_PRELOAD="{xdg}" ls',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))
        # A name git does not read, or reads only for its messages, stays allowed before git, and so do the names and
        # values GIT_PREFIXES allows.
        for command in (
            "LC_ALL=C git status",
            "NAME=x git status",
            "GIT_PAGER=cat git log -1",
            "GIT_EDITOR=true git status",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))
        self.assertEqual("deny", self.bash("GIT_PAGER=less git log -1", self.tree))

    def test_a_configuration_the_guard_cannot_read_for_trace2_targets_is_refused_while_a_hub_is_guarded(self) -> None:
        failure = git_client.GitError("git could not read it", returncode=128)
        outside = self.root / "outside"
        outside.mkdir()
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        with mock.patch.object(hub_guard, "configured_written_targets", side_effect=failure):
            for shell in (self.bash, self.powershell):
                with self.subTest(shell=shell.__name__):
                    self.assertEqual("deny", shell("ls", self.tree))
                    self.assertIn("could not read the files the git configuration names for git to write", self.reason)
            self.assertIsNone(self.bash("ls", outside, unshared))
            self.assertIn("could not read the files the git configuration names for git to write", self.stderr)
        # A configured target the guard cannot place is refused the same way.
        placed = hub_guard.placed_file

        def unplaced_trace(value: str) -> Path | None:
            return None if value.endswith("trace.json") else placed(value)

        with mock.patch.object(hub_guard, "placed_file", side_effect=unplaced_trace):
            configured = self.global_configuration({"eventTarget": self.root / "trace.json"})
            environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
            self.assertEqual("deny", self.bash("ls", self.tree, environment))
            self.assertIn("a file git writes that the guard cannot place", self.reason)
            self.assertIsNone(self.bash("ls", outside, {**unshared, "GIT_CONFIG_GLOBAL": str(configured)}))
            self.assertIn("could not tell where trace2.eventtarget has git write", self.stderr)

    def test_a_worktree_call_given_another_store_or_view_of_objects_is_refused(self) -> None:
        # A linked worktree writes the refs its hub's store holds, its branch among them, so a commit or fetch there
        # given another store, alternates, a shallow or graft file, or replacements moves the hub's refs to objects
        # the hub's store lacks, or a gc prunes objects they need.
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        foreign = (
            ("GIT_OBJECT_DIRECTORY", str(elsewhere)),
            ("GIT_COMMON_DIR", str(self.other_git_directory())),
            ("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(elsewhere)),
            ("GIT_ALTERNATE_OBJECT_DIRECTORIES", ""),
            ("GIT_SHALLOW_FILE", str(elsewhere / "shallow")),
            ("GIT_SHALLOW_FILE", ""),
            ("GIT_GRAFT_FILE", str(elsewhere / "grafts")),
            ("GIT_REPLACE_REF_BASE", "refs/elsewhere/"),
        )
        second = self.second_worktree()
        for name, value in foreign:
            environment = {**self.environment, name: value}
            for cwd, command in (
                (self.tree, "git commit --allow-empty -m x"),
                (self.tree, "git fetch origin"),
                (second, "git gc --prune=now"),
                (self.root, f'git -C "{self.tree}" status'),
                (self.tree, "gh pr checkout 12"),
            ):
                for shell in (self.bash, self.powershell):
                    with self.subTest(name=name, value=value, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(f"the refs the guarded hub {self.hub} shares with its worktrees", self.reason)
                        self.assertIn(f"given {name} from the session's environment", self.reason)
                        self.assertIn(f"without {name}", self.reason)
            # Another repository shares no hub's store.
            with self.subTest(name=name, value=value, cwd="other"):
                other = self.other_git_directory().parent
                if name != "GIT_COMMON_DIR":
                    self.assertIsNone(self.bash("git commit --allow-empty -m x", other, environment))
        # A worktree's own store, given as such, is its own.
        own = hub_guard.own_paths(hub_guard.Place(self.tree, self.tree))
        assert own is not None
        self.assertIsNone(
            self.bash(
                "git commit --allow-empty -m x",
                self.tree,
                {**self.environment, "GIT_OBJECT_DIRECTORY": str(own.objects)},
            )
        )
        # Real git: a commit in the worktree with another store, reading the hub's through alternates, moves the
        # branch the hub's store holds to a commit that store lacks.
        branch = self.git(self.tree, "branch", "--show-current").strip()
        store = {
            **self.environment,
            "GIT_OBJECT_DIRECTORY": str(elsewhere),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(own.objects),
        }
        run = run_git(
            self.tree,
            store,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "elsewhere",
        )
        self.assertEqual(0, run.returncode, run.stderr)
        moved = self.git(self.hub, "rev-parse", f"refs/heads/{branch}").strip()
        missing = run_git(self.hub, self.environment, "cat-file", "-e", moved)
        self.assertNotEqual(0, missing.returncode, "the hub's store holds the commit")

    def test_the_store_a_call_shares_is_unsettled_where_its_place_is_and_no_hub_s_outside_every_repository(
        self,
    ) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        cases = (
            (hub_guard.Place(None, None), (None, True)),
            (hub_guard.Place(self.tree, self.tree, repository_named=True, repository=None), (None, True)),
            (hub_guard.Place(plain, plain), (None, False)),
            (hub_guard.Place(self.tree, self.tree), (self.hub, False)),
        )
        for place, expected in cases:
            with self.subTest(place=place):
                hub, unknown = hub_guard.shared_store_hub(place)
                self.assertEqual(expected, (None if hub is None else Path(hub), unknown))
        shallow = {"GIT_SHALLOW_FILE": str(self.root / "shallow")}
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        # A place the text does not settle may share a guarded hub's store: refused while one is in view, and passed
        # over with none.
        for command in ('cd "$SOMEWHERE_UNSET" && git status', 'cd "$SOMEWHERE_UNSET" && git -C "$PWD" status'):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree, {**unshared, **shallow}))
                self.assertIn("a place the guard cannot settle", self.reason)
                self.assertIsNone(self.bash(command, plain, {**unshared, **shallow}))
        # A directory outside every repository shares no store, with a hub in view or not.
        for shell, command in ((self.bash, f'git -C "{plain}" status'), (self.powershell, f"git -C '{plain}' status")):
            with self.subTest(command=command, shell=shell.__name__):
                self.assertIsNone(shell(command, self.tree, {**unshared, **shallow}))
        self.assertIsNone(self.bash("git status", plain, {**unshared, **shallow}))

    def test_a_worktree_call_whose_store_the_guard_cannot_place_is_refused_while_a_hub_is_guarded(self) -> None:
        failure = git_client.GitError("git could not read it", returncode=128)
        other = self.other_git_directory().parent
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        alternates = {**unshared, "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(self.root)}
        with mock.patch.object(hub_guard, "own_paths", side_effect=failure):
            # From the worktree a guarded hub is in view; from another repository alone none is, which is noted.
            for shell in (self.bash, self.powershell):
                with self.subTest(shell=shell.__name__):
                    self.assertEqual("deny", shell(f'git -C "{other}" status', self.tree, alternates))
                    self.assertIn("refs a guarded hub may share, from a place the guard cannot settle", self.reason)
            self.assertIsNone(self.bash("git status", other, alternates))

    def test_the_guard_s_own_git_calls_write_no_trace_the_environment_names(self) -> None:
        # The guard runs git to find the hubs in view, and to read the configuration's trace2 targets, so an inherited
        # trace in a hub file, or one the configuration names, would be written by the guard itself, whatever the
        # command.
        readme = self.hub / "README.md"
        before = readme.read_bytes()
        configured = self.global_configuration({"eventTarget": readme, "perfTarget": readme, "normalTarget": readme})
        for environment in (
            {**self.environment, "GIT_TRACE": str(readme), "GIT_TRACE2_PERF": str(readme)},
            {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)},
        ):
            with self.subTest(environment=sorted(set(environment) - set(self.environment))):
                self.assertEqual("deny", self.bash("ls", self.tree, environment))
                self.assertEqual("deny", self.edit(readme, self.tree, environment=environment))
                self.assertIsNone(self.edit(self.tree / "notes.txt", self.tree, environment=environment))
                self.assertEqual(before, readme.read_bytes())

    def test_an_inherited_path_the_guard_cannot_place_is_noted_with_no_hub_in_view(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        self.git(outside, "init", "-q")
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        for name, value, said in (
            ("GIT_OBJECT_DIRECTORY", "objects", "could not tell where GIT_OBJECT_DIRECTORY has `git add` write"),
            ("GIT_REDIRECT_STDOUT", "out.txt", "could not tell where GIT_REDIRECT_STDOUT has git write"),
        ):
            with self.subTest(name=name):
                self.assertIsNone(self.bash("git add .", outside, {**unshared, name: value}))
                self.assertIn(said, self.stderr)

    def test_an_absolute_store_git_cannot_place_is_refused_while_a_hub_is_guarded_and_noted_otherwise(self) -> None:
        # git answering with an error, for the call's own store or for the hub that may hold the named one, says
        # nothing about where the store lies, so it may be a guarded hub's.
        failure = git_client.GitError("git could not read it", returncode=128)
        outside = self.root / "outside"
        outside.mkdir()
        self.git(outside, "init", "-q")
        unshared = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        for name in ("GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR"):
            store = str(self.root / "elsewhere")
            for failing in ("own_paths", "protecting_hub"):
                with mock.patch.object(hub_guard, failing, side_effect=failure):
                    for shell in (self.bash, self.powershell):
                        with self.subTest(name=name, failing=failing, shell=shell.__name__):
                            self.assertEqual("deny", shell("git add .", self.tree, {**self.environment, name: store}))
                            self.assertIn("a store the guard cannot place", self.reason)
                    with self.subTest(name=name, failing=failing, hub=None):
                        self.assertIsNone(self.bash("git add .", outside, {**unshared, name: store}))
                        self.assertIn(f"could not tell where {name} has `git add` write", self.stderr)

    if not platform_support.WINDOWS:
        # Windows makes a symbolic link only with a privilege a test cannot count on.

        def test_an_inherited_index_file_reached_through_a_symbolic_link_is_judged_by_its_target(self) -> None:
            linked_file = self.root / "linked-index"
            linked_file.symlink_to(self.hub / ".git" / "index")
            linked_folder = self.root / "linked-git"
            linked_folder.symlink_to(self.hub / ".git", target_is_directory=True)
            for index in (linked_file, linked_folder / "index"):
                with self.subTest(index=str(index)):
                    self.assertEqual("deny", self.bash("git add .", self.tree, self.index_file(index)))
            elsewhere = self.root / "elsewhere"
            elsewhere.mkdir()
            (self.root / "linked-elsewhere").symlink_to(elsewhere / "index")
            self.assertIsNone(self.bash("git add .", self.tree, self.index_file(self.root / "linked-elsewhere")))

    def test_gh_checking_a_branch_out_is_placed_by_the_inherited_variables(self) -> None:
        # gh runs git in its own directory with the environment it was given.
        self.assertIsNone(self.bash("gh pr checkout 12", self.tree))
        for name, value in (("GIT_INDEX_FILE", self.hub / ".git" / "index"), ("GIT_DIR", self.hub / ".git")):
            with self.subTest(name=name):
                environment = {**self.environment, name: str(value)}
                self.assertEqual("deny", self.bash("gh pr checkout 12", self.tree, environment))

    def test_an_inherited_location_variable_cannot_hide_the_hub_from_the_file_guard(self) -> None:
        other = self.other_repository() / ".git"
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            with self.subTest(name=name):
                event = {"tool_name": "Edit", "tool_input": {"file_path": str(self.hub / "README.md")}}
                self.assertEqual("deny", self.decision(event, {**self.environment, name: str(other)}))


class WriteGuardTests(GuardTestCase):
    """A file a program or a cmdlet writes at a path its words name, a link a command makes, and a file the
    environment or the configuration names for an https call or git's file-system monitor to write, judged as a
    redirection's target is."""

    def outside(self) -> tuple[Path, dict[str, str]]:
        """A directory outside every hub, and the environment of a session there, with no guarded hub in view."""
        outside = self.root / "outside"
        outside.mkdir(exist_ok=True)
        return outside, {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}

    def configuration_file(self, text: str, name: str) -> Path:
        """A global git configuration that includes the fixture's own and adds text, in a folder of its own."""
        folder = self.root / "configuration"
        folder.mkdir(exist_ok=True)
        config = folder / name
        fixture = Path(self.environment["GIT_CONFIG_GLOBAL"]).as_posix()
        config.write_text(f'[include]\n\tpath = "{fixture}"\n{text}', encoding="utf-8")
        return config

    def test_a_redirection_or_a_file_tool_that_writes_a_configuration_file_is_refused(self) -> None:
        # A setting written into a file git reads its system or global configuration from reaches every git a later
        # command starts, though the guard judged the configuration before the command ran.
        configured = self.configuration_file("", "global.gitconfig")
        environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
        held = "a file git reads its system or global configuration from"
        for shell, command in (
            (self.bash, f'printf x >> "{configured}"'),
            (self.bash, f'echo x > "{configured}" && git status'),
            (self.powershell, f"'x' >> '{configured}'"),
        ):
            with self.subTest(command=command, shell=shell.__name__):
                self.assertEqual("deny", shell(command, self.tree, environment))
                self.assertIn(f"redirects its output into {configured}, {held}", self.reason)
        # A program that writes it by its own words is one the guard does not read.
        for shell, command in (
            (self.bash, f'cp notes.txt "{configured}"'),
            (self.powershell, f"Set-Content '{configured}' x"),
        ):
            with self.subTest(command=command, shell=shell.__name__):
                self.assertIsNone(shell(command, self.tree, environment))
        for tool in sorted(hub_guard.FILE_TOOLS):
            with self.subTest(tool=tool):
                self.assertEqual("deny", self.edit(configured, self.tree, tool, environment))
                self.assertIn(f"The {tool} tool writes {configured}, {held}", self.reason)
        # Another file is allowed, and with no guarded hub in view the configuration is not asked.
        self.assertIsNone(self.edit(self.root / "notes.txt", self.tree, environment=environment))
        outside, unshared = self.outside()
        unshared["GIT_CONFIG_GLOBAL"] = str(configured)
        with mock.patch.object(hub_guard, "configuration_files", side_effect=AssertionError("asked")):
            self.assertIsNone(self.edit(configured, outside, environment=unshared))

    if not platform_support.WINDOWS:
        # Windows makes a symbolic link only with a privilege a test cannot count on.

        def test_a_write_through_a_link_that_exists_is_judged_by_what_the_link_names(self) -> None:
            readme = self.hub / "README.md"
            linked_file = self.root / "linked-readme"
            linked_file.symlink_to(readme)
            linked_folder = self.root / "linked-hub"
            linked_folder.symlink_to(self.hub, target_is_directory=True)
            for command, written in (
                (f'printf x >> "{linked_file}"', linked_file),
                (f'printf x >> "{linked_folder / "README.md"}"', linked_folder / "README.md"),
            ):
                with self.subTest(command=command):
                    self.assertEqual("deny", self.bash(command, self.tree))
                    self.assertIn(f" {written}, a file of a guarded hub.", self.reason)
            for path in (linked_file, linked_folder / "README.md"):
                with self.subTest(edit=str(path)):
                    self.assertEqual("deny", self.edit(path, self.tree))
            elsewhere = self.root / "linked-elsewhere"
            elsewhere.symlink_to(self.root / "notes.txt")
            self.assertIsNone(self.bash(f'printf x >> "{elsewhere}"', self.tree))
            self.assertIsNone(self.edit(elsewhere, self.tree))
            self.assertEqual("hub\n", readme.read_text(encoding="utf-8"))

    def test_a_tls_key_log_the_environment_names_in_a_hub_is_refused_for_every_command(self) -> None:
        # libcurl, which git's https transport runs, writes the keys of each TLS session to the file SSLKEYLOGFILE
        # names, so every https git call writes there, a program's and gh's own among them.
        for value in (self.hub / "new-file", self.hub / "README.md"):
            environment = {**self.environment, "SSLKEYLOGFILE": str(value)}
            for cwd, command in ((self.tree, "git fetch origin"), (self.tree, "gh pr view"), (self.root, "ls")):
                for shell in (self.bash, self.powershell):
                    with self.subTest(value=value.name, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(
                            f"holds SSLKEYLOGFILE={value}, and every https call a command makes, a git call's or "
                            "another program's, writes there, into a file of a guarded hub",
                            self.reason,
                        )
                        self.assertIn("without SSLKEYLOGFILE", self.reason)
        # A relative path is opened from where each program starts, which the guard does not place.
        self.assertEqual("deny", self.bash("ls", self.tree, {**self.environment, "SSLKEYLOGFILE": "keys.log"}))
        self.assertIn("a relative path each program opens from where it starts", self.reason)
        for text in ("", str(self.root / "keys.log"), "/dev/null"):
            with self.subTest(text=text):
                self.assertIsNone(self.bash("ls", self.tree, {**self.environment, "SSLKEYLOGFILE": text}))
        outside, unshared = self.outside()
        self.assertIsNone(self.bash("ls", outside, {**unshared, "SSLKEYLOGFILE": "keys.log"}))
        self.assertIn("could not tell where SSLKEYLOGFILE has git write", self.stderr)

    def test_a_cookie_jar_or_socket_folder_the_configuration_names_in_a_hub_is_refused_for_every_command(self) -> None:
        # git writes the cookie jar http.cookieFile names back after each https call while http.saveCookies is on, for
        # any URL, and git's file-system monitor makes its socket in the folder fsmonitor.socketDir names.
        readme = (self.hub / "README.md").as_posix()
        docs = (self.hub / "docs").as_posix()
        cases = [
            ("cookie", f'[http]\n\tcookieFile = "{readme}"\n\tsaveCookies = true\n', "http.cookiefile", readme),
            (
                "url",
                f'[http "https://example.com"]\n\tcookieFile = "{readme}"\n\tsaveCookies\n',
                "http.https://example.com.cookiefile",
                readme,
            ),
            (
                "mixed",
                f'[http]\n\tcookieFile = "{readme}"\n[http "https://example.com"]\n\tsaveCookies = yes\n',
                "http.cookiefile",
                readme,
            ),
            ("socket", f'[fsmonitor]\n\tsocketDir = "{docs}"\n', "fsmonitor.socketdir", docs),
        ]
        for label, text, setting, value in cases:
            configured = self.configuration_file(text, f"{label}.gitconfig")
            environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
            for cwd, command in ((self.tree, "git status"), (self.tree, "gh pr view"), (self.root, "ls")):
                for shell in (self.bash, self.powershell):
                    with self.subTest(case=label, command=command, shell=shell.__name__):
                        self.assertEqual("deny", shell(command, cwd, environment))
                        self.assertIn(f"The session's global git configuration sets {setting} to {value}", self.reason)
                        self.assertIn(", into a file of a guarded hub, so every command is refused", self.reason)
                        self.assertIn(f"remove {setting} from it from a terminal", self.reason)
        # A leading ~/ is read from the home.
        configured = self.configuration_file('[http]\n\tcookieFile = "~/README.md"\n\tsaveCookies = 1\n', "home.gc")
        environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured), "HOME": str(self.hub)}
        self.assertEqual("deny", self.bash("ls", self.root, environment))
        self.assertIn("sets http.cookiefile to ~/README.md", self.reason)
        # A cookie jar git does not write back, or one no hub holds, is allowed; a relative one is refused while a hub
        # is guarded, and noted with none in view.
        for label, text in (
            ("unsaved", f'[http]\n\tcookieFile = "{readme}"\n'),
            ("off", f'[http]\n\tcookieFile = "{readme}"\n\tsaveCookies = false\n'),
            ("elsewhere", f'[http]\n\tcookieFile = "{(self.root / "jar").as_posix()}"\n\tsaveCookies = true\n'),
        ):
            configured = self.configuration_file(text, f"{label}.gitconfig")
            with self.subTest(case=label):
                self.assertIsNone(
                    self.bash("ls", self.root, {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)})
                )
        configured = self.configuration_file("[http]\n\tcookieFile = jar\n\tsaveCookies = true\n", "relative.gitconfig")
        self.assertEqual("deny", self.bash("ls", self.tree, {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}))
        self.assertIn("a path git reads from where each git starts, or expands for itself", self.reason)
        outside, unshared = self.outside()
        self.assertIsNone(self.bash("ls", outside, {**unshared, "GIT_CONFIG_GLOBAL": str(configured)}))
        self.assertIn("could not tell where http.cookiefile has git write", self.stderr)
        # Real git: the listing the guard asks for names each such setting by the name the guard reads.
        configured = self.configuration_file(cases[1][1] + cases[3][1], "listed.gitconfig")
        listed = run_git(
            self.tree,
            {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)},
            "config",
            "--includes",
            "--null",
            "--get-regexp",
            hub_guard.CONFIGURED_WRITES,
        )
        names = {entry.partition("\n")[0] for entry in listed.stdout.split("\0") if entry}
        self.assertEqual(
            {"http.https://example.com.cookiefile", "http.https://example.com.savecookies", "fsmonitor.socketdir"},
            names,
        )

    def test_the_governance_session_s_own_commands_run_from_the_hub(self) -> None:
        tools = self.hub / "tools"
        tools.mkdir()
        (tools / "project_release_sync.py").write_text("print(1)\n", encoding="utf-8")
        self.git(self.hub, "add", "tools/project_release_sync.py")
        self.git(self.hub, "commit", "-q", "-m", "a script")
        deployed = self.root / "home" / ".claude" / "skills" / "review-prs" / "scripts" / "review_pipeline.py"
        notes = self.root / "notes.md"
        for command in (
            f'python3 "{deployed}" prepare --host claude-code --pull owner/repo#12',
            f"python3 -c \"print(open('{notes}').read())\"",
            f'sed -n 1,5p "{notes}"',
            f'printf "x\\n" >> "{notes}"',
            "gh pr merge 12 --auto --squash",
            "gh pr view 12 --json state,mergeStateStatus -q .state",
            "gh pr checks 12",
            "gh pr diff 12 --name-only",
            "gh api repos/owner/repo/pulls/12/comments --paginate",
            "gh api graphql -f query='{ viewer { login } }'",
            f'gh issue create --title x --label bug --milestone v0.15.0 --body-file "{self.root / "issue.md"}"',
            f'python3 "{deployed}" prepare --host claude-code --pull owner/repo#12',
            f'python3 "{deployed}" status --pull owner/repo#12',
            f'python3 -B "{deployed}" finish --pull owner/repo#12 --report "{self.root / "report.md"}"',
            "git pull --ff-only origin main",
            f'git worktree remove "{self.tree}"',
            ".venv/bin/python -B tools/project_release_sync.py",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_a_program_the_guard_does_not_read_is_allowed_from_the_hub_whatever_its_words(self) -> None:
        # Any program other than git is run by its own words, which the guard does not read: a word a variable
        # holds, a hub file a tool rewrites, and a word that names a hub folder are all allowed, from the hub and from
        # a worktree alike. The hub-state check, not the guard, reports what such a program changed.
        for command in (
            'python3 -B "$P" check',
            "python tools/skill_reference.py --write",
            "python -B tools/runtime_prompts.py --write --role deployer",
            "cp notes.txt README.md",
            "sed -i s/hub/x/ README.md",
            "bash -c 'ls -la'",
            "env PYTHONUTF8=1 python x.py",
            "$GIT checkout main",
            "eval 'git checkout main'",
        ):
            for cwd in (self.hub, self.tree):
                with self.subTest(command=command, cwd=cwd.name):
                    self.assertIsNone(self.bash(command, cwd))
        for command in (
            'python3 -B "$env:P" check',
            "python tools/skill_reference.py --write",
            "Copy-Item x README.md",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.powershell(command, self.hub))
        self.assertEqual("hub\n", (self.hub / "README.md").read_text(encoding="utf-8"), "nothing ran")

    def test_a_cookie_jar_a_repository_s_own_configuration_names_in_a_hub_refuses_a_git_call_there(self) -> None:
        readme = (self.hub / "README.md").as_posix()
        other = self.root / "other"
        run_git(self.root, self.environment, "init", "-q", str(other)).check_returncode()
        config = other / ".git" / "config"
        original = config.read_text(encoding="utf-8")
        for label, text, setting in (
            ("cookie", f'[http]\n\tcookieFile = "{readme}"\n\tsaveCookies = true\n', "http.cookiefile"),
            ("socket", f'[fsmonitor]\n\tsocketDir = "{(self.hub / "docs").as_posix()}"\n', "fsmonitor.socketdir"),
        ):
            config.write_text(original + text, encoding="utf-8")
            for shell, command in (
                (self.bash, f'git -C "{other}" ls-remote https://example.com/x'),
                (self.bash, f'cd "{other}" && git fetch'),
                (self.powershell, f"git -C '{other}' ls-remote https://example.com/x"),
            ):
                with self.subTest(case=label, command=command):
                    self.assertEqual("deny", shell(command, self.tree))
                    self.assertIn(
                        f"The local git configuration a git call in {other} reads sets {setting} to", self.reason
                    )
                    self.assertIn(", into a file of a guarded hub, so the command is refused.", self.reason)
            # A command with no git call there reads none of it.
            self.assertIsNone(self.bash("git status", self.tree))
            self.assertIsNone(self.bash(f'ls "{other}"', self.tree))
        config.write_text(original, encoding="utf-8")
        self.assertIsNone(self.bash(f'git -C "{other}" status', self.tree))

    def test_a_cookie_jar_a_conditional_include_sets_only_for_another_checkout_is_refused_there(self) -> None:
        readme = (self.hub / "README.md").as_posix()
        other = self.root / "other"
        run_git(self.root, self.environment, "init", "-q", str(other)).check_returncode()
        jar = self.root / "configuration" / "jar.gitconfig"
        jar.parent.mkdir(exist_ok=True)
        jar.write_text(f'[http]\n\tcookieFile = "{readme}"\n\tsaveCookies = true\n', encoding="utf-8")
        configured = self.configuration_file(
            f'[includeIf "gitdir:{other.as_posix()}/"]\n\tpath = "{jar.as_posix()}"\n', "elsewhere.gitconfig"
        )
        environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
        self.assertEqual("deny", self.bash(f'git -C "{other}" fetch', self.tree, environment))
        self.assertIn(f"The global git configuration a git call in {other} reads sets http.cookiefile to", self.reason)
        self.assertIsNone(self.bash("git status", self.tree, environment))

    def test_a_command_that_writes_a_repository_s_configuration_and_runs_git_outside_the_hubs_is_refused(
        self,
    ) -> None:
        readme = (self.hub / "README.md").as_posix()
        other = self.root / "other"
        cookie = f"[http]\\n\\tcookieFile = {readme}\\n\\tsaveCookies = true\\n"
        written = "which may be a repository's own git configuration, and runs git outside every guarded hub"
        for shell, command in (
            (
                self.bash,
                f'git init -q "{other}" && printf "{cookie}" >> "{other}/.git/config" && '
                f'git -C "{other}" ls-remote https://example.com/x',
            ),
            (self.bash, f'printf x > "{other}/.GIT/Config" && git -C "{other}" fetch'),
            (self.bash, f'printf x > "{other}/config" && git --git-dir="{other}" fetch'),
            (
                self.bash,
                f'git -C "{other}" config --file "{other}/.git/config" http.cookieFile x && git -C "{other}" fetch',
            ),
            (self.powershell, f"'x' > '{other}/.git/config'; git -C '{other}' fetch"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", shell(command, self.tree))
                self.assertIn(written, self.reason)
        # A configuration written in a command of its own is read by the next command's judgement; a config file the
        # session's own checkout reads lies in the hub, which is refused as any hub file is.
        run_git(self.root, self.environment, "init", "-q", str(other)).check_returncode()
        for command in (
            f'git -C "{other}" config --file "{other}/.git/config" http.cookieFile x',
            f'printf x > "{self.root}/config" && git status',
            f'git init -q "{self.root}/fresh" && git -C "{self.root}/fresh" status',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))

    def test_a_command_that_writes_a_file_an_include_of_a_git_call_s_configuration_names_is_refused(self) -> None:
        other = self.root / "other"
        run_git(self.root, self.environment, "init", "-q", str(other)).check_returncode()
        included = self.root / "includes" / "included.gitconfig"
        included.parent.mkdir()
        included.write_text("", encoding="utf-8")
        relative = other / ".git" / "relative.gitconfig"
        relative.write_text("", encoding="utf-8")
        with (other / ".git" / "config").open("a", encoding="utf-8") as config:
            config.write(f'[include]\n\tpath = "{included.as_posix()}"\n\tpath = relative.gitconfig\n')
        for shell, command, written in (
            (self.bash, f'printf x > "{included}" && git -C "{other}" fetch', included),
            (self.bash, f'printf x > "{relative}" && git -C "{other}" fetch', relative),
            (self.powershell, f"'x' > '{included}'; git -C '{other}' fetch", included),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", shell(command, self.tree))
                self.assertIn(
                    f"The command writes {os.path.realpath(written)}, a file the git configuration", self.reason
                )
                self.assertIn(f"`git fetch` reads in {other} includes", self.reason)
        self.assertIsNone(self.bash(f'printf x > "{self.root}/elsewhere.txt" && git -C "{other}" fetch', self.tree))

    def test_a_git_call_s_configuration_git_cannot_read_is_refused_while_a_hub_is_guarded(self) -> None:
        other = self.root / "other"
        run_git(self.root, self.environment, "init", "-q", str(other)).check_returncode()
        failure = git_client.GitError("bad config line 1", returncode=128)
        with mock.patch.object(hub_guard, "repository_configuration", side_effect=failure):
            self.assertEqual("deny", self.bash(f'git -C "{other}" fetch', self.tree))
            self.assertIn(f"The guard could not read the git configuration `git fetch` reads in {other}", self.reason)
            outside, unshared = self.outside()
            self.assertIsNone(self.bash(f'git -C "{other}" fetch', outside, unshared))
            self.assertIn("could not read the git configuration `git fetch` reads", self.stderr)

    def test_a_place_whose_configuration_lookup_git_cannot_answer_is_refused_while_a_hub_is_guarded(self) -> None:
        failure = git_client.GitError("git could not read the place", returncode=128)
        with mock.patch.object(hub_guard, "alias_lookups", side_effect=failure):
            self.assertEqual("deny", self.bash("git status", self.tree))
            self.assertIn(
                "The guard could not read where `git status` runs (git could not read the place)", self.reason
            )
            outside, unshared = self.outside()
            self.assertIsNone(self.bash("git status", outside, unshared))
            self.assertIn("could not read where `git status` runs", self.stderr)

    def test_an_include_the_guard_cannot_place_refuses_a_command_that_writes_a_file(self) -> None:
        unplaced = hub_guard.RepositoryConfiguration([], [None])
        with mock.patch.object(hub_guard, "repository_configuration", return_value=unplaced):
            self.assertEqual("deny", self.bash("printf x > out.txt && git status", self.tree))
            self.assertIn("includes a file the guard cannot place, which the command may write", self.reason)
            # A command that writes nothing reads the same configuration harmlessly.
            self.assertIsNone(self.bash("git status", self.tree))

    def test_a_file_tool_edit_is_refused_while_the_configuration_files_cannot_be_named_or_placed(self) -> None:
        failure = git_client.GitError("git could not read it", returncode=128)
        notes = self.root / "notes.txt"
        for label, patched, part in (
            ("unread", {"side_effect": failure}, "could not tell which files git reads its system and global"),
            ("unplaced", {"return_value": [None]}, "cannot place a file git reads its system or global configuration"),
        ):
            with mock.patch.object(hub_guard, "configuration_files", **patched):
                for tool in sorted(hub_guard.FILE_TOOLS):
                    with self.subTest(case=label, tool=tool):
                        self.assertEqual("deny", self.edit(notes, self.tree, tool))
                        self.assertIn(f"The {tool} tool writes {notes}, and the guard", self.reason)
                        self.assertIn(part, self.reason)

    def test_a_tls_key_log_assigned_before_git_is_refused_and_before_another_program_is_not_read(self) -> None:
        # An assignment before git hands the variable to every https call it makes, as the inherited one does.
        log = self.hub / "keys.log"
        self.assertEqual("deny", self.bash(f'SSLKEYLOGFILE="{log}" git fetch origin', self.tree))
        self.assertIn("an assignment before git, SSLKEYLOGFILE", self.reason)
        for command in (f'SSLKEYLOGFILE="{log}" gh pr view', f'SSLKEYLOGFILE="{log}" ls'):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))

    def test_a_cookie_jar_set_on_the_command_line_the_session_inherits_is_refused(self) -> None:
        readme = (self.hub / "README.md").as_posix()
        for label, carried in (
            (
                "count",
                {
                    "GIT_CONFIG_COUNT": "2",
                    "GIT_CONFIG_KEY_0": "http.cookieFile",
                    "GIT_CONFIG_VALUE_0": readme,
                    "GIT_CONFIG_KEY_1": "http.saveCookies",
                    "GIT_CONFIG_VALUE_1": "true",
                },
            ),
            ("parameters", {"GIT_CONFIG_PARAMETERS": f"'http.cookieFile'='{readme}' 'http.saveCookies'='true'"}),
        ):
            with self.subTest(case=label):
                self.assertEqual("deny", self.bash("ls", self.root, {**self.environment, **carried}))
                self.assertIn(f"The session's command git configuration sets http.cookiefile to {readme}", self.reason)

    def test_a_cookie_jar_a_conditional_include_sets_where_the_session_works_is_refused(self) -> None:
        readme = (self.hub / "README.md").as_posix()
        jar = self.root / "configuration" / "jar.gitconfig"
        jar.parent.mkdir(exist_ok=True)
        jar.write_text(f'[http]\n\tcookieFile = "{readme}"\n\tsaveCookies = true\n', encoding="utf-8")
        for condition in ("onbranch:feat/**", "gitdir:**/.git/worktrees/**"):
            configured = self.configuration_file(
                f'[includeIf "{condition}"]\n\tpath = "{jar.as_posix()}"\n', "conditional.gitconfig"
            )
            with self.subTest(condition=condition):
                environment = {**self.environment, "GIT_CONFIG_GLOBAL": str(configured)}
                self.assertEqual("deny", self.bash("git fetch origin", self.tree, environment))
                self.assertIn(f"sets http.cookiefile to {readme}", self.reason)


class PowerShellGuardTests(GuardTestCase):
    def test_a_hyphenated_name_is_a_powershell_command_whatever_its_verb(self) -> None:
        # A module PowerShell loads on demand may define a function by any hyphenated name, as Windows' own
        # DeliveryOptimization module defines Delete-DeliveryOptimizationCache, and PowerShell runs it before a program.
        # The guard does not read it, so it is allowed, but it may move the location, so a git call after it is placed
        # where the text does not settle.
        for command in (
            "Delete-DeliveryOptimizationCache -Force",
            "BackupToAAD-BitLockerKeyProtector -MountPoint C:",
            "Flush-EtwTraceSession -Name x",
            "pre-commit run",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.powershell(command, self.tree))
                self.assertEqual("deny", self.powershell(f"{command}; git commit -m x", self.tree))
                self.assertIn(UNPLACED.format("commit"), self.stderr + self.reason)
        # The program's name with .exe, or a path, names the program alone, which moves nothing; Bash has no such
        # commands.
        self.assertIsNone(self.powershell("dotnet-format.exe --version; git commit -m x", self.tree))
        self.assertIsNone(self.bash("pre-commit run; git commit -m x", self.tree))

    """End to end through the hook, which starts a real pwsh; the reading is covered by PowerShellUnderstoodSetTests."""

    end_to_end = True

    def test_the_hub_refuses_powershell_git_writes_but_allows_maintenance_and_work_in_a_worktree(self) -> None:
        self.assertEqual("deny", self.powershell("git switch feat/demo", self.hub))
        self.assertIsNone(
            self.powershell("git status; git switch main; git pull --ff-only origin main; git stash list", self.hub)
        )
        self.assertIsNone(self.powershell(f"Set-Location '{self.tree}'; git commit -m \"it's done\"", self.hub))

    def test_a_worktree_session_cannot_reach_into_the_hub_through_either_shell(self) -> None:
        self.assertIsNone(self.powershell("git commit -m x", self.tree))
        self.assertEqual("deny", self.powershell(f"Set-Location '{self.hub}'; git commit -m x", self.tree))
        self.assertEqual("deny", self.bash(f'git -C "{self.hub}" switch feat/demo', self.tree))

    def test_a_location_variable_the_command_sets_is_a_part_the_guard_does_not_read(self) -> None:
        # A cmdlet the guard does not read may move the location, so the git call after it is held to the hub's forms;
        # an assignment is not read, and the git call after it is placed where the command starts.
        for command in (
            "Rename-Item env:FOO GIT_WORK_TREE; git commit -m x",
            f"Set-Item Environment::GIT_WORK_TREE '{self.hub}'; git reset --hard",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
                self.assertIn("could not tell where", self.reason)
        command = f"($env:GIT_WORK_TREE) = '{self.hub}'; $other, $env:GIT_DIR = 2, 'x'; git reset --hard"
        self.assertIsNone(self.powershell(command, self.tree))
        notes = self.root / "notes.txt"
        self.assertEqual("deny", self.powershell(f"Set-Content '{notes}' GIT_WORK_TREE; git commit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)

    def test_a_powershell_command_pwsh_cannot_read_is_allowed_with_a_note(self) -> None:
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
                self.assertIsNone(self.powershell("git switch feat/demo", cwd, environment))
                self.assertIn("could not find pwsh", self.stderr)


UNREAD = "The guard does not settle part of a git call in this command ("


class UnderstoodSetTests(GuardTestCase):
    """What the guard reads in a Bash command, one row for each thing it reads: its git calls and where each acts, and
    its redirections. Every other part is a program the guard does not read, allowed from the hub and from a worktree
    alike."""

    def outside(self) -> Path:
        outside = self.root / "not-a-repository"
        outside.mkdir(exist_ok=True)
        return outside

    def test_a_literal_chain_places_each_git_call_by_the_directories_it_moves_through(self) -> None:
        missing = self.root / "missing"
        for command, cwd, expected in (
            (f'cd "{self.hub}" && git commit -m x', self.tree, "deny"),
            (f'cd "{self.hub}"; git commit -m x', self.tree, "deny"),
            (f'cd "{self.tree}" && git commit -m x', self.hub, None),
            # git runs only where cd failed, so in the directory the shell started in; a cd to a directory that
            # exists is read as one that succeeds, so what runs after || is placed there too, which only refuses more.
            (f'cd "{missing}" || git commit -m x', self.tree, None),
            (f'cd "{missing}" || git commit -m x', self.hub, "deny"),
            (f'cd "{self.hub}" || git commit -m x', self.tree, "deny"),
            # A pipeline's commands run in subshells of their own, and a background job in one too.
            (f'cd "{self.hub}" | true; git commit -m x', self.tree, None),
            (f'cd "{self.hub}" & git commit -m x', self.tree, None),
            (f'pushd "{self.hub}" && popd && git commit -m x', self.tree, None),
            (f'pushd "{self.tree}" && popd && git commit -m x', self.hub, "deny"),
            # popd on an empty stack fails and stays put; one given an argument leaves the directory unknown.
            ("popd; git commit -m x", self.tree, None),
            ("popd; git commit -m x", self.hub, "deny"),
            (f'pushd "{self.tree}" && popd && git commit -m x', self.tree, None),
            (f'pushd "{self.tree}" && popd +1 && git commit -m x', self.tree, "deny"),
            (f'pushd "{self.tree}" && popd -n && git commit -m x', self.tree, "deny"),
            (f'cd -- "{self.hub}" && git commit -m x', self.tree, "deny"),
            # A directory that is no directory now fails, unless an earlier command makes it one.
            (f'cd "{missing}"; git commit -m x', self.hub, "deny"),
            (f'cd "{missing}" && git commit -m x', self.hub, None),
            ("cd .claude/worktrees/feat-demo && git commit -m x", self.hub, None),
            ("cd ../../.. && git commit -m x", self.tree, "deny"),
            # git a wrapper runs is read past the wrapper's options (command_wrappers).
            (f'timeout 60 git -C "{self.hub}" commit -m x', self.tree, "deny"),
            (f'timeout -k 5 60 git -C "{self.tree}" commit -m x', self.hub, None),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.bash(command, cwd))

    def test_a_compound_command_is_read_and_one_that_changes_the_directory_leaves_it_unknown(self) -> None:
        for command, cwd, expected in (
            ("for f in a b; do git status $f; done", self.tree, None),
            ("for f in a b; do git log -1 $f; done", self.tree, "deny"),
            ("if true; then git status; fi && git log -1", self.hub, None),
            ("while false; do git commit -m x; done", self.hub, "deny"),
            (f'if true; then cd "{self.tree}"; fi; git commit -m x', self.hub, "deny"),
            (f'( cd "{self.hub}" ); git commit -m x', self.tree, None),
            (f'{{ cd "{self.hub}"; }}; git status', self.tree, None),
            (f'{{ cd "{self.hub}"; }}; git commit -m x', self.tree, "deny"),
            ("case x in x) git switch feat/demo ;; esac", self.hub, "deny"),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, cwd))
        self.bash(f'if true; then cd "{self.tree}"; fi; git commit -m x', self.hub)
        self.assertIn("The guard could not tell where `git commit` runs", self.reason)

    def test_words_and_arguments_the_text_does_not_settle_are_judged_by_where_git_acts(self) -> None:
        for command, cwd, expected in (
            # A word log is given may be --output; one after -- is a path.
            ('git log -1 "$REV"', self.hub, "deny"),
            ('git log -1 -- "$REV"', self.hub, None),
            # In a hub every word before -- is read against its row, so one the text does not settle is refused.
            ('git status "$REV"', self.hub, "deny"),
            ('git status -- "$REV"', self.hub, None),
            ('git -C "$SOMEWHERE" status', self.hub, None),
            ('git commit -m "$(date)"', self.tree, None),
            ('git branch "$name"', self.hub, "deny"),
            ('git fetch origin "$ref"', self.hub, "deny"),
            ('git checkout "$branch"', self.hub, "deny"),
            ('git -C "$SOMEWHERE" commit -m x', self.tree, "deny"),
            ("git $VERB x", self.hub, "deny"),
            ("git $VERB x", self.tree, "deny"),
            (
                f'python -B tools/worktrees.py list > "{Path(tempfile.gettempdir()).as_posix()}/out.txt"',
                self.tree,
                None,
            ),
            # A redirection whose target the text does not settle is not read.
            ('python -B tools/worktrees.py list > "$f"', self.tree, None),
            ("ls *.md | head -1", self.hub, None),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.bash(command, cwd))
        self.bash('git branch "$name"', self.hub)
        self.assertIn("is given a word the text does not settle", self.reason)

    def test_a_substitution_and_a_here_document_are_read_as_what_they_run_and_as_data(self) -> None:
        for command, cwd, expected in (
            (f'echo "$(git -C "{self.hub}" switch feat/demo)"', self.tree, "deny"),
            (f"cat <<'EOF'\ngit -C '{self.hub}' switch feat/demo\nEOF", self.tree, None),
            (f"cat <<EOF\n$(git -C '{self.hub}' switch feat/demo)\nEOF", self.tree, "deny"),
            ("git commit -F - <<'EOF'\nfix(tools): a message\nEOF", self.tree, None),
            ("cat <<< 'git commit -m x'", self.hub, None),
            (f"diff <(git -C '{self.hub}' show HEAD) <(echo x)", self.tree, None),
            (f"diff <(git -C '{self.hub}' reset --hard) <(echo x)", self.tree, "deny"),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, cwd))

    def test_a_redirection_into_a_guarded_hub_is_refused_as_a_file_tool_s_edit_is(self) -> None:
        for command, cwd, expected in (
            (f'echo x > "{self.hub / "README.md"}"', self.tree, "deny"),
            ("echo x >> README.md", self.hub, "deny"),
            (f'echo x 2> "{self.hub / ".git" / "HEAD"}"', self.tree, "deny"),
            ("echo x > notes.txt", self.tree, None),
            # A target the text does not settle, or a relative one in a directory it does not settle, is not read.
            ('echo x > "$out"', self.hub, None),
            ('cd "$d" && echo x > notes.txt', self.tree, None),
            ("echo x > local.json", self.hub, None),
            ("echo x > '$null'", self.hub, "deny"),
            ("git status > /dev/null 2>&1", self.hub, None),
            ("echo x >&2", self.hub, None),
            (f"cat <<'EOF' > \"{self.hub / 'README.md'}\"\nx\nEOF", self.tree, "deny"),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, cwd))
        self.bash("echo x >> README.md", self.hub)
        self.assertIn("redirects its output into", self.reason)
        # A pattern's target is judged by the folder its match lies in.
        self.assertEqual("deny", self.bash("echo x > READ*.md", self.hub))
        self.assertIn("the file a pattern matches in", self.reason)
        self.assertIsNone(self.bash("echo x > READ*.md", self.tree))
        # A target under the system's temporary folder, by a variable naming it that the environment holds, is placed.
        environment = {**self.environment, "TMP": tempfile.gettempdir()}
        self.assertIsNone(self.bash('echo x > "$TMP/out.txt"', self.tree, environment))
        self.assertEqual("hub\n", (self.hub / "README.md").read_text(encoding="utf-8"))

    def test_gh_checking_a_branch_out_is_judged_as_the_checkout_git_makes(self) -> None:
        for command, cwd, expected in (
            ("gh pr checkout 12", self.hub, "deny"),
            ("gh pr co 12", self.hub, "deny"),
            ("gh -R owner/repo pr checkout 12", self.hub, "deny"),
            ("gh repo sync", self.hub, "deny"),
            ("gh pr checkout 12", self.tree, None),
            ("gh pr view 12 --json title", self.hub, None),
            ('gh api "repos/$OWNER/x"', self.hub, None),
            ('gh pr "$verb" 12', self.hub, None),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.bash(command, cwd))

    def test_a_gh_flag_before_its_subcommand_is_not_read_past(self) -> None:
        # cobra takes the word after a flag it does not know as that flag's value, so `gh pr -b x checkout 12` runs
        # pr checkout while its first two plain words are pr and x: a gh call the guard does not read.
        for command in (
            "gh pr -b x checkout 12",
            "gh pr --branch x checkout 12",
            "gh repo -b x sync",
            "gh -b x pr checkout 12",
            "gh pr --bogus=x checkout 12",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))
        # gh's flags that take a value are read past, so the checkout after them is read.
        for command in ("gh -R o/r pr checkout 12", "gh --repo=o/r pr checkout 12", "gh -Ro/r repo sync"):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
        for command in (
            "gh pr list --state open",
            "gh -R o/r pr view 1",
            "gh --repo=o/r pr view 1",
            "gh -Ro/r pr view 1",
            "gh pr view 12 --json title",
            "gh api -X GET repos/o/r/pulls",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))
        self.assertEqual("deny", self.bash("gh pr checkout 12", self.hub))

    def test_an_assignment_before_a_command_is_read_by_the_name_it_assigns(self) -> None:
        for command, expected in (
            ("LC_ALL=C sort notes.txt", None),
            ("PYTHONUTF8=1 python -B tools/worktrees.py list", None),
            ("GIT_EDITOR=true git rebase --continue", None),
            ("GIT_AUTHOR_NAME='A Name' git commit -m x", None),
            ("LC_ALL=C git log -1", None),
            ("GIT_EDITOR=vim git rebase --continue", "deny"),
            (f'GIT_DIR="{self.hub / ".git"}" git status', "deny"),
            ("HOME=/elsewhere git status", "deny"),
            ("XDG_CONFIG_HOME=/elsewhere git status", "deny"),
            # An element's or an array's assignment before git is judged by its name, its value unsettled.
            ("GIT_DIR[0]=x git commit -m x", "deny"),
            ("GIT_WORK_TREE=(x) git reset --hard", "deny"),
            ("GIT_AUTHOR_NAME[0]=x git commit -m x", "deny"),
            ("NAME[0]=x git status", None),
            # Before any other program, and alone, an assignment is not read.
            ("PATH=/elsewhere python -B x.py", None),
            ("x=1; echo done", None),
            ("PATH=/elsewhere; echo done", None),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, self.tree))

    def test_the_commands_sessions_run_most_are_read(self) -> None:
        for command in (
            f'cd "{self.tree}" && python -B -m pytest tests/tools/test_x.py -q 2>&1 | tail -20',
            "grep -n 'def ' tools/hub_guard.py | head -40",
            "sed -n '1,40p' docs/design.md",
            "git diff --stat && git status --short",
            "python -m ruff check a.py b.py",
            "timeout 600 python -B -m pytest tests/tools -q",
            'gh pr view 12 --json title,body --jq ".title"',
            "ls -la; wc -l README.md; cat README.md | tr a-z A-Z",
            'while read -r line; do echo "$line"; done < notes.txt',
            "set -euo pipefail; echo ok",
            "[ -f README.md ] && echo yes",
            "command -v pwsh",
            # cp to a folder the text does not settle, as "$x/", is refused like a redirection there (WriteGuardTests).
            "mkdir -p build && cp README.md build/",
            "./tools/some-script.sh --flag && node x.js && npm test",
            "xargs -0 grep -n x < list.txt",
            "timeout 60 python -B x.py",
            "env PYTHONUTF8=1 python -B x.py",
            "sudo ls",
            "bash -c 'ls && echo hi'",
            'cd "$(git rev-parse --show-toplevel)" && python -B -m pytest tests/tools/test_x.py -q',
            'for f in a.py b.py; do python -m ruff check "$f"; done',
        ):
            for cwd in (self.tree, self.hub):
                with self.subTest(command=command, cwd=cwd.name):
                    self.assertIsNone(self.bash(command, cwd))

    def test_git_a_wrapper_or_a_literal_shell_script_runs_is_judged_as_a_bare_git_call(self) -> None:
        hub = self.hub
        for command, cwd in (
            ("env git config coding-agent-skills.hubGuard false", self.hub),
            ("env git config coding-agent-skills.hubGuard false", self.tree),
            ("command git config coding-agent-skills.hubGuard false", self.tree),
            ("bash -c 'git config coding-agent-skills.hubGuard false'", self.tree),
            (f'env git -C "{hub}" checkout -b x', self.tree),
            (f'timeout 60 git -C "{hub}" checkout -b x', self.tree),
            (f'command git -C "{hub}" checkout -b x', self.tree),
            (f'exec git -C "{hub}" checkout -b x', self.tree),
            (f'nohup git -C "{hub}" checkout -b x', self.tree),
            (f'sudo git -C "{hub}" checkout -b x', self.tree),
            (f'xargs git -C "{hub}" checkout < /dev/null', self.tree),
            (f'gtimeout 5 env -i git -C "{hub}" checkout -b x', self.tree),
            (f"bash -c \"git -C '{hub}' checkout -b x\"", self.tree),
            ("bash -lc 'git checkout -b x'", self.hub),
            ("env GIT_EDITOR=true git rebase --continue", self.hub),
            (f'env -C "{hub}" git commit -m x', self.tree),
            ("command cd .. && git commit -m x", self.tree),
            ("bash -c 'echo x > README.md'", self.hub),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual("deny", self.bash(command, cwd))
        for command in (f"env git -C '{hub}' checkout -b x", f"bash -c \"git -C '{hub}' checkout -b x\""):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
        # What the wrapper's words do not settle, and git that only reads, stay allowed.
        for command, cwd in (
            ('timeout "$T" git commit -m x', self.tree),
            ('bash -c "$SCRIPT"', self.hub),
            ("env git status", self.hub),
            ("bash -c 'git log -1'", self.hub),
            ("command -v git", self.hub),
            ("timeout 60 git commit -m x", self.tree),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertIsNone(self.bash(command, cwd))

    def test_a_builtin_or_a_function_the_guard_does_not_follow_leaves_the_directory_unknown(self) -> None:
        for command in (
            f"eval 'cd {self.hub}'; git commit -m x",
            "source env.sh; git commit -m x",
            ". env.sh; git commit -m x",
            'command "$TOOL" status; git commit -m x',
            f"f() {{ cd {self.hub}; }}; git commit -m x",
            f"if true; then eval 'cd {self.hub}'; fi; git commit -m x",
            f"{{ f() {{ cd {self.hub}; }}; }}; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(UNPLACED.format("commit"), self.reason)
        # command -v and -V only print, so the commit after them stays in the worktree.
        for command in ("command -v git; git commit -m x", "command -V git && git commit -m x"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))
        self.assertEqual(
            {".", "source", "eval", "builtin", "command", "trap", "fc", "alias", "unalias", "enable", "hash"},
            set(hub_guard.BUILTINS_NOT_FOLLOWED),
        )

    def test_a_cdpath_search_and_cd_dash_leave_the_directory_unknown(self) -> None:
        environment = {**self.environment, "CDPATH": str(self.root)}
        self.assertEqual("deny", self.bash("cd sub && git commit -m x", self.hub, environment))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        # A target CDPATH does not search, one starting ./ or ../, is placed as written.
        self.assertIsNone(self.bash("cd ./.claude/worktrees/feat-demo && git commit -m x", self.hub, environment))
        self.assertEqual("deny", self.bash("cd - && git commit -m x", self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)


class PowerShellUnderstoodSetTests(GuardTestCase):
    """The understood set in PowerShell, read by its own parser: pipelines of commands whose elements are constants and
    parameters, joined by && and ||, with Set-Location, Push-Location, and Pop-Location followed. Each case starts a
    real pwsh, as the hook does."""

    def test_a_literal_chain_places_each_git_call_and_every_other_node_is_allowed(self) -> None:
        for command, cwd, expected in (
            (f"Set-Location '{self.hub}'; git commit -m x", self.tree, "deny"),
            (f"cd '{self.hub}' && git commit -m x", self.tree, "deny"),
            (f"Set-Location -LiteralPath '{self.tree}'; git commit -m x", self.hub, None),
            (f"Push-Location '{self.hub}'; Pop-Location; git commit -m x", self.tree, None),
            # Pop-Location on an empty stack stays put; one given an argument leaves the location unknown.
            ("Pop-Location; git commit -m x", self.tree, None),
            ("Pop-Location; git commit -m x", self.hub, "deny"),
            (f"Push-Location '{self.tree}'; Pop-Location -StackName x; git commit -m x", self.tree, "deny"),
            (f"pushd '{self.tree}'; popd -StackName x; git commit -m x", self.tree, "deny"),
            ("Get-ChildItem | Select-Object -First 3", self.hub, None),
            ("git status 2>&1 | Out-String", self.hub, None),
            ("git com'mit' -m x", self.hub, "deny"),
            ("git co`mmit -m x", self.hub, "deny"),
            ("gh pr checkout 12", self.hub, "deny"),
            ("'text'", self.hub, None),
            ("$x = 1; git status", self.tree, None),
            ("(Get-Command git).Source", self.tree, None),
            ("foreach ($c in 'a', 'b') { if ($c) { Write-Output $c } }", self.tree, None),
            (f"if ($true) {{ git -C '{self.hub}' commit -m x }}", self.tree, "deny"),
            ("[int]'5'; [string] 7", self.tree, None),
            ("[IO.File]::Exists('x')", self.tree, None),
            ("[System.Diagnostics.Process] 'x'", self.tree, None),
            ("$x.ToString()", self.tree, None),
            ("$x.$name", self.tree, None),
            ("Get-ChildItem | ForEach-Object Name", self.tree, None),
            ("Get-ChildItem | ForEach-Object -MemberName Delete", self.tree, None),
            ("Get-ChildItem | ForEach-Object -MemberName:Delete -ArgumentList:$true", self.tree, None),
            ("Get-ChildItem | ForEach-Object -Process:{ $_.Name }", self.tree, None),
            ("Get-ChildItem | Where-Object { $_.Length -gt 1 } | Sort-Object { $_.Name }", self.tree, None),
            ("Invoke-Command { git status }", self.tree, None),
            ("iex 'git status'", self.tree, None),
            # A part the guard does not read, a cast, a member, a module's function, a runner, a dot-sourced script,
            # or a cmdlet's write, is allowed where no git call it reads follows it.
            ("dotnet-format --verify-no-changes", self.tree, None),
            ("dotnet-format.exe --verify-no-changes", self.tree, None),
            ("timeout 5 git status", self.tree, None),
            ("$env:X = 1; git status", self.tree, None),
            ("$PSDefaultParameterValues = @{}", self.tree, None),
            ("git -C $env:SOMEWHERE status", self.tree, None),
            ("git -C $env:SOMEWHERE commit -m x", self.tree, "deny"),
            (f"Write-Output (git -C '{self.hub}' commit -m x)", self.tree, "deny"),
            ("Write-Output $x.Name", self.tree, None),
            ("git status > $env:TEMP\\out.txt", self.tree, None),
            ("Invoke-Expression 'git status'", self.tree, None),
            ("& $exe status", self.tree, None),
            ("[scriptblock]::Create('git status')", self.tree, None),
            ("if ($true) { git status }", self.tree, None),
            ("Get-ChildItem | ForEach-Object { $_.Name }", self.tree, None),
            ("Set-Location env:", self.tree, None),
            ("Get-Item alias:git", self.tree, None),
            ("Set-Content -Path:env:GIT_DIR -Value x; git commit -m x", self.tree, None),
            ("Set-Content -LiteralPath:env:GIT_DIR -Value x", self.tree, None),
            ("Copy-Item a.txt -Destination:env:GIT_DIR", self.tree, None),
            ("Set-Content $p x", self.tree, None),
            ("Get-Content -LiteralPath:README.md", self.tree, None),
            ("Start-Process git", self.tree, None),
            (". ./profile.ps1", self.tree, None),
            (f"git status > '{self.hub / 'README.md'}'", self.tree, "deny"),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.powershell(command, cwd))

    def test_a_file_redirection_is_read_in_every_statement_that_can_carry_one(self) -> None:
        readme = self.hub / "README.md"
        for command in (
            f"'x' > '{readme}'",
            f"Write-Output x >> '{readme}'",
            f"if ($true) {{ 'x' > '{readme}' }}",
            f"foreach ($c in 'a') {{ $c > '{readme}' }}",
            f"git status && 'x' > '{readme}'",
            f"$y = 'x'; $y > '{readme}'",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
        self.assertIsNone(self.powershell("'x' > notes.txt", self.tree))
        # A target the text does not settle is not read.
        self.assertIsNone(self.powershell("'x' > $f", self.hub))
        # > $null discards the output; a file named $null is a file, written in the hub here.
        self.assertIsNone(self.powershell("git status > $null", self.hub))
        self.assertEqual("deny", self.powershell("'x' > '$null'", self.hub))
        self.assertEqual("hub\n", readme.read_text(encoding="utf-8"))

    def test_a_location_powershell_may_move_is_unknown_after_it(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        # With a part it does not read, a git call is read from no place where any command may move the location: held
        # to the hub's forms while a hub is in view, and allowed with a note where none is.
        command = f"Set-Location '{self.hub}'; $x.ToString(); git commit -m x"
        self.assertEqual("deny", self.powershell(command, self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        self.assertIsNone(self.powershell(command, outside, environment))
        self.assertIn(UNPLACED.format("commit"), self.stderr)
        for command in (
            # A location a nested command moves, and one a pipeline of several commands moves, is unknown after it.
            f"Write-Output (Set-Location '{self.tree}'); git commit -m x",
            f"Set-Location '{self.tree}' | Out-Null; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub))
                self.assertIn(UNPLACED.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell("Set-Location -StackName x; git commit -m x", self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        self.assertIsNone(self.powershell("Set-Location -StackName x; git status", self.tree))
        self.assertEqual("deny", self.powershell("cd -; git commit -m x", self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        home = {**self.environment, "HOME": str(self.root)}
        self.assertEqual("deny", self.powershell(f"Set-Location '~/{self.hub.name}'; git commit -m x", self.tree, home))

    def test_the_home_is_the_one_the_environment_names_and_unknown_where_it_is_not_absolute(self) -> None:
        # The guard once fell back on Path.home(), the guard process's own home, which reads a relative profile
        # folder as given, so ~ was read relative to the guard's working directory.
        bare = {name: value for name, value in self.environment.items() if name not in {"HOME", "USERPROFILE"}}
        profile = {**bare, executable_locator.HOME_VARIABLE: str(self.root)}
        relative = {**bare, executable_locator.HOME_VARIABLE: "relative home"}
        for command, environment in (
            (f"cd ~/'{self.hub.name}' && git commit -m x", profile),
            ("cd ~ && git commit -m x", relative),
            ("cd && git commit -m x", relative),
            ("git -C ~ commit -m x", relative),
        ):
            with self.subTest(command=command, home=environment[executable_locator.HOME_VARIABLE]):
                self.assertEqual("deny", self.bash(command, self.tree, environment))
        for command, environment in (
            (f"Set-Location '~/{self.hub.name}'; git commit -m x", profile),
            ("Set-Location ~; git commit -m x", relative),
            ("Set-Location; git commit -m x", relative),
        ):
            with self.subTest(command=command, home=environment[executable_locator.HOME_VARIABLE]):
                self.assertEqual("deny", self.powershell(command, self.tree, environment))

    def test_the_home_is_read_from_the_environment_the_shell_is_given_and_no_other(self) -> None:
        # A denial reads alike for a hub and for a place left unknown, so the commands above cannot tell a home read
        # from the guard's own environment, or a relative one taken as given, from the one the shell is given.
        given = self.root / "given home"
        self.assertEqual(given, hub_guard.home({"HOME": str(given)}))
        self.assertEqual(given, hub_guard.home({executable_locator.HOME_VARIABLE: str(given)}))
        self.assertIsNone(hub_guard.home({"HOME": "relative home", "USERPROFILE": str(given)}))
        self.assertIsNone(hub_guard.home({executable_locator.HOME_VARIABLE: "relative home"}))
        self.assertIsNone(hub_guard.home({}))
        # A known home settles ~, so a commit that reaches a worktree only through it is read there and allowed.
        at_hub = {**self.environment, "HOME": str(self.hub)}
        self.assertIsNone(self.bash(f"cd ~/'{TREE.as_posix()}' && git commit -m x", self.hub, at_hub))

    def test_with_a_part_it_does_not_read_each_git_call_is_still_read_from_a_place_left_unknown(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        self.assertEqual(
            "deny", self.powershell(f"if ($true) {{ git -C '{self.hub}' commit -m x }}", outside, environment)
        )
        self.assertIsNone(self.powershell("if ($true) { git commit -m x }", outside, environment))


class GitCallTests(GuardTestCase):
    """git's own options, its subcommands, and its aliases, read through the hook in each shell."""

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
            "git fetch --update-head-ok origin",
            "git fetch -qu origin",
            # A refspec with a destination writes the ref a later fast-forward to origin/main follows.
            "git fetch origin feat/demo:refs/remotes/origin/main",
            "git fetch origin +feat/demo:main",
            "git fetch --set-upstream origin feat/demo",
            "git fetch --refmap= origin",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(WRITES.format("fetch"), self.reason)
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
            (f'git --git-dir="{git_dir}" commit -m x', outside),
            (f'git --git-dir "{git_dir}" --work-tree "{self.tree}" reset --hard', self.tree),
        ):
            with self.subTest(command=command, cwd=str(cwd)):
                self.assertEqual("deny", self.bash(command, cwd))
                self.assertIn("is not a read-only form the hub allows", self.reason)
        self.assertEqual("deny", self.powershell(f"git --git-dir='{git_dir}' commit -m x", self.tree))
        other = self.other_repository() / ".git"
        self.assertIsNone(self.bash(f'git --git-dir="{other}" commit -m x', self.tree))
        self.assertIsNone(self.bash(f'git --git-dir="{self.root / "no repository"}" commit -m x', self.tree))

    def test_a_git_directory_git_cannot_read_is_refused_never_taken_for_no_hub(self) -> None:
        other = self.other_repository() / ".git"
        (other / "config").write_text("[core\nbogus\n", encoding="utf-8")
        self.assertEqual("deny", self.bash(f'git --git-dir="{other}" commit -m x', self.tree))
        self.assertIn("cannot place it while a hub is guarded", self.reason)
        self.assertEqual("deny", self.edit(other / "HEAD", tool="Write"))

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

    def test_an_alias_lookup_git_fails_names_its_reason_on_one_line_or_its_exit_code(self) -> None:
        real = hub_guard.git
        for result, reason in (
            (git_client.GitResult(2, "", ""), "git config failed with exit code 2"),
            (git_client.GitResult(3, "", "error: bad config line 4\nfatal: \x1b[0munreadable\n"), None),
        ):
            failed = result

            def answer(cwd: Path, *arguments: str, failed: git_client.GitResult = failed) -> git_client.GitResult:
                return failed if "alias.nosuch" in arguments else real(cwd, *arguments)

            expected = reason or "error: bad config line 4 fatal: [0munreadable"
            with self.subTest(reason=expected), mock.patch.object(hub_guard, "git", side_effect=answer):
                self.assertEqual("deny", self.bash("git nosuch", self.tree))
                self.assertIn(f"The alias `git nosuch` could not be read ({expected}), so it is refused.", self.reason)

    def test_a_shell_alias_handed_a_word_the_text_does_not_settle_cannot_be_read(self) -> None:
        self.git(self.hub, "config", "alias.peek", "!git status")
        self.assertEqual("deny", self.bash('git peek "$x"', self.tree))
        self.assertIn("a word after it is one the text does not settle", self.reason)
        self.assertIsNone(self.bash("git peek x", self.tree))

    def test_a_subcommand_quoted_in_part_is_the_word_each_shell_makes_of_it(self) -> None:
        for command in ("git com'mit' -m x", 'git co"mm"it -m x'):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, self.hub))
                    self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell("git co`mmit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)
        self.assertEqual("deny", self.bash("g'it' co\\mmit -m x", self.hub))
        self.assertIn(WRITES.format("commit"), self.reason)

    def test_apply_check_after_a_double_dash_is_a_patch_name(self) -> None:
        for command, expected in (
            ("git apply change.patch -- --check", "deny"),
            ("git apply --check --apply change.patch", "deny"),
            ("git apply --check -- change.patch", None),
            ("git apply --check change.patch", None),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, self.hub))

    def writes_from_everywhere(self, words: str) -> None:
        """A git call whose words write a file of the hub is refused from the hub, with -C from outside every
        repository, and with the hub's path written out from a worktree, through either shell."""
        relative = words.format(target="README.md", directory="docs")
        absolute = words.format(target=f'"{self.hub / "README.md"}"', directory=f'"{self.hub / "docs"}"')
        for shell in (self.bash, self.powershell):
            for command, cwd in (
                (f"git {relative}", self.hub),
                (f'git -C "{self.hub}" {relative}', self.root),
                (f"git {absolute}", self.tree),
            ):
                with self.subTest(shell=shell.__name__, command=command, cwd=cwd.name):
                    self.assertEqual("deny", shell(command, cwd))
                    self.assertIn("a file of a guarded hub", self.reason)

    def test_a_git_option_that_writes_a_file_of_the_hub_is_refused_wherever_git_runs(self) -> None:
        for words in (
            *(
                f"{subcommand} --output={{target}}"
                for subcommand in (
                    *("diff", "log", "show", "whatchanged", "diff-tree", "diff-files", "diff-index", "range-diff"),
                    *("format-patch", "rev-list", "shortlog", "reflog", "blame", "fast-export"),
                )
            ),
            "diff --output {target}",
            "archive --output={target} HEAD",
            "archive --outp={target} HEAD",
            "archive -o {target} HEAD",
            "archive -o{target} HEAD",
            "archive -vo {target} HEAD",
            "format-patch -1 -o {directory}",
            "format-patch -1 --output-directory={directory}",
            "format-patch -1 --output-d {directory}",
            "bundle create {target} HEAD",
            "bundle create -q --version 3 {target} HEAD",
            "fast-export --export-marks={target} HEAD",
            "fast-export --export-ma {target} HEAD",
            "mailsplit -o{directory} mail.mbox",
            "pack-objects {target}",
            "bugreport -o {directory}",
            "bugreport --output-directory={directory}",
            "diagnose -o {directory}",
            "config --file {target} a.b c",
            "config -f{target} a.b c",
            "apply --build-fake-ancestor={target} change.patch",
            "read-tree --index-output={target} HEAD",
            "index-pack -o {target} pack.pack",
            "repack --expire-to={directory}",
            "init {directory}",
            "clone https://example.invalid/x.git {directory}",
            "worktree add -b topic {directory} HEAD",
        ):
            self.writes_from_everywhere(words)

    def test_a_git_output_into_the_hub_s_git_directory_or_its_default_directory_is_refused(self) -> None:
        for command in (
            "git diff --output=.git/config",
            "git format-patch -1",
            "git bugreport",
            "git diagnose",
            "git init",
            "git unpack-file 0123abc",
            "git clone https://example.invalid/x.git",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertEqual("deny", self.bash(f'git -C "{self.hub}" {command.removeprefix("git ")}', self.root))
                self.assertIn("a file of a guarded hub", self.reason)

    def test_a_git_option_that_writes_elsewhere_or_writes_nothing_is_allowed(self) -> None:
        for command, cwd in (
            ("git diff --output=out.patch", self.tree),
            ("git archive -o out.tar HEAD", self.tree),
            ("git format-patch -1", self.tree),
            ("git format-patch -1 --stdout", self.tree),
            ("git diff --output=/dev/null", self.tree),
            ("git log --output-indicator-new=+ -p -1", self.tree),
            ("git diff -- --output=README.md", self.hub),
            ("git bundle create out.bundle HEAD", self.tree),
            ("git worktree add -b fix/topic .claude/worktrees/fix-topic", self.hub),
            ("git init", self.tree),
            (f'git clone https://example.invalid/x.git "{self.root / "copy"}"', self.tree),
            ("git config --file out.config a.b c", self.tree),
            (f'git config --file "{self.hub / "local.json"}" a.b c', self.tree),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertIsNone(self.bash(command, cwd))

    def test_an_operand_a_root_or_an_in_place_mode_that_writes_the_hub_is_refused_wherever_git_runs(self) -> None:
        for words in (
            'interpret-trailers --in-place --trailer "Reviewed-by: outside" {target}',
            "interpret-trailers --in {target}",
            "mailinfo {target} out.patch",
            "mailinfo -k --encoding utf-8 out.msg {target}",
            "merge-file {target} base other",
            "merge-file -L a -L b --marker-size 9 {target} base other",
            "checkout-index --prefix={directory} -a -f",
            "checkout-index -a -f --prefix {directory}",
            "checkout-index --pre {directory} -a",
            "apply --directory={directory} change.patch",
            "am --directory {directory} mail.mbox",
            "worktree move other {target}",
        ):
            self.writes_from_everywhere(words)

    def test_a_root_or_an_operand_naming_the_hub_from_a_worktree_is_refused_in_each_spelling(self) -> None:
        up = Path(os.path.relpath(self.hub, self.tree)).as_posix()
        for command, shells in (
            (f"git checkout-index --prefix={up}/ -a -f", (self.bash, self.powershell)),
            (f"git checkout-index --prefix {up}/ -a -f", (self.bash, self.powershell)),
            (f"git checkout-index -a -f --prefix={up}/", (self.bash, self.powershell)),
            (f'git checkout-index -a -f --prefix "{self.hub.as_posix()}/"', (self.bash, self.powershell)),
            (f'git checkout-index "--prefix={self.hub.as_posix()}/" -a -f', (self.bash, self.powershell)),
            (f"git checkout-index --prefix={up}/.git/ -a -f", (self.bash,)),
            (f"git apply --unsafe-paths --directory={up} change.patch", (self.bash, self.powershell)),
            (f'git apply --unsafe-paths --directory "{self.hub.as_posix()}" change.patch', (self.bash,)),
            (f"git am --directory={up} mail.mbox", (self.bash, self.powershell)),
            (f"git merge-file {up}/README.md base other", (self.bash, self.powershell)),
            (f"git mailinfo {up}/README.md {up}/docs/out.patch", (self.bash, self.powershell)),
            (f'git interpret-trailers --in-place --trailer "Reviewed-by: x" {up}/README.md', (self.bash,)),
            (f'git -C "{self.hub}" interpret-trailers --in-place README.md', (self.bash, self.powershell)),
            (f"git worktree move . {up}/docs/moved", (self.bash, self.powershell)),
        ):
            for shell in shells:
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, self.tree))
                    self.assertIn("a file of a guarded hub", self.reason)

    def test_apply_with_unsafe_paths_writes_where_its_patch_says_so_it_is_refused_while_a_hub_is_guarded(self) -> None:
        for command in ("git apply --unsafe-paths change.patch", "git apply --unsafe change.patch"):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn("writes a file the text does not settle", self.reason)
        self.assertIsNone(self.bash("git apply --unsafe-paths --no-unsafe-paths change.patch", self.tree))
        self.assertIsNone(self.bash("git apply -- --unsafe-paths", self.tree))

    def test_an_option_s_value_spelled_as_a_mode_s_off_or_printing_form_leaves_the_write_judged(self) -> None:
        up = Path(os.path.relpath(self.hub, self.tree)).as_posix()
        for command, cwd, reason in (
            ("git apply --unsafe-paths --exclude --no-unsafe-paths change.patch", self.tree, "does not settle"),
            ("git apply --unsafe-paths --include=--no-unsafe-paths change.patch", self.tree, "does not settle"),
            ("git apply --unsafe-paths -p --no-unsafe-paths change.patch", self.tree, "does not settle"),
            (f"git interpret-trailers --in-place --trailer --no-in-place {up}/README.md", self.tree, "guarded hub"),
            (f"git merge-file -L -p {up}/README.md base other", self.tree, "guarded hub"),
            (f"git merge-file --marker-size --stdout {up}/README.md base other", self.tree, "guarded hub"),
            ("git format-patch --subject-prefix --stdout -1", self.hub, "guarded hub"),
            ("git format-patch --author --stdout -1", self.hub, "guarded hub"),
        ):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, cwd))
                    self.assertIn(reason, self.reason)

    def test_an_operand_writer_that_reads_the_hub_or_writes_elsewhere_is_allowed(self) -> None:
        up = Path(os.path.relpath(self.hub, self.tree)).as_posix()
        for command, cwd in (
            (f'git interpret-trailers --trailer "Reviewed-by: x" {up}/CLAUDE.md', self.tree),
            (f"git interpret-trailers {up}/README.md", self.tree),
            (f"git interpret-trailers --in-place --no-in-place {up}/README.md", self.tree),
            (f"git interpret-trailers -- --in-place {up}/README.md", self.tree),
            (f'git interpret-trailers --trailer "Reviewed-by: x" {up}/README.md', self.tree),
            ("git interpret-trailers --in-place README.md", self.tree),
            ("git mailinfo out.msg out.patch", self.tree),
            ("git merge-file README.md base other", self.tree),
            (f"git merge-file -p {up}/README.md base other", self.tree),
            (f"git merge-file --stdout {up}/README.md base other", self.tree),
            (f"git merge-file README.md {up}/README.md {up}/CLAUDE.md", self.tree),
            ("git checkout-index --prefix=export/ -a", self.tree),
            (f'git checkout-index --prefix="{(self.root / "export").as_posix()}/" -a', self.tree),
            ("git checkout-index -a -f", self.tree),
            ("git apply --directory=sub change.patch", self.tree),
            ("git am --directory sub mail.mbox", self.tree),
            ("git worktree move . ../elsewhere", self.tree),
            (f'git worktree move . "{(self.root / "moved").as_posix()}"', self.tree),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertIsNone(self.bash(command, cwd))

    def test_a_git_config_that_writes_is_refused_while_a_hub_is_guarded(self) -> None:
        for words in (
            "config coding-agent-skills.hubGuard false",
            "config --local --unset coding-agent-skills.hubGuard",
            "config --unset-all coding-agent-skills.hubGuard",
            "config --unse coding-agent-skills.hubGuard",
            "config unset coding-agent-skills.hubGuard",
            "config set coding-agent-skills.hubGuard false",
            "config core.worktree /elsewhere",
            "config --global core.fsmonitor true",
            "config --worktree core.pager less",
            "config --remove-section coding-agent-skills",
            "config edit",
            "config -e",
            'config "$key" false',
        ):
            for shell in (self.bash, self.powershell):
                for command, cwd in (
                    (f"git {words}", self.hub),
                    (f'git -C "{self.hub}" {words}', self.root),
                    (f"git {words}", self.tree),
                ):
                    with self.subTest(shell=shell.__name__, command=command, cwd=cwd.name):
                        self.assertEqual("deny", shell(command, cwd))
        self.assertEqual("true\n", self.git(self.hub, "config", "coding-agent-skills.hubGuard"))
        for words in (
            "config --get coding-agent-skills.hubGuard",
            "config coding-agent-skills.hubGuard",
            "config get coding-agent-skills.hubGuard",
            "config --list --show-origin",
            "config list --local",
            "config --get-regexp alias",
            "config --type=bool --get coding-agent-skills.hubGuard",
            "config --file .gitmodules --get-regexp path",
        ):
            with self.subTest(command=words):
                self.assertIsNone(self.bash(f"git {words}", self.hub))
                self.assertIsNone(self.bash(f'git -C "{self.hub}" {words}', self.root))

    def test_a_git_option_or_setting_that_runs_a_command_is_refused_while_a_hub_is_guarded(self) -> None:
        for words in (
            "-c core.pager=less log",
            "-c core.pager='git reset --hard HEAD~1' -p log",
            "-c pager.log=less log",
            "-c diff.external=true diff",
            "-c core.fsmonitor=true status",
            "-c core.sshCommand=true fetch origin",
            "-c core.hooksPath=/elsewhere status",
            "-c include.path=/elsewhere status",
            "--config-env core.pager=PAGER log",
            '-c "$setting" status',
            "fetch --upload-pack=true .",
            "pull --upload-pack true . main",
            "ls-remote --upload-pack=true .",
            "push --receive-pack=true . HEAD",
            "push --exec=true . HEAD",
            "archive --remote=. --exec=true HEAD",
            "grep -Otrue x",
            "grep --open-files-in-pager x",
            "rebase -x true HEAD~1",
            "rebase --exec=true HEAD~1",
            "bisect run true",
            "submodule foreach true",
            "difftool -x true",
            "filter-branch --tree-filter true HEAD",
        ):
            for command, cwd in ((f"git {words}", self.hub), (f'git -C "{self.hub}" {words}', self.root)):
                with self.subTest(command=command, cwd=cwd.name):
                    self.assertEqual("deny", self.bash(command, cwd))
            with self.subTest(command=words, cwd="worktree"):
                self.assertEqual("deny", self.bash(f"git {words}", self.tree))
        for command in (
            "git -c color.ui=never log -1",
            "git -c core.quotepath=false status",
            "git -c user.name=Someone -c user.email=a@b.invalid log -1",
            "git -c alias.go=status go",
            "git -c Color.Diff.Meta=blue diff",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_a_word_the_text_does_not_settle_given_to_a_subcommand_that_may_write_is_refused(self) -> None:
        for command, cwd in (
            ('git diff "$out"', self.hub),
            ('git diff "$out"', self.tree),
            ('git log -1 "$out"', self.tree),
            ('git archive "$out" HEAD', self.tree),
            ('git fetch "$out"', self.tree),
            ('git merge-file README.md "$base" other', self.tree),
            ('git mailinfo msg patch "$out"', self.tree),
            ('git interpret-trailers "$mode" README.md', self.tree),
            ('git checkout-index -a "$prefix"', self.tree),
            ('git am "$out" mail.mbox', self.tree),
            ('git apply "$mode" change.patch', self.tree),
            ('git worktree move . "$out"', self.tree),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual("deny", self.bash(command, cwd))
                self.assertIn("a word the text does not settle", self.reason)
        for command in (
            'git status "$out"',
            'git commit -m "$(cat notes.txt)"',
            'git diff -- "$out"',
            'git merge-file -- README.md "$base" other',
            'git interpret-trailers -- "$out"',
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))


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

    def test_the_forms_the_hub_allows_are_the_read_only_forms(self) -> None:
        listed = contract_names("The rows of `READ_ONLY_FORMS` are", ". Most read")
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(sorted(hub_guard.READ_ONLY_FORMS), sorted(listed))

    def test_the_file_tools_are_the_ones_the_guard_judges_by_their_path(self) -> None:
        self.assertEqual(sorted(hub_guard.FILE_TOOLS), sorted(contract_names("**File tools.**", "is refused when")))

    def test_the_hook_matches_exactly_the_tools_the_guard_reads(self) -> None:
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        (group,) = settings["hooks"]["PreToolUse"]
        self.assertEqual(sorted({*hub_guard.FILE_TOOLS, *hub_guard.SHELL_READERS}), sorted(group["matcher"].split("|")))
        self.assertEqual(
            ["Bash", "Edit", "Monitor", "NotebookEdit", "PowerShell", "Write"], sorted(group["matcher"].split("|"))
        )

    def test_the_refs_that_shadow_origin_main_are_the_guard_s(self) -> None:
        named = contract_names("(`FAST_FORWARD_SHADOWS`:", "), or when git cannot list them")
        self.assertEqual(sorted(hub_guard.FAST_FORWARD_SHADOWS), sorted(named))
        # Pinned against literals: the refs gitrevisions reads `origin/main` as before or beside the remote one.
        self.assertEqual(
            {"refs/origin/main", "refs/tags/origin/main", "refs/heads/origin/main", "refs/remotes/origin/main/HEAD"},
            set(hub_guard.FAST_FORWARD_SHADOWS),
        )

    def test_the_fast_forward_options_are_the_guard_s(self) -> None:
        named = set(contract_names("`merge --ff-only origin/main`, each also with", "; the target is named in full"))
        pull, merge = hub_guard.READ_ONLY_FORMS["pull"], hub_guard.READ_ONLY_FORMS["merge"]
        self.assertEqual(sorted(named | {"--ff-only"}), sorted(pull.flags))
        self.assertEqual(sorted(named - {"--no-rebase"} | {"--ff-only"}), sorted(merge.flags))
        self.assertEqual((frozenset(), frozenset()), (pull.valued | pull.attached, merge.valued | merge.attached))

    def test_git_s_options_with_a_value_are_the_ones_the_guard_reads(self) -> None:
        # The guard reads git_client's list, the one home of the options git reads a value after.
        self.assertEqual(sorted(git_client.GLOBAL_VALUE_OPTIONS), sorted(contract_names("or as the next word (", ")")))

    def test_the_git_options_that_write_or_run_and_the_settings_understood_are_the_ones_its_contract_names(
        self,
    ) -> None:
        def entries(table: Mapping[str, frozenset[str]]) -> list[str]:
            return sorted(f"{subcommand} {option}" for subcommand, options in table.items() for option in options)

        writing = contract_names("those `GIT_WRITING_OPTIONS` names:", ". Each is read in every spelling")
        self.assertEqual(entries(hub_guard.GIT_WRITING_OPTIONS), sorted(writing))
        running = contract_names("in every spelling and wherever the word stands:", ". So is a `bisect`")
        spellings = {
            subcommand: frozenset(
                {*(f"--{name}" for name in options.long), *(f"-{letter}" for letter in options.short)}
            )
            for subcommand, options in hub_guard.GIT_RUNNING_OPTIONS.items()
        }
        self.assertEqual(entries(spellings), sorted(running))
        sections = contract_names("every setting of a section `GIT_SECTIONS_UNDERSTOOD` names (", ")")
        self.assertEqual(sorted(hub_guard.GIT_SECTIONS_UNDERSTOOD), sorted(sections))
        # format-patch writes there only without --stdout, which the list names beside it.
        defaults = contract_names("a word after `--` is an operand.", "(`GIT_DEFAULT_WRITES`)")
        self.assertEqual(sorted(hub_guard.GIT_DEFAULT_WRITES), sorted(set(defaults) - {"--stdout"}))
        operands = contract_names("(`GIT_DEFAULT_WRITES`), and", "at a path an operand names (`GIT_OPERAND_WRITERS`)")
        self.assertEqual(sorted(hub_guard.GIT_OPERAND_WRITERS), sorted(operands))
        unread = contract_names("form deciding.", "lets a")
        self.assertEqual(entries({key: row.modes for key, row in hub_guard.GIT_UNREAD_MODES.items()}), sorted(unread))

    def test_the_powershell_commands_the_guard_reads_are_the_ones_its_contract_names(self) -> None:
        named = contract_names("these cmdlets, by its name or an alias PowerShell gives it:", "Every other node")
        cmdlets = {name for name in hub_guard.POWERSHELL_COMMANDS.values() if "-" in name}
        self.assertEqual(sorted(cmdlets), sorted(name.casefold() for name in named if "-" in name))

    def test_the_reading_time_and_the_nesting_limit_are_pinned(self) -> None:
        settings = json.loads((REPOSITORY_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        [hook] = settings["hooks"]["PreToolUse"][0]["hooks"]
        self.assertEqual(
            (4, 15, 10.0, 30, "block"),
            (
                hub_guard.NESTING_LIMIT,
                hub_guard.READING_SECONDS,
                worktrees.GUARD_GIT_TIMEOUT_SECONDS,
                hook["timeout"],
                hook["onFailure"],
            ),
        )
        document = (REPOSITORY_ROOT / "docs" / "parallel-sessions.md").read_text(encoding="utf-8")
        self.assertIn("more than four deep", document)
        self.assertIn("within 15 seconds", document)


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
        # Each git command a document or a repository skill tells the hub to run, as the document writes it, and the
        # same command with its placeholders filled in; the guard must allow each from the hub through both shells.
        # The repository skills that work on the hub, repo-cleanup and shepherd-pull-requests, and tools/worktrees.py,
        # run git from Python, whose git calls the hook never sees; the hook judges only the command line that starts
        # their Python, as any unlisted program's (WriteGuardTests), so the git commands a session types are these.
        # repo-cleanup's sweep names the folder that holds the hub, so in a guarded clone it runs from outside it.
        for document, written, command in (
            ("CLAUDE.md", "`git worktree remove <path>`", "git worktree remove '.claude/worktrees/feat-demo'"),
            ("CLAUDE.md", "`git branch -D <kind>/<name>`", "git branch -D 'feat/demo'"),
            ("CLAUDE.md", "`git fetch origin main`", "git fetch origin main"),
            (
                "docs/parallel-sessions.md",
                "git worktree remove '.claude/worktrees/<kind>-<name>'",
                "git worktree remove '.claude/worktrees/feat-demo'",
            ),
            ("docs/parallel-sessions.md", "git branch -D '<kind>/<name>'", "git branch -D 'feat/demo'"),
            ("docs/parallel-sessions.md", "python tools/worktrees.py list", "python tools/worktrees.py list"),
            ("CONTRIBUTING.md", "git pull --ff-only origin main", "git pull --ff-only origin main"),
            ("CONTRIBUTING.md", "git fetch origin main --tags", "git fetch origin main --tags"),
            ("docs/installation.md", "git pull --ff-only origin main", "git pull --ff-only origin main"),
            ("docs/implementing-changes.md", "`git pull --ff-only origin main`", "git pull --ff-only origin main"),
            ("docs/implementing-changes.md", "`git merge --ff-only origin/main`", "git merge --ff-only origin/main"),
            (
                "docs/releasing.md",
                "git grep -n -i -w -F -f '<list>' -- .",
                "git grep -n -i -w -F -f 'terms.txt' -- .",
            ),
            ("docs/code-review-operations.md", "`git ls-remote origin HEAD`", "git ls-remote origin HEAD"),
            ("README.md", "`git pull --ff-only origin main`", "git pull --ff-only origin main"),
            ("docs/parallel-sessions.md", "`switch main`", "git switch main"),
            ("docs/parallel-sessions.md", "`checkout main`", "git checkout -q main"),
            # The remedies the deployer prints, and its recovery guide repeats, for a deployment, which runs from
            # the hub.
            ("deployer/manifest.py", "'git pull --ff-only origin main'", "git pull --ff-only origin main"),
            ("docs/recovery.md", "`git pull --ff-only origin main`", "git pull --ff-only origin main"),
            ("deployer/source.py", "'git show HEAD:source.json'", "git show HEAD:source.json"),
        ):
            text = (REPOSITORY_ROOT / document).read_text(encoding="utf-8")
            with self.subTest(document=document, written=written):
                self.assertIn(written, text)
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertIsNone(shell(command, self.hub))
                    self.assertNotIn("could not", self.stderr)
        for command in ("git fetch --prune origin", "git status --short", "git log --oneline -5", "git worktree list"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))


class ReadOnlyFormTests(GuardTestCase):
    """A git call that acts on a guarded hub is allowed only in a form READ_ONLY_FORMS declares."""

    def refused_in_the_hub(
        self, commands: Sequence[str], reason: str | None = "is not a read-only form the hub allows"
    ) -> None:
        for command in commands:
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertEqual("deny", shell(command, self.hub))
                    if reason is not None:
                        self.assertIn(reason, self.reason)
            with self.subTest(command=command, cwd="-C from outside"):
                outside = self.root / "outside"
                outside.mkdir(exist_ok=True)
                words = command.removeprefix("git ")
                self.assertEqual("deny", self.bash(f'git -C "{self.hub}" {words}', outside))

    def test_reflog_s_writing_forms_are_refused(self) -> None:
        self.refused_in_the_hub(
            (
                "git reflog delete --updateref 'refs/heads/main@{0}'",
                "git reflog delete --rewrite 'refs/heads/main@{0}'",
                "git reflog delete --update 'refs/heads/main@{0}'",
                "git reflog delete --updateref 'main@{0}'",
                "git reflog expire --updateref --expire=now refs/heads/main",
                "git reflog expire --expire=now --all",
                "git reflog --updateref",
                "git reflog drop",
            )
        )

    def test_apply_is_allowed_only_to_check_or_describe_a_patch(self) -> None:
        self.refused_in_the_hub(
            (
                "git apply --check --no-check change.patch",
                "git apply --check --no-che change.patch",
                "git apply --check --no-ch change.patch",
                "git apply --check --no-check",
                "git apply --check --no-check --index change.patch",
                "git apply --check --index change.patch",
                "git apply --check --apply change.patch",
                "git apply --check --app change.patch",
                "git apply --stat --apply change.patch",
                "git apply --chec change.patch",
                "git apply change.patch",
                "git apply change.patch -- --check",
                "git apply --check -3 change.patch",
            )
        )
        for command in ("git apply --check change.patch", "git apply --check", "git apply --stat -- change.patch"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_a_dash_word_after_a_valued_option_written_alone_is_refused(self) -> None:
        # git takes the word after a valued option written alone as its value whatever it holds, so the guard may not
        # read a dash word there as an option of its own: --check would be --exclude's value and the patch applied, and
        # -l --format's value and origin/main or v9.9.9 a ref created.
        self.refused_in_the_hub(
            (
                "git apply --exclude --check change.patch",
                "git apply -p --check change.patch",
                "git apply --exclude -- --check change.patch",
                "git branch --format -l origin/main",
                "git tag --format -l v9.9.9",
                "git branch --contains -l origin/main",
            ),
            reason="as the value of",
        )
        for command in (
            "git apply --exclude=--check --check change.patch",
            "git apply -p1 --check change.patch",
            "git branch --format=-l",
            "git tag --format=-l -l",
            "git branch --format x -l",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_an_upstream_cannot_be_set_and_then_followed_by_a_pull(self) -> None:
        self.refused_in_the_hub(
            (
                "git branch -u feat/demo",
                "git branch --set-upstream-to=origin/feat/demo",
                "git branch --set-upstream-to origin/feat/demo main",
                "git fetch --set-upstream origin feat/demo",
                "git pull --ff-only --set-upstream origin main",
            )
        )
        self.refused_in_the_hub(("git pull --ff-only",), reason="only to origin/main, not to its upstream.")
        self.git(self.hub, "branch", "-u", "feat/demo")
        self.assertEqual("deny", self.bash("git pull --ff-only", self.hub))
        self.assertIn("only to origin/main, not to its upstream.", self.reason)
        self.assertIsNone(self.bash("git pull --ff-only origin main", self.hub))

    def test_the_writes_earlier_outside_reviews_found_stay_refused(self) -> None:
        # The writes a subcommand's option or operand makes, and the prefixes before git, that earlier passes found.
        self.refused_in_the_hub(
            (
                "git diff --output=README.md",
                "git log --outp=README.md",
                "git archive -o README.md HEAD",
                "git config coding-agent-skills.hubGuard false",
                "git interpret-trailers --in-place README.md",
                "git mailinfo README.md CLAUDE.md",
                "git merge-file README.md base other",
                "git checkout-index --prefix=docs/ -a",
                "git apply --unsafe-paths change.patch",
                "git reflog delete --updateref 'main@{0}'",
                "git replace HEAD HEAD~2",
                "git notes add -m x",
                "git tag v0.0.1",
                "git push origin main",
                "git gc",
                "git submodule status",
                "git stash",
                "git stash push -m x",
                "git worktree add -f ../x main",
                "git worktree remove -f ../x",
                "git worktree move ../x ../y",
                "git remote set-url origin https://example.invalid/x.git",
                "git remote add other https://example.invalid/x.git",
                "git switch -c other",
                "git checkout -- main",
                "git checkout feat/demo",
                "git status --porcelain=v2 --ext-diff",
                "git log --ext-diff -p",
                "git log --textconv -p",
            ),
            reason=None,
        )

    def test_a_subcommand_no_row_names_is_refused_unless_it_is_an_alias_of_a_row(self) -> None:
        self.refused_in_the_hub(("git frobnicate", "git hash-object -w README.md", "git maintenance run"))
        self.git(self.hub, "config", "alias.peek", "log --oneline -1")
        self.git(self.hub, "config", "alias.rewind", "reflog delete --updateref main@{0}")
        self.assertIsNone(self.bash("git peek", self.hub))
        self.assertEqual("deny", self.bash("git rewind", self.hub))
        self.assertIn("is an alias", self.reason)

    def test_a_word_the_text_does_not_settle_is_refused_unless_it_is_an_operand_a_row_allows(self) -> None:
        for command in ('git log "$x"', 'git fetch origin "$x"', 'git pull --ff-only origin "$x"', 'git branch "$x"'):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
        self.assertIsNone(self.bash('git log --oneline -- "$x"', self.hub))

    def test_a_branch_the_hub_creates_is_a_task_s_and_never_shadows_origin_main(self) -> None:
        # worktree add -b origin/main creates refs/heads/origin/main, which git reads for origin/main before
        # refs/remotes/origin/main, so a later merge --ff-only origin/main would follow an unmerged commit.
        self.refused_in_the_hub(
            (
                "git worktree add -b origin/main .claude/worktrees/x feat/demo",
                "git worktree add -borigin/main .claude/worktrees/x feat/demo",
                "git worktree add -B origin/main .claude/worktrees/x feat/demo",
                "git worktree add -b main .claude/worktrees/x feat/demo",
                "git worktree add -b HEAD .claude/worktrees/x feat/demo",
                "git worktree add -b refs/remotes/origin/main .claude/worktrees/x feat/demo",
                "git worktree add -b remotes/origin/main .claude/worktrees/x feat/demo",
                "git worktree add -b topic .claude/worktrees/x feat/demo",
                "git worktree add -b fix/Upper .claude/worktrees/x feat/demo",
                "git worktree add -b -q .claude/worktrees/x feat/demo",
                "git worktree add .claude/worktrees/x feat/demo",
                "git worktree add .claude/worktrees/x",
            )
        )
        for command in (
            "git worktree add -b fix/x .claude/worktrees/fix-x origin/main",
            "git worktree add -q -b feat/new-thing .claude/worktrees/feat-new-thing main",
            "git worktree add -bfix/x .claude/worktrees/fix-x main",
        ):
            for shell in (self.bash, self.powershell):
                with self.subTest(command=command, shell=shell.__name__):
                    self.assertIsNone(shell(command, self.hub))

    def test_a_fast_forward_to_origin_main_is_refused_while_another_ref_of_that_name_exists(self) -> None:
        self.git(self.hub, "update-ref", "refs/remotes/origin/main", "main")
        self.assertIsNone(self.bash("git merge --ff-only origin/main", self.hub))
        # The two steps the review found: a hub form that created refs/heads/origin/main, then the fast-forward.
        self.assertEqual("deny", self.bash("git worktree add -b origin/main .claude/worktrees/x feat/demo", self.hub))
        for ref in ("refs/heads/origin/main", "refs/tags/origin/main", "refs/origin/main"):
            with self.subTest(ref=ref):
                self.git(self.hub, "update-ref", ref, "feat/demo")
                for shell in (self.bash, self.powershell):
                    self.assertEqual("deny", shell("git merge --ff-only origin/main", self.hub))
                    self.assertIn(f"would follow {ref}, not refs/remotes/origin/main", self.reason)
                self.assertEqual("deny", self.bash(f'git -C "{self.hub}" merge --ff-only origin/main', self.tree))
                self.assertIsNone(self.bash("git merge --ff-only origin/main", self.tree))
                self.git(self.hub, "update-ref", "-d", ref)
        self.assertIsNone(self.bash("git merge --ff-only origin/main", self.hub))
        self.assertIsNone(self.bash("git pull --ff-only origin main", self.hub))

    def test_a_fast_forward_is_refused_when_the_guard_cannot_list_the_refs_it_may_follow(self) -> None:
        self.git(self.hub, "update-ref", "refs/remotes/origin/main", "main")
        real = hub_guard.git

        def failing(stderr: str) -> Any:
            def git(cwd: Path, *arguments: str) -> git_client.GitResult:
                if arguments[:1] == ("for-each-ref",):
                    return git_client.GitResult(128, "", stderr)
                return real(cwd, *arguments)

            return git

        for stderr, said in (
            ("fatal: cannot\n  read refs\n", "(fatal: cannot read refs)."),
            # A control character, as a terminal escape, is flattened too, never handed on to the reader's terminal.
            ("fatal: \x1b[31mred\x1b[0m\n", "(fatal: [31mred [0m)."),
            ("", "(git for-each-ref exited with 128)."),
        ):
            with self.subTest(stderr=stderr), mock.patch.object(hub_guard, "git", failing(stderr)):
                for shell in (self.bash, self.powershell):
                    self.assertEqual("deny", shell("git merge --ff-only origin/main", self.hub))
                    self.assertIn("the guard could not list the refs it may follow " + said, self.reason)
        self.assertIsNone(self.bash("git merge --ff-only origin/main", self.hub))

    def test_the_branch_row_deletes_any_local_branch_and_with_r_any_remote_tracking_ref_as_it_says(self) -> None:
        # The guard holds no delete to a task's <kind>/<name> branch, since retirement deletes others too, and the row's
        # reason says so.
        for command in ("git branch -D scratch", "git branch -d feat/demo", "git branch -r -d origin/feature"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))
        reason = hub_guard.READ_ONLY_FORMS["branch"].reason
        self.assertIn("deletes any local branch git will delete, not only a task's", reason)
        self.assertIn("with -r any remote-tracking ref", reason)

    def test_an_operand_a_row_takes_none_of_and_a_delete_naming_no_branch_are_refused(self) -> None:
        for command, reason in (
            ("git remote -v origin", "it takes no operand"),
            ("git worktree list extra", "it takes no operand"),
            ("git worktree prune extra", "it takes no operand"),
            ("git count-objects x", "it takes no operand"),
            ("git version x", "it takes no operand"),
            ("git branch -D", "it deletes no branch"),
            ("git branch --delete", "it deletes no branch"),
            ("git branch -d -q", "it deletes no branch"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn(reason, self.reason)
        for command in ("git remote", "git worktree list", "git count-objects -v", "git version"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_a_valued_option_given_a_word_the_text_does_not_settle_is_refused(self) -> None:
        for command in ('git log -n "$x"', 'git fetch -j "$x" origin', 'git branch --contains "$x"'):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.hub))
                self.assertIn("a word the text does not settle", self.reason)
        self.assertIsNone(self.bash("git fetch -j 2 origin", self.hub))

    def test_an_option_is_read_only_spelled_in_full_and_a_bundle_only_of_flags(self) -> None:
        self.refused_in_the_hub(
            (
                "git status --shor",
                "git log --onel",
                "git branch --show-cur",
                "git branch -dm feat/demo other",
                "git fetch --pru origin",
                "git worktree list --porc",
            )
        )
        for command in ("git branch -qD feat/demo", "git log -n 3 --format=%H", "git log -3", "git status -sb"):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.hub))

    def test_a_call_whose_place_the_text_does_not_settle_is_held_to_the_hub_s_forms(self) -> None:
        for command, subcommand in (
            ('cd "$x" && git push', "push"),
            ('cd "$x" && git tag v1', "tag"),
            ('git -C "$x" reflog delete "main@{0}"', "reflog"),
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(UNPLACED.format(subcommand), self.reason)
        self.assertIsNone(self.bash('cd "$x" && git status', self.tree))


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
    ("value", "expected"),
    [
        ("/srv/trace", True),
        # git reads a backslash as a separator and a drive as absolute, as git for Windows does.
        ("C:\\x\\trace", True),
        ("C:/x", True),
        ("\\\\host\\share\\t", True),
        ("relative.txt", False),
        ("af_unix:/tmp/s", False),
        ("1", False),
    ],
)
def test_a_trace_value_names_a_file_git_writes_when_git_reads_it_as_absolute(value: str, expected: bool) -> None:
    assert hub_guard.git_writes_trace_at(value) is expected


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


def test_git_builtins_reads_each_line_git_lists_as_one_name_and_nothing_from_a_failure() -> None:
    # git --list-cmds=builtins prints one name a line; a line is split on "\n" alone, never on other whitespace.
    listing = "add\nam\ncommit tree\nx\ty\n"  # a line holding a space or a tab stays one name
    answers = [git_client.GitResult(0, listing, ""), git_client.GitResult(129, "", "unknown option\n")]
    expected_sets: tuple[frozenset[str], ...] = (frozenset({"add", "am", "commit tree", "x\ty"}), frozenset())
    for answer, expected in zip(answers, expected_sets, strict=True):
        hub_guard.git_builtins.cache_clear()
        with mock.patch.object(hub_guard, "git", return_value=answer):
            assert hub_guard.git_builtins() == expected
    hub_guard.git_builtins.cache_clear()
    assert {"grep", "diff", "status"} <= hub_guard.git_builtins()


class GitDirectoryNameTests(unittest.TestCase):
    def test_a_git_directory_is_named_as_its_file_system_compares_names(self) -> None:
        # .GIT is a git directory where its folder's file system folds case, as NTFS and macOS's default APFS do,
        # and another folder where it keeps case: the file system decides, never the platform's name.
        with tempfile.TemporaryDirectory() as temporary:
            place = Path(temporary).resolve()
            (place / ".GIT").mkdir()
            for folds in (True, False):
                with (
                    self.subTest(folds=folds),
                    mock.patch.dict(path_keys._FOLDS_CASE, {str(place): folds}),
                    mock.patch.object(hub_guard, "guarded_hub_of_repository", return_value=place) as of_repository,
                    mock.patch.object(hub_guard, "guarded_hub_at", return_value=None),
                ):
                    self.assertEqual(folds, hub_guard.hub_protected(place / ".GIT" / "config"))
                    self.assertEqual([mock.call(place / ".GIT")] if folds else [], of_repository.call_args_list)

    def test_a_common_directory_spelled_in_another_case_is_the_hub_s_where_its_folder_folds_case(self) -> None:
        # git names the common directory as it was given, so on a folding file system .GIT is the hub's.
        with tempfile.TemporaryDirectory() as temporary:
            place = Path(temporary).resolve()
            common = place / ".GIT"
            common.mkdir()
            answer = f"{common}\n{common}\n"
            for folds in (True, False):
                hub_guard.guarded_hub_of_repository.cache_clear()
                with (
                    self.subTest(folds=folds),
                    mock.patch.dict(path_keys._FOLDS_CASE, {str(place): folds}),
                    mock.patch.object(worktrees.CLIENT, "output", return_value=answer),
                    mock.patch.object(hub_guard, "guarded_hub_at", return_value=place) as hub_at,
                ):
                    self.assertEqual(place if folds else None, hub_guard.guarded_hub_of_repository(common))
                    self.assertEqual([mock.call(place)] if folds else [], hub_at.call_args_list)
            hub_guard.guarded_hub_of_repository.cache_clear()


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [("", "."), ("a", "."), ("/", "/"), ("/srv/x", "/srv/"), ("a/b", "a/"), ("C:\\x\\y", "C:\\x\\"), ("~/", "~/")],
)
def test_a_pattern_s_matches_lie_in_the_folder_before_its_last_separator(prefix: str, expected: str) -> None:
    assert expected == hub_guard.pattern_folder(prefix)
