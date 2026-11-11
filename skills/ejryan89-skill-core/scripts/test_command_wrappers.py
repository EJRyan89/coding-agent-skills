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
        "sudo",
        "doas",
        "setsid",
        "ionice",
        "chrt",
        "taskset",
        "chroot",
        "unbuffer",
        "chronic",
        "strace",
        "ltrace",
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
        # Its value attached, --unset places the program alike on macOS's env, where it is -u with nset=B attached.
        (["env", "--unset=B", "--null", "git"], ["git"]),
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
        (["command", "-pv", "git"], []),
        (["command", "-pV", "git"], []),
        (["command", "--", "git", "-v"], ["git", "-v"]),
        (["command", "git", "push", "-v"], ["git", "push", "-v"]),
        (["command", "-p", "git", "push", "-V"], ["git", "push", "-V"]),
        (["env"], []),
        (["git", "push"], []),
        (["sudo", "git", "push"], ["git", "push"]),
        (["sudo", "-u", "me", "-E", "A=1", "git"], ["git"]),
        (["sudo", "--user=me", "-h", "git"], ["git"]),
        (["sudo", "-hhost", "--preserve-env", "git"], ["git"]),
        (["sudo", "-a", "type", "--auth-type", "type", "git"], ["git"]),
        (["doas", "-u", "me", "-n", "git"], ["git"]),
        (["setsid", "-fw", "git"], ["git"]),
        (["ionice", "-c", "2", "-n7", "--ignore", "git"], ["git"]),
        (["chrt", "-f", "--sched-runtime", "5", "10", "git"], ["git"]),
        (["chrt", "-T", "5", "-o", "0", "git"], ["git"]),
        (["taskset", "-c", "0-3", "git"], ["git"]),
        (["taskset", "0x1", "git"], ["git"]),
        (["chroot", "--userspec", "me", "/root", "git"], ["git"]),
        (["unbuffer", "-p", "git"], ["git"]),
        (["chronic", "-ev", "git"], ["git"]),
        (["strace", "-f", "-o", "log", "-e", "trace=all", "-s", "80", "git"], ["git"]),
        (["strace", "--output=log", "--quiet", "git"], ["git"]),
        (["ltrace", "-f", "-o", "log", "-l", "lib", "git"], ["git"]),
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
        # macOS's env reads --unset as `-` and -u with nset attached, and git as its program.
        (["env", "--unset", "git", "push"], "env --unset"),
        (["env", "-i", "--unset", "x", "git"], "env --unset"),
        (["sudo", "--us", "me", "git"], "sudo --us"),
        (["doas", "--user", "me", "git"], "doas --user"),
        (["chronic", "-x", "git"], "chronic -x"),
    ],
)
def test_an_option_the_table_does_not_settle_leaves_the_program_unread(words: list[str], unread: str) -> None:
    assert unwrapped(words) == [f"{UNREAD_PROGRAM}{unread}"]


def test_a_caller_s_own_catalogue_is_read_the_same_way() -> None:
    assert unwrapped(["run", "-u", "me", "git"], {"run": option_table("u:")}) == ["git"]
    assert unwrapped(["run", "git"]) == []


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
