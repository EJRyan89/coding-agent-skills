"""The one reader of the git subcommands a skill's Bash and PowerShell scripts run.

A skill's runtime support is held to the git subcommands its scripts run, since one that reaches a remote needs network
access. Repository validation's runtime-support policy and audit-skill's runtime-support reader both read a script's
git subcommands here, so the two derive the same needs: a Bash script by `bash_git_subcommands`, and a PowerShell
script by `powershell_git_subcommands` over the commands PowerShell's own parser lists for it, as
powershell_commands's COMMANDS_RUN reads them. A Python script's git calls are runtime_support's
`python_git_subcommands`.

A Bash command is read wherever Bash starts one: at a line's start, after an operator, in a group, a subshell, or a
substitution, in each case item after its pattern, and in a function's body; a case word, a pattern, and a function's
name run nothing. A command is read past its leading keywords and assignments, Bash's time keyword, each wrapper of
command_wrappers' catalogue, and find's actions, to a git command's own words, and into the script eval, a shell's -c,
or a pwsh or Windows PowerShell start-up runs. A program is the one its word's file name names, so git named by a path
or with .exe is git, in Bash as written and in PowerShell in any case. The script a start-up runs is PowerShell, which
the caller's PowerShellReader lists the commands of, so validation reads it through its toolchain and a skill's script
through powershell_commands.read.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from pathlib import PureWindowsPath

import command_wrappers
import git_client
from command_wrappers import ASSIGNMENT, CASE_ITEM_ENDS, PIPES, TIME_KEYWORD_OPTIONS, bash_program
from executable_locator import program_name
from powershell_commands import UNREAD_WORD
from powershell_startup import POWERSHELL_PROGRAMS, UnknownWord, startup_command

# The words of each external command a PowerShell script runs, its program's name first, as
# powershell_commands.command_words gives them.
PowerShellReader = Callable[[str], Sequence[Sequence[str]]]

# A Bash command's text from where it may start, at the line's start, after an operator, a case item's end, an opening
# or closing parenthesis, or inside a substitution, up to the operator or substitution that ends it outside quotes and
# escapes; a substitution inside it is its own. A double quote a substitution splits from its pair stays in the text
# either side, so a quoted case word's `in` is read after it. CommandStarts says which of these texts Bash runs.
SHELL_COMMAND = re.compile(
    r"(^|;;&|;;|;&|\|\||&&|\|&|[;&|()`]|\$\()"
    r"((?:\\.|'[^']*'|\"(?:\\.|[^\"\\$`]|\$(?!\())*\"|\$(?!\()|[^;&|()`\n\\'\"$]|\")*)"
)
# The words that may stand before a command's program: Bash's reserved words and a group's brace. Bash's time keyword
# is read by program_words.
LEADING_WORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
# The starts of a substitution's command, which runs wherever it stands, in a case word or a pattern too.
SUBSTITUTIONS = frozenset({"$(", "`"})
# What CommandStarts reads a backquote that closes its substitution as, since SHELL_COMMAND starts a text at both.
BACKQUOTE_END = "`)"


class CommandStarts:
    """Which of a Bash script's texts, as SHELL_COMMAND splits them in order, Bash runs as a command, as validation's
    shell_commands reads them: a case word up to its `in`, and each pattern up to the `)` that ends it, run nothing but
    their substitutions; the text after that `)` is the item's command, up to the ;; ;& or ;;& that ends the item or
    the `esac` that ends the case. The text after any other `)`, or after the backquote that closes a substitution,
    runs only where it ends a function's `()`, since a subshell or a substitution is followed by a redirection, an
    operator, or the rest of the word or command it stands in."""

    def __init__(self) -> None:
        self.state = "command"
        self.previous = ("", "-")
        self.backquoted = False

    def runs(self, start: str, command: str) -> bool:
        """Whether the text `command` that `start` begins runs as a command, read as the script's next text."""
        previous, self.previous = self.previous, (start, command)
        words = shell_words(command)
        if start == "`":
            self.backquoted = not self.backquoted
            start = start if self.backquoted else BACKQUOTE_END
        if start in SUBSTITUTIONS and self.state != "command":
            return True
        if start in CASE_ITEM_ENDS:
            self.state = "patterns"
        if self.state == "case-word":
            # After a substitution in a quoted case word, the text first ends that word, as `" in` does.
            if words[:1] == ["in"] or (command[:1].strip() and words[1:2] == ["in"]):
                self.state = "patterns"
            return False
        if self.state == "patterns":
            if start != ")":
                if words[:1] == ["esac"]:
                    self.state = "command"
                return False
            self.state = "command"
        elif start == BACKQUOTE_END or (start == ")" and not (previous[0] == "(" and not previous[1].strip())):
            return False
        program = program_words(words, reserved=True)
        if program[:1] == ["case"]:
            self.state = "patterns" if program[2:3] == ["in"] else "case-word"
            return False
        return True


