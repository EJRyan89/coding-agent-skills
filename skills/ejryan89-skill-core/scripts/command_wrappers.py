"""The one reading of the commands that run a command a later word names: a wrapper such as xargs, env, or sudo,
find's actions, and a shell's -c script.

A reader of a shell script's commands goes past such a command to the one it runs: `unwrapped` past a wrapper of
COMMAND_WRAPPERS, read by option_reader's getopt reading, `find_action` to the command one of find's FIND_COMMANDS
runs, `shell_script_index` to the script a shell's -c runs, and `trap_action` to the script a trap sets to run.
Repository validation's command, git subcommand, and pipe readers and audit-skill's Bash and git subcommand readers
read through these, so they agree on every wrapper and every script a command starts. Every Bash builtin is decided
here too, and a use of one that runs text no reader reads, as `source` or `mapfile -C` does, is a program `unwrapped`
gives as UNREAD_PROGRAM: validation's command policy refuses the script that holds it, while git_subcommands gives no
subcommand for it and audit-skill's program reader stops at it, as each does for any wrapper it cannot read.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping

from executable_locator import program_name
from option_reader import MACOS_ENV_LETTERS, OptionTable, option_refusal, option_table

# The commands that run the program a later word names, each with every option it takes, in getopt's spelling as
# option_table reads it, and read by option_refusal, the one getopt reader: the program is the first word after the
# options, after the assignments env takes, and after the duration timeout takes. A long option is placed only spelled
# in full, and a word the table does not settle, an abbreviation or an option the table does not list, leaves the
# program unread (UNREAD_PROGRAM) rather than taken for the program. command -v and -V only look a program up. Each
# program's table beyond the shell's builtins is its getopt table, as the repository's option catalogues record it, and
# a coreutils program's every option GNU's build or the Rust coreutils' lists, as env's -f and --file, -a and --argv0,
# and the -h and -V of the Rust coreutils' runners (tests/tools/test_command_wrapper_catalogues.py holds each row to the
# catalogues); sudo also takes assignments, and chrt, taskset, and
# chroot take one operand, a priority, a CPU mask, or a new root, before the program. Every reader of a command's
# program reads these, so a reader that needs another wrapper adds it here.
COMMAND_WRAPPERS: dict[str, OptionTable] = {
    "command": option_table("pvV"),
    "builtin": option_table(),
    "exec": option_table("cla:"),
    "nohup": option_table("hV", "help version"),
    "time": option_table("af:o:pqvV", "append format= output= portability quiet verbose help version"),
    "nice": option_table("hn:V", "adjustment= help version", numbered=re.compile(r"-[+-]?[0-9]+")),
    "env": option_table(
        "0a:C:f:hiS:u:vV",
        " ".join(
            (
                "null argv0= chdir= file= ignore-environment split-string= unset= debug block-signal=?",
                "default-signal=? ignore-signal=? list-signal-handling help version",
            )
        ),
        assignments=True,
        # Every letter of macOS's env places its command alike, so only a word the two builds read as taking
        # different words leaves the program unread.
        dash_build=MACOS_ENV_LETTERS,
        dash_safe=MACOS_ENV_LETTERS.replace(":", ""),
    ),
    "timeout": option_table(
        "fhk:ps:vV", "foreground kill-after= preserve-status signal= verbose help version", operands=1
    ),
    "stdbuf": option_table("hi:o:e:V", "input= output= error= help version"),
    "xargs": option_table(
        "0a:d:E:e::I:i::L:l::n:oP:prs:tx",
        " ".join(
            (
                "null arg-file= delimiter= eof=? replace=? max-lines=? max-args= open-tty max-procs= interactive",
                "no-run-if-empty max-chars= process-slot-var= show-limits verbose exit help version",
            )
        ),
    ),
    "sudo": option_table(
        "Aa:BbC:c:D:Eeg:Hh::iKklNnPp:R:r:SsT:t:U:u:Vv",
        " ".join(
            (
                "askpass auth-type= bell background close-from= login-class= chdir= preserve-env=? edit group=",
                "set-home help host= login remove-timestamp reset-timestamp list no-update non-interactive",
                "preserve-groups prompt= chroot= role= stdin shell command-timeout= type= other-user= user= version",
                "validate",
            )
        ),
        assignments=True,
    ),
    "doas": option_table("a:C:Lnsu:"),
    "setsid": option_table("cfwhV", "ctty fork wait help version"),
    "ionice": option_table("c:n:p:P:tu:hV", "class= classdata= pid= pgid= ignore uid= help version"),
    "chrt": option_table(
        "abdefimoprRT:P:D:vhV",
        " ".join(
            (
                "all-tasks batch deadline ext fifo idle max other pid rr reset-on-fork sched-runtime= sched-period=",
                "sched-deadline= verbose help version",
            )
        ),
        operands=1,
    ),
    "taskset": option_table("acphV", "all-tasks cpu-list pid help version", operands=1),
    "chroot": option_table("hV", "groups= userspec= skip-chdir help version", operands=1),
    "unbuffer": option_table("p"),
    "chronic": option_table("ev"),
    "strace": option_table(
        "Aa:b:CcDdE:e:FfhI:ikno:O:P:p:qrS:s:tTU:u:VvwX:xyYzZ",
        " ".join(
            (
                "output-append-mode columns= detach-on= summary summary-only daemonize=? debug env= follow-forks",
                "output-separately help interruptible= instruction-pointer stack-trace syscall-number output=",
                "summary-syscall-overhead= trace-path= attach= quiet=? relative-timestamps=? summary-sort-by=",
                "string-limit= absolute-timestamps=? syscall-times=? summary-columns= user= version no-abbrev=?",
                "summary-wall-clock const-print-style= strings-in-hex=? decode-fds=? decode-pids=? successful-only",
                "failed-only trace= signal= status= inject= fault= abbrev= verbose= raw= read= write= kvm=",
                "secontext=? seccomp-bpf kill-on-exit tips=?",
            )
        ),
    ),
    "ltrace": option_table(
        "a:A:bcCD:e:fF:hil:Ln:o:p:rs:StTu:Vw:x:",
        "align= no-signals demangle debug= config= help library= indent= output= version where=",
    ),
}
# The prefixes a package manager installs a build of a wrapper under beside the system's own (wrapper_name): Homebrew's
# GNU coreutils and findutils take a g, as gtimeout, genv, and gxargs, and its uutils coreutils a u, as utimeout. A
# wrapper is read under either prefix whether or not a package builds it. Two stated limits, since no script here names
# either: a program of its own whose name is a prefix and a wrapper's, as Windows' gsudo or CSCS's uenv, is read as that
# wrapper, its subcommand taken for the program; and find, a shell, and every other program a reader names are matched
# by their own names alone, so a prefixed build of one, as gfind, is read as a program of its own.
BUILD_PREFIXES = ("g", "u")
# env's options whose value is a command line, split into the words it runs.
ENV_SPLIT_OPTIONS = frozenset({"-S", "--split-string"})
LOOKUP_ONLY_OPTIONS = frozenset({"-v", "-V"})
# find's actions that run the program the next word names.
FIND_COMMANDS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# The commands that run a script a word holds: a shell the script after its -c.
SCRIPT_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
# A shell's option letters that take the next word: -o and +o a set option's name, -O and +O a shopt option's. Its
# long options that do are BASH_LONG_OPTIONS_WITH_VALUE.
SHELL_ARGUMENT_LETTERS = frozenset("oO")
BASH_LONG_OPTIONS_WITH_VALUE = frozenset({"--rcfile", "--init-file"})
# A shell variable's assignment, which may stand before a command's program, as `A=1` or `a[0]+=x`.
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
# What a reader records for a program a wrapper runs but that no literal names, such as `xargs "$tool"`, so the command
# policy refuses it rather than passing it unseen; it never names a program.
UNREAD_PROGRAM = "<unread> "

# Every Bash builtin is decided here, in exactly one table, as tests/tools/test_command_wrapper_catalogues.py holds to
# each builtin the validated Bash's `compgen -b` prints and to a pinned list of the newest Bash's:
# - COMMAND_WRAPPERS holds command, builtin, and exec, each read as the command it runs;
# - OPTION_WRAPPERS holds the builtins read as the command they run only given their option, as jobs -x;
# - SCRIPT_BUILTINS holds the builtins that run a script each reader reads, eval and trap;
# - REFUSED_BUILTINS holds the builtins with a use that runs text no reader reads, each with when and why, a use
#   `unwrapped` gives as UNREAD_PROGRAM, while every other use of the builtin runs nothing;
# - RUNS_NOTHING holds the builtins that run no command but themselves, whatever their words;
# - NOT_READ holds, each with why, the builtins that run text only in a way no script reaches, or change what a later
#   word runs, which no reader judges.
# The builtins that run a script each reader reads: eval's arguments, and the action a trap sets (trap_action).
SCRIPT_BUILTINS = frozenset({"eval", "trap"})
# The builtins that run the command their operands name, in the shell itself, only given an option, each with its
# getopt table, read as a wrapper's is, and that option's letter: jobs -x. Given no such option, one runs nothing.
OPTION_WRAPPERS = {"jobs": (option_table("lnprsx"), "x")}
# The builtins whose words name a variable or an arithmetic expression whose array subscript Bash expands as it reads
# the word, so a command substitution the subscript's text holds runs even where the word was quoted, as in
# `printf -v 'a[$(touch x)]' y`; export, readonly, and getopts do once the variable they set has the integer
# attribute, as after `declare -i x`, and wait does for the variable its -p names, in Bash 5.1 and later.
SUBSCRIPT_BUILTINS = frozenset(
    {"let", "declare", "typeset", "local", "read", "printf", "unset", "test", "[", "export", "readonly", "getopts"}
    | {"wait"}
)
# A command or process substitution anywhere after a subscript's `[` in a word, a nested subscript's included, and
# Bash 5.3's `${ command; }` and `${| command; }`, which run in the shell itself; it errs toward refusing a word whose
# substitution stands after its subscript closes.
SUBSCRIPT_SUBSTITUTION = re.compile(r"\[.*(?:\$\(|`|[<>]\(|\$\{[\s|])", re.DOTALL)
# The builtins that run text no reader reads given one of these option letters, each with the letters and why.
OPTION_RUNNERS = {
    **dict.fromkeys(("mapfile", "readarray"), ("C", "runs its -C callback in the shell itself")),
    "compgen": ("CFW", "runs its -C command or its -F function, or expands its -W wordlist's substitutions"),
    # enable -n, which makes a later name run the program of that name in the builtin's place, as hash -p makes a later
    # name run another file, is not refused: a stated limit, since no reader follows what a later name runs.
    "enable": ("f", "loads a builtin from a shared object"),
}
# The uses of a builtin that run text no reader reads, each with when and why.
REFUSED_BUILTINS = {
    **dict.fromkeys((".", "source"), "always: runs a file's commands"),
    "fc": "unless it only lists, given -l without -e or -s: runs commands again from the history",
    **{
        name: f"given {' or '.join(f'-{letter}' for letter in letters)}: {reason}"
        for name, (letters, reason) in OPTION_RUNNERS.items()
    },
    **dict.fromkeys(SUBSCRIPT_BUILTINS, "given a word whose array subscript holds a command or process substitution"),
}
# The builtins that run no command but themselves, whatever their words.
RUNS_NOTHING = frozenset(
    {":", "true", "false", "echo", "pwd", "cd", "pushd", "popd", "dirs", "type", "help", "caller", "times", "history"}
    | {"ulimit", "umask", "break", "continue", "return", "exit", "logout", "shift", "bg", "fg"}
    | {"disown", "kill", "suspend", "set", "shopt", "compopt"}
)
# The catalogues this module decides, each with the test that holds every entry of it, which ejryan89-skill-core's
# catalogue_tests.py reads.
CATALOGUE_TESTS = {
    "Bash's builtins, as `compgen -b` prints them": (
        "tests/tools/test_command_wrapper_catalogues.py::test_every_builtin_bash_prints_is_decided_once"
    ),
    "each wrapper's options, as its GNU and Rust coreutils builds list them": (
        "tests/tools/test_command_wrapper_catalogues.py::CatalogueTests."
        "test_each_wrapper_row_reads_every_option_its_gnu_and_rust_builds_list_as_they_take_it"
    ),
    "each wrapper's options, as macOS's builds list them": (
        "tests/tools/test_command_wrapper_catalogues.py::CatalogueTests."
        "test_each_wrapper_row_reads_every_option_a_macos_build_lists_that_it_reads_as_that_build_takes_it"
    ),
}
# The builtins that run text in a way no script reaches, or change what a later word runs, each with why; no reader
# judges them.
NOT_READ = {
    "complete": "runs its -C command or -F function only at an interactive prompt's completion, no script's",
    "bind": "runs its -x command only at an interactive prompt's key, which no script reaches",
    "alias": "changes what a later name runs only where expand_aliases is set, which the set and shopt catalogue reads",
    "unalias": "changes what a later name runs only where expand_aliases is set, as alias does",
    "hash": "with -p makes a later name run the file it names, which no reader follows",
}


def bash_program(word: str) -> str:
    """The program a Bash command's word names, by its file name, as Bash finds it: `/usr/bin/git` and `git.exe` are
    git, the .exe Windows lets its caller add dropped in any case, while the name itself is matched as written, so `Git`
    is no git."""
    return program_name(word.rpartition("/")[2])


def wrapper_name(program: str, wrappers: Mapping[str, OptionTable] = COMMAND_WRAPPERS) -> str:
    """The wrapper of `wrappers` a program's name, by its file name, builds: its own name, or, for a build a package
    manager installs under a prefix BUILD_PREFIXES names, as gtimeout or uenv, the name after the prefix. Any other
    name is returned as it is."""
    unprefixed = program[1:]
    if program not in wrappers and program[:1] in BUILD_PREFIXES and unprefixed in wrappers:
        return unprefixed
    return program


def unwrapped(words: list[str], wrappers: Mapping[str, OptionTable] = COMMAND_WRAPPERS) -> list[str]:
    """The words of the command a wrapper of `wrappers` runs, from its program on: past the wrapper's options and their
    values, in every spelling option_refusal reads, the assignments env takes, and the duration timeout takes, with
    the command line env's -S or --split-string holds split into the words it runs. A wrapper is read by wrapper_name,
    so a build under a prefix, as gtimeout, is read as the one it builds. Empty for a command that is no wrapper, one
    that names no program, and `command` given -v or -V among its own options, alone or in a bundle such as `-pv`,
    which only looks one up; a -v after the program is the program's own. A wrapper option the table does not settle,
    such as an abbreviation, gives one word, UNREAD_PROGRAM and the wrapper with that option, so no word is taken for
    the program unread, and so does a use of a builtin that runs text no reader reads (builtin_refusal), with why."""
    refusal = builtin_refusal(words)
    if refusal is not None:
        return [f"{UNREAD_PROGRAM}{words[0]} {refusal}"]
    if words[0] in OPTION_WRAPPERS:
        return option_wrapped(words)
    head, rest = wrapper_name(words[0], wrappers), words[1:]
    table = wrappers.get(head)
    if table is None:
        return []
    reading = option_refusal(table, list(rest))
    if reading.refused is not None:
        return [f"{UNREAD_PROGRAM}{words[0]} {reading.refused}"]
    if head == "command" and any(looks_up(word) for word in rest[: reading.stop]):
        return []
    split = [value or "" for option, value in reading.values if option in ENV_SPLIT_OPTIONS]
    if head == "env" and split:
        return unwrapped(
            ["env", *(word for value in split for word in split_line(value)), *rest[reading.stop :]], wrappers
        )
    return rest[reading.stop :]


def option_wrapped(words: list[str]) -> list[str]:
    """The words of the command a builtin OPTION_WRAPPERS names runs, from its program on, given its option, as jobs
    -x runs one; empty given no such option, and UNREAD_PROGRAM with the option for one its table does not settle."""
    table, letter = OPTION_WRAPPERS[words[0]]
    rest = words[1:]
    reading = option_refusal(table, list(rest))
    if reading.refused is not None:
        return [f"{UNREAD_PROGRAM}{words[0]} {reading.refused}"]
    options = [word for word in rest[: reading.stop] if word != "--"]
    return rest[reading.stop :] if any(letter in word[1:] for word in options) else []


def builtin_refusal(words: list[str]) -> str | None:
    """Why a builtin's words run text no reader reads, as REFUSED_BUILTINS names each such use, or None. An option
    letter is read in any option word, one starting with `-`, alone or bundled, wherever it stands, so an operand
    that looks like one is taken for one, erring toward refusing, and a subscript's substitution is read in any word of
    a SUBSCRIPT_BUILTINS builtin.
    Stated limits, since no script here writes one: a subscript a variable fills, as `read "a[$i]"`, whose value Bash
    expands again; an ANSI-C `$'...'` word whose escape spells the substitution; text an integer variable receives as
    data, as `declare -i i; read i <<< 'a[$(x)]'` or a mapfile into an integer array; none of which is refused, since a
    reader sees no substitution in the word; an option word a variable or a substitution fills, as
    `o=-W; compgen "$o" '$(x)' x`, `mapfile "$o" cb`, or `enable "$o" lib.so`, which a reader takes as the literal its
    text is; and `enable -n`, which makes a later name run the program of that name in the builtin's place, as
    `hash -p` makes one run another file, since no reader follows what a later name runs."""
    head, rest = words[0], words[1:]
    letters = {letter for word in rest if word.startswith("-") and word not in {"-", "--"} for letter in word[1:]}
    if head in {".", "source"}:
        return "runs a file's commands"
    if head == "fc" and not ("l" in letters and not letters & {"e", "s"}):
        return "runs commands again from the history"
    if head in OPTION_RUNNERS and letters & set(OPTION_RUNNERS[head][0]):
        return OPTION_RUNNERS[head][1]
    if head in SUBSCRIPT_BUILTINS and any(SUBSCRIPT_SUBSTITUTION.search(word) for word in rest):
        return "expands a command substitution in an array subscript"
    return None


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


def trap_action(arguments: list[str]) -> str | None:
    """The script Bash's trap builtin sets to run when a condition fires, given its words after `trap`, as Bash reads
    them: the first operand, after an optional `--`, when a condition follows it; None for a trap given an option,
    -l, -p, or -P, alone or bundled, which lists, and for one whose first operand is the only one, `-`, or an unsigned
    integer, which each reset the conditions named, as `trap 0 INT` resets the exit and interrupt traps. An unsigned
    integer that names no signal, as in `trap 99 INT`, is the action Bash sets; the reader drops it, and it would run a
    program named by that number, so no cue, print, exit code, program, or git subcommand is lost. A first operand
    only running settles, as in `trap "$cleanup" EXIT`, is the action, which each reader reads as it reads such a
    script given to eval. A stated limit, since no script here writes one: an unquoted first operand only running
    settles that expands to nothing or to an option, as in `trap $none 'git push' EXIT`, moves the action Bash reads to
    the next word, which is read as a condition."""
    if arguments[:1] and arguments[0].startswith("-") and arguments[0] not in {"-", "--"}:
        return None
    operands = arguments[1:] if arguments[:1] == ["--"] else arguments
    if len(operands) < 2 or operands[0] == "-" or (operands[0].isascii() and operands[0].isdigit()):
        return None
    return operands[0]


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
