"""The hub guard: the Claude Code PreToolUse hook .claude/settings.json runs, which reads the hook JSON on stdin.

Usage:
  python tools/hub_guard.py   reads one PreToolUse event on stdin and prints a deny decision, or nothing

The guard refuses file edits inside the hub and git commands that would move the hub's HEAD or write its tree,
whether they run through the Bash tool, the PowerShell tool, or the Monitor tool, whose command runs in Bash. It fails
closed: it reads a command only as far as it understands it, Bash by tools/bash_syntax.py's grammar and PowerShell by
PowerShell's own parser (tools/hub_guard-powershell.ps1), judges every git call it can place, and refuses every part
it does not understand while a guarded hub is in view. What it understands is the tables below, each a catalogue a
shell publishes with every entry understood or in NOT_READ, and docs/parallel-sessions.md ("The guard's contract")
states the contract. Every decision is made here. It finds the checkout that holds a path, and runs git, through
tools/worktrees.py, which owns the hub and its worktrees. It is inert unless `git config coding-agent-skills.hubGuard
true` was set in this clone.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_reader
import whole_number
from bounded_process import run_bounded
from console import run_main
from git_client import GLOBAL_VALUE_OPTIONS as GIT_OPTIONS_WITH_VALUE
from git_client import GitError, GitResult, output_line, output_lines
from job_tree import process_tree

from deployer import platform_support
from tools import bash_syntax, worktrees
from tools.worktrees import CLIENT, GUARD_GIT_TIMEOUT_SECONDS, MAIN, Checkout, same_path

GUARD_SETTING = "coding-agent-skills.hubGuard"
FILE_TOOLS = {"Edit", "NotebookEdit", "Write"}
# Git subcommands that can move HEAD or the branch it names, or write the index or the tracked files of the
# checkout they run in, whichever subcommand or option makes them do it: the plumbing and porcelain that do, the
# interactive tools that can (difftool and mergetool, gui, citool), and the importers that commit and check
# out (archimport, cvsimport, p4, quiltimport, svn). A subcommand that writes only objects, refs other than HEAD's
# branch (notes, replace, tag), or other worktrees (worktree) is not here. fetch is judged by its options: with
# --update-head-ok it moves the branch HEAD names, as git pull asks it to.
WRITING_SUBCOMMANDS = {
    "add",
    "am",
    "apply",
    "archimport",
    "bisect",
    "branch",
    "checkout",
    "checkout-index",
    "cherry-pick",
    "citool",
    "clean",
    "commit",
    "cvsimport",
    "difftool",
    "fast-import",
    "filter-branch",
    "gui",
    "history",
    "merge",
    "merge-file",
    "merge-index",
    "merge-one-file",
    "mergetool",
    "mv",
    "p4",
    "pull",
    "quiltimport",
    "read-tree",
    "rebase",
    "replay",
    "rerere",
    "reset",
    "restore",
    "revert",
    "rm",
    "sparse-checkout",
    "stage",
    "stash",
    "submodule",
    "svn",
    "switch",
    "symbolic-ref",
    "update-index",
    "update-ref",
}
FETCH = "fetch"
# The only fast-forwards the hub takes: a subcommand's words besides FAST_FORWARD_OPTIONS must be one of these.
FAST_FORWARD_TARGETS = {"merge": [["origin/main"]], "pull": [[], ["origin", "main"]]}
FAST_FORWARD_OPTIONS = {"--ff-only", "-q", "--quiet", "-v", "--verbose", "--no-rebase"}
# The submodule subcommands that only read; no subcommand at all means status. foreach runs any command.
READ_SUBMODULE = {None, "status", "summary"}
# git's own options, read as git.c reads them: no abbreviation, and a value either attached after = or the next word.
# Those that take a value are git_client's GLOBAL_VALUE_OPTIONS, imported above as GIT_OPTIONS_WITH_VALUE.
GIT_OPTIONS_WITHOUT_VALUE = {
    "-p",
    "--paginate",
    "-P",
    "--no-pager",
    "--bare",
    "--no-replace-objects",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-advice",
    "--literal-pathspecs",
    "--glob-pathspecs",
    "--noglob-pathspecs",
    "--icase-pathspecs",
}
# Options after which git prints something and runs no subcommand of the command's.
GIT_OPTIONS_THAT_PRINT = {"--version", "-v", "--help", "-h", "--exec-path", "--html-path", "--man-path", "--info-path"}
# The variables that choose the repository or work tree whose HEAD, index, and files a git command writes.
# GIT_COMMON_DIR is not one: it moves only the refs and objects every worktree shares, never a checkout's HEAD, index,
# or tree. Nor is GIT_INDEX_FILE, which moves only the index; the guard judges such a call by the checkout it runs in.
LOCATION_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE")
# The variables that carry command-line configuration, which git reads for core.worktree and alias.<name>.
CONFIGURATION_VARIABLES = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")
CONFIGURATION_PAIR = re.compile(r"GIT_CONFIG_(?:KEY|VALUE)_[0-9]+")
# The environment variable by which Bash hands a shell it starts a function exported with export -f.
EXPORTED_FUNCTION = re.compile(r"^BASH_FUNC_(.+)%%$")

# ---------------------------------------------------------------------------------------------------------------
# What the readers understand
#
# The guard reads a command only as far as it understands it, and refuses the rest while a guarded hub is in view
# ("The guard's contract" in docs/parallel-sessions.md). Each table below is one catalogue the shells publish, and
# every entry is either understood, with how, or in NOT_READ, with why the guard does not read it. The guard's suites
# generate their rows from the catalogue each shell lists itself, `compgen -b`, `compgen -k`, `set -o`, `shopt`, and
# `compgen -v` from Bash and the AST node types by reflection from PowerShell, and fail on an entry neither table holds.

# The programs that run another command their words name, by the catalogue the guard refuses: each is refused unless
# the guard reads the command it runs, which RUNNER_OPTIONS and the readers of env, xargs, bash, sh, pwsh, and find
# settle, and which is then read as a command. Every other program a literal name starts is understood: what it does
# inside itself, in its own language (a Python script, an awk program, a test suite), the guard does not see.
RUNNERS: dict[str, str] = {
    **dict.fromkeys(("bash", "sh"), "a shell, read only as `-c` with a literal script, which is read as Bash"),
    "pwsh": "PowerShell, read only as `-Command` or `-c` with one literal script, after -NoProfile, -NonInteractive, "
    "or "
    "-NoLogo, which is read as PowerShell",
    "powershell": "Windows PowerShell, whose parser differs from pwsh's, so the guard does not read its script",
    **dict.fromkeys(
        ("zsh", "dash", "ksh", "mksh", "fish", "csh", "tcsh", "busybox"), "a shell whose script the guard does not read"
    ),
    "cmd": "Windows' command interpreter, whose script the guard does not read",
    "wsl": "runs a command in a Linux distribution, which the guard does not read",
    "env": "runs its command after the assignments before it, read when it has no option and each assignment is one an "
    "assignment before that command may make",
    "xargs": "runs its command with words from its input, read with those words unsettled",
    **dict.fromkeys(("timeout", "nohup", "nice", "stdbuf"), "runs the command after its options, read as a command"),
    **dict.fromkeys(
        ("sudo", "doas", "su", "runuser"), "runs a command as another user, in an environment its policy decides"
    ),
    "chroot": "runs a command under another root",
    "ssh": "runs a command on another host, this one among them",
    **dict.fromkeys(
        (
            *("ionice", "chrt", "taskset", "setsid", "flock", "unbuffer", "chronic", "strace", "ltrace", "time"),
            *("watch", "script", "parallel"),
        ),
        "runs a command its words name, whose options the guard does not read",
    ),
}
# The runners whose options the guard reads before the command they run: each one's options that take no value, those
# that take the next word or their value attached, and how many operands come before the command.
RUNNER_OPTIONS: dict[str, tuple[frozenset[str], frozenset[str], int]] = {
    "timeout": (
        frozenset({"-v", "--verbose", "--preserve-status", "--foreground"}),
        frozenset({"-s", "--signal", "-k", "--kill-after"}),
        1,
    ),
    "nohup": (frozenset(), frozenset(), 0),
    "nice": (frozenset(), frozenset({"-n", "--adjustment"}), 0),
    "stdbuf": (frozenset(), frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}), 0),
    "xargs": (
        frozenset({"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit"}),
        frozenset(
            {
                *("-n", "--max-args", "-P", "--max-procs", "-L", "--max-lines", "-I", "-d", "--delimiter", "-s"),
                *("--max-chars", "-a", "--arg-file", "-E"),
            }
        ),
        0,
    ),
}
GH_LIMIT_ALLOWED = {
    "RUNNER_OPTIONS": "xargs's own -L, the input lines it hands each command; it is read from a command, and no gh "
    "listing is made here"
}
# The options that make a program the guard understands run a command its words name: find's run the command after
# them, which is read; the others are refused, and so is any word of such a program that the text does not settle.
FIND_COMMANDS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
COMMAND_OPTIONS: dict[str, frozenset[str]] = {
    "find": FIND_COMMANDS,
    "rg": frozenset({"--pre"}),
    "sort": frozenset({"--compress-program"}),
    "tar": frozenset(
        {
            *("--to-command", "--use-compress-program", "-I", "--checkpoint-action", "--info-script", "-F"),
            *("--new-volume-script", "--rsh-command", "--rmt-command"),
        }
    ),
}
# pwsh's options that change nothing the guard reads before -Command.
PWSH_QUIET = frozenset({"-noprofile", "-noninteractive", "-nologo"})
# The variables an assignment before git may give it, each with the values it may take, or none for any literal
# value: who a commit is by and when, an editor that edits nothing, and no prompt. Every other variable git reads
# may change where it acts, what configuration and aliases it reads, or what program it runs.
GIT_PREFIXES: dict[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            *("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_AUTHOR_DATE", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"),
            *("GIT_COMMITTER_DATE", "GIT_TERMINAL_PROMPT"),
        ),
        frozenset(),
    ),
    **dict.fromkeys(("GIT_EDITOR", "GIT_SEQUENCE_EDITOR"), frozenset({"true", ":"})),
    "GIT_PAGER": frozenset({"cat", ""}),
}
# gh runs git itself for these subcommands, which check a branch out in the checkout gh runs in.
GH_CHECKOUTS = {("pr", "checkout"), ("pr", "co"), ("repo", "sync")}
# The variables naming the system's temporary folder, whose value from the environment a redirection's target may
# take, as `> "$TMP/out.txt"` does.
TEMPORARY_VARIABLES = frozenset({"TMP", "TEMP", "TMPDIR"})
# Files a redirection may write that are no file of a checkout.
DEVICE_FILES = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul", "NUL"})

# The Bash builtins the guard understands (`compgen -b` lists every one), with what each does that the guard reads.
# Every other builtin is in NOT_READ.
BUILTINS_UNDERSTOOD = {
    **dict.fromkeys((":", "true", "false"), "runs nothing"),
    **dict.fromkeys(("echo", "pwd", "type", "jobs", "times", "help", "caller"), "prints, and changes nothing"),
    "printf": "prints its words; -v, which assigns a variable, is not read",
    **dict.fromkeys(("test", "["), "tests its words; -v, which may evaluate a subscript as arithmetic, is not read"),
    "cd": "moves the shell to a directory, which places the git calls after it",
    "pushd": "moves the shell to a directory and keeps the one it left, which places the git calls after it",
    "popd": "moves the shell back to the directory pushd kept",
    **dict.fromkeys(("break", "continue", "exit", "return"), "ends a loop, a function, or the shell"),
    "shift": "renumbers the positional parameters",
    "wait": "waits for background jobs; an option, as -p, which assigns a variable, is not read",
    "kill": "sends a signal",
    **dict.fromkeys(("umask", "ulimit"), "sets the mode or the limits of what the shell starts"),
    "read": "assigns its input to names the guard needs no value of (a free name)",
    "command": "with -v or -V prints what a name is; run any other way it starts the name, which is not read",
    "set": "turns on or off the options SET_OPTIONS decides, or sets the positional parameters",
    "shopt": "turns on or off the options SHOPT_OPTIONS decides, or prints them",
}
# The Bash reserved words the guard understands (`compgen -k`); every other one is in NOT_READ. A compound command's
# words and bodies are read, a git call in one is placed only where no directory change in it may precede it, and the
# directory after one that changes it is unknown.
KEYWORDS_UNDERSTOOD = {
    **dict.fromkeys(("if", "then", "else", "elif", "fi"), "a conditional, whose bodies are read"),
    **dict.fromkeys(("case", "esac", "in"), "a case, whose word, patterns, and bodies are read"),
    **dict.fromkeys(("for", "select", "while", "until", "do", "done"), "a loop, whose words and bodies are read"),
    **dict.fromkeys(("{", "}"), "a group, whose body is read"),
    "!": "negates a pipeline's status",
    "time": "times a pipeline, which is read",
}
# set's options (`set -o`): each with the letter that names it in a bundle, or none, and the state in which the guard
# does not read a shell that holds it, "on" or "off", or "" where neither changes what it reads; NOT_READ says why
# for each that has one. An option the table does not name is not read either.
SET_OPTIONS: dict[str, tuple[str, str]] = {
    # Exports each later assignment, so a free name reaches every program's environment, git's among them where
    # names fold case.
    "allexport": ("a", "on"),
    # A brace expansion leaves a word unsettled, on or off.
    "braceexpand": ("B", ""),
    # Line editing, history, and job notices, which a command run as a script never uses.
    **dict.fromkeys(("emacs", "vi", "history", "ignoreeof", "nolog"), ("", "")),
    "histexpand": ("H", ""),
    "notify": ("b", ""),
    # Each ends the shell sooner, runs nothing, or fails a command the guard reads as running either way.
    "errexit": ("e", ""),
    "noexec": ("n", ""),
    "onecmd": ("t", ""),
    "nounset": ("u", ""),
    "noclobber": ("C", ""),
    "pipefail": ("", ""),
    # Traps, which the text cannot set, since trap is not read.
    "errtrace": ("E", ""),
    "functrace": ("T", ""),
    "hashall": ("h", ""),
    "monitor": ("m", ""),
    # A glob leaves a word unsettled, on or off.
    "noglob": ("f", ""),
    "verbose": ("v", ""),
    "keyword": ("k", "on"),
    "posix": ("", "on"),
    "physical": ("P", "on"),
    # Only how a nested bash starts, which the guard does not read.
    "privileged": ("p", ""),
    # Expands PS4 before each command, which the guard reads where the environment's PS4 holds no expansion.
    "xtrace": ("x", ""),
    "interactive-comments": ("", "off"),
    # Git Bash's, which drops a carriage return the guard reads as a blank.
    "igncr": ("", ""),
}
SET_LETTERS = {letter: name for name, (letter, _) in SET_OPTIONS.items() if letter}
# shopt's options (`shopt`): each with the state in which the guard does not read a shell that holds it.
SHOPT_OPTIONS: dict[str, str] = {
    "cdable_vars": "on",
    "lastpipe": "on",
    "cdspell": "on",
    **dict.fromkeys(("compat31", "compat32", "compat40", "compat41", "compat42", "compat43", "compat44"), "on"),
    "extdebug": "on",
    "interactive_comments": "off",
    # The rest change nothing the guard reads: globs and expansions leave a word unsettled whatever they do, the
    # builtins they change (local, the arrays, the subscripts) are not read, aliases the text cannot define, and
    # completion, history, prompts, jobs, mail, and output are an interactive shell's.
    **dict.fromkeys(
        (
            *("dotglob", "extglob", "failglob", "globasciiranges", "globskipdots", "globstar", "nocaseglob"),
            *("nullglob", "extquote", "patsub_replacement", "noexpand_translation", "expand_aliases", "sourcepath"),
            *("execfail", "inherit_errexit", "gnu_errfmt", "shift_verbose", "localvar_inherit", "localvar_unset"),
            *("nocasematch", "array_expand_once", "assoc_expand_once", "autocd", "checkhash", "checkjobs"),
            *("checkwinsize", "cmdhist", "complete_fullquote", "completion_strip_exe", "direxpand", "dirspell"),
            *("force_fignore", "histappend", "histreedit", "histverify", "hostcomplete", "huponexit", "lithist"),
            *("mailwarn", "no_empty_cmd_completion", "progcomp", "progcomp_alias", "promptvars", "varredir_close"),
            *("xpg_echo", "bash_source_fullpath", "login_shell", "restricted_shell"),
        ),
        "",
    ),
}
# The variables Bash itself reads or sets, from its manual's "Shell Variables" and `compgen -v` in a bash started
# with no environment, each with "" where assigning it changes nothing the guard reads, or why it does. A name Bash
# neither reads nor sets is free: the guard needs its value only where an expansion of it stands, which leaves its
# word unsettled.
BASH_VARIABLES: dict[str, str] = {
    **dict.fromkeys(
        (
            *("_", "BASH", "BASHPID", "BASH_ARGC", "BASH_ARGV", "BASH_ARGV0", "BASH_COMMAND"),
            *("BASH_EXECUTION_STRING", "BASH_LINENO", "BASH_MONOSECONDS", "BASH_REMATCH", "BASH_SOURCE"),
            *("BASH_SUBSHELL", "BASH_TRAPSIG", "BASH_VERSINFO", "BASH_VERSION", "CHILD_MAX", "COLUMNS"),
            *("COMP_CWORD", "COMP_KEY", "COMP_LINE", "COMP_POINT", "COMP_TYPE", "COMP_WORDBREAKS", "COMP_WORDS"),
            *("COMPREPLY", "COPROC", "COPROC_PID", "DIRSTACK", "EMACS", "EPOCHREALTIME", "EPOCHSECONDS", "EUID"),
            *("FCEDIT", "FIGNORE", "FUNCNAME", "GLOBIGNORE", "GLOBSORT", "GROUPS", "histchars", "HISTCMD"),
            *("HISTCONTROL", "HISTFILE", "HISTFILESIZE", "HISTIGNORE", "HISTSIZE", "HISTTIMEFORMAT", "HOSTFILE"),
            *("HOSTNAME", "HOSTTYPE", "IFS", "IGNOREEOF", "INPUTRC", "INSIDE_EMACS", "LANG", "LC_ALL"),
            *("LC_COLLATE", "LC_CTYPE", "LC_MESSAGES", "LC_NUMERIC", "LC_TIME", "LINENO", "LINES", "MACHTYPE"),
            *("MAIL", "MAILCHECK", "MAILPATH", "MAPFILE", "OPTARG", "OPTERR", "OPTIND", "OSTYPE", "PIPESTATUS"),
            *("PPID", "PROMPT_COMMAND", "PROMPT_DIRTRIM", "PS0", "PS1", "PS2", "PS3", "RANDOM", "READLINE_ARGUMENT"),
            *("READLINE_LINE", "READLINE_MARK", "READLINE_POINT", "REPLY", "SECONDS", "SHELL", "SHLVL", "SRANDOM"),
            *("TERM", "TIMEFORMAT", "TMOUT", "TMPDIR", "UID", "auto_resume"),
            # Git Bash's own, from the MSYS runtime.
            *platform_support.GIT_BASH_SHELL_VARIABLES,
        ),
        "",
    ),
    "PATH": "chooses the program every later command name runs",
    "EXECIGNORE": "hides programs from the search, so a later command name runs another",
    "BASH_ALIASES": "its elements are aliases, which a later word runs as other text",
    "BASH_CMDS": "its elements choose the program a later command name runs",
    "BASH_LOADABLES_PATH": "chooses the builtins enable loads",
    "BASH_ENV": "names a file a bash runs as it starts",
    "ENV": "names a file a bash in POSIX mode runs as it starts",
    "HOME": "is where cd with no directory and a leading ~ go, and where git reads its global configuration",
    "PWD": "is where a logical cd .. starts from",
    "OLDPWD": "is where cd - goes",
    "CDPATH": "is searched by cd for a relative directory",
    "PS4": "is expanded before each command xtrace traces, running any substitution it holds",
    "SHELLOPTS": "lists set's options, read-only",
    "BASHOPTS": "lists shopt's options, read-only",
    "POSIXLY_CORRECT": "turns on POSIX mode",
    "BASH_COMPAT": "turns on an earlier Bash's behavior",
    "BASH_XTRACEFD": "sends xtrace's output to a descriptor",
    "FUNCNEST": "limits how deep functions nest",
}

# The kinds of expansion a word may hold, by the form bash_syntax gives each, and the parameter-expansion operators
# inside `${ }`: each understood here leaves its word one the text does not settle, which an argument of a program in
# a program may be, and each other is in NOT_READ.
EXPANSIONS_UNDERSTOOD = {
    "$NAME": "a variable's value",
    "${NAME}": "a variable's value",
    "$@": "the positional parameters",
    **dict.fromkeys(("$*", "$#", "$?", "$-", "$$", "$!", "$0", "$1"), "a special parameter's value"),
    "$(": "a command substitution, whose commands are read",
    "`": "a command substitution, whose commands are read",
    "<(": "a process substitution, whose commands are read",
    ">(": "a process substitution, whose commands are read",
    "~": "the home directory, or another user's or a directory stack's",
    "$'": "an ANSI-C string holding an escape",
    "glob": "pathname expansion, *, ?, and [ ]",
    "brace": "brace expansion, {a,b} and {1..3}",
    **dict.fromkeys(
        (
            *("${NAME:-word}", "${NAME-word}", "${NAME:+word}", "${NAME+word}", "${NAME:?word}", "${NAME?word}"),
            *("${#NAME}", "${NAME#word}", "${NAME##word}", "${NAME%word}", "${NAME%%word}", "${NAME/word}"),
            *("${NAME//word}", "${NAME/#word}", "${NAME/%word}", "${NAME^}", "${NAME^^}", "${NAME,}", "${NAME,,}"),
        ),
        "a variable's value, tested or matched against a word",
    ),
    "${NAME:=word}": "assigns a free name, and is its value",
    "${NAME=word}": "assigns a free name, and is its value",
}
# Bash's parameter-expansion operators after a name inside `${ }`, longest first, by the form they make.
PARAMETER_OPERATORS = (
    (":=", "${NAME:=word}"),
    (":-", "${NAME:-word}"),
    (":+", "${NAME:+word}"),
    (":?", "${NAME:?word}"),
    ("=", "${NAME=word}"),
    ("-", "${NAME-word}"),
    ("+", "${NAME+word}"),
    ("?", "${NAME?word}"),
    ("##", "${NAME##word}"),
    ("#", "${NAME#word}"),
    ("%%", "${NAME%%word}"),
    ("%", "${NAME%word}"),
    ("//", "${NAME//word}"),
    ("/#", "${NAME/#word}"),
    ("/%", "${NAME/%word}"),
    ("/", "${NAME/word}"),
    ("^^", "${NAME^^}"),
    ("^", "${NAME^}"),
    (",,", "${NAME,,}"),
    (",", "${NAME,}"),
    (":", "${NAME:offset}"),
    ("@", "${NAME@op}"),
    ("[", "${NAME[subscript]}"),
)
PARAMETER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[@*#?$!-]")
GLOB = re.compile(r"[*?]|\[.*\]")
BRACE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")

# The PowerShell AST node types (System.Management.Automation.Language, by reflection) the guard understands: a
# list of pipelines of commands, joined by && and ||, whose elements are constants, strings without expansions, and
# parameters, with redirections. Every other type is in NOT_READ.
POWERSHELL_UNDERSTOOD = {
    "ScriptBlockAst": "the command's text, whose statements are read",
    "NamedBlockAst": "the block the statements stand in",
    "PipelineAst": "a pipeline of commands",
    "PipelineChainAst": "pipelines joined by && or ||",
    "CommandAst": "a command and its elements",
    "CommandParameterAst": "a parameter, written as a word",
    "CommandExpressionAst": "an expression standing as a command, read by what it holds",
    "StringConstantExpressionAst": "a literal word or string",
    "ConstantExpressionAst": "a number",
    "ExpandableStringExpressionAst": "a double-quoted string, whose expansions leave its word unsettled",
    "AssignmentStatementAst": "an assignment, understood to a plain variable PowerShell neither sets nor reads itself",
    "MemberExpressionAst": "a property's value, understood for an instance's member a constant names, never a "
    "static one",
    **dict.fromkeys(
        ("ConvertExpressionAst", "TypeExpressionAst", "TypeConstraintAst"),
        "a cast or a type, understood for a type POWERSHELL_CASTS names",
    ),
    "ScriptBlockExpressionAst": "a script block, understood as an argument of a command "
    "POWERSHELL_BLOCK_COMMANDS names, "
    "whose commands are read",
    **dict.fromkeys(
        ("IfStatementAst", "ForEachStatementAst", "StatementBlockAst", "BreakStatementAst", "ContinueStatementAst"),
        "control flow, whose conditions and bodies are read, and a foreach's variable as an assignment",
    ),
    "VariableExpressionAst": "a variable's value, which leaves its word one the text does not settle",
    **dict.fromkeys(
        (
            *("ParenExpressionAst", "SubExpressionAst", "ArrayExpressionAst", "ArrayLiteralAst"),
            *("BinaryExpressionAst", "HashtableAst", "IndexExpressionAst", "TernaryExpressionAst"),
        ),
        "computes a value from what it holds, each command in it read, which leaves its word unsettled",
    ),
    "FileRedirectionAst": "a redirection to a file",
    "MergingRedirectionAst": "a redirection of one stream into another, as 2>&1",
}
# The PowerShell commands the guard understands, by every name and alias PowerShell gives them, lower-cased, each with
# the cmdlet it stands for. A program by any other name is read as in Bash, but for a name PowerShell keeps for its
# own commands (powershell_command), which is not read.
POWERSHELL_LOCATION = {**dict.fromkeys(("set-location", "cd", "chdir", "sl"), "set-location")}
POWERSHELL_PUSH = {**dict.fromkeys(("push-location", "pushd"), "push-location")}
POWERSHELL_POP = {**dict.fromkeys(("pop-location", "popd"), "pop-location")}
POWERSHELL_COMMANDS = {
    **POWERSHELL_LOCATION,
    **POWERSHELL_PUSH,
    **POWERSHELL_POP,
    **dict.fromkeys(("get-childitem", "ls", "dir", "gci"), "get-childitem"),
    **dict.fromkeys(("get-content", "cat", "gc", "type"), "get-content"),
    **dict.fromkeys(("select-string", "sls"), "select-string"),
    **dict.fromkeys(("select-object", "select"), "select-object"),
    **dict.fromkeys(("sort-object", "sort"), "sort-object"),
    **dict.fromkeys(("measure-object", "measure"), "measure-object"),
    **dict.fromkeys(("write-output", "echo", "write"), "write-output"),
    **dict.fromkeys(("get-location", "pwd", "gl"), "get-location"),
    **dict.fromkeys(("get-item", "gi"), "get-item"),
    **dict.fromkeys(("get-command", "gcm"), "get-command"),
    **dict.fromkeys(("get-process", "gps", "ps"), "get-process"),
    **dict.fromkeys(("format-table", "ft"), "format-table"),
    **dict.fromkeys(("foreach-object", "%", "foreach"), "foreach-object"),
    **dict.fromkeys(("where-object", "?", "where"), "where-object"),
    **dict.fromkeys(("group-object", "group"), "group-object"),
    **dict.fromkeys(("format-list", "fl"), "format-list"),
    **dict.fromkeys(("start-sleep", "sleep"), "start-sleep"),
    **dict.fromkeys(("remove-item", "rm", "del", "erase", "rd", "ri", "rmdir"), "remove-item"),
    **dict.fromkeys(("copy-item", "cp", "copy", "cpi"), "copy-item"),
    **dict.fromkeys(("move-item", "mv", "move", "mi"), "move-item"),
    **dict.fromkeys(("new-item", "ni"), "new-item"),
    **dict.fromkeys(("resolve-path", "rvpa"), "resolve-path"),
    **{
        name: name
        for name in (
            *("write-host", "out-string", "out-null", "test-path", "join-path", "split-path", "get-date"),
            *("get-filehash", "convertfrom-json", "convertto-json", "set-content", "add-content", "out-file"),
            "mkdir",
        )
    },
}
# The variables PowerShell sets or reads itself, by Get-Variable in a pwsh started with no profile and by its
# documentation of the automatic and preference variables, lower-cased: an assignment to one, as to
# $PSDefaultParameterValues or $PSNativeCommandArgumentPassing, changes how a later command runs.
POWERSHELL_VARIABLES = frozenset(
    name.lower()
    for name in (
        *("$", "?", "^", "_", "args", "ConfirmPreference", "DebugPreference", "EnabledExperimentalFeatures", "Error"),
        *("ErrorActionPreference", "ErrorView", "Event", "EventArgs", "EventSubscriber", "ExecutionContext", "false"),
        *("foreach", "FormatEnumerationLimit", "HOME", "Host", "InformationPreference", "input", "IsCoreCLR"),
        *("IsLinux", "IsMacOS", "IsWindows", "LASTEXITCODE", "LogCommandHealthEvent", "LogCommandLifecycleEvent"),
        *("LogEngineHealthEvent", "LogEngineLifecycleEvent", "LogProviderHealthEvent", "LogProviderLifecycleEvent"),
        *("Matches", "MaximumHistoryCount", "MyInvocation", "NestedPromptLevel", "null", "OFS", "OutputEncoding"),
        *("PID", "PROFILE", "ProgressPreference", "PSBoundParameters", "PSCmdlet", "PSCommandPath", "PSCulture"),
        *("PSDebugContext", "PSDefaultParameterValues", "PSEdition", "PSEmailServer", "PSHOME", "PSItem"),
        *("PSModuleAutoLoadingPreference", "PSNativeCommandArgumentPassing"),
        *("PSNativeCommandUseErrorActionPreference", "PSScriptRoot", "PSSenderInfo", "PSSessionApplicationName"),
        *("PSSessionConfigurationName", "PSSessionOption", "PSStyle", "PSUICulture", "PSVersionTable", "PWD"),
        *("sender", "ShellId", "StackTrace", "switch", "this", "Transcript", "true", "VerbosePreference"),
        *("WarningPreference", "WhatIfPreference"),
    )
)
# The commands POWERSHELL_COMMANDS names that take a path, on which one on a provider other than the file system is
# not read.
POWERSHELL_PATH_COMMANDS = frozenset(
    {
        *("set-location", "push-location", "get-childitem", "get-content", "get-item", "remove-item", "copy-item"),
        *("move-item", "new-item", "set-content", "add-content", "out-file", "test-path", "resolve-path", "join-path"),
        *("split-path", "get-filehash", "mkdir"),
    }
)
# The types a cast or a type expression may name, lower-cased as PowerShell's accelerators and their full names:
# converting to one runs no code the text does not show.
POWERSHELL_CASTS = frozenset(
    {
        *("string", "int", "int32", "bool", "boolean", "array", "object[]", "hashtable", "pscustomobject"),
        *("psobject", "datetime", "version", "collections.hashtable", "management.automation.pscustomobject"),
    }
)
# The commands whose script-block arguments the guard reads: each runs its block in the session, on its input.
POWERSHELL_BLOCK_COMMANDS = frozenset(
    {"foreach-object", "where-object", "sort-object", "select-object", "group-object"}
)
# The names PowerShell gives a command before it looks for a program, each held to the shell: every alias Get-Alias
# lists in a pwsh started with no profile, each function PowerShell itself defines with no verb in its name (as
# Get-Command lists those of no module), and every verb Get-Verb lists, which begins the name of each cmdlet and
# function. A command by such a name that POWERSHELL_COMMANDS does not hold is not read; any other name is a program.
POWERSHELL_ALIASES = frozenset(
    name.lower()
    for name in (
        *("%", "?", "ac", "cat", "cd", "chdir", "clc", "clear", "clhy", "cli", "clp", "cls", "clv", "cnsn", "compare"),
        *("copy", "cp", "cpi", "cpp", "cvpa", "dbp", "del", "diff", "dir", "dnsn", "ebp", "echo", "epal", "epcsv"),
        *("erase", "etsn", "exsn", "fc", "fhx", "fl", "foreach", "ft", "fw", "gal", "gbp", "gc", "gci", "gcm", "gcs"),
        *("gdr", "gerr", "ghy", "gi", "gin", "gjb", "gl", "gm", "gmo", "gp", "gps", "gpv", "group", "gsn", "gsv"),
        *("gtz", "gu", "gv", "h", "history", "icm", "iex", "ihy", "ii", "ipal", "ipcsv", "ipmo", "irm", "iwr", "kill"),
        *("lp", "ls", "man", "md", "measure", "mi", "mount", "move", "mp", "mv", "nal", "ndr", "ni", "nmo", "nsn"),
        *("nv", "ogv", "oh", "popd", "ps", "pushd", "pwd", "r", "rbp", "rcjb", "rcsn", "rd", "rdr", "ren", "ri"),
        *("rjb", "rm", "rmdir", "rmo", "rni", "rnp", "rp", "rsn", "rv", "rvpa", "sajb", "sal", "saps", "sasv", "sbp"),
        *("scb", "select", "set", "shcm", "si", "sl", "sleep", "sls", "sort", "sp", "spjb", "spps", "spsv", "start"),
        *("stz", "sv", "tee", "type", "where", "wjb", "write"),
    )
)
POWERSHELL_FUNCTIONS = frozenset(
    name.lower()
    for name in (
        *("cd..", "cd\\", "cd~", "help", "mkdir", "more", "oss", "Pause", "prompt", "TabExpansion2", "Configuration"),
        *("ImportSystemModules", "PSConsoleHostReadLine"),
        # pwsh off Windows: replaces the session with the program its words name, a runner the guard does not read.
        "exec",
        *(f"{letter}:" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    )
)
POWERSHELL_VERBS = frozenset(
    name.lower()
    for name in (
        *("Add", "Approve", "Assert", "Backup", "Block", "Build", "Checkpoint", "Clear", "Close", "Compare"),
        *("Complete", "Compress", "Confirm", "Connect", "Convert", "ConvertFrom", "ConvertTo", "Copy", "Debug", "Deny"),
        *("Deploy", "Disable", "Disconnect", "Dismount", "Edit", "Enable", "Enter", "Exit", "Expand", "Export"),
        *("Find", "Format", "Get", "Grant", "Group", "Hide", "Import", "Initialize", "Install", "Invoke", "Join"),
        *("Limit", "Lock", "Measure", "Merge", "Mount", "Move", "New", "Open", "Optimize", "Out", "Ping", "Pop"),
        *("Protect", "Publish", "Push", "Read", "Receive", "Redo", "Register", "Remove", "Rename", "Repair"),
        *("Request", "Reset", "Resize", "Resolve", "Restart", "Restore", "Resume", "Revoke", "Save", "Search"),
        *("Select", "Send", "Set", "Show", "Skip", "Split", "Start", "Step", "Stop", "Submit", "Suspend", "Switch"),
        *("Sync", "Test", "Trace", "Unblock", "Undo", "Uninstall", "Unlock", "Unprotect", "Unpublish", "Unregister"),
        *("Update", "Use", "Wait", "Watch", "Write"),
    )
)
# A PowerShell path on a provider other than the file system: a drive whose name is more than one letter, such as
# env:, alias:, or HKLM:, or a provider-qualified path such as Environment::.
PROVIDER_PATH = re.compile(r"^(?:(?:[\w.]+\\)?[A-Za-z]\w*::|[A-Za-z][\w-]+:)")

# Every catalogue entry the guard does not read, with why: a command holding one is refused while a guarded hub is in
# view. Keyed by its catalogue and its name as that catalogue lists it.
NOT_READ = {
    # Bash builtins.
    **{f"builtin {name}": "runs a file's commands, which the guard does not read" for name in (".", "source")},
    "builtin alias": "defines a name a later word runs as other text",
    "builtin unalias": "changes what a later word runs",
    **{f"builtin {name}": "job control, which a command run as a script does not use" for name in ("bg", "fg")},
    "builtin disown": "job control, which a command run as a script does not use",
    "builtin suspend": "job control, which a command run as a script does not use",
    "builtin logout": "a login shell's exit, which a command run as a script does not use",
    "builtin bind": "binds a key to a command, which -x runs",
    "builtin builtin": "runs the builtin its next word names",
    "builtin enable": "loads a builtin or turns one off, which changes what a later name runs",
    "builtin eval": "runs text as commands",
    "builtin exec": "replaces the shell with the command its words name, or changes its file descriptors",
    "builtin fc": "runs commands again from the history",
    "builtin hash": "makes a name run another program, with -p",
    "builtin history": "edits the history, from which fc runs commands",
    "builtin trap": "runs text as commands when a signal or a command fires it",
    "builtin let": "evaluates arithmetic, which may assign any variable and run a subscript's substitution",
    "builtin getopts": "assigns variables from the positional parameters",
    "builtin unset": "removes a variable or a function, by a name the guard does not follow",
    "builtin dirs": "prints or clears the directory stack popd reads",
    **{
        f"builtin {name}": "declares a variable: exports it, makes a name reference or an integer of it, which change "
        "what a later command sees or evaluates"
        for name in ("declare", "typeset", "local", "export", "readonly")
    },
    **{f"builtin {name}": "assigns an array, and runs a callback with -C" for name in ("mapfile", "readarray")},
    **{
        f"builtin {name}": "completion, whose -C and -F run a command or a function"
        for name in ("compgen", "complete", "compopt")
    },
    # Bash reserved words, and the constructs the grammar reads without one.
    "keyword [[": "a conditional expression, whose arithmetic comparisons and -v evaluate arithmetic",
    "keyword ]]": "ends a conditional expression",
    "keyword function": "defines a function, which may take any command name",
    "keyword coproc": "runs a command as a coprocess, in the background with its descriptors kept",
    "construct function definition": "defines a function, which may take any command name, git's among them",
    "construct arithmetic command": "(( )) evaluates arithmetic, which may assign any variable",
    "construct arithmetic for": "for (( )) evaluates arithmetic, which may assign any variable",
    "construct element assignment": "NAME[SUBSCRIPT]= evaluates its subscript as arithmetic",
    "construct assignment before git": "an assignment before git may change where it acts or what configuration "
    "it reads",
    # set's and shopt's options, in the state the tables refuse.
    "set -o allexport": "exports each later assignment, so a name reaches git's environment",
    "set -o keyword": "puts each assignment word after a command's name into its environment, as GIT_DIR=<dir> after "
    "git",
    "set -o posix": "changes how assignments before special builtins, aliases, and expansions are read",
    "set -o physical": "cd resolves symbolic links, where the guard reads a directory as Bash's logical cd does",
    "set -o interactive-comments": "off, a word starting with # is no comment",
    "shopt cdable_vars": "a cd to a name that is no directory takes the variable by that name as one",
    "shopt lastpipe": "the last command of a pipeline runs in the shell itself, so a cd there moves it",
    "shopt cdspell": "cd corrects a misspelled directory to another",
    **{f"shopt {name}": "an earlier Bash's behavior" for name in ("compat31", "compat32", "compat40", "compat41")},
    **{f"shopt {name}": "an earlier Bash's behavior" for name in ("compat42", "compat43", "compat44")},
    "shopt extdebug": "runs the debugger's profile, and lets a DEBUG trap skip or replace commands",
    "shopt interactive_comments": "off, a word starting with # is no comment",
    # Expansions.
    "expansion $((": "arithmetic, which may assign any variable and run a subscript's substitution",
    "expansion $[": "arithmetic, which may assign any variable and run a subscript's substitution",
    "expansion NAME=( )": "an array assignment, which evaluates each element's subscript as arithmetic",
    "expansion ${NAME[subscript]}": "a subscript, evaluated as arithmetic",
    "expansion ${NAME:offset}": "an offset and length, evaluated as arithmetic",
    "expansion ${!NAME}": "indirection, which evaluates a subscript the value names, or lists names",
    "expansion ${NAME@op}": "a transformation, whose @P expands a prompt and runs what it holds",
    "expansion ${ command; }": "runs a command in the shell itself, Bash 5.3's",
    "expansion ${| command; }": "runs a command in the shell itself and takes REPLY, Bash 5.3's",
    "expansion nested ${": "an expansion inside another's word, which the guard does not read apart",
    "expansion ${unread}": "a braced expansion of a form the guard does not read",
    "expansion assigning": "assigns a variable Bash or git reads, or one the environment holds",
    # PowerShell AST node types.
    **{
        f"PowerShell {name}": "computes a value the text does not settle"
        for name in ("AttributedExpressionAst", "UsingExpressionAst")
    },
    "PowerShell UnaryExpressionAst": "++ and -- assign the variable they hold, which may be the environment's",
    **{
        f"PowerShell {name}": "reaches a member, which may run any code, git among it"
        for name in ("InvokeMemberExpressionAst", "BaseCtorInvokeMemberExpressionAst")
    },
    **{
        f"PowerShell {name}": "defines a function, a type, a configuration, or a parameter, or loads a module"
        for name in (
            *("FunctionDefinitionAst", "TypeDefinitionAst", "FunctionMemberAst", "PropertyMemberAst"),
            *("ConfigurationDefinitionAst", "DynamicKeywordStatementAst", "UsingStatementAst", "ParamBlockAst"),
            *("ParameterAst", "AttributeAst", "NamedAttributeArgumentAst"),
        )
    },
    **{
        f"PowerShell {name}": "a statement the guard does not read in PowerShell"
        for name in (
            *("SwitchStatementAst", "ForStatementAst", "WhileStatementAst", "DoWhileStatementAst"),
            *("DoUntilStatementAst", "TryStatementAst", "CatchClauseAst", "TrapStatementAst", "DataStatementAst"),
            *("BlockStatementAst", "ReturnStatementAst", "ExitStatementAst", "ThrowStatementAst"),
        )
    },
    **{
        f"PowerShell {name}": "text PowerShell could not parse, which runs nothing"
        for name in ("ErrorStatementAst", "ErrorExpressionAst")
    },
}

GIT_NAMES = {"git"}
# How deep a substitution and a shell alias are followed; deeper is a command the guard does not read.
NESTING_LIMIT = 4
POWERSHELL_READER = Path(__file__).with_name("hub_guard-powershell.ps1")
# How long the guard reads and judges one command, pwsh included, before it is a command the guard cannot read in
# time. The hook's own 30-second timeout in .claude/settings.json blocks a command it cuts short (onFailure), so this
# leaves room for starting Python and writing the decision; the hub lookups before and after reading, and each git
# call they start, are held to it too (lookups_within).
READING_SECONDS = 15
# How much of the reader's output is kept: far more than any command a tool call carries, and a reading cut short is
# not JSON, so it is a command the guard cannot read rather than one read in part.
POWERSHELL_OUTPUT_LIMIT = 1 << 22
# How many paths a command names are looked up for a guarded hub when none is in view and it holds a part the guard
# does not read.
NAMED_PATH_LIMIT = 32
DENY_REASON = (
    "This checkout is the hub: it stays on main and is never edited, so parallel sessions cannot carry each "
    "other's changes (docs/parallel-sessions.md). Create a worktree with "
    "`python tools/worktrees.py new <kind> <name>`, enter it, and work there."
)
PLACE_IT = "Write the directory into the command itself, as `git -C <dir>` or `cd <dir> &&`, and run it again."
REWRITE_IT = (
    "Write it as plain commands with literal words, running each program by its name rather than through another "
    "that runs it (RUNNERS in tools/hub_guard.py), and run it again; the guard's contract in "
    "docs/parallel-sessions.md lists what it reads."
)


# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "powershell_events": "starts the pwsh platform_support.find_pwsh located on the session's PATH",
}


GIT_STATUS_ALLOWED = {
    "guard_enabled": (
        "reports git's reason to the user: exit 1 is git's answer that the opt-in is unset, and any other failure "
        "skips the guard with a note naming git's stderr, as the guard's contract says"
    ),
    "git_builtins": "fails closed: an empty list makes every subcommand a possible alias, which is read and judged",
    "hub_protected": (
        "fails closed: a check-ignore git cannot answer counts the path as tracked, so the edit is refused"
    ),
    "Judge.alias_refusal": (
        "fails closed: any failure but exit 1 refuses the call; exit 1 is git's answer that no such alias is set, or "
        "that the name is one no alias can have, which git cannot run either"
    ),
}


def same_place(left: Path | None, right: Path | None) -> bool:
    return left is right or (left is not None and right is not None and same_path(left, right))


def guard_enabled(hub: Path) -> bool:
    """Reads the opt-in from the repository's own configuration file only: --local passes over command-line
    configuration, which GIT_CONFIG_PARAMETERS and GIT_CONFIG_COUNT carry and which would otherwise outrank it. git
    exits 1 for a setting that is not there; any other failure, such as a value that is no boolean, is a hub it cannot
    look up, allowed with a note rather than read as unguarded unseen."""
    result = git(hub, "config", "--local", "--type=bool", "--get", GUARD_SETTING)
    if result.returncode not in (0, 1):
        said = " ".join(result.stderr.split()) or f"git config exited with {result.returncode}"
        print(f"hub guard skipped: the opt-in of {hub} could not be read: {said}", file=sys.stderr)
    return result.returncode == 0 and output_line(result.stdout) == "true"


@functools.cache
def guarded_hub_at(path: Path) -> Path | None:
    """The guarded hub git acts on from path: the one whose checkout holds it, or, inside a git directory, where git
    has no work tree and acts on that directory's repository, the one whose own git directory it is; None when it is
    a worktree, another repository, or none."""
    checkout = checkout_of(path)
    if checkout is None:
        repository = git_directory_holding(path)
        return None if repository is None else guarded_hub_of_repository(repository)
    if not checkout.is_hub or not guard_enabled(checkout.hub):
        return None
    return checkout.hub


def git_directory_holding(path: Path) -> Path | None:
    """The git directory git finds from path when path lies inside one; None in a work tree, outside every
    repository, and where nothing of path exists.

    Raises GitError when git answers otherwise with an error, or with lines other than the two asked for, so a start
    git cannot read is never read as one outside every repository."""
    directory = worktrees.existing_directory(path)
    if directory is None:
        return None
    arguments = ["rev-parse", "--path-format=absolute", "--is-inside-git-dir", "--git-dir"]
    try:
        output = CLIENT.output(arguments, directory=directory, timeout=lookup_timeout())
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    lines = output_lines(output)
    if len(lines) != 2:
        raise GitError(f"git rev-parse answered {len(lines)} lines for the git directory holding {path}, not two")
    return Path(lines[1]) if lines[0] == "true" else None


@functools.cache
def guarded_hub_of_repository(path: Path) -> Path | None:
    """The guarded hub whose own git directory path names, as --git-dir or GIT_DIR does; None for a worktree's, another
    repository's, or a path git answers names no repository, where git runs nothing.

    Raises GitError when git cannot tell, or the path or the folder above it cannot be looked at, so each caller
    refuses rather than read the path as no guarded hub's.
    """
    directory = path if worktrees.is_folder(path) else path.parent
    if not worktrees.is_folder(directory):
        return None
    arguments = ["--git-dir", str(path), "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"]
    try:
        output = CLIENT.output(arguments, directory=directory, timeout=lookup_timeout())
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    lines = output_lines(output)
    if len(lines) != 2:
        raise GitError(f"git rev-parse answered {len(lines)} lines for the git directory {path}, not two")
    if not same_path(Path(lines[0]), Path(lines[1])):
        return None
    common = Path(lines[1])
    if os.path.normcase(common.name) != ".git":
        return None
    hub = guarded_hub_at(common.parent)
    return hub if hub is not None and same_path(hub, common.parent) else None


def resolve(value: str, cwd: Path) -> Path:
    path = Path(platform_support.from_shell_path(value))
    return path if path.is_absolute() else cwd / path


@functools.cache
def git_builtins() -> frozenset[str]:
    """The subcommands git runs itself, which no alias can stand for; empty when git cannot list them."""
    result = git(Path(__file__).resolve().parent, "--list-cmds=builtins")
    return frozenset(output_lines(result.stdout)) if result.returncode == 0 else frozenset()


def hub_protected(path: Path) -> bool:
    """Whether path lies in the tracked surface of a guarded hub or anywhere in its git directory; ignored files such
    as local settings do not.

    A checkout or git directory git answers with an error for may be a guarded hub's, so it is protected; where git
    did not run or finish, the GitError goes to the hook, which allows the edit with a note."""
    normalized = Path(os.path.normpath(path))
    for directory in (normalized, *normalized.parents):
        if os.path.normcase(directory.name) == ".git" and directory.is_dir():
            try:
                return guarded_hub_of_repository(directory) is not None
            except GitError as exc:
                if not exc.answered:
                    raise
                return True

    try:
        hub = guarded_hub_at(normalized)
    except GitError as exc:
        if not exc.answered:
            raise
        return True
    if hub is None:
        return False
    return git(hub, "check-ignore", "-q", "--no-index", str(normalized)).returncode != 0


def hubs_in_view(cwd: Path, environment: Mapping[str, str]) -> set[Path]:
    """The guarded hubs a session can reach: the hub of its own directory's checkout, a worktree's included, and of
    its project's, or the guarded hub whose git directory holds one. A directory whose checkout git cannot read is
    noted and passed over, so the others still count."""
    directories = [cwd]
    project = environment.get("CLAUDE_PROJECT_DIR")
    if project:
        directories.append(Path(platform_support.from_shell_path(project)))
    found: set[Path] = set()
    for directory in directories:
        try:
            checkout = checkout_of(directory)
            hub = guarded_hub_at(directory) if checkout is None else checkout.hub
        except GitError as exc:
            print(f"hub guard could not look up the hub of {directory}: {exc}", file=sys.stderr)
            continue
        if hub is not None and guard_enabled(hub):
            found.add(hub)
    return found


# ---------------------------------------------------------------------------------------------------------------
# What the readers produce


@dataclass(frozen=True)
class Place:
    """Where one git call acts, as far as the text settles it.

    start is where git starts, after each -C; tree is the work tree whose files it writes; repository is the git
    directory whose HEAD and index it writes when one is named, and otherwise git finds it from start. None is a
    place the text does not settle. aliases holds the alias.<name> settings given on the command line, or is None when
    command-line configuration the text does not settle may hold any.
    """

    start: Path | None
    tree: Path | None
    repository: Path | None = None
    repository_named: bool = False
    aliases: Mapping[str, str | None] | None = field(default_factory=dict)


@dataclass(frozen=True)
class GitCall:
    place: Place
    subcommand: str | None  # None when the text does not settle it
    arguments: list[str | None]  # None for a word the text does not settle
    # git's own options as read before the subcommand, from which an alias's expansion goes on reading.
    options: GitOptions | None = None
    # gh for a gh command that runs git itself to check a branch out.
    program: str = "git"

    @property
    def directory(self) -> Path | None:
        return self.place.tree


@dataclass
class Reading:
    """What the guard read from one command: the git calls it makes, in order, the files its redirections write,
    the parts of it the guard does not read, which are refused while a guarded hub is in view, the paths it names,
    and notes on what it passed over."""

    environment: Mapping[str, str]
    text: str = ""
    calls: list[GitCall] = field(default_factory=list)
    writes: list[Path] = field(default_factory=list)
    unread: list[str] = field(default_factory=list)
    named: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    context_hubs: set[Path] = field(default_factory=set)
    # When the guard's reading time ends, by reading_clock.
    deadline: float = math.inf

    def cannot_read(self, what: str) -> None:
        """A part of the command the guard does not read: refused while a guarded hub is in view."""
        if what not in self.unread:
            self.unread.append(what)

    def time_left(self) -> float:
        """The seconds left of the reading time; past it, raises ReadingTimeout, which the hook refuses as a failure
        of the guard's own when the command names git while a guarded hub is in view."""
        return time_left(self.deadline)


