"""The one reader of the git subcommands a skill's Bash and PowerShell scripts run (git_subcommands.py), and the
reading of a PowerShell source's commands it runs on (powershell_commands.py)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import git_subcommands
import powershell_commands
from powershell_commands import UNREAD_WORD


def no_powershell(script: str) -> Sequence[Sequence[str]]:
    raise AssertionError(f"read PowerShell it should not start: {script!r}")


def listed(scripts: dict[str, list[list[str]]], read: list[str]) -> git_subcommands.PowerShellReader:
    """A PowerShellReader that lists each script's commands from `scripts` and notes each script it is asked for."""

    def reader(script: str) -> Sequence[Sequence[str]]:
        read.append(script)
        return scripts.get(script, [])

    return reader


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ('git -C "$CLONE" fetch --tags origin', {"fetch"}),
        ("OUT=$(git ls-remote --tags origin) && git push", {"ls-remote", "push"}),
        ("if git rev-parse --git-dir; then", {"rev-parse"}),
        ('echo "run git fetch first"', set()),
        ("  # git pull later", set()),
        ("legit fetch", set()),
        # Bash finds a program by its name as written, so Git is no git.
        ("Git push", set()),
        # A wrapper's options are read by its getopt table to the command it runs, and a lookup runs nothing.
        ("nice -n 5 git push", {"push"}),
        ("env -i A=1 timeout --signal TERM 5 git fetch", {"fetch"}),
        ("xargs -n 1 git push", {"push"}),
        ("env -S 'git push'", {"push"}),
        ("command -v git", set()),
        ("xargs echo git push", set()),
        ("timeout --sig TERM 5 git push", set()),
        # find's actions, eval, and a shell's -c script are read as commands of their own.
        ("find . -exec git status {} \\; -exec echo push \\;", {"status"}),
        ("eval git fetch", {"fetch"}),
        ("bash -lc 'cd x && git pull'", {"pull"}),
        # A quoted operator is a word, a substitution in a string is a command, and so is a group's or a continuation's.
        ("echo 'a; git push'", set()),
        ('echo "now $(git rev-parse HEAD)"', {"rev-parse"}),
        ("{ git push; }", {"push"}),
        ("git \\\n  fetch", {"fetch"}),
        ("x=1 git push 'unclosed", {"push"}),
        # A subcommand no literal names is read as its word.
        ('git "$sub"', {"$sub"}),
        # Bash's time keyword takes -p and then --, each optional, before the pipeline it times; a second -p, or one
        # after --, is the timed command's own word. After a pipe, the newline right after one, an assignment, or in
        # find's action, `time` is the time program, read by its options.
        ("time -p git push", {"push"}),
        ("time -p -- git push", {"push"}),
        ("! time -p git push", {"push"}),
        ("time -p time -p git push", {"push"}),
        ("x=$(time -p git fetch)", {"fetch"}),
        ("time -p -p git push", set()),
        ("time -- -p git push", set()),
        ("echo a | time -p -p git push", {"push"}),
        ("echo a |\n time -p -p git push", {"push"}),
        ("A=1 time -p -p git push", {"push"}),
        ("find . -exec time -p -p git push \\;", {"push"}),
    ],
)
def test_a_bash_script_runs_the_git_subcommand_after_gits_own_options(line: str, expected: set[str]) -> None:
    assert git_subcommands.bash_git_subcommands(line, no_powershell) == expected


# The script a pwsh or Windows PowerShell start-up runs is PowerShell, whose commands the caller's reader lists: by
# -Command, -CommandWithArgs, or Windows PowerShell's first positional word, named by a path or with .exe too, and
# behind a wrapper or in find's action.
@pytest.mark.parametrize(
    ("line", "script"),
    [
        ("pwsh -c 'git push'", "git push"),
        ("pwsh -NoProfile -Command git push", "git push"),
        ("pwsh -CommandWithArgs 'git push' extra", "git push"),
        ("powershell 'git push'", "git push"),
        ("pwsh.exe -c 'git push'", "git push"),
        ("/usr/bin/pwsh -c 'git push'", "git push"),
        ("env pwsh -c 'git push'", "git push"),
        ("find . -exec pwsh -c 'git push' \\;", "git push"),
        ("x=$(pwsh -c 'git push')", "git push"),
    ],
)
def test_the_script_a_powershell_start_up_runs_is_read_through_the_powershell_reader(line: str, script: str) -> None:
    read: list[str] = []
    reader = listed({script: [["git", "push"]]}, read)
    assert git_subcommands.bash_git_subcommands(line, reader) == {"push"}
    assert read == [script]


# A start-up that runs no script, a file, an encoded script, one read from standard input, or one a word that is no
# literal holds, gives the reader nothing to read.
@pytest.mark.parametrize(
    "words",
    [
        ["pwsh"],
        ["pwsh", "-File", "push.ps1"],
        ["pwsh", "push.ps1", "git", "push"],
        ["pwsh", "-EncodedCommand", "ZwBpAHQAIABwAHUAcwBoAA=="],
        ["pwsh", "-Command", "-"],
        ["pwsh", "-Command", UNREAD_WORD],
        ["pwsh", "-Command", "git", UNREAD_WORD],
        ["pwsh", UNREAD_WORD, "-Command", "git push"],
    ],
)
def test_a_start_up_whose_script_no_reader_sees_gives_no_subcommand(words: list[str]) -> None:
    assert git_subcommands.powershell_git_subcommands([words], no_powershell) == set()