def bash_git_subcommands(text: str, read_powershell: PowerShellReader) -> set[str]:
    """The subcommand of each git command a Bash script runs, outside comments: one where a command starts, a
    substitution's, a case item's, and a function body's included, and one a wrapper, find's actions, eval, a shell's
    -c script, or a PowerShell start-up runs."""
    found: set[str] = set()
    after_pipe = False
    starts = CommandStarts()
    for line in text.replace("\\\n", " ").splitlines():
        if line.lstrip().startswith("#"):
            continue
        start, command = "", ""
        for match in SHELL_COMMAND.finditer(line):
            # After a pipe, or at the start of the line right after one, `time` is the time program, not the keyword.
            reserved = match.group(1) not in PIPES and not (match.group(1) == "" and after_pipe)
            start, command = match.groups()
            if not starts.runs(start, command):
                continue
            found |= command_git_subcommands(shell_words(command), read_powershell, reserved)
        # A line whose last operator is a pipe with no command after it hands its output to the next line's command.
        after_pipe = start in PIPES and not command.strip()
    return found


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
        for subcommand in command_git_subcommands(
            [powershell_program(words[0]), *words[1:]], read_powershell, reserved=False
        )
    }


def powershell_program(word: str) -> str:
    """The program a PowerShell command's first word names, as PowerShell finds it: its file name without .exe in any
    case, folded; UNREAD_WORD unchanged."""
    return word if word == UNREAD_WORD else program_name(PureWindowsPath(word).name).casefold()


def shell_words(command: str) -> list[str]:
    """A command's words as Bash splits them, its quotes removed; a command shlex cannot split, as one with an unclosed
    quote, is split at whitespace with each word's outer quotes stripped."""
    try:
        return shlex.split(command)
    except ValueError:
        return [word.strip("\"'") for word in command.split()]


def command_git_subcommands(words: list[str], read_powershell: PowerShellReader, reserved: bool = True) -> set[str]:
    """The git subcommand a simple command runs: past its leading keywords and assignments, Bash's time keyword and the
    -p and -- it takes, through each wrapper of command_wrappers' catalogue, find's actions, eval, a shell's -c
    script, and a PowerShell start-up's script, to a git command's own words. `reserved` says `time` in the command's
    place is Bash's keyword, false where a pipe or find's action runs it, and in PowerShell, which has no such
    keyword; after an assignment it is the time program, which the catalogue reads."""
    words = program_words(words, reserved)
    while words:
        head, rest = words[0], words[1:]
        name = bash_program(head)
        if name == "git":
            subcommand = git_client.subcommand(rest)
            return {subcommand} if subcommand else set()
        if head == "eval":
            return bash_git_subcommands(" ".join(rest), read_powershell)
        if name in command_wrappers.SCRIPT_SHELLS:
            index = command_wrappers.shell_script_index(rest)
            return bash_git_subcommands(rest[index], read_powershell) if index is not None else set()
        program = program_name(PureWindowsPath(head).name).casefold()
        if program in POWERSHELL_PROGRAMS:
            return startup_git_subcommands(program, rest, read_powershell)
        if name == "find":
            return {
                found
                for action in command_wrappers.find_actions(rest)
                for found in command_git_subcommands(action, read_powershell, reserved=False)
            }
        words = command_wrappers.unwrapped([name, *rest])
        if words and words[0].startswith(command_wrappers.UNREAD_PROGRAM):
            return set()
    return set()


def startup_git_subcommands(program: str, words: list[str], read_powershell: PowerShellReader) -> set[str]:
    """The git subcommands the script a pwsh or Windows PowerShell start-up runs, given its words after the program,
    as powershell_startup reads its parameters: none for a start-up that runs no script, an -EncodedCommand, a
    script read from standard input, or one a word that is no literal, UNREAD_WORD, holds or may name, whose commands
    no reader sees."""
    known = [None if word == UNREAD_WORD else word for word in words]
    command = startup_command(known, positional_command=program == "powershell", positional_file=program == "pwsh")
    if command is None or isinstance(command, UnknownWord) or command.encoded:
        return set()
    script = known[command.start : command.start + 1 if command.with_arguments else len(known)]
    texts = [word for word in script if word is not None]
    if not texts or texts == ["-"] or len(texts) < len(script):
        return set()
    return powershell_git_subcommands(read_powershell(" ".join(texts)), read_powershell)


def program_words(words: list[str], reserved: bool) -> list[str]:
    """A command's words from its program on: past its leading keywords and assignments, a function's `function` and
    name, and Bash's time keyword where `reserved` holds and no assignment stands before it, with the -p and then the
    --, each optional, it takes before the pipeline it times."""
    while words:
        if words[0] == "function" and reserved:
            words = words[2:]
        elif words[0] == "time" and reserved:
            words = words[1:]
            for option in TIME_KEYWORD_OPTIONS:
                if words and words[0] == option:
                    words = words[1:]
        elif words[0] in LEADING_WORDS:
            words = words[1:]
        elif ASSIGNMENT.match(words[0]):
            reserved = False
            words = words[1:]
        else:
            break
    return words