class ReadingTimeout(Exception):
    """The guard's reading time ended before the command was read and judged."""


def reading_clock() -> float:
    return time.monotonic()


def time_left(deadline: float) -> float:
    """The seconds until deadline, by reading_clock; past it, raises ReadingTimeout."""
    left = deadline - reading_clock()
    if left <= 0:
        raise ReadingTimeout(f"the guard did not finish reading it within {READING_SECONDS} seconds")
    return left


@dataclass
class LookupBound:
    """When the hub lookups must end, by reading_clock: the reading deadline of the command being read and judged,
    while it is, and no bound otherwise, as for a file event, whose git calls each keep GUARD_GIT_TIMEOUT_SECONDS."""

    deadline: float = math.inf


LOOKUPS = LookupBound()


def lookup_timeout() -> float:
    """How long one git call of a hub lookup may take: GUARD_GIT_TIMEOUT_SECONDS, or the reading time left when that
    is less; past the reading time, raises ReadingTimeout, so a lookup starts no git call the deadline has passed."""
    return min(time_left(LOOKUPS.deadline), GUARD_GIT_TIMEOUT_SECONDS)


@contextlib.contextmanager
def lookups_within(deadline: float) -> Iterator[None]:
    """Holds every hub lookup made inside the block, and each git call it starts, to deadline."""
    outer, LOOKUPS.deadline = LOOKUPS.deadline, deadline
    try:
        yield
    finally:
        LOOKUPS.deadline = outer