# A PowerShell script's commands, as powershell_commands lists them, are read through the same wrappers, find's
# actions, shell -c scripts, and start-ups as a Bash script's; a word that is no literal is UNREAD_WORD.
@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        ([["git", "push"]], {"push"}),
        ([["git", "-C", UNREAD_WORD, "fetch", "origin"]], {"fetch"}),
        ([["git", UNREAD_WORD]], {UNREAD_WORD}),
        ([["env", "git", "push"]], {"push"}),
        ([["find", ".", "-exec", "git", "push", "{}", "+"]], {"push"}),
        ([["bash", "-c", "git push"]], {"push"}),
        ([["command", "-v", "git", "push"]], set()),
        ([["time", "-p", "-p", "git", "push"]], {"push"}),
        ([["echo", "git", "push"]], set()),
        # PowerShell finds the program in any case, so Git and GIT, .exe already dropped, run git.
        ([["Git", "push"]], {"push"}),
        ([["GIT", "-C", "dir", "fetch"]], {"fetch"}),
        ([[UNREAD_WORD, "push"]], set()),
    ],
)
def test_a_powershell_scripts_commands_give_the_git_subcommands_they_run(
    commands: list[list[str]], expected: set[str]
) -> None:
    assert git_subcommands.powershell_git_subcommands(commands, no_powershell) == expected


def test_a_start_up_inside_a_started_script_is_read_in_turn() -> None:
    read: list[str] = []
    reader = listed({"pwsh -c 'git fetch'": [["pwsh", "-c", "git fetch"]], "git fetch": [["git", "fetch"]]}, read)
    assert git_subcommands.powershell_git_subcommands([["pwsh", "-c", "pwsh -c 'git fetch'"]], reader) == {"fetch"}
    assert read == ["pwsh -c 'git fetch'", "git fetch"]


# What COMMANDS_RUN lists for one command becomes its words: the name without .exe, its literal words, UNREAD_WORD for
# each other, and the program Start-Process starts given its argument list's words.
@pytest.mark.parametrize(
    ("item", "words"),
    [
        ({"source": 0, "name": "git", "words": ["push", None]}, ["git", "push", UNREAD_WORD]),
        ({"source": 0, "name": "git.EXE", "words": ["fetch"]}, ["git", "fetch"]),
        ({"source": 0, "name": "git", "arguments": ["-C dir", "push"]}, ["git", "-C", "dir", "push"]),
        ({"source": 0, "name": "git", "arguments": ["push", None]}, ["git", "push", UNREAD_WORD]),
        ({"source": 0, "unread": "& $tool"}, None),
    ],
)
def test_a_listed_command_becomes_its_words(item: dict[str, object], words: list[str] | None) -> None:
    assert powershell_commands.command_words(item) == words


# pwsh's own parser lists what a PowerShell source runs, Start-Process's program included, for every source in one
# call; the cmdlets PowerShell ships are its own, not external commands.
def test_pwsh_lists_the_external_commands_each_source_runs() -> None:
    commands = powershell_commands.read(
        [
            "git push origin\nWrite-Output done",
            "Start-Process git.exe -ArgumentList 'fetch', $remote",
            "function git { }\ngit push",
            "",
        ]
    )
    assert commands == [[["git", "push", "origin"]], [["git", "fetch", UNREAD_WORD]], [], []]
    assert powershell_commands.read([]) == []


def test_a_git_push_a_start_up_runs_in_a_powershell_script_is_read_through_pwsh() -> None:
    def reader(script: str) -> Sequence[Sequence[str]]:
        return powershell_commands.read([script])[0]

    (commands,) = powershell_commands.read(["env pwsh -c 'Start-Process git -ArgumentList push'"])
    assert git_subcommands.powershell_git_subcommands(commands, reader) == {"push"}


def test_pwsh_that_cannot_be_found_or_cannot_list_the_commands_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "find_executable", lambda name: None)
    with pytest.raises(powershell_commands.PowerShellUnread, match="pwsh was not found"):
        powershell_commands.read(["git push"])


def test_a_source_json_cannot_write_is_refused_before_pwsh_starts() -> None:
    with pytest.raises(powershell_commands.PowerShellUnread, match="cannot be handed to pwsh"):
        powershell_commands.read(["git push \ud800"])


def test_pwsh_that_prints_no_list_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "[Console]::Out.WriteLine('not json'); exit 3")
    with pytest.raises(powershell_commands.PowerShellUnread, match="exit code 3"):
        powershell_commands.read(["git push"])
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "[Console]::Out.WriteLine('{}')")
    with pytest.raises(powershell_commands.PowerShellUnread, match="exit code 0"):
        powershell_commands.read(["git push"])


def test_pwsh_that_does_not_finish_in_time_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(powershell_commands, "COMMANDS_RUN", "Start-Sleep -Seconds 30")
    with pytest.raises(powershell_commands.PowerShellUnread, match="did not finish within 1 seconds"):
        powershell_commands.read(["git push"], timeout=1)
