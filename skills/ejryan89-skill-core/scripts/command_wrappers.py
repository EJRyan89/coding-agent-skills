"""The one reading of the commands that run a command a later word names: a wrapper such as xargs or env, find's
actions, and a shell's -c script.

A reader of a shell script's commands goes past such a command to the one it runs: `unwrapped` past a wrapper of
COMMAND_WRAPPERS, read by option_reader's getopt reading, `find_action` to the command one of find's FIND_COMMANDS
runs, and `shell_script_index` to the script a shell's -c runs. Repository validation's command and git subcommand
readers and audit-skill's git subcommand reader read through these, so they agree on every wrapper.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from executable_locator import program_name
from option_reader import OptionTable, option_refusal, option_table

# The commands that run the program a later word names, each with every option it takes, in getopt's spelling as
# option_table reads it, and read by option_refusal, the one getopt reader: the program is the first word after the
# options, after the assignments env takes, and after the duration timeout takes. A long option is placed only spelled
# in full, and a word the table does not settle, an abbreviation or an option the table does not list, leaves the
# program unread (UNREAD_PROGRAM) rather than taken for the program. command -v and -V only look a program up.
COMMAND_WRAPPERS: dict[str, OptionTable] = {
    "command": option_table("pvV"),
    "builtin": option_table(),
    "exec": option_table("cla:"),
    "nohup": option_table("", "help version"),
    "time": option_table("af:o:pqvV", "append format= output= portability quiet verbose help version"),
    "nice": option_table("n:", "adjustment= help version", numbered=re.compile(r"-[+-]?[0-9]+")),
    "env": option_table(
        "0a:C:iS:u:v",
        " ".join(
            (
                "null argv0= chdir= ignore-environment split-string= unset= debug block-signal=? default-signal=?",
                "ignore-signal=? list-signal-handling help version",
            )
        ),
        assignments=True,
    ),
    "timeout": option_table(
        "fk:ps:v", "foreground kill-after= preserve-status signal= verbose help version", operands=1
    ),
    "stdbuf": option_table("i:o:e:", "input= output= error= help version"),
    "xargs": option_table(
        "0a:d:E:e::I:i::L:l::n:oP:prs:tx",
        " ".join(
            (
                "null arg-file= delimiter= eof=? replace=? max-lines=? max-args= open-tty max-procs= interactive",
                "no-run-if-empty max-chars= process-slot-var= show-limits verbose exit help version",
            )
        ),
    ),
}
# env's options whose value is a command line, split into the words it runs.
ENV_SPLIT_OPTIONS = frozenset({"-S", "--split-string"})
LOOKUP_ONLY_OPTIONS = frozenset({"-v", "-V"})
# find's actions that run the program the next word names; tools/hub_guard.py's allow-list of find's options admits
# none of them.
FIND_COMMANDS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# The commands that run a script a word holds: a shell the script after its -c.
SCRIPT_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# A shell's option letters that take the next word: -o and +o a set option's name, -O and +O a shopt option's. Its
# long options that do are BASH_LONG_OPTIONS_WITH_VALUE.
SHELL_ARGUMENT_LETTERS = frozenset("oO")
BASH_LONG_OPTIONS_WITH_VALUE = frozenset({"--rcfile", "--init-file"})
# A shell variable's assignment, which may stand before a command's program, as `A=1` or `a[0]+=x`.
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
# A pipe hands the next command what the one before it writes; `time` after one is the time program, not Bash's
# keyword.
PIPES = frozenset({"|", "|&"})
# The operators that end a case item's commands, after which the next item's patterns stand.
CASE_ITEM_ENDS = frozenset({";;", ";&", ";;&"})
# The words Bash's time keyword takes before the pipeline it times, each unquoted, each optional, and in this order.
TIME_KEYWORD_OPTIONS: tuple[str, ...] = ("-p", "--")
# What a reader records for a program a wrapper runs but that no literal names, such as `xargs "$tool"`, so the command
# policy refuses it rather than passing it unseen; it never names a program.
UNREAD_PROGRAM = "<unread> "


def bash_program(word: str) -> str:
    """The program a Bash command's word names, by its file name, as Bash finds it: `/usr/bin/git` and `git.exe` are
    git, the .exe Windows lets its caller add dropped in any case, while the name itself is matched as written, so `Git`
    is no git."""
    return program_name(word.rpartition("/")[2])


def unwrapped(words: list[str], wrappers: Mapping[str, OptionTable] = COMMAND_WRAPPERS) -> list[str]:
    """The words of the command a wrapper of `wrappers` runs, from its program on: past the wrapper's options and their
    values, in every spelling option_refusal reads, the assignments env takes, and the duration timeout takes, with
    the command line env's -S or --split-string holds split into the words it runs. Empty for a command that is no
    wrapper, one that names no program, and `command` given -v or -V among its own options, alone or in a bundle such
    as `-pv`, which only looks one up; a -v after the program is the program's own. A wrapper option the table does not
    settle, such as an abbreviation, gives one word, UNREAD_PROGRAM and the wrapper with that option, so no word is
    taken for the program unread."""
    head, rest = words[0], words[1:]
    table = wrappers.get(head)
    if table is None:
        return []
    reading = option_refusal(table, list(rest))
    if reading.refused is not None:
        return [f"{UNREAD_PROGRAM}{head} {reading.refused}"]
    if head == "command" and any(looks_up(word) for word in rest[: reading.stop]):
        return []
    split = [value or "" for option, value in reading.values if option in ENV_SPLIT_OPTIONS]
    if head == "env" and split:
        return unwrapped(
            ["env", *(word for value in split for word in split_line(value)), *rest[reading.stop :]], wrappers
        )
    return rest[reading.stop :]


def looks_up(word: str) -> bool:
    """Whether one of `command`'s option words, alone or bundled, holds a letter of LOOKUP_ONLY_OPTIONS."""
    return word.startswith("-") and word != "--" and any(f"-{letter}" in LOOKUP_ONLY_OPTIONS for letter in word[1:])


