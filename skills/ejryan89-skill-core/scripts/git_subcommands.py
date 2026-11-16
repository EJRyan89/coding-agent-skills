"""The one reader of the git subcommands a skill's Bash and PowerShell scripts run.

A skill's runtime support is held to the git subcommands its scripts run, since one that reaches a remote needs network
access. Repository validation's runtime-support policy and audit-skill's runtime-support reader both read a script's
git subcommands here, so the two derive the same needs: a Bash script by `bash_git_subcommands`, and a PowerShell
script by `powershell_git_subcommands` over the commands PowerShell's own parser lists for it, as
powershell_commands's COMMANDS_RUN reads them. A Python script's git calls are runtime_support's
`python_git_subcommands`.

A Bash script is read through bash_syntax, the repository's one Bash grammar, which Bash's own syntax check holds: a
command is read wherever Bash starts one, in a substitution, a case item, and a function's body too, and a comment and
a here-document's body run nothing, but the command substitutions of an unquoted body run. A script the grammar
cannot read raises its BashSyntaxError, for its caller to fail. A command is read past its leading assignments, each
wrapper of command_wrappers' catalogue, and find's actions, to a git command's own words, and into the script eval,
a shell's -c, or a pwsh or Windows PowerShell start-up runs. A program is the one its word's file name names, so git
named by a path or with .exe is git, in Bash as written and in PowerShell in any case. The script a start-up runs is
PowerShell, which the caller's PowerShellReader lists the commands of, so validation reads it through its toolchain
and a skill's script through powershell_commands.read.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from pathlib import PureWindowsPath

import bash_syntax
import command_wrappers
import git_client
from command_wrappers import bash_program
from executable_locator import program_name
from powershell_commands import UNREAD_WORD
from powershell_startup import POWERSHELL_PROGRAMS, UnknownWord, startup_command

# A part of a Bash word only running settles, such as a substitution, as it stands in the word: one Bash word, free of
# quotes and metacharacters, so the script eval or a shell's -c runs keeps it one word wherever it stands, and no
# subcommand's name, so a git subcommand holding it is read as UNREAD_WORD, one that may be any. A PowerShell command's
# word no literal holds, the parser's UNREAD_WORD, is read in this form too, as bash_word renders it.
UNREAD_PART = "%unread%"
# The words of each external command a PowerShell script runs, its program's name first, as
# powershell_commands.command_words gives them.
PowerShellReader = Callable[[str], Sequence[Sequence[str]]]


def bash_git_subcommands(text: str, read_powershell: PowerShellReader) -> set[str]:
    """The subcommand of each git command a Bash script runs, read through bash_syntax, the grammar Bash's own syntax
    check holds: one wherever Bash starts a command, a substitution's, a case item's, and a function body's included,
    one a wrapper, find's actions, eval, a shell's -c script, or a PowerShell start-up runs, and one a substitution in
    an unquoted here-document's body runs. A comment, and a here-document's body, are read as Bash reads them, so
    neither runs a command, and a line ends at LF alone.

    A script the grammar cannot read raises its BashSyntaxError, and so does the script eval or a shell's -c runs:
    Bash runs the lines before a syntax error, so a reader that guessed at such a script could miss a git command it
    runs. A part
    of a word only running settles, such as a substitution, is UNREAD_PART where it stands: one word in the script
    eval or a shell's -c runs, so `eval "git push $(x)"` reads push and `eval "$(ssh-agent -s)"` reads nothing, and a
    git subcommand holding it is UNREAD_WORD, one that may be any. An ANSI-C `$'...'` string holding an escape is such
    a part, so `bash -c $'git push\\n'` reads no subcommand.

    Beyond what it reads: a here-document a shell reads as its script, as in `bash <<EOF`, `bash -s <<'EOF'`, or
    `source /dev/stdin <<EOF`, is data, so a git command in its body goes unread. No script here writes one."""
    return {
        found
        for command in bash_syntax.simple_commands(text)
        for found in program_git_subcommands(bash_syntax.command_words(command, UNREAD_PART), read_powershell)
    }


def powershell_git_subcommands(commands: Iterable[Sequence[str]], read_powershell: PowerShellReader) -> set[str]:
    """The subcommand of each git command a PowerShell script runs, given the words of each external command it runs:
    git's own, and one a wrapper, find's actions, a shell's -c script, or a PowerShell start-up runs. A word that is
    no literal is the parser's UNREAD_WORD, so a subcommand no literal names is read as that word. PowerShell finds
    the program a command names in any case and by a path with either slash, so `Git`, `GIT.EXE`, and
    `C:/Program Files/Git/cmd/git` run git; a Bash script's program is matched as written."""
    return {
        subcommand
        for words in commands
        if words
        for subcommand in program_git_subcommands(
            [bash_word(word) for word in [powershell_program(words[0]), *words[1:]]], read_powershell
        )
    }