def git(cwd: Path, *arguments: str) -> GitResult:
    """Every git call the guard makes, run as tools/worktrees.py runs one, within what lookup_timeout leaves it."""
    return worktrees.git(cwd, *arguments, timeout=lookup_timeout())


def checkout_of(path: Path) -> Checkout | None:
    """The checkout holding path, as tools/worktrees.py finds it, its git call within what lookup_timeout leaves it."""
    return worktrees.checkout_of(path, lookup_timeout())


# A word is its text, or None when the text does not settle its value (an unset variable, a $( ) result).
Word = str | None
# The variables a shell has set, by name; a value is None when the text does not settle it.
Variables = Mapping[str, Word]


def moved(directory: Path | None, target: Word) -> Path | None:
    """directory after moving to target; an absolute target settles it even when directory is unknown."""
    if target is None:
        return None
    path = Path(platform_support.from_shell_path(target))
    if path.is_absolute():
        return path
    return None if directory is None else directory / path


def command_name(word: Word) -> str | None:
    """The lower-cased name a command is called by, without its directory or .exe; None when it is computed."""
    return None if word is None else platform_support.program_name(Path(word).name).lower()


def folded(name: str) -> str:
    """The name git reads a variable by: in any case on Windows, where git takes git_dir as GIT_DIR, and only as
    written elsewhere."""
    return name.upper() if platform_support.ENVIRONMENT_NAMES_FOLD_CASE else name