def split_line(value: str) -> list[str]:
    """A command line split into its words as env's -S splits it; a line shlex cannot split stays one word."""
    try:
        return shlex.split(value)
    except ValueError:
        return [value]


def find_actions(words: list[str]) -> list[list[str]]:
    """The words of each command find's FIND_COMMANDS run, given find's words after its name, in order: each action
    with a word after it, up to the `;` that ends it or the `+` after `{}` that does, or to the end when neither
    does."""
    return [
        find_action(words[index + 1 :])
        for index, word in enumerate(words)
        if word in FIND_COMMANDS and index + 1 < len(words)
    ]


def find_action(words: list[str]) -> list[str]:
    """The words of the command one of find's FIND_COMMANDS runs, given the words after the action: up to the `;` that
    ends it or the `+` after `{}` that does, or to the end when neither does."""
    for index, word in enumerate(words[1:], start=1):
        if word == ";" or (word == "+" and words[index - 1] == "{}"):
            return words[:index]
    return words


def shell_script_index(arguments: list[str]) -> int | None:
    """Where, among the words after a shell's name, the script its -c runs stands, read as the shell reads its
    options: -c alone or in a bundle such as -lc or -ec, before or after the shell's other options, the script being
    the first word that is no option, after the value -o, -O, --rcfile, or --init-file takes and after `--`; None for
    a shell started without -c, which runs a file or its standard input."""
    runs_script, argument_next, options_over = False, False, False
    for index, word in enumerate(arguments):
        if argument_next:
            argument_next = False
        elif options_over or not word.startswith(("-", "+")) or word in {"-", "+"}:
            return index if runs_script else None
        elif word == "--":
            options_over = True
        elif word.startswith("--"):
            argument_next = word in BASH_LONG_OPTIONS_WITH_VALUE
        else:
            runs_script = runs_script or "c" in word[1:]
            argument_next = any(letter in SHELL_ARGUMENT_LETTERS for letter in word[1:])
    return None