def bash_word(word: str) -> str:
    """A PowerShell command's word in the form a Bash command's word takes, so program_git_subcommands reads the two
    alike: the parser's UNREAD_WORD, a word no literal holds, as UNREAD_PART, one word only running settles, which
    the script eval or a shell's -c runs keeps one word; any other word unchanged."""
    return UNREAD_PART if word == UNREAD_WORD else word


def powershell_program(word: str) -> str:
    """The program a PowerShell command's first word names, as PowerShell finds it: its file name without .exe in any
    case, folded; UNREAD_WORD unchanged."""
    return word if word == UNREAD_WORD else program_name(PureWindowsPath(word).name).casefold()


def program_git_subcommands(words: list[str], read_powershell: PowerShellReader) -> set[str]:
    """The git subcommand a simple command runs, given its words from its program's name, each part only running
    settles as UNREAD_PART, a PowerShell command's as bash_word renders it: git's own, or one each wrapper of
    command_wrappers' catalogue, find's actions, eval, a shell's -c script, or a PowerShell start-up's script runs.
    A -c script that is UNREAD_PART alone runs nothing this can read."""
    while words:
        head, rest = words[0], words[1:]
        name = bash_program(head)
        if name == "git":
            subcommand = git_client.subcommand(rest)
            return {UNREAD_WORD if subcommand and UNREAD_PART in subcommand else subcommand} if subcommand else set()
        if head == "eval":
            # Each UNREAD_PART stands in eval's script as one word, never as script text of its own.
            return bash_git_subcommands(" ".join(rest), read_powershell)
        if name in command_wrappers.SCRIPT_SHELLS:
            index = command_wrappers.shell_script_index(rest)
            if index is None or rest[index] == UNREAD_PART:
                return set()
            return bash_git_subcommands(rest[index], read_powershell)
        program = program_name(PureWindowsPath(head).name).casefold()
        if program in POWERSHELL_PROGRAMS:
            return startup_git_subcommands(program, rest, read_powershell)
        if name == "find":
            return {
                found
                for action in command_wrappers.find_actions(rest)
                for found in program_git_subcommands(action, read_powershell)
            }
        words = command_wrappers.unwrapped([name, *rest])
        if words and words[0].startswith(command_wrappers.UNREAD_PROGRAM):
            return set()
    return set()


def startup_git_subcommands(program: str, words: list[str], read_powershell: PowerShellReader) -> set[str]:
    """The git subcommands the script a pwsh or Windows PowerShell start-up runs, given its words after the program,
    as powershell_startup reads its parameters: none for a start-up that runs no script, an -EncodedCommand, a
    script read from standard input, or one a word holding a part only running settles, UNREAD_PART, holds or may
    name, whose commands no reader sees."""
    known = [None if UNREAD_PART in word else word for word in words]
    command = startup_command(known, positional_command=program == "powershell", positional_file=program == "pwsh")
    if command is None or isinstance(command, UnknownWord) or command.encoded:
        return set()
    script = known[command.start : command.start + 1 if command.with_arguments else len(known)]
    texts = [word for word in script if word is not None]
    if not texts or texts == ["-"] or len(texts) < len(script):
        return set()
    return powershell_git_subcommands(read_powershell(" ".join(texts)), read_powershell)