def is_tracked(name: str) -> bool:
    """Whether git reads this variable to choose where it acts or which aliases it has."""
    canonical = folded(name)
    return (
        canonical in LOCATION_VARIABLES
        or canonical in CONFIGURATION_VARIABLES
        or bool(CONFIGURATION_PAIR.fullmatch(canonical))
    )


def tracked_variables(variables: Variables) -> dict[str, Word]:
    return {name: value for name, value in variables.items() if is_tracked(name)}


def git_variables(variables: Variables, notes: list[str]) -> dict[str, Word]:
    """The tracked variables git reads, by the name git reads them by.

    On Windows two spellings of one name can both reach git; git takes the upper-case one, so a conflict is judged by
    it, with a note, and when no upper-case spelling is among them their value is not settled.
    """
    spellings: dict[str, dict[str, Word]] = {}
    for name, value in tracked_variables(variables).items():
        spellings.setdefault(folded(name), {})[name] = value
    values: dict[str, Word] = {}
    for canonical, found in spellings.items():
        distinct = set(found.values())
        if len(distinct) == 1:
            values[canonical] = distinct.pop()
        elif canonical in found:
            values[canonical] = found[canonical]
            others = ", ".join(sorted(name for name in found if name != canonical))
            notes.append(
                f"{canonical} and {others} name different values; git reads {canonical}, so it was judged by it"
            )
        else:
            values[canonical] = None
    return values


def inherited_variables(environment: Mapping[str, str]) -> dict[str, Word]:
    """The tracked variables a shell starts with: those the guard's own process was given."""
    return {name: value for name, value in environment.items() if is_tracked(name)}


# ---------------------------------------------------------------------------------------------------------------
# The git reader


@dataclass
class Configuration:
    """Command-line configuration a git call sees: GIT_CONFIG_PARAMETERS, GIT_CONFIG_COUNT and its pairs, then each -c
    and --config-env, the later winning. settled is False once an entry's key is not settled by the text."""

    entries: list[tuple[str, Word]] = field(default_factory=list)
    settled: bool = True

    def add(self, setting: Word) -> None:
        if setting is None:
            self.settled = False
            return
        key, has_value, value = setting.partition("=")
        self.entries.append((key.lower(), value if has_value else "true"))

    def value(self, key: str) -> tuple[bool, Word]:
        """Whether key is set on the command line, and its value; a configuration not settled may set any key."""
        found = [value for name, value in self.entries if name == key]
        if found:
            return True, found[-1]
        return (False, None) if self.settled else (True, None)

    def aliases(self) -> dict[str, str | None] | None:
        if not self.settled:
            return None
        return {name.removeprefix("alias."): value for name, value in self.entries if name.startswith("alias.")}


def environment_configuration(values: Mapping[str, Word]) -> Configuration:
    """The command-line configuration git's environment carries, in the order git reads it."""
    configuration = Configuration()
    if "GIT_CONFIG_PARAMETERS" in values:
        parameters = values["GIT_CONFIG_PARAMETERS"]
        try:
            settings = shlex.split(parameters) if parameters is not None else None
        except ValueError:
            settings = None
        for setting in settings if settings is not None else [None]:
            configuration.add(setting)
    if "GIT_CONFIG_COUNT" in values:
        count = values["GIT_CONFIG_COUNT"]
        # git reads the count as an int, so six digits is generous, and a longer one would keep the loop below going
        # for practically ever; a count past it is one the guard cannot settle.
        entries = None if count is None else whole_number.parse(count.strip(), limit=6)
        if entries is None:
            configuration.settled = False
            return configuration
        for index in range(entries):
            key, value = values.get(f"GIT_CONFIG_KEY_{index}"), values.get(f"GIT_CONFIG_VALUE_{index}")
            configuration.add(None if key is None or value is None else f"{key}={value}")
    for key, _ in configuration.entries:
        if key.startswith(("include.", "includeif.")):
            configuration.settled = False
    return configuration


@dataclass
class GitOptions:
    """git's own options as read so far: where it starts, the git directory and work tree named, and configuration."""

    start: Path | None
    git_dir: tuple[bool, Word]
    work_tree: tuple[bool, Word]
    configuration: Configuration

    @classmethod
    def initial(cls, cwd: Path | None, values: Mapping[str, Word], opaque: bool) -> GitOptions:
        if opaque:
            # Variables the text does not show may name any repository or work tree.
            return cls(cwd, (True, None), (True, None), Configuration(settled=False))
        return cls(
            cwd,
            ("GIT_DIR" in values, values.get("GIT_DIR")),
            ("GIT_WORK_TREE" in values, values.get("GIT_WORK_TREE")),
            environment_configuration(values),
        )

    def copy(self) -> GitOptions:
        return replace(self, configuration=replace(self.configuration, entries=list(self.configuration.entries)))

    def place(self) -> Place:
        """Relative paths are read from where git finally starts, as git reads them once every -C has run."""
        named_dir, git_dir = self.git_dir
        repository = (
            None if not named_dir or (self.start is None and not is_absolute(git_dir)) else moved(self.start, git_dir)
        )
        named_tree, work_tree = self.work_tree
        configured, core_worktree = self.configuration.value("core.worktree")
        if named_tree:
            tree = moved(self.start, work_tree)
        elif configured:
            # A relative core.worktree is read from the git directory, which only a named one settles.
            base = repository if named_dir else None
            tree = moved(base, core_worktree)
        else:
            tree = self.start
        return Place(self.start, tree, repository, named_dir, self.configuration.aliases())


def is_absolute(value: Word) -> bool:
    return value is not None and Path(platform_support.from_shell_path(value)).is_absolute()


