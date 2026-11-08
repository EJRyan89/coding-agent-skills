"""Regression tests for the one reading of the commands that run a command a later word names."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from command_wrappers import (
    COMMAND_WRAPPERS,
    FIND_COMMANDS,
    UNREAD_PROGRAM,
    find_actions,
    shell_script_index,
    split_line,
    unwrapped,
)
from option_reader import option_table


def test_the_catalogue_names_each_wrapper_and_find_s_actions() -> None:
    assert set(COMMAND_WRAPPERS) == {
        "command",
        "builtin",
        "exec",
        "nohup",
        "time",
        "nice",
        "env",
        "timeout",
        "stdbuf",
        "xargs",
    }
    assert {"-exec", "-execdir", "-ok", "-okdir"} == FIND_COMMANDS


@pytest.mark.parametrize(
    ("words", "program"),
    [
        (["xargs", "git", "push"], ["git", "push"]),
        (["xargs", "-0n", "1", "git", "push"], ["git", "push"]),
        (["env", "-i", "A=1", "-u", "B", "git"], ["git"]),
        (["env", "-S", "git push", "x"], ["git", "push", "x"]),
        (["env", "--split-string=-u B git push"], ["git", "push"]),
        (["timeout", "--signal", "TERM", "5", "git"], ["git"]),
        (["nice", "-5", "git"], ["git"]),
        (["nice", "-n", "5", "git"], ["git"]),
        (["stdbuf", "-o", "L", "git"], ["git"]),
        (["exec", "-a", "name", "git"], ["git"]),
        (["command", "-p", "git"], ["git"]),
        (["builtin", "git"], ["git"]),
        (["nohup", "git"], ["git"]),
        (["time", "-p", "git"], ["git"]),
        (["command", "-v", "git"], []),
        (["env"], []),
        (["git", "push"], []),
    ],
)
def test_unwrapped_reads_past_a_wrapper_to_the_command_it_runs(words: list[str], program: list[str]) -> None:
    assert unwrapped(words) == program


@pytest.mark.parametrize(
    ("words", "unread"),
    [
        (["timeout", "--sig", "TERM", "5", "git"], "timeout --sig"),
        (["xargs", "--max-a", "1", "git"], "xargs --max-a"),
        (["env", "-W", "git"], "env -W"),
    ],
)
def test_an_option_the_table_does_not_settle_leaves_the_program_unread(words: list[str], unread: str) -> None:
    assert unwrapped(words) == [f"{UNREAD_PROGRAM}{unread}"]


def test_a_caller_s_own_catalogue_is_read_the_same_way() -> None:
    assert unwrapped(["sudo", "-u", "me", "git"], {"sudo": option_table("u:")}) == ["git"]
    assert unwrapped(["sudo", "git"]) == []


def test_a_split_line_shlex_cannot_split_stays_one_word() -> None:
    assert split_line("git 'push") == ["git 'push"]
    assert split_line("git 'a b'") == ["git", "a b"]


@pytest.mark.parametrize(
    ("words", "actions"),
    [
        ([".", "-exec", "git", "push", "{}", "+"], [["git", "push", "{}"]]),
        ([".", "-exec", "git", "push", ";"], [["git", "push"]]),
        ([".", "-execdir", "git", "push"], [["git", "push"]]),
        ([".", "-exec", "a", ";", "-ok", "b", "{}", "+"], [["a"], ["b", "{}"]]),
        ([".", "-exec", "+", "{}", "+"], [["+", "{}"]]),
        ([".", "-name", "x", "-exec"], []),
    ],
)
def test_find_s_actions_each_run_to_the_word_that_ends_it(words: list[str], actions: list[list[str]]) -> None:
    assert find_actions(words) == actions


@pytest.mark.parametrize(
    ("arguments", "index"),
    [
        (["-c", "git push"], 1),
        (["-lc", "git push"], 1),
        (["-e", "-c", "git push"], 2),
        (["-o", "pipefail", "-c", "git push"], 3),
        (["--rcfile", "rc", "-c", "git push"], 3),
        (["-c", "--", "git push"], 2),
        (["script.sh"], None),
        (["-e"], None),
        (["-", "-c"], None),
    ],
)
def test_a_shell_s_c_script_is_found_as_the_shell_reads_its_options(arguments: list[str], index: int | None) -> None:
    assert shell_script_index(arguments) == index
