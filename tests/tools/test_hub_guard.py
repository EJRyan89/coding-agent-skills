"""Regression suite for tools/hub_guard.py: the hub guard hook, its understood set, and its contract."""

from __future__ import annotations

import contextlib
import io
import itertools
import json
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
from bounded_process import Finished, run_bounded
from guard_limits import PROCESS_SECONDS, process_limits_held
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
        # The reading clock stands still unless a test moves it: a reading in this process ends with the reading time
        # only where a test says when, never because a loaded runner was slow to start git or pwsh.
        clock = mock.patch.object(hub_guard, "reading_clock", return_value=0.0)
        clock.start()
        self.addCleanup(clock.stop)
        # Each process a reading starts, git or pwsh, gets a limit no loaded runner reaches rather than what is left of
        # the reading time, so a slow start never ends a reading either; self.asked holds the limits the guard asked.
        self.asked = self.enterContext(process_limits_held())
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

    def test_pwsh_that_does_not_read_in_the_time_left_leaves_a_command_the_guard_cannot_read(self) -> None:
        reading = hub_guard.Reading(isolated_environment(), text="git commit -m x")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", side_effect=subprocess.TimeoutExpired("pwsh", 15)),
        ):
            self.assertIsNone(hub_guard.powershell_events("git commit -m x", reading))
        self.assertEqual(["pwsh did not read the command in what was left of the guard's 15 seconds"], reading.unread)

    def test_a_command_with_no_json_form_is_one_the_guard_cannot_read_and_pwsh_never_starts(self) -> None:
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
            reading.unread,
        )

    def test_output_that_is_not_utf8_is_read_with_replacement_characters_and_a_repeated_key_is_not(self) -> None:
        # A console code page can reach the reader's output; a byte it cannot decode costs only that character.
        cases: tuple[tuple[bytes, dict[str, Any] | None, list[str]], ...] = (
            (b'{"nodes": [], "detail": "caf\xe9"}', {"nodes": [], "detail": "caf\ufffd"}, []),
            (b'{"nodes": [], "nodes": []}', None, []),
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
                if events is None:
                    self.assertEqual(["pwsh could not read the command ()"], reading.unread)


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

    def test_an_edit_in_a_hub_git_directory_that_cannot_be_looked_at_is_refused(self) -> None:
        with unstatable(self.hub / ".git", self.hub):
            self.assertEqual("deny", self.edit(self.hub / ".git" / "config"))
        self.assertEqual(hub_guard.DENY_REASON, self.reason)


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
            ({"tool_name": "Bash", "tool_input": {"command": "eval x"}, "cwd": str(self.tree)}, None, "deny"),
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

    def test_a_hub_a_named_place_reaches_is_in_view_for_a_place_the_text_does_not_settle(self) -> None:
        # The session starts outside every repository, so only the named place brings the hub into view, and the
        # unsettled place is refused only while one is.
        unsettled = 'cd "$NOT_SET_HERE" && git commit -m x'
        self.assertIsNone(self.bash(unsettled, self.outside, self.unshared))
        command = f'git -C "{self.hub.as_posix()}" status; {unsettled}'
        self.assertEqual("deny", self.bash(command, self.outside, self.unshared))
        self.assertIn("The guard could not tell where `git commit` runs", self.reason)


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

    A `python` shim first on PATH records its arguments, so a test sees whether the command would start Python at
    all, and with what, without depending on which Python the machine has.
    """

    def run_hook(
        self, variables: Mapping[str, str] | None = None, note: str = "", status: int = 0
    ) -> tuple[int, list[str] | None]:
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
        shim = f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "$SHIM_RECORD"\nexit {status}\n'
        (shims / "python").write_text(shim, encoding="utf-8")
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

    def test_the_update_skills_command_is_refused_at_its_deployed_path_wherever_a_guarded_hub_is_in_view(self) -> None:
        # update-coding-agent-skills runs its script with bash on the clone it deploys from, the hub. The guard trusts
        # no script by its path, the deployed update script's exact path under the skills directory included, since a
        # session's file tools may write there; so the skill runs outside a session whose project is the clone, where
        # Claude Code loads no hook of the clone's, and its text says so.
        skill = REPOSITORY_ROOT / "skills" / "update-coding-agent-skills"
        script = self.root / "home dir" / ".claude" / "skills" / skill.name / "scripts" / "update.sh"
        script.parent.mkdir(parents=True)
        script.write_bytes((skill / "scripts" / "update.sh").read_bytes())
        command = f"bash '{script.as_posix()}' '{self.hub.as_posix()}' 'release'"
        outside = self.root / "outside"
        outside.mkdir()
        for cwd in (self.hub, self.tree, outside):
            with self.subTest(cwd=cwd.name):
                self.assertEqual("deny", self.bash(command, cwd))
                self.assertIn("runs bash", self.reason)
        self.assertEqual("deny", self.powershell(command, self.hub))
        text = (skill / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("`git config coding-agent-skills.hubGuard true`", text)
        self.assertIn("outside the clone", text)

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
            ("git unread", "is an alias for `!eval x`: The guard does not read part"),
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
            # git uses the other repository's HEAD and index and writes the directory it starts in.
            # The command itself cannot set one: an assignment before git, export, and env are parts the guard does
            # not read, refused while a hub is in view.
            (f'GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'export GIT_DIR="{other}"; git reset --hard', self.hub, "deny"),
            (f'env GIT_DIR="{other}" git reset --hard', self.hub, "deny"),
            (f'GIT_DIR="{other}" git reset --hard', self.tree, "deny"),
        ):
            with self.subTest(command=command, cwd=cwd):
                self.assertEqual(expected, self.bash(command, cwd))
        inherited = {**self.environment, "GIT_DIR": str(other)}
        self.assertEqual("deny", self.bash("git reset --hard", self.hub, inherited))
        self.assertEqual("deny", self.powershell("git reset --hard", self.hub, inherited))
        self.assertIsNone(self.bash("git reset --hard", self.tree, inherited))
        # GIT_COMMON_DIR moves only the refs and objects every worktree shares.
        common = {**self.environment, "GIT_COMMON_DIR": str(other)}
        self.assertEqual("deny", self.bash("git reset --hard", self.hub, common))
        self.assertIsNone(self.bash("git reset --hard", self.tree, common))
        self.assertEqual("main\n", self.git(self.hub, "branch", "--show-current"))
        self.assertEqual("", self.git(self.hub, "status", "--porcelain"))

    def test_an_inherited_location_variable_cannot_hide_the_hub_from_the_file_guard(self) -> None:
        other = self.other_repository() / ".git"
        for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            with self.subTest(name=name):
                event = {"tool_name": "Edit", "tool_input": {"file_path": str(self.hub / "README.md")}}
                self.assertEqual("deny", self.decision(event, {**self.environment, name: str(other)}))


class PowerShellGuardTests(GuardTestCase):
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
        self.assertEqual("deny", self.bash(f"pwsh -c 'git -C \"{self.hub}\" switch feat/demo'", self.tree))

    def test_a_location_variable_the_command_sets_is_a_part_the_guard_does_not_read(self) -> None:
        for command in (
            "Rename-Item env:FOO GIT_WORK_TREE; git commit -m x",
            f"Set-Item Environment::GIT_WORK_TREE '{self.hub}'; git reset --hard",
            f"($env:GIT_WORK_TREE) = '{self.hub}'; $other, $env:GIT_DIR = 2, 'x'; git reset --hard",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
                self.assertIn(UNREAD, self.reason)
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
                self.assertEqual("deny", self.powershell("git status", cwd, environment))
                self.assertIn("could not find pwsh", self.reason)


UNREAD = "The guard does not read part of this command ("


class UnderstoodSetTests(GuardTestCase):
    """The understood set of the guard's contract, one row for each thing it reads and each class it refuses: a command
    is read only as far as the guard understands it, and a part it does not understand is refused while a guarded hub
    is in view, from the hub and from a worktree alike."""

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
            (f'timeout 60 git -C "{self.hub}" commit -m x', self.tree, "deny"),
            (f'timeout -k 5 60 git -C "{self.tree}" commit -m x', self.hub, "deny"),
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
            ('python -B tools/worktrees.py list > "$f"', self.tree, "deny"),
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
            ('echo x > "$out"', self.tree, "deny"),
            ('cd "$d" && echo x > notes.txt', self.tree, "deny"),
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
            ('gh pr "$verb" 12', self.hub, "deny"),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.bash(command, cwd))

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
            ("PATH=/elsewhere python -B x.py", "deny"),
            ("x=1; echo done", None),
            ("PATH=/elsewhere; echo done", "deny"),
        ):
            with self.subTest(command=command):
                self.assertEqual(expected, self.bash(command, self.tree))

    def test_each_class_of_construct_the_guard_does_not_read_is_refused_while_a_hub_is_in_view(self) -> None:
        # One row for each class the contract refuses, each in a command that runs no git of its own text.
        for command in (
            "bash script.sh",
            "bash -lc 'echo hi'",
            "bash -c 'git status'",
            "sh script.sh",
            "pwsh -File x.ps1",
            "zsh -c 'echo hi'",
            "cmd /c dir",
            "eval 'echo hi'",
            "source .venv/bin/activate",
            ". ./env.sh",
            "exec ls",
            "alias g=git",
            "f() { echo hi; }",
            "function f { echo hi; }",
            "export PYTHONUTF8=1",
            "declare -x X=1",
            "set -a",
            "set -o keyword",
            "shopt -s lastpipe",
            "trap 'echo hi' EXIT",
            "hash -p /bin/ls git",
            "enable -n echo",
            "let i=1",
            "(( i = 1 ))",
            "for (( i = 0; i < 2; i++ )); do echo $i; done",
            "echo $((1 + 2))",
            "echo $[1 + 2]",
            "echo ${a[0]}",
            "echo ${x:1:2}",
            "echo ${!x}",
            "echo ${x@P}",
            "echo ${ echo hi; }",
            "a[0]=x",
            "a=(x y)",
            "[[ -n x ]]",
            "test -v 'a[0]'",
            "$cmd status",
            "xargs -I{} echo {}",
            "env -S 'ls'",
            "env PATH=/elsewhere ls",
            "sudo git status",
            "watch ls",
            "find . -exec sudo ls {} +",
            "find . -exec ls {} +",
            "echo x > $out",
            "printf -v x %s y",
            "read PATH",
            "unset x",
            "echo 'unterminated",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(UNREAD, self.reason)
                self.assertIsNone(self.bash(command, self.outside()))

    def test_the_commands_sessions_run_most_are_read(self) -> None:
        for command in (
            'cd "$(git rev-parse --show-toplevel)" && python -B -m pytest tests/tools/test_x.py -q 2>&1 | tail -20',
            "grep -n 'def ' tools/hub_guard.py | head -40",
            "sed -n '1,40p' docs/design.md",
            "git diff --stat && git status --short",
            'for f in a.py b.py; do python -m ruff check "$f"; done',
            "timeout 600 python -B -m pytest tests/tools -q",
            'gh pr view 12 --json title,body --jq ".title"',
            "ls -la; wc -l README.md; cat README.md | tr a-z A-Z",
            'while read -r line; do echo "$line"; done < notes.txt',
            "set -euo pipefail; echo ok",
            "[ -f README.md ] && echo yes",
            "command -v pwsh",
            'x=$(mktemp -d) && cp README.md "$x/"',
            "./tools/some-script.sh --flag && node x.js && npm test",
            "xargs -0 grep -n x < list.txt",
            "timeout 60 python -B x.py",
            "env PYTHONUTF8=1 python -B x.py",
            "sudo ls",
            "bash -c 'ls && echo hi'",
        ):
            with self.subTest(command=command):
                self.assertIsNone(self.bash(command, self.tree))

    def test_with_no_hub_in_view_a_part_the_guard_does_not_read_is_allowed_unless_a_named_path_reaches_one(
        self,
    ) -> None:
        outside = self.outside()
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        self.assertIsNone(self.bash("export X=1; git commit -m x", outside, environment))
        self.assertIn("could not judge part of the command", self.stderr)
        for command in (
            f'export GIT_DIR="{self.hub / ".git"}"; git commit -m x',
            f'export GIT_WORK_TREE="{self.hub}"; git reset --hard',
            f"eval 'cd \"{self.hub}\"'; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, outside, environment))
                self.assertIn(UNREAD, self.reason)

    def test_a_cdpath_search_and_cd_dash_leave_the_directory_unknown(self) -> None:
        environment = {**self.environment, "CDPATH": str(self.root)}
        self.assertEqual("deny", self.bash("cd sub && git commit -m x", self.hub, environment))
        self.assertIn(UNPLACED.format("commit"), self.reason)
        # A target CDPATH does not search, one starting ./ or ../, is placed as written.
        self.assertIsNone(self.bash("cd ./.claude/worktrees/feat-demo && git commit -m x", self.hub, environment))
        self.assertEqual("deny", self.bash("cd - && git commit -m x", self.tree))
        self.assertIn(UNPLACED.format("commit"), self.reason)

    def test_a_runner_around_git_is_refused_and_around_another_command_understood(self) -> None:
        for command, cwd, expected in (
            (f'nohup git -C "{self.tree}" status', self.tree, "deny"),
            (f'nice -n 5 git -C "{self.tree}" log', self.tree, "deny"),
            ("echo x | xargs git log", self.tree, "deny"),
            (f"bash -c 'git -C \"{self.tree}\" status'", self.tree, "deny"),
            (f"pwsh -NoProfile -Command 'git -C \"{self.tree}\" status'", self.tree, "deny"),
            ("find . -execdir git status {} \\;", self.hub, "deny"),
            ("timeout 60 python -B tools/worktrees.py list", self.hub, None),
            ("echo x | xargs grep -n x", self.tree, None),
            (f"bash -c 'cd \"{self.hub}\"'; git commit -m x", self.tree, None),
            ("timeout 40 gh pr checkout 3", self.hub, "deny"),
            ("timeout 40 gh pr view 3", self.hub, None),
        ):
            with self.subTest(command=command, cwd=cwd.name):
                self.assertEqual(expected, self.bash(command, cwd))

    def test_a_variable_a_program_reads_options_from_is_refused_before_a_shell_runner_at_any_depth(self) -> None:
        action = "TAR_OPTIONS='--checkpoint=1 --checkpoint-action=exec=\"git reset --hard HEAD~1\"'"
        for command in (
            f"{action} tar cf /tmp/review.tar README.md",
            f"{action} bash -c 'tar cf /tmp/review.tar README.md'",
            "TAPE=host:/x bash -c 'tar cf - README.md'",
            "RIPGREP_CONFIG_PATH=rg.conf bash -c 'rg pattern .'",
            "bash -c \"RIPGREP_CONFIG_PATH=rg.conf bash -c 'rg pattern .'\"",
            "TAPE=host:/x pwsh -NoProfile -Command 'tar cf - README.md'",
            "TAR_OPTIONS=--to-command=x pwsh -Command 'tar xf a.tar'",
        ):
            for cwd in (self.hub, self.tree):
                with self.subTest(command=command, cwd=cwd.name):
                    self.assertEqual("deny", self.bash(command, cwd))
        self.assertIsNone(self.bash("OTHER=x bash -c 'tar cf /tmp/review.tar README.md'", self.tree))

    def test_the_leftovers_recorded_on_the_fail_closed_issue_are_refused(self) -> None:
        # The binders found against the modelling reader, and the forms its last review left open.
        for command in (
            "i='POSIXLY_CORRECT=1' export i; echo $((i)); git status",
            "i='POSIXLY_CORRECT=1' readonly i; echo $((i)); git status",
            "i=1; trap 'i=POSIXLY_CORRECT=1' DEBUG; echo $((i)); git status",
            "read -r v; i=1; i=$v eval 'echo $((i))'; git status",
            "read -r v; f(){ echo $((i)); }; i=$v f; git status",
            "x=1; declare i=$x; git status",
            "trap 'git commit -m x' RETURN; git status",
            "trap 'git commit -m x' ERR; git status",
            "[[ -v 'a[EXPR]' ]]; git status",
            "test -v 'a[EXPR]'; git status",
            "[ -v 'a[EXPR]' ]; git status",
            '[ $op "a[$k]" ]; git status',
            "for i in 1 2; do if true; then :; fi; echo $((i)); done; git status",
            'command declare "$x=1"; git status',
            'builtin declare "$x=1"; git status',
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.bash(command, self.tree))
                self.assertIn(UNREAD, self.reason)

    def test_a_shell_option_the_environment_turns_on_is_judged_by_its_table(self) -> None:
        for variable, value, expected in (
            ("SHELLOPTS", "braceexpand:hashall:interactive-comments", None),
            ("SHELLOPTS", "braceexpand:keyword", "deny"),
            ("SHELLOPTS", "braceexpand:frobnicate", "deny"),
            ("BASHOPTS", "extglob:nullglob", None),
            ("BASHOPTS", "cdable_vars", "deny"),
            ("POSIXLY_CORRECT", "1", "deny"),
            ("BASH_COMPAT", "4.4", "deny"),
        ):
            with self.subTest(variable=variable, value=value):
                environment = {**self.environment, variable: value}
                self.assertEqual(expected, self.bash("git status", self.tree, environment))
        environment = {**self.environment, "PS4": "+ $(git commit -m x) "}
        self.assertEqual("deny", self.bash("set -x; git status", self.tree, environment))
        for source in ("set -o xtrace;", "shopt -so xtrace;", "set -ex;"):
            with self.subTest(source=source):
                self.assertEqual("deny", self.bash(f"{source} true", self.tree, environment))
        # Whatever turns it on: the environment's SHELLOPTS too, before any command is read.
        for value in ("xtrace", "braceexpand:xtrace"):
            with self.subTest(shellopts=value):
                self.assertEqual("deny", self.bash("true", self.tree, {**environment, "SHELLOPTS": value}))
        self.assertIsNone(self.bash("true", self.tree, {**self.environment, "SHELLOPTS": "xtrace"}))
        self.assertIsNone(self.bash("set -x; git status", self.tree))
        environment = {**self.environment, "BASH_FUNC_ls%%": "() { git commit -m x; }"}
        self.assertEqual("deny", self.bash("ls", self.tree, environment))
        self.assertIsNone(self.bash("cat README.md", self.hub, environment))


class PowerShellUnderstoodSetTests(GuardTestCase):
    """The understood set in PowerShell, read by its own parser: pipelines of commands whose elements are constants and
    parameters, joined by && and ||, with Set-Location, Push-Location, and Pop-Location followed. Each case starts a
    real pwsh, as the hook does."""

    def test_a_literal_chain_places_each_git_call_and_every_other_node_is_refused(self) -> None:
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
            ("[IO.File]::Exists('x')", self.tree, "deny"),
            ("[System.Diagnostics.Process] 'x'", self.tree, "deny"),
            ("$x.ToString()", self.tree, "deny"),
            ("$x.$name", self.tree, "deny"),
            ("Get-ChildItem | ForEach-Object Name", self.tree, "deny"),
            ("Get-ChildItem | ForEach-Object -MemberName Delete", self.tree, "deny"),
            ("Get-ChildItem | ForEach-Object -MemberName:Delete -ArgumentList:$true", self.tree, "deny"),
            ("Get-ChildItem | ForEach-Object -Process:{ $_.Name }", self.tree, None),
            ("Get-ChildItem | Where-Object { $_.Length -gt 1 } | Sort-Object { $_.Name }", self.tree, None),
            ("Invoke-Command { git status }", self.tree, "deny"),
            ("iex 'git status'", self.tree, "deny"),
            ("dotnet-format --verify-no-changes", self.tree, None),
            ("timeout 5 git status", self.tree, "deny"),
            ("$env:X = 1; git status", self.tree, "deny"),
            ("$PSDefaultParameterValues = @{}", self.tree, "deny"),
            ("git -C $env:SOMEWHERE status", self.tree, None),
            ("git -C $env:SOMEWHERE commit -m x", self.tree, "deny"),
            (f"Write-Output (git -C '{self.hub}' commit -m x)", self.tree, "deny"),
            ("Write-Output $x.Name", self.tree, None),
            ("git status > $env:TEMP\\out.txt", self.tree, "deny"),
            ("Invoke-Expression 'git status'", self.tree, "deny"),
            ("& $exe status", self.tree, "deny"),
            ("[scriptblock]::Create('git status')", self.tree, "deny"),
            ("if ($true) { git status }", self.tree, None),
            ("Get-ChildItem | ForEach-Object { $_.Name }", self.tree, None),
            ("Set-Location env:", self.tree, "deny"),
            ("Get-Item alias:git", self.tree, "deny"),
            ("Set-Content -Path:env:GIT_DIR -Value x; git commit -m x", self.tree, "deny"),
            ("Set-Content -LiteralPath:env:GIT_DIR -Value x", self.tree, "deny"),
            ("Copy-Item a.txt -Destination:env:GIT_DIR", self.tree, "deny"),
            ("Set-Content $p x", self.tree, "deny"),
            ("Get-Content -LiteralPath:README.md", self.tree, None),
            ("Start-Process git", self.tree, "deny"),
            (". ./profile.ps1", self.tree, "deny"),
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
            "'x' > $f",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.tree))
        self.assertIsNone(self.powershell("'x' > notes.txt", self.tree))
        # > $null discards the output; a file named $null is a file, written in the hub here.
        self.assertIsNone(self.powershell("git status > $null", self.hub))
        self.assertEqual("deny", self.powershell("'x' > '$null'", self.hub))
        self.assertEqual("hub\n", readme.read_text(encoding="utf-8"))

    def test_a_location_powershell_may_move_is_unknown_after_it(self) -> None:
        outside = self.root / "not-a-repository"
        outside.mkdir()
        environment = {name: value for name, value in self.environment.items() if name != "CLAUDE_PROJECT_DIR"}
        # With a part it does not read, a git call is read from no place where any command may move the location.
        self.assertEqual(
            "deny", self.powershell(f"Set-Location '{self.hub}'; $x.ToString(); git commit -m x", outside, environment)
        )
        for command in (
            # A location a nested command moves, and one a pipeline of several commands moves, is unknown after it.
            f"Write-Output (Set-Location '{self.tree}'); git commit -m x",
            f"Set-Location '{self.tree}' | Out-Null; git commit -m x",
        ):
            with self.subTest(command=command):
                self.assertEqual("deny", self.powershell(command, self.hub))
                self.assertIn(UNPLACED.format("commit"), self.reason)
        self.assertEqual("deny", self.powershell("Set-Location -StackName x; git status", self.tree))
        self.assertIn("Set-Location's parameter -StackName", self.reason)
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
            ("git worktree add -b topic .claude/worktrees/fix-topic", self.hub),
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

    def test_the_fast_forward_options_are_the_guard_s(self) -> None:
        named = set(contract_names("`merge --ff-only origin/main`, each also with", "; the target is named in full"))
        pull, merge = hub_guard.READ_ONLY_FORMS["pull"], hub_guard.READ_ONLY_FORMS["merge"]
        self.assertEqual(sorted(named | {"--ff-only"}), sorted(pull.flags))
        self.assertEqual(sorted(named - {"--no-rebase"} | {"--ff-only"}), sorted(merge.flags))
        self.assertEqual((frozenset(), frozenset()), (pull.valued | pull.attached, merge.valued | merge.attached))

    def test_git_s_options_with_a_value_are_the_ones_the_guard_reads(self) -> None:
        # The guard reads git_client's list, the one home of the options git reads a value after.
        self.assertEqual(sorted(git_client.GLOBAL_VALUE_OPTIONS), sorted(contract_names("or as the next word (", ")")))

    def test_the_runners_the_guard_refuses_are_the_ones_its_contract_names(self) -> None:
        named = contract_names("`RUNNERS`:", "While a hub is guarded")
        self.assertEqual(sorted(hub_guard.RUNNERS), sorted(named))
        always = contract_names("`RUNNERS_ALWAYS_REFUSED`:", "Of these")
        self.assertEqual(sorted(hub_guard.RUNNERS_ALWAYS_REFUSED), sorted(always))
        reading_options = contract_names("`RUNNER_OPTIONS`, which names for each of", "the options known to be safe")
        self.assertEqual(sorted(hub_guard.RUNNER_OPTIONS), sorted(reading_options))
        programs = contract_names("`PROGRAM_OPTIONS`:", "are read by an allow-list")
        self.assertEqual(sorted(hub_guard.PROGRAM_OPTIONS), sorted(programs))
        variables = contract_names("`PROGRAM_VARIABLES`:", ", is refused where")
        self.assertEqual(
            sorted(name for names in hub_guard.PROGRAM_VARIABLES.values() for name in names), sorted(variables)
        )

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

    def test_every_not_read_entry_names_a_reason(self) -> None:
        self.assertTrue(hub_guard.NOT_READ)
        for entry, reason in hub_guard.NOT_READ.items():
            with self.subTest(entry=entry):
                self.assertRegex(entry, r"^(builtin|keyword|construct|set -o|shopt|expansion|PowerShell) \S")
                self.assertIsInstance(reason, str)
                self.assertGreater(len(reason.split()), 2, "a reason says why, in more than a word or two")

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
        # run git from Python, which the hook never sees, so the commands a session types are these.
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