def read_git_options(arguments: list[Word], options: GitOptions, reading: Reading) -> int | None:
    """Reads git's own options into options; returns the index of the subcommand, or None when git runs none."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None or not word.startswith("-"):
            return index
        option, inline, inline_value = word.partition("=")
        separate = option in GIT_OPTIONS_WITH_VALUE and not inline
        value = inline_value if inline else (arguments[index + 1] if index + 1 < len(arguments) else None)
        if word in GIT_OPTIONS_THAT_PRINT:
            return None
        if word == "-C":
            options.start = moved(options.start, value) if value != "" else options.start
        elif option == "--git-dir":
            options.git_dir = (True, value)
        elif option == "--work-tree":
            options.work_tree = (True, value)
        elif word == "-c":
            options.configuration.add(value)
        elif option == "--config-env":
            # The value comes from a variable the text names, which the guard does not follow; the key it settles.
            if value is None:
                options.configuration.settled = False
            else:
                options.configuration.entries.append((value.partition("=")[0].lower(), None))
        elif word == "--bare":
            options.git_dir = options.git_dir if options.git_dir[0] else (True, ".")
        elif option not in GIT_OPTIONS_WITH_VALUE and word not in GIT_OPTIONS_WITHOUT_VALUE:
            reading.notes.append(f"git's option {word} is not one the guard reads")
            return -1
        index += 2 if separate else 1
    return None


def read_git(arguments: list[Word], cwd: Path | None, reading: Reading) -> None:
    """One git call: its place, from the variables the hook's environment gives it and its own options, and its
    subcommand. The command itself assigns none it is read with: an assignment before git, and every builtin that
    exports one, is a part the guard does not read."""
    values = git_variables(inherited_variables(reading.environment), reading.notes)
    record_git_call(arguments, GitOptions.initial(cwd, values, opaque=False), reading)


def record_git_call(arguments: list[Word], options: GitOptions, reading: Reading) -> None:
    index = read_git_options(arguments, options, reading)
    if index is None:
        return
    if index < 0 or arguments[index] is None:
        if index >= 0:
            reading.notes.append("a git subcommand is computed at run time")
        reading.calls.append(GitCall(options.place(), None, [], options.copy()))
        return
    # A word the text does not settle is kept as None, which refusal reads as any option.
    reading.calls.append(GitCall(options.place(), arguments[index], arguments[index + 1 :], options.copy()))


# ---------------------------------------------------------------------------------------------------------------
# Judging a call


def allowed_form(subcommand: str, arguments: list[str]) -> bool:
    """Whether a refused subcommand is called in one of the forms that leave the hub on main and its tree alone."""
    if subcommand in {"checkout", "switch"}:
        return [argument for argument in arguments if argument not in {"-q", "--quiet"}] == [MAIN]
    if subcommand == "stash":
        return bool(arguments) and arguments[0] in {"list", "show"}
    if subcommand == "apply":
        return only_checks_a_patch(arguments)
    if subcommand == "submodule":
        return next((argument for argument in arguments if not argument.startswith("-")), None) in READ_SUBMODULE
    if subcommand == "branch":
        return not renames_a_branch(arguments)
    return False


def options_before_separator(arguments: list[str]) -> list[str]:
    """The words git reads as options: those before a `--`, after which every word is an operand."""
    return arguments[: arguments.index("--")] if "--" in arguments else arguments


def only_checks_a_patch(arguments: list[str]) -> bool:
    """Whether git apply only checks: --check among its options, and no --apply, or a prefix of it, that applies the
    patch all the same."""
    options = options_before_separator(arguments)
    applies = any(len(option) >= 4 and "--apply".startswith(option) for option in options)
    return "--check" in options and not applies


def updates_head(arguments: list[str]) -> bool:
    """Whether git fetch is given --update-head-ok, a prefix of it git accepts, or -u, alone or in a bundle, which let
    it move the branch HEAD names. -j and -o take a value, which ends a bundle."""
    for option in options_before_separator(arguments):
        if option.startswith("--"):
            name = option[2:].partition("=")[0]
            if len(name) >= len("update-h") and "update-head-ok".startswith(name):
                return True
        elif option.startswith("-") and len(option) > 1:
            for letter in option[1:]:
                if letter == "u":
                    return True
                if letter in "jo":
                    break
    return False


def renames_a_branch(arguments: list[str]) -> bool:
    """Whether git branch is asked to move a branch, which renames HEAD's own when it names none or that one.

    git takes a unique prefix of a long option and a bundle of short ones, so --mov and -fm both move.
    A short option's attached value ends its bundle; -u also takes a separate value, while -t does not.
    """
    options = iter(arguments)
    for argument in options:
        if argument == "--":
            return False
        if argument.startswith("--"):
            name, inline, _ = argument[2:].partition("=")
            if len(name) >= 2 and "move".startswith(name):
                return True
            # Shorter prefixes also match git's hidden, valueless --set-upstream option.
            if not inline and name.startswith("set-upstream-") and "set-upstream-to".startswith(name):
                next(options, None)
        elif argument.startswith("-"):
            for index, option in enumerate(argument[1:], start=1):
                if option in {"m", "M"}:
                    return True
                if option in {"u", "t"}:
                    if option == "u" and index == len(argument) - 1:
                        next(options, None)
                    break
    return False


def refusal(call: GitCall) -> str | None:
    """Why this git call would move HEAD or write the index or tree of the checkout it runs in; None if it would not."""
    if call.program == "gh":
        return f"`gh {call.subcommand}` checks a branch out in the checkout it runs in."
    subcommand = call.subcommand
    if None in call.arguments and (subcommand == FETCH or subcommand in WRITING_SUBCOMMANDS):
        return f"`git {subcommand}` is given a word the text does not settle, which may be any of its options."
    arguments = [argument for argument in call.arguments if argument is not None]
    if subcommand == FETCH:
        if updates_head(arguments):
            return "`git fetch --update-head-ok` would move the branch the hub's HEAD names."
        return None
    if subcommand not in WRITING_SUBCOMMANDS or allowed_form(subcommand, arguments):
        return None
    if subcommand in {"merge", "pull"} and "--ff-only" in arguments:
        target = [argument for argument in arguments if argument not in FAST_FORWARD_OPTIONS]
        if target in FAST_FORWARD_TARGETS[subcommand]:
            return None
        shown = " ".join(target) or "its upstream"
        return f"`git {subcommand} --ff-only` moves the hub only to origin/main, not to {shown}."
    return f"`git {subcommand}` would move the hub's HEAD or write its index or tree."


def reached_hub(place: Place) -> tuple[Path | None, bool]:
    """The guarded hub a call at place writes, and whether part of the place is not settled.

    git writes the HEAD and index of its repository (the one named, or the one it finds from where it starts) and the
    files of its work tree, so either one in a guarded hub is a write there.
    """
    unknown = place.tree is None
    hub = guarded_hub_at(place.tree) if place.tree is not None else None
    if place.repository_named:
        if place.repository is None:
            unknown = True
        else:
            try:
                hub = hub or guarded_hub_of_repository(place.repository)
            except GitError:
                unknown = True  # a repository git cannot read may be a guarded hub
    elif place.start is None:
        unknown = True
    else:
        hub = hub or guarded_hub_at(place.start)
    return hub, unknown


@dataclass
class Judge:
    """Judges a reading's calls against the guarded hubs in view of the session."""

    hubs: set[Path]
    environment: Mapping[str, str]
    notes: list[str]
    deadline: float

    def denied(self, reading: Reading, depth: int = 0) -> str | None:
        if reading.unread:
            if self.hubs:
                return (
                    f"The guard does not read part of this command ({reading.unread[0]}), so it is refused while a "
                    f"hub is guarded. {REWRITE_IT}"
                )
            self.notes.extend(reading.unread)
        for path in reading.writes:
            reading.time_left()
            if hub_protected(path):
                return f"The command redirects its output into {path}, a file of a guarded hub."
        for call in reading.calls:
            reading.time_left()
            reason = self.judge(call, depth)
            if reason is not None:
                return reason
        return None

    def judge(self, call: GitCall, depth: int) -> str | None:
        if call.subcommand is None and self.hubs:
            # Whatever place it runs in, a subcommand the text does not settle may be an alias that writes a hub.
            return "The guard could not tell which git subcommand runs, so it is refused while a hub is guarded."
        hub, unknown = reached_hub(call.place)
        if hub is None and not (unknown and self.hubs):
            if unknown and (call.subcommand is None or refusal(call) is not None):
                self.notes.append(f"could not tell where `git {call.subcommand or '<computed>'}` runs")
            if unknown or not may_be_alias(call):
                return None
            # Placed outside every guarded hub, the call writes no hub itself, but an alias may run what does.
            return self.alias_refusal(call, alias_lookups(call.place), depth)
        if call.subcommand is None:
            return (
                "The guard could not tell which git subcommand runs, and it refuses one it cannot read where it may "
                f"write a guarded hub. {PLACE_IT if hub is None else ''}".rstrip()
            )
        reason = refusal(call)
        if reason is None and may_be_alias(call):
            hubs = [hub] if hub is not None else sorted(self.hubs)
            reason = self.alias_refusal(call, [AliasLookup(each, None, each) for each in hubs], depth)
        if reason is None or hub is not None:
            return reason
        return (
            f"The guard could not tell where `git {call.subcommand}` runs, and it refuses a git command that would "
            f"write the checkout it runs in when it cannot place it while a hub is guarded. {PLACE_IT}"
        )

    def unreadable(self, call: GitCall, why: str) -> str | None:
        """An alias that cannot be read is refused while a guarded hub is in view, unlike a command whose text cannot
        be: it hides its subcommand. With none in view it is allowed with a note."""
        message = f"The alias `git {call.subcommand}` could not be read ({why})"
        if self.hubs:
            return f"{message}, so it is refused."
        self.notes.append(message)
        return None

    def alias_refusal(self, call: GitCall, lookups: list[AliasLookup], depth: int) -> str | None:
        """Why the alias a call names would write a hub; None when it is no alias, or one that would not.

        git runs a builtin before an alias, so only a subcommand that is no builtin is looked up: first among aliases
        given on the command line, then in the configuration of each place the lookups name.
        """
        subcommand = call.subcommand or ""
        aliases = call.place.aliases
        if aliases is None:
            return self.unreadable(call, "command-line configuration the text does not settle may define it")
        if subcommand.lower() in aliases:
            expansion = aliases[subcommand.lower()]
            if expansion is None:
                return self.unreadable(call, "its value is computed at run time")
            return self.expansion_refusal(call, expansion, depth, call.place.tree)
        for lookup in lookups:
            repository = () if lookup.repository is None else ("--git-dir", str(lookup.repository))
            result = git(lookup.directory, *repository, "config", "--get", f"alias.{subcommand}")
            if result.returncode == 1:
                continue
            if result.returncode != 0:
                return self.unreadable(call, result.stderr.strip())
            reason = self.expansion_refusal(call, result.stdout.removesuffix("\n"), depth, lookup.top)
            if reason is not None:
                return reason
        return None

    def expansion_refusal(self, call: GitCall, expansion: str, depth: int, top: Path | None) -> str | None:
        """Why what an alias expands to would write a hub; a shell alias runs at top, the top of the work tree."""
        if depth >= NESTING_LIMIT:
            return self.unreadable(call, f"it nests more than {NESTING_LIMIT} deep")
        reading = Reading(self.environment, expansion, deadline=self.deadline)
        if expansion.startswith("!"):
            if None in call.arguments:
                return self.unreadable(call, "a word after it is one the text does not settle")
            # git runs a shell alias at the top of the work tree, with the call's arguments after it.
            script = " ".join([expansion[1:], *(shlex.quote(argument or "") for argument in call.arguments)])
            reading.text = script
            read_bash(script, top, reading, depth + 1)
        else:
            try:
                words: list[Word] = list(shlex.split(expansion))
            except ValueError as exc:
                return self.unreadable(call, str(exc))
            options = call.options.copy() if call.options is not None else GitOptions.initial(None, {}, opaque=True)
            record_git_call([*words, *call.arguments], options, reading)
        reason = Judge(self.hubs, self.environment, [], self.deadline).denied(reading, depth + 1)
        if reason is not None:
            return f"`git {call.subcommand}` is an alias for `{expansion}`: {reason}"
        if reading.notes or reading.unread:
            return self.unreadable(call, (reading.unread or reading.notes)[0])
        return None


@dataclass(frozen=True)
class AliasLookup:
    """A place git reads an alias from: the directory it starts in, the git directory it is named, if any, and the top
    of the work tree, where a shell alias runs."""

    directory: Path
    repository: Path | None
    top: Path | None


def may_be_alias(call: GitCall) -> bool:
    """Whether a call's subcommand may be an alias: one the guard neither refuses itself nor finds among git's
    builtins, which no alias can stand for."""
    return (
        call.program == "git"
        and call.subcommand is not None
        and call.subcommand not in WRITING_SUBCOMMANDS | {FETCH} | git_builtins()
    )


def alias_lookups(place: Place) -> list[AliasLookup]:
    """Where git reads the aliases of a call at a settled place: the configuration of the repository it uses, found
    from where it starts or named, which a worktree shares with its hub. A place that does not exist runs nothing; one
    that cannot be looked at raises GitError, as a place git cannot read does, never reads as one that runs nothing."""
    directory = place.start or place.tree
    if directory is None or not worktrees.is_folder(directory):
        return []
    checkout = checkout_of(place.tree) if place.tree is not None and place.tree == place.start else None
    top = checkout.toplevel if checkout is not None else place.tree
    return [AliasLookup(directory, place.repository if place.repository_named else None, top)]


def denied(reading: Reading, hubs: Iterable[Path] = (), depth: int = 0) -> str | None:
    """Why a reading's calls would write a guarded hub, or None; notes on what was passed over join the reading's."""
    judge = Judge(set(hubs), reading.environment, reading.notes, reading.deadline)
    with lookups_within(reading.deadline):
        return judge.denied(reading, depth)


# ---------------------------------------------------------------------------------------------------------------
# Where a shell is


@dataclass(frozen=True)
class Directory:
    """Where a shell is, as far as the text settles it, and its pushd stack; None is a directory it does not."""

    cwd: Path | None
    stack: tuple[Path | None, ...] = ()


UNKNOWN = Directory(None)


def joined(first: Directory, second: Directory) -> Directory:
    """A shell that may be in either directory: what the two agree on stays settled, and the rest is not."""
    cwd = first.cwd if same_place(first.cwd, second.cwd) else None
    if len(first.stack) == len(second.stack) and all(map(same_place, first.stack, second.stack)):
        return Directory(cwd, first.stack)
    return Directory(cwd, (None,) * max(len(first.stack), len(second.stack)))


@dataclass(frozen=True)
class Outcome:
    """Where a shell is after a command that succeeded, and after one that failed, which && and || go on from."""

    ok: Directory
    fail: Directory

    def merged(self) -> Directory:
        return joined(self.ok, self.fail)


def certain(directory: Directory) -> Outcome:
    return Outcome(directory, directory)


def moved_to(directory: Directory, target: Path, stack: tuple[Path | None, ...]) -> Outcome:
    """A directory change to target: where it succeeds, the shell is there; a target that is no directory now fails,
    which leaves the shell where it was, unless an earlier command makes it one."""
    path = Path(os.path.normpath(target))
    arrived = Directory(path, stack)
    return certain(arrived) if path.is_dir() else Outcome(arrived, directory)


def home(environment: Mapping[str, str]) -> Path:
    value = environment.get("HOME")
    return Path(platform_support.from_shell_path(value)) if value else Path.home()


def free_name(name: str, environment: Mapping[str, str]) -> bool:
    """Whether a variable's value is one nothing the guard reads takes from the shell: a name Bash neither reads nor
    sets, or one it reads for what changes nothing the guard reads, that git does not read, and that the environment
    does not hold, so that assigning it reaches no program."""
    held = any(each == name for each in environment)
    return bool(bash_syntax.NAME.fullmatch(name)) and harmless_name(name) and not held


def harmless_name(name: str) -> bool:
    """Whether a variable is one an assignment before a program may give it: one git does not read, and Bash does not
    read for what the guard reads."""
    return (
        BASH_VARIABLES.get(name, "") == ""
        and not folded(name).startswith("GIT_")
        and not EXPORTED_FUNCTION.match(folded(name))
    )


# ---------------------------------------------------------------------------------------------------------------
# The Bash reader


@dataclass(frozen=True)
class BashStart:
    """What a bash takes from the environment it starts with that the reader needs: its home, whether it searches
    CDPATH, the functions it is handed by name, and whether xtrace may run what PS4 holds."""

    home: Path
    cdpath: bool
    functions: frozenset[str]
    expanding_ps4: bool


def bash_start(reading: Reading) -> BashStart:
    """Reads the shell state the hook's environment hands the Bash tool's shell, and tells the reading of each
    option it turns on that the tables do not read."""
    environment = reading.environment
    for variable, table in (("SHELLOPTS", "set"), ("BASHOPTS", "shopt")):
        for option in filter(None, environment.get(variable, "").split(":")):
            judge_option(reading, table, option, True, f"the {variable} the environment holds")
    for variable, option in (("POSIXLY_CORRECT", "posix"), ("BASH_COMPAT", "compat44")):
        if variable in environment:
            table = "set" if option == "posix" else "shopt"
            judge_option(reading, table, option, True, f"the {variable} the environment holds")
    functions = frozenset(
        folded(match.group(1)) for match in (EXPORTED_FUNCTION.match(folded(name)) for name in environment) if match
    )
    ps4 = environment.get("PS4", "")
    return BashStart(home(environment), bool(environment.get("CDPATH")), functions, any(mark in ps4 for mark in "$`"))


def judge_option(reading: Reading, table: str, option: str | None, on: bool, source: str) -> None:
    """Tells the reading of an option turned on or off that the tables do not read a shell under."""
    if option is None:
        reading.cannot_read(f"an option {source} turns on or off that the text does not settle")
        return
    state = (SET_OPTIONS.get(option, ("", "unknown"))[1]) if table == "set" else SHOPT_OPTIONS.get(option, "unknown")
    if state == "unknown":
        reading.cannot_read(
            f"the option {option}, which {source} turns {'on' if on else 'off'}, is one the guard does not know"
        )
    elif state == ("on" if on else "off"):
        reason = NOT_READ[f"set -o {option}" if table == "set" else f"shopt {option}"]
        reading.cannot_read(f"{source} turns {option} {state}: {reason}")


class BashReader:
    """Reads a Bash command tree for the git calls it makes and where each acts, and tells the reading of each part
    the guard does not read.

    Only a list of simple commands is placed exactly: a command that runs only when another succeeds or fails goes on
    from that outcome, a subshell, a pipeline's commands, and a background job run in a copy whose changes die with it,
    and a compound command that changes the directory leaves every git call in it, and every one after it, in a
    directory the text does not settle.
    """

    def __init__(self, reading: Reading, depth: int, start: BashStart) -> None:
        self.reading = reading
        self.depth = depth
        self.start = start

    def nested(self) -> BashReader:
        return BashReader(self.reading, self.depth + 1, self.start)

    def read_text(self, text: str, directory: Directory) -> Directory:
        if self.depth > NESTING_LIMIT:
            self.reading.cannot_read(f"substitutions nest more than {NESTING_LIMIT} deep")
            return UNKNOWN
        try:
            tree = bash_syntax.parse(text)
        except bash_syntax.BashSyntaxError as exc:
            self.reading.cannot_read(f"Bash would not read it as written, or the guard cannot ({exc})")
            return UNKNOWN
        return self.read_list(tree, directory)

    def read_list(self, node: bash_syntax.CommandList, directory: Directory) -> Directory:
        for and_or, background in node.items:
            outcome = self.read_and_or(and_or, directory)
            if not background:
                directory = outcome.merged()
        return directory

    def read_and_or(self, node: bash_syntax.AndOr, directory: Directory) -> Outcome:
        outcome = self.read_pipeline(node.first, directory)
        for operator, pipeline in node.rest:
            if operator == "&&":
                after = self.read_pipeline(pipeline, outcome.ok)
                outcome = Outcome(after.ok, joined(outcome.fail, after.fail))
            else:
                after = self.read_pipeline(pipeline, outcome.fail)
                outcome = Outcome(joined(outcome.ok, after.ok), after.fail)
        return outcome

    def read_pipeline(self, node: bash_syntax.Pipeline, directory: Directory) -> Outcome:
        if len(node.commands) == 1:
            outcome = self.read_command(node.commands[0], directory)
        else:
            for command in node.commands:
                self.read_command(command, directory)
            outcome = certain(directory)
        return Outcome(outcome.fail, outcome.ok) if node.negated else outcome

    def read_command(self, node: bash_syntax.Command, directory: Directory) -> Outcome:
        self.reading.time_left()
        if isinstance(node, bash_syntax.Simple):
            return self.read_simple(node, directory)
        if isinstance(node, bash_syntax.FunctionDefinition):
            self.reading.cannot_read(NOT_READ["construct function definition"])
            return certain(UNKNOWN)
        self.read_redirects(node.redirects, directory)
        if isinstance(node, bash_syntax.Subshell) and arithmetic_command(node):
            self.reading.cannot_read(NOT_READ["construct arithmetic command"])
        moves = changes_directory(node)
        inside = UNKNOWN if moves else directory
        if isinstance(node, bash_syntax.Loop):
            if node.header:
                self.reading.cannot_read(NOT_READ["construct arithmetic for"])
            if node.name is not None and not free_name(node.name, self.reading.environment):
                self.reading.cannot_read(
                    f"a loop assigns {node.name}, which Bash or git reads or the environment holds"
                )
            self.read_words(node.words or [], directory)
        if isinstance(node, bash_syntax.Case):
            self.read_words([node.word, *node.patterns], directory)
        for body in command_bodies(node):
            self.read_list(body, inside)
        if moves and not isinstance(node, bash_syntax.Subshell):
            return certain(UNKNOWN)
        return certain(directory)

    def read_words(self, words: Iterable[bash_syntax.Word], directory: Directory) -> list[Word]:
        """Each word's value, None where the text does not settle it, once every substitution in it is read."""
        values = []
        for word in words:
            for text in word.substitutions:
                self.nested().read_text(text, directory)
            values.append(self.word_value(word))
        return values

    def word_value(self, word: bash_syntax.Word) -> Word:
        """The text Bash makes of a word, or None where only running settles it; tells the reading of each part of
        it the guard does not read."""
        for name in word.assigns:
            self.read_assigned(name)
        pieces: list[str] = []
        settled = True
        for part in word.parts:
            if part.kind == "text":
                if not part.quoted and "$[" in part.text:
                    self.reading.cannot_read(NOT_READ["expansion $["])
                if not part.quoted and (GLOB.search(part.text) or BRACE.search(part.text)):
                    settled = False
                pieces.append(part.text)
            elif part.kind == "home":
                pieces.append(str(self.start.home))
            elif part.kind == "parameter":
                settled = False
            else:
                settled = False
                self.read_dynamic(part.text)
        if not settled:
            return None
        value = "".join(pieces)
        self.reading.named.extend(named_paths(value))
        return value

    def read_assigned(self, name: str) -> None:
        if name.startswith("!") or "[" in name or not free_name(name, self.reading.environment):
            self.reading.cannot_read(f"{NOT_READ['expansion assigning']} ({name})")

    def read_dynamic(self, form: str) -> None:
        """Tells the reading of an expansion of a form the guard does not read."""
        key = expansion_form(form)
        if key not in EXPANSIONS_UNDERSTOOD:
            reason = NOT_READ.get(f"expansion {key}", "an expansion the guard does not know")
            self.reading.cannot_read(f"{reason} ({form})")

    def read_redirects(self, redirects: list[bash_syntax.Redirect], directory: Directory) -> None:
        for redirect in redirects:
            here = redirect.here
            if here is not None:
                for text in here.substitutions:
                    self.nested().read_text(text, directory)
                for name in here.assigns:
                    self.read_assigned(name)
                for part in here.expansions:
                    self.read_dynamic(part.text)
                if not here.quoted and "$[" in here.body:
                    self.reading.cannot_read(NOT_READ["expansion $["])
                continue
            if redirect.target is None:
                continue
            value = self.read_words([redirect.target], directory)[0]
            if redirect.operator in {"<", "<<<"} or (
                redirect.operator in {"<&", ">&"} and value is not None and DESCRIPTOR_TARGET.fullmatch(value)
            ):
                continue
            read_written(self.target_value(redirect.target) if value is None else value, directory, self.reading)

    def target_value(self, word: bash_syntax.Word) -> Word:
        """A redirection target the text settles but for the system's temporary folder, named by a variable
        TEMPORARY_VARIABLES lists that the environment holds; None for any other."""
        pieces: list[str] = []
        for part in word.parts:
            if part.kind == "text" and (part.quoted or not (GLOB.search(part.text) or BRACE.search(part.text))):
                pieces.append(part.text)
            elif part.kind == "home":
                pieces.append(str(self.start.home))
            elif (
                part.kind == "parameter"
                and part.text in TEMPORARY_VARIABLES
                and self.reading.environment.get(part.text)
            ):
                pieces.append(self.reading.environment[part.text])
            else:
                return None
        return "".join(pieces)

    def read_simple(self, node: bash_syntax.Simple, directory: Directory) -> Outcome:
        words = node.words
        values = self.read_words(words, directory)
        self.read_redirects(node.redirects, directory)
        index = 0
        while index < len(words) and words[index].assignment is not None:
            index += 1
        prefixes: list[tuple[str, Word]] = []
        for word in words[:index]:
            assignment = word.assignment
            if assignment is None or word.array:
                continue  # an array's value, NAME=( ), is a form word_value does not read
            if assignment.element:
                self.reading.cannot_read(NOT_READ["construct element assignment"])
            elif index == len(words) and not free_name(assignment.name, self.reading.environment):
                self.reading.cannot_read(f"assigns {assignment.name}, which Bash or git reads or the environment holds")
            else:
                prefixes.append((assignment.name, self.word_value(bash_syntax.Word("", assignment.value))))
        if index == len(words):
            return certain(directory)
        return self.read_command_words(values[index:], directory, prefixes)

    def read_prefixes(self, prefixes: list[tuple[str, Word]], git: bool) -> None:
        """The assignments before a command, which its environment holds while it runs: before git, only those
        GIT_PREFIXES names with a value it allows, and those Bash reads for nothing the guard reads, since git takes
        where it acts and its configuration from its environment; before any other command, a name git does not read
        and Bash reads for nothing the guard reads."""
        for name, value in prefixes:
            if git and name in GIT_PREFIXES:
                allowed = GIT_PREFIXES[name]
                if value is None or (allowed and value not in allowed):
                    self.reading.cannot_read(f"{NOT_READ['construct assignment before git']} ({name})")
            elif git and name not in BASH_VARIABLES:
                self.reading.cannot_read(f"{NOT_READ['construct assignment before git']} ({name})")
            elif not harmless_name(name):
                self.reading.cannot_read(f"assigns {name} before a command, which Bash or git reads")

    def read_command_words(
        self, values: list[Word], directory: Directory, prefixes: list[tuple[str, Word]] | None = None
    ) -> Outcome:
        name, arguments = values[0], values[1:]
        self.read_prefixes(prefixes or [], git=name is not None and command_name(name) in GIT_NAMES)
        if name is None:
            self.reading.cannot_read(
                "a command name the text does not settle, which may be any command, git among them"
            )
            return certain(UNKNOWN)
        if "/" not in name and "\\" not in name:
            if folded(name) in self.start.functions:
                self.reading.cannot_read(f"{name} is a function the environment hands the shell, which may run git")
                return certain(UNKNOWN)
            if f"builtin {name}" in NOT_READ:
                self.reading.cannot_read(f"{name}: {NOT_READ[f'builtin {name}']}")
                return certain(UNKNOWN)
            # The grammar reads [[ and coproc as a command's name; Bash reads each as a reserved word.
            if f"keyword {name}" in NOT_READ:
                self.reading.cannot_read(f"{name}: {NOT_READ[f'keyword {name}']}")
                return certain(UNKNOWN)
            if name in BUILTINS_UNDERSTOOD:
                return self.read_builtin(name, arguments, directory)
        program = command_name(name) or ""
        if program in GIT_NAMES:
            read_git(arguments, directory.cwd, self.reading)
        elif program in RUNNERS:
            return self.read_runner(program, arguments, directory)
        elif program == "gh":
            read_gh(arguments, directory, self.reading)
        elif program in COMMAND_OPTIONS:
            return self.read_program(program, arguments, directory)
        return certain(directory)

    def read_runner(self, program: str, arguments: list[Word], directory: Directory) -> Outcome:
        """A program that runs another command: the command it runs is read where the guard reads the runner's form,
        and the runner is refused otherwise."""
        if program in RUNNER_OPTIONS:
            start, reason = runner_start(program, arguments)
            if reason is not None:
                self.reading.cannot_read(reason)
                return certain(UNKNOWN)
            if start is None:
                return certain(directory)
            # xargs hands its command words from its input, which the text does not settle.
            extra: list[Word] = [None] if program == "xargs" else []
            return self.read_command_words([*arguments[start:], *extra], directory)
        if program == "env":
            return self.read_env(arguments, directory)
        script = shell_script(program, arguments)
        if script is not None and program in {"bash", "sh"}:
            # The script runs in a shell of its own, so what it changes dies with it.
            self.nested().read_text(script, directory)
            return certain(directory)
        if script is not None:
            read_powershell(script, directory.cwd, self.reading, self.depth + 1)
            return certain(directory)
        self.reading.cannot_read(f"runs {program}: {RUNNERS[program]}")
        return certain(UNKNOWN)

    def read_env(self, arguments: list[Word], directory: Directory) -> Outcome:
        """env with no option, whose assignments are read as ones before the command it runs."""
        prefixes: list[tuple[str, Word]] = []
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None or word.startswith("-"):
                self.reading.cannot_read(f"runs env: {RUNNERS['env']} ({word or 'a word the text does not settle'})")
                return certain(UNKNOWN)
            name, assigns, value = word.partition("=")
            if not assigns:
                break
            if not bash_syntax.NAME.fullmatch(name):
                self.reading.cannot_read(f"runs env: {RUNNERS['env']} ({word})")
                return certain(UNKNOWN)
            prefixes.append((name, value))
            index += 1
        if index == len(arguments):
            self.read_prefixes(prefixes, git=False)
            return certain(directory)
        return self.read_command_words(arguments[index:], directory, prefixes)

    def read_program(self, program: str, arguments: list[Word], directory: Directory) -> Outcome:
        """A program with options that run a command: find's run the command after them, read as a command, in a
        directory of the found file's for -execdir and -okdir; any other such option is refused, and so is a word the
        text does not settle, which may be one."""
        options = COMMAND_OPTIONS[program]
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None:
                self.reading.cannot_read(
                    f"{program} may run a command its words name (a word the text does not settle)"
                )
                return certain(UNKNOWN)
            option = word.partition("=")[0]
            if program == "find" and word in FIND_COMMANDS:
                end = next(
                    (place for place in range(index + 1, len(arguments)) if arguments[place] in {";", "+"}),
                    len(arguments),
                )
                if end > index + 1:
                    place = directory if word in {"-exec", "-ok"} else UNKNOWN
                    self.read_command_words(arguments[index + 1 : end], place)
                index = end + 1
                continue
            if option in options:
                self.reading.cannot_read(f"{program} runs a command its words name ({word})")
                return certain(UNKNOWN)
            index += 1
        return certain(directory)

    def read_builtin(self, name: str, arguments: list[Word], directory: Directory) -> Outcome:
        if name == "cd":
            return self.read_cd(arguments, directory, push=False)
        if name == "pushd":
            return self.read_cd(arguments, directory, push=True)
        if name == "popd":
            if arguments:
                return certain(Directory(None, directory.stack[1:]))
            if not directory.stack:
                return certain(directory)
            return certain(Directory(directory.stack[0], directory.stack[1:]))
        if name == "printf" and arguments and (arguments[0] is None or arguments[0].startswith("-v")):
            self.reading.cannot_read("printf -v assigns a variable")
        elif name in {"test", "["}:
            self.read_test(arguments)
        elif name == "wait" and any(word is None or word.startswith("-") for word in arguments):
            self.reading.cannot_read("wait is given an option, as -p, which assigns a variable")
        elif name == "command" and (not arguments or arguments[0] not in {"-v", "-V"}):
            self.reading.cannot_read("command runs the name it is given, which may be git under another name")
        elif name == "read":
            self.read_read(arguments)
        elif name == "set":
            self.read_set(arguments)
        elif name == "shopt":
            self.read_shopt(arguments)
        return certain(directory)

    def read_cd(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """cd or pushd to a directory: a literal one settles where the shell goes; one the text does not settle, cd -,
        a CDPATH search, and pushd's rotations leave it unknown, and an option is not read."""
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        elif arguments and arguments[0] is not None and arguments[0].startswith(("-", "+")) and arguments[0] != "-":
            self.reading.cannot_read(
                f"{'pushd' if push else 'cd'}'s option {arguments[0]}, which the guard does not read"
            )
            return certain(UNKNOWN)
        stack = (directory.cwd, *directory.stack) if push else directory.stack
        if not arguments:
            if push:
                return certain(Directory(None, (None, *directory.stack[1:])))
            return moved_to(directory, self.start.home, stack)
        if len(arguments) > 1:
            return certain(directory)  # Bash refuses more than one directory, and stays
        target = arguments[0]
        if target is None or target == "-":
            return certain(Directory(None, stack))
        path = Path(platform_support.from_shell_path(target))
        if not path.is_absolute():
            relative_to_cwd = target.startswith(("./", "../")) or target in {".", ".."}
            if directory.cwd is None or (self.start.cdpath and not relative_to_cwd):
                return certain(Directory(None, stack))
            path = directory.cwd / path
        return moved_to(directory, path, stack)

    def read_test(self, arguments: list[Word]) -> None:
        """test and [ evaluate a subscript as arithmetic for -v, which runs what the subscript holds: refused where
        a word that is -v, or may be, has an operand after it that holds a subscript or may."""
        words = arguments[:-1] if arguments[-1:] == ["]"] else arguments
        for index, word in enumerate(words[:-1]):
            operand = words[index + 1]
            if (word is None or word == "-v") and (operand is None or "[" in operand):
                self.reading.cannot_read("test -v evaluates its operand's subscript as arithmetic")
                return

    def read_read(self, arguments: list[Word]) -> None:
        """read's options and the names it assigns, each of which must be free."""
        names: list[Word] = []
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None:
                self.reading.cannot_read("read is given a word the text does not settle, which may be any name")
                return
            if word == "--":
                names.extend(arguments[index + 1 :])
                break
            if not word.startswith("-") or len(word) == 1:
                names.extend(arguments[index:])
                break
            for position, letter in enumerate(word[1:], start=1):
                if letter in "rse":
                    continue
                if letter in "adpntNui":
                    attached = word[position + 1 :]
                    value = attached or (arguments[index + 1] if index + 1 < len(arguments) else None)
                    if letter == "a":
                        names.append(value)
                    if not attached:
                        index += 1
                    break
                self.reading.cannot_read(f"read's option -{letter}, which the guard does not read")
                return
            index += 1
        for name in names:
            if name is None or not free_name(name, self.reading.environment):
                self.reading.cannot_read(f"read assigns {name}, which Bash or git reads or the environment holds")

    def read_set(self, arguments: list[Word]) -> None:
        """set's options, each judged by SET_OPTIONS, before its positional parameters."""
        index = 0
        while index < len(arguments):
            word = arguments[index]
            if word is None:
                self.reading.cannot_read("set is given a word the text does not settle, which may be any option")
                return
            if word in {"--", "-"} or not word.startswith(("-", "+")):
                return
            on = word.startswith("-")
            for letter in word[1:]:
                if letter == "o":
                    index += 1
                    option = arguments[index] if index < len(arguments) else ""
                    if option != "":
                        judge_option(self.reading, "set", option, on, "set")
                        self.note_xtrace(option, on)
                else:
                    option = SET_LETTERS.get(letter)
                    judge_option(self.reading, "set", option or letter, on, "set")
                    self.note_xtrace(option, on)
            index += 1

    def note_xtrace(self, option: str | None, on: bool) -> None:
        if option == "xtrace" and on and self.start.expanding_ps4:
            self.reading.cannot_read(NOT_READ_XTRACE)

    def read_shopt(self, arguments: list[Word]) -> None:
        """shopt's -s and -u, each judged by SHOPT_OPTIONS, or by SET_OPTIONS with -o; printing changes nothing."""
        state: bool | None = None
        table = "shopt"
        for word in arguments:
            if word is None:
                self.reading.cannot_read("shopt is given a word the text does not settle, which may be any option")
                return
            if word.startswith("-") and len(word) > 1:
                for letter in word[1:]:
                    if letter in "su":
                        state = letter == "s"
                    elif letter == "o":
                        table = "set"
                    elif letter not in "pq":
                        self.reading.cannot_read(f"shopt's option -{letter}, which the guard does not read")
                        return
            elif state is not None:
                judge_option(self.reading, table, word, state, "shopt")
                self.note_xtrace(word if table == "set" else None, state)


def named_paths(value: str) -> list[str]:
    """The paths a settled word may name: its value after its last =, and each quoted part of it, as a string eval
    is handed holds them, that holds a separator."""
    candidates = [value.rpartition("=")[2], *QUOTED_PART.findall(value)]
    return [candidate for candidate in candidates if "/" in candidate or "\\" in candidate]


QUOTED_PART = re.compile(r"""["']([^"']+)["']""")


def runner_start(program: str, arguments: list[Word]) -> tuple[int | None, str | None]:
    """Where the command a runner RUNNER_OPTIONS names starts among its words, after its options and its operands, or
    None where it starts none; with why, for a form the guard does not read: an option its table does not name, an
    abbreviation or a bundle among them, or a word the text does not settle where an option may stand."""
    flags, valued, operands = RUNNER_OPTIONS[program]
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word is None:
            return None, f"{program} is given a word the text does not settle, which may be an option"
        if word == "--":
            index += 1
            break
        if not word.startswith("-") or word == "-":
            break
        attached = word[:2] in valued and not word.startswith("--")
        niceness = program == "nice" and word[1:].isdigit()
        if word in valued:
            index += 2
        elif word in flags or attached or niceness or (word.partition("=")[0] in valued and "=" in word):
            index += 1
        else:
            return None, f"{program}'s option {word}, which the guard does not read"
    start = index + operands
    return (start if start < len(arguments) else None), None


def shell_script(program: str, arguments: list[Word]) -> str | None:
    """The literal script a shell runs, where the guard reads the shell's form: `bash -c <script>` or `sh -c
    <script>`, with any words after it its positional parameters, and pwsh's `-Command` or `-c` with one script after
    the options PWSH_QUIET names; None for every other form."""
    if program in {"bash", "sh"}:
        if len(arguments) >= 2 and arguments[0] == "-c" and arguments[1] is not None:
            return arguments[1]
        return None
    if program != "pwsh":
        return None
    index = 0
    while index < len(arguments) and arguments[index] is not None and str(arguments[index]).lower() in PWSH_QUIET:
        index += 1
    rest = arguments[index:]
    if len(rest) == 2 and rest[0] is not None and rest[0].lower() in {"-c", "-command"} and rest[1] is not None:
        return rest[1]
    return None


def read_written(value: Word, directory: Directory, reading: Reading) -> None:
    """A file a redirection writes: one the text places in a guarded hub is refused, as a file tool's edit is, and so
    is one whose path or directory the text does not settle, which may lie in one."""
    if value is None:
        reading.cannot_read("a redirection writes a file the text does not settle, which may lie in a hub")
        return
    if value in DEVICE_FILES or value.lower() == "$null":
        return
    path = Path(platform_support.from_shell_path(value))
    if not path.is_absolute() and directory.cwd is None:
        reading.cannot_read(f"a redirection writes {value} in a directory the text does not settle")
    else:
        reading.writes.append(path if path.is_absolute() else (directory.cwd or Path()) / path)


def powershell_command(name: str) -> bool:
    """Whether PowerShell finds a name among its own commands before it looks for a program: an alias, a function it
    defines itself, or a cmdlet's or function's verb-noun name."""
    lowered = name.lower()
    verb, dash, _ = lowered.partition("-")
    return lowered in POWERSHELL_ALIASES or lowered in POWERSHELL_FUNCTIONS or bool(dash and verb in POWERSHELL_VERBS)


def read_gh(arguments: list[Word], directory: Directory, reading: Reading) -> None:
    """gh, which runs git itself to check a branch out for the subcommands GH_CHECKOUTS names, in the checkout it
    runs in."""
    named: list[Word] = []
    words = iter(arguments)
    for word in words:
        if word in {"-R", "--repo", "--hostname"}:
            next(words, None)
        elif word is None or not word.startswith("-"):
            named.append(word)
    named = named[:2]
    if named[:1] == [None] or (named[:1] in (["pr"], ["repo"]) and None in named):
        reading.cannot_read("a gh subcommand the text does not settle, which may check a branch out")
    elif tuple(named) in GH_CHECKOUTS:
        place = Place(directory.cwd, directory.cwd)
        reading.calls.append(GitCall(place, " ".join(str(word) for word in named), [], program="gh"))


NOT_READ_XTRACE = "xtrace expands PS4 before each command, and the environment's PS4 holds an expansion, which may run"
# A redirection's target that names a descriptor to duplicate or close, as in 2>&1 or >&-.
DESCRIPTOR_TARGET = re.compile(r"[0-9]+-?|-")


def expansion_form(form: str) -> str:
    """The catalogue entry of an expansion, by the form bash_syntax gives its dynamic part."""
    if form.startswith("${") and form.endswith("}"):
        return parameter_form(form[2:-1])
    if form.startswith("$") and len(form) == 2:
        return "$1" if form[1].isdigit() else form
    return "NAME=( )" if form == "(" else form


def parameter_form(content: str) -> str:
    """The catalogue entry of a braced expansion's content: a name and an operator from PARAMETER_OPERATORS, read
    only when its word holds no further expansion the guard would have to read apart."""
    if not content or content[0] in " \t\n":
        return "${ command; }"
    if content[0] == "|":
        return "${| command; }"
    if content[0] == "!":
        return "${!NAME}"
    if content[0] == "#" and len(content) > 1 and PARAMETER_NAME.fullmatch(content[1:]):
        return "${#NAME}"
    name = PARAMETER_NAME.match(content)
    if name is None:
        return "${unread}"
    rest = content[name.end() :]
    if not rest:
        return "${NAME}"
    for operator, key in PARAMETER_OPERATORS:
        if rest.startswith(operator):
            word = rest[len(operator) :]
            if key in EXPANSIONS_UNDERSTOOD and any(mark in word for mark in ("${", "$((", "$[")):
                return "nested ${"
            return key
    return "${unread}"


def arithmetic_command(node: bash_syntax.Subshell) -> bool:
    """Whether a subshell is an arithmetic command, (( )), which the grammar reads as a subshell holding only a
    subshell; ( ( ) ), which Bash runs the same, reads alike."""
    body = node.body
    if len(body.items) != 1:
        return False
    and_or, background = body.items[0]
    commands = and_or.first.commands
    return not background and not and_or.rest and len(commands) == 1 and isinstance(commands[0], bash_syntax.Subshell)


def command_bodies(node: bash_syntax.Command) -> list[bash_syntax.CommandList]:
    """The lists a compound command runs, its conditions included."""
    if isinstance(node, (bash_syntax.Subshell, bash_syntax.Group)):
        return [node.body]
    if isinstance(node, bash_syntax.If):
        lists = [each for clause in node.clauses for each in clause]
        return [*lists, node.otherwise] if node.otherwise is not None else lists
    if isinstance(node, bash_syntax.Loop):
        return [node.condition, node.body] if node.condition is not None else [node.body]
    if isinstance(node, bash_syntax.Case):
        return list(node.branches)
    return []


def changes_directory(node: object) -> bool:
    """Whether a compound command holds a command that may change the shell's directory: cd, pushd, or popd, or one
    whose name the text does not settle, which is refused while a hub is in view anyway."""
    if isinstance(node, bash_syntax.Simple):
        names = [word for word in node.words if word.assignment is None][:1]
        return bool(names) and names[0].literal in {"cd", "pushd", "popd", None}
    if isinstance(node, bash_syntax.CommandList):
        return any(changes_directory(and_or) for and_or, _ in node.items)
    if isinstance(node, bash_syntax.AndOr):
        return any(changes_directory(pipeline) for pipeline in (node.first, *(each for _, each in node.rest)))
    if isinstance(node, bash_syntax.Pipeline):
        return any(changes_directory(command) for command in node.commands)
    return isinstance(node, bash_syntax.Command) and any(changes_directory(body) for body in command_bodies(node))


def read_bash(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a Bash command started in cwd with the environment the hook was given."""
    BashReader(reading, depth, bash_start(reading)).read_text(command, Directory(cwd))


# ---------------------------------------------------------------------------------------------------------------
# The PowerShell reader


@functools.cache
def powershell_reader() -> str:
    """The reader as pwsh is handed it on its command line: without its comment lines, which run nothing and would
    otherwise bring it past the 32767 characters a command line Windows starts may hold. The reader holds no
    here-string, so no line of a string begins with #."""
    lines = POWERSHELL_READER.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def powershell_events(command: str, reading: Reading) -> dict[str, Any] | None:
    """What PowerShell's own parser makes of one command, or None, with why, when it could not be read.

    pwsh starts with -Command rather than -File, so no execution policy applies, and not with -EncodedCommand,
    which endpoint protection treats as a sign of malware. The command travels base64-encoded on stdin, past any
    console code page.
    """
    pwsh = platform_support.find_pwsh(reading.environment.get("PATH", ""))
    if pwsh is None:
        reading.cannot_read("could not find pwsh to read a PowerShell command")
        return None
    payload = json.dumps({"command": command})
    try:
        # run_bounded takes git's repository-selecting variables out of the hook's environment, and the tree ends
        # every process pwsh starts once it has finished or been killed.
        result = run_bounded(
            [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", powershell_reader()],
            min(reading.time_left(), READING_SECONDS),
            input_bytes=base64.b64encode(payload.encode("utf-8")),
            env=reading.environment,
            output_limit=POWERSHELL_OUTPUT_LIMIT,
            tree=process_tree(hidden_window=True),
        )
    except subprocess.TimeoutExpired:
        reading.cannot_read(f"pwsh did not read the command in what was left of the guard's {READING_SECONDS} seconds")
        return None
    except OSError as exc:
        reading.cannot_read(f"could not start pwsh ({exc})")
        return None
    try:
        output = json_reader.loads(result.stdout.decode("utf-8", "replace"))
    except ValueError:
        errors = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reading.cannot_read(f"pwsh could not read the command ({(errors or [''])[0]})")
        return None
    if not isinstance(output, dict):
        reading.cannot_read("pwsh answered with something other than the reader's object")
        return None
    if "error" in output:
        # PowerShell parses a whole script before it runs any of it, so a command that does not parse runs
        # nothing; a Bash command's lines before a syntax error do run.
        reading.notes.append(f"PowerShell would not parse the command, so none of it runs ({output['error']})")
        return None
    return output


def read_powershell(command: str, cwd: Path | None, reading: Reading, depth: int = 0) -> None:
    """Reads a PowerShell command through PowerShell's own parser: understood only when every node of its tree is one
    POWERSHELL_UNDERSTOOD names, and then read statement by statement as Bash's commands are."""
    del depth  # PowerShell nests nothing the guard reads
    output = powershell_events(command, reading)
    if output is None:
        return
    nodes, statements, commands = output.get("nodes"), output.get("statements"), output.get("commands")
    if not isinstance(nodes, list) or not isinstance(commands, list):
        reading.cannot_read("pwsh answered without the nodes and commands the reader lists")
        return
    unread = [str(node) for node in nodes if node not in POWERSHELL_UNDERSTOOD]
    for node in unread:
        reading.cannot_read(f"{node}: {NOT_READ.get(f'PowerShell {node}', 'a node type the guard does not know')}")
    reader = PowerShellReader(reading, commands)
    unread.extend(reader.read_forms(output))
    if unread or not isinstance(statements, list):
        # Each git call is still read, from a directory only a command list the guard reads settles.
        moves = any(reader.may_move(command) for command in commands)
        for command in commands:
            reader.read_git_only(command, None if moves else cwd)
        return
    directory = Directory(cwd)
    for statement in statements:
        directory = reader.read_statement(statement, directory)


class PowerShellReader:
    """Reads the statements of a PowerShell command the guard understands, as BashReader reads Bash's."""

    def __init__(self, reading: Reading, commands: list[Any]) -> None:
        self.reading = reading
        self.commands = commands

    def read_forms(self, output: Mapping[str, Any]) -> list[str]:
        """Tells the reading of each understood node type's form that is not read: an assignment to the environment
        or to PowerShell's own variable, a static member or one the text does not name, a cast to a type
        POWERSHELL_CASTS does not name, and a script block handed to a command POWERSHELL_BLOCK_COMMANDS does not
        name; returns them."""
        found = [
            f"assigns {name if isinstance(name, str) else 'something other than a plain variable'}, which "
            "PowerShell or git reads, or which the guard does not follow"
            for name, qualified in (
                (assigned.get("name"), assigned.get("qualified")) if isinstance(assigned, dict) else (None, False)
                for assigned in output.get("assigned") or []
            )
            if not isinstance(name, str) or qualified or name.lower() in POWERSHELL_VARIABLES
        ]
        found.extend(
            "a static member, or one the text does not name, which may run any code"
            for member in output.get("members") or []
            if not isinstance(member, dict) or member.get("static") or not member.get("constant")
        )
        found.extend(
            f"a cast or a type the guard does not read ({name}), whose conversion may run code"
            for name in output.get("types") or []
            if not isinstance(name, str) or name.lower().removeprefix("system.") not in POWERSHELL_CASTS
        )
        found.extend(
            f"a script block handed to {command or 'no command'}, which may run it as anything"
            for command in output.get("blocks") or []
            if POWERSHELL_COMMANDS.get(command.lower() if isinstance(command, str) else "")
            not in POWERSHELL_BLOCK_COMMANDS
        )
        for reason in found:
            self.reading.cannot_read(reason)
        return found

    def element(self, element: Any) -> list[Word]:
        """A command element's words: its text, a list's each, or None where the text does not settle it."""
        if isinstance(element, dict) and element.get("kind") == "words":
            texts = element.get("texts")
            if isinstance(texts, list) and all(isinstance(text, str) for text in texts):
                return list(texts)
        if not isinstance(element, dict) or element.get("kind") not in {"word", "parameter"}:
            return [None]
        text = element.get("text")
        return [text if isinstance(text, str) else None]

    def words(self, command: Any) -> list[Word]:
        elements = command.get("elements") if isinstance(command, dict) else None
        words: list[Word] = [
            word for element in (elements if isinstance(elements, list) else []) for word in self.element(element)
        ]
        for word in words[1:]:
            if word is not None:
                self.reading.named.extend(named_paths(word))
        return words or [None]

    def may_move(self, command: Any) -> bool:
        """Whether a command may move the session's location: one that names a location cmdlet, or whose name the
        text does not settle; an expression standing as a command moves nothing."""
        if isinstance(command, dict) and command.get("expression"):
            return False
        name = self.words(command)[0]
        return name is None or name.lower() in {**POWERSHELL_LOCATION, **POWERSHELL_PUSH, **POWERSHELL_POP}

    def read_git_only(self, command: Any, cwd: Path | None) -> None:
        words = self.words(command)
        if words[0] is not None and command_name(words[0]) in GIT_NAMES:
            read_git(words[1:], cwd, self.reading)

    def read_statement(self, statement: Any, directory: Directory) -> Directory:
        pipelines = statement.get("pipelines") if isinstance(statement, dict) else None
        operators = statement.get("operators") if isinstance(statement, dict) else None
        nested = statement.get("nested") if isinstance(statement, dict) else None
        if not isinstance(pipelines, list) or not isinstance(operators, list) or not isinstance(nested, list):
            self.reading.cannot_read("a PowerShell statement the reader could not list")
            return UNKNOWN
        # A command inside the statement's expressions runs before it, in the session, so one that may move the
        # location leaves it unknown for the statement and after it.
        inside = self.read_pipeline(nested, directory).merged() if nested else directory
        if not same_place(inside.cwd, directory.cwd) or any(
            self.may_move(self.commands[index])
            for index in nested
            if isinstance(index, int) and index < len(self.commands)
        ):
            directory = UNKNOWN
        if not pipelines:
            # An assignment, an if, or a foreach: what it runs is all nested, read above.
            return directory
        outcome = self.read_pipeline(pipelines[0], directory)
        for operator, pipeline in zip(operators, pipelines[1:], strict=False):
            if operator == "&&":
                after = self.read_pipeline(pipeline, outcome.ok)
                outcome = Outcome(after.ok, joined(outcome.fail, after.fail))
            else:
                after = self.read_pipeline(pipeline, outcome.fail)
                outcome = Outcome(joined(outcome.ok, after.ok), after.fail)
        return directory if statement.get("background") else outcome.merged()

    def read_pipeline(self, pipeline: Any, directory: Directory) -> Outcome:
        indexes = pipeline if isinstance(pipeline, list) else []
        commands = [self.commands[index] for index in indexes if isinstance(index, int) and index < len(self.commands)]
        if len(commands) == 1:
            return self.read_command(commands[0], directory)
        moved = False
        for command in commands:
            moved = self.may_move(command) or moved
            self.read_command(command, directory)
        # Every command of a PowerShell pipeline runs in the session, so a location one sets outlives it.
        return certain(UNKNOWN if moved else directory)

    def read_command(self, command: Any, directory: Directory) -> Outcome:
        self.reading.time_left()
        if not isinstance(command, dict):
            return certain(directory)
        if command.get("expression"):
            return certain(directory)
        if command.get("invocation") == "Dot":
            self.reading.cannot_read("a command dot-sourced into the session, which may define anything")
            return certain(UNKNOWN)
        words = self.words(command)
        for redirection in command.get("redirections") or []:
            if isinstance(redirection, dict) and redirection.get("kind") == "file":
                read_written(self.element(redirection.get("target"))[0], directory, self.reading)
        name = words[0]
        if name is None:
            self.reading.cannot_read(
                "a command name the text does not settle, which may be any command, git among them"
            )
            return certain(UNKNOWN)
        if name.lower() in POWERSHELL_COMMANDS:
            elements = command.get("elements") or []
            if POWERSHELL_COMMANDS[name.lower()] == "foreach-object" and any(
                not isinstance(element, dict) or element.get("kind") not in {"block", "parameter"}
                for element in elements[1:]
            ):
                # ForEach-Object given a name rather than a script block calls the member by that name.
                self.reading.cannot_read("ForEach-Object given a member's name runs that member, which may run code")
                return certain(UNKNOWN)
            return self.read_cmdlet(name, words[1:], directory)
        program = command_name(name) or ""
        if program in GIT_NAMES:
            # A native program is handed ~ as written; whether pwsh expands it first differs by release.
            arguments = [None if word is not None and word.startswith("~") else word for word in words[1:]]
            read_git(arguments, directory.cwd, self.reading)
        elif program == "gh":
            read_gh(words[1:], directory, self.reading)
        elif powershell_command(name):
            self.reading.cannot_read(f"runs {name}, a PowerShell command the guard does not read, which may run git")
            return certain(UNKNOWN)
        elif program in RUNNERS:
            self.reading.cannot_read(
                f"runs {program} from PowerShell: {RUNNERS[program]}, which the guard reads only from Bash"
            )
            return certain(UNKNOWN)
        elif program in COMMAND_OPTIONS and any(
            word is None or word.partition("=")[0] in COMMAND_OPTIONS[program] for word in words[1:]
        ):
            self.reading.cannot_read(f"{program} may run a command its words name")
            return certain(UNKNOWN)
        return certain(directory)

    def read_cmdlet(self, name: str, arguments: list[Word], directory: Directory) -> Outcome:
        """A command POWERSHELL_COMMANDS names: one that moves the location is followed, any other changes nothing the
        guard reads, unless a path it is given lies on a provider other than the file system."""
        cmdlet = POWERSHELL_COMMANDS[name.lower()]
        for word in arguments if cmdlet in POWERSHELL_PATH_COMMANDS else []:
            if word is not None and PROVIDER_PATH.match(word) and not word.startswith("-"):
                self.reading.cannot_read(f"{name} names {word}, a path on a provider other than the file system")
                return certain(UNKNOWN)
        if cmdlet == "set-location":
            return self.read_location(arguments, directory, push=False)
        if cmdlet == "push-location":
            return self.read_location(arguments, directory, push=True)
        if cmdlet == "pop-location":
            if arguments:
                return certain(Directory(None, directory.stack[1:]))
            if not directory.stack:
                return certain(directory)
            return certain(Directory(directory.stack[0], directory.stack[1:]))
        return certain(directory)

    def read_location(self, arguments: list[Word], directory: Directory, push: bool) -> Outcome:
        """Set-Location or Push-Location to a literal file-system path, by -Path, -LiteralPath, or position; a leading
        ~ is home, and - and + leave the location unknown, as a path the text does not settle does."""
        if arguments and arguments[0] is not None and arguments[0].lower() in {"-path", "-literalpath"}:
            arguments = arguments[1:]
        stack = (directory.cwd, *directory.stack) if push else directory.stack
        if not arguments:
            return moved_to(directory, home(self.reading.environment), stack)
        target = arguments[0]
        if target is not None and target.startswith("-") and target != "-":
            self.reading.cannot_read(f"Set-Location's parameter {target}, which the guard does not read")
            return certain(UNKNOWN)
        if len(arguments) > 1 or target is None or target in {"-", "+"}:
            return certain(Directory(None, stack))
        if target == "~" or target.startswith(("~/", "~\\")):
            path = home(self.reading.environment) / target[2:]
        else:
            path = Path(target)
        if not path.is_absolute():
            if directory.cwd is None:
                return certain(Directory(None, stack))
            path = directory.cwd / path
        return moved_to(directory, path, stack)


ShellReader = Callable[[str, "Path | None", Reading, int], None]
# Monitor's command runs in the shell the Bash tool uses, so the guard reads it as Bash.
SHELL_READERS: dict[str, ShellReader] = {"Bash": read_bash, "Monitor": read_bash, "PowerShell": read_powershell}


def read_command(tool: str, command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """Every git call a shell tool's command makes, with where each acts, and what could not be read."""
    reading = start_reading(command, cwd, environment)
    read_into(reading, tool, cwd, environment)
    return reading


def read_into(reading: Reading, tool: str, cwd: Path, environment: Mapping[str, str]) -> None:
    """Reads the reading's command with the tool's shell reader, its hub lookups held to the reading time."""
    with lookups_within(reading.deadline):
        SHELL_READERS[tool](reading.text, cwd, reading, 0)


def start_reading(command: str, cwd: Path, environment: Mapping[str, str]) -> Reading:
    """A reading of one command, with the guarded hubs in view of the session, before any of it is read."""
    deadline = reading_clock() + READING_SECONDS
    guarded_hub_at.cache_clear()
    guarded_hub_of_repository.cache_clear()
    # The hubs in view decide whether a command the guard cannot read in time is refused, so their lookups keep
    # GUARD_GIT_TIMEOUT_SECONDS for each git call rather than end with the reading time; the session's own directories
    # bound them, never the command.
    return Reading(environment, command, context_hubs=hubs_in_view(cwd, environment), deadline=deadline)


def reached_hubs(reading: Reading, found: set[Path] | None = None) -> set[Path]:
    """The guarded hubs a reading's settled places name, each looked up within the reading time, added to found.

    A place git answers with an error for, or whose git call the reading time cuts short, may be a guarded hub's, and
    is never read as a place outside every repository: every other place is still looked up, so the hubs it reaches
    join found, and then the first such error is raised."""
    found = set() if found is None else found
    failures: list[GitError] = []
    with lookups_within(reading.deadline):
        for call in reading.calls:
            reading.time_left()
            try:
                hub, _ = reached_hub(call.place)
            except GitError as exc:
                failures.append(exc)
                continue
            if hub is not None:
                found.add(hub)
    if failures:
        raise failures[0]
    return found


def named_hubs(reading: Reading, cwd: Path, found: set[Path]) -> set[Path]:
    """The guarded hubs the paths a command names lie in, looked up when none is in view and the command holds a part
    the guard does not read, which may reach one of them; at most NAMED_PATH_LIMIT directories, each within the
    reading time. A path git answers with an error for may be a guarded hub's, raised once the others are looked up."""
    directories: list[Path] = []
    for name in reading.named:
        try:
            directory = worktrees.existing_directory(resolve(name, cwd))
        except (GitError, ValueError):
            continue  # text that is no path the system can look at names no hub
        if directory is not None and not any(same_path(directory, each) for each in directories):
            directories.append(directory)
    failures: list[GitError] = []
    with lookups_within(reading.deadline):
        for directory in directories[:NAMED_PATH_LIMIT]:
            reading.time_left()
            try:
                hub = guarded_hub_at(directory)
            except GitError as exc:
                failures.append(exc)
                continue
            if hub is not None:
                found.add(hub)
    if failures:
        raise failures[0]
    return found


# ---------------------------------------------------------------------------------------------------------------
# The hook


def guard(stream: str) -> int:
    # The command and the guarded hubs in view, known before the command is read, so that a reader or a judgment that
    # fails leaves a command the guard cannot read; None until then.
    in_view: tuple[str, set[Path]] | None = None
    # Whether the hubs of the places the command names are being looked up: a lookup the reading time cuts short, or
    # one git answers with an error for, leaves a place not looked up, which may be a guarded hub's.
    looking_up = False
    try:
        event = json_reader.loads(stream)
        tool = event.get("tool_name", "")
        tool_input = event.get("tool_input") or {}
        cwd = Path(platform_support.from_shell_path(event.get("cwd") or str(Path.cwd())))
        refused: str | None = None
        if tool in FILE_TOOLS:
            target = tool_input.get("file_path") or tool_input.get("notebook_path")
            if not isinstance(target, str):
                print(f"hub guard skipped: the {tool} event names no file path", file=sys.stderr)
            elif hub_protected(resolve(target, cwd)):
                refused = DENY_REASON
        elif tool in SHELL_READERS and not isinstance(tool_input.get("command"), str):
            print(f"hub guard skipped: the {tool} event names no command", file=sys.stderr)
        elif tool in SHELL_READERS:
            reading = start_reading(tool_input["command"], cwd, os.environ)
            hubs = set(reading.context_hubs)
            in_view = (reading.text, hubs)
            read_into(reading, tool, cwd, os.environ)
            looking_up = True
            reached_hubs(reading, hubs)
            if reading.unread and not hubs:
                named_hubs(reading, cwd, hubs)
            looking_up = False
            reason = denied(reading, hubs)
            refused = None if reason is None else f"{reason} {DENY_REASON}"
            for note in reading.notes:
                print(f"hub guard could not judge part of the command: {note}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - a broken guard must not block every tool; the regression suite pins what it refuses
        cut_short = looking_up and isinstance(exc, (ReadingTimeout, GitError))
        if in_view is None or not (in_view[1] or cut_short):
            print(f"hub guard skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0
        # Where a guarded hub is in view, a command the guard failed to read or judge is one it does not read,
        # whether a reader fails on nesting past Python's recursion limit or on any other error.
        refused = (
            f"The guard failed reading this command ({type(exc).__name__}: {exc}), so it is refused while a hub is "
            f"guarded. {REWRITE_IT} {DENY_REASON}"
        )
    if refused is not None:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": refused,
                    }
                }
            )
        )
    return 0


def main() -> int:
    return guard(sys.stdin.read())


if __name__ == "__main__":
    run_main(main, errors="backslashreplace")
