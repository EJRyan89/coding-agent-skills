"""The one reader of the git subcommands a skill's Bash and PowerShell scripts run.

A skill's runtime support is held to the git subcommands its scripts run, since one that reaches a remote needs network
access. Repository validation's runtime-support policy and audit-skill's runtime-support reader both read a script's
git subcommands here, so the two derive the same needs: a Bash script by `bash_git_subcommands`, and a PowerShell
script by `powershell_git_subcommands` over the commands PowerShell's own parser lists for it, as
powershell_commands's COMMANDS_RUN reads them. A Python script's git calls are runtime_support's
`python_git_subcommands`.

A Bash command is read wherever Bash starts one: at a line's start, after an operator, in a group, a subshell, or a
substitution, in each case item after its pattern, and in a function's body; a case word, a pattern, and a function's
name run nothing. A line ends at LF alone, as Bash ends one, and a here-document's body is data, of which only the
command substitutions of an unquoted one run. A command is read past its leading keywords and assignments, Bash's
time keyword, each wrapper of command_wrappers' catalogue, and find's actions, to a git command's own words, and into
the script eval, a shell's -c, or a pwsh or Windows PowerShell start-up runs. A program is the one its word's file
name names, so git named by a path or with .exe is git, in Bash as written and in PowerShell in any case. The script a
start-up runs is PowerShell, which the caller's PowerShellReader lists the commands of, so validation reads it through
its toolchain and a skill's script through powershell_commands.read.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
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
# A Bash word as written, its quoted and escaped parts kept, up to the unquoted blank that ends it.
SHELL_WORD = re.compile(r"(?:\\.|'[^']*'|\"(?:\\.|[^\"\\])*\"|[^ \t\r\n'\"\\])+", re.DOTALL)
# The blanks shlex splits a command's words at.
WORD_BLANKS = " \t\r\n"
# The words that may stand before a command's program: Bash's reserved words and a group's brace, each a keyword only
# spelled with no quote or escape, as Bash recognizes a reserved word. Bash's time keyword is read by program_start.
LEADING_WORDS = frozenset({"if", "then", "else", "elif", "while", "until", "do", "!", "{"})
# The starts of a substitution's command, which runs wherever it stands, in a case word or a pattern too.
SUBSTITUTIONS = frozenset({"$(", "`"})
# What CommandStarts reads a backquote that closes its substitution as, since SHELL_COMMAND starts a text at both.
BACKQUOTE_END = "`)"
# The blanks Bash splits words at and a comment's `#` may follow: the space and the tab, never the form feed, the
# line separator, or another character str.strip() removes.
BASH_BLANKS = " \t"
# What ends an unquoted word in Bash, so a `#` after one of them, or at a line's start, starts a comment.
WORD_ENDS = " \t;&|()<>"
# A double-quoted string from its opening quote, a backslash escaping the character after it.
DOUBLE_QUOTED = re.compile(r'"(?:\\.|[^"\\])*"', re.DOTALL)
# A here-document's delimiter word as written, up to the blank or metacharacter that ends it, its quoted and escaped
# parts kept: a $'...' string, whose backslashes escape, a '...' one, whose do not, and a "..." or $"..." one.
DELIMITER_WORD = re.compile(
    r"(?:\\.|\$'(?:\\.|[^'\\])*'|'[^']*'|\$?\"(?:\\.|[^\"\\])*\"|[^ \t\n|&;()<>'\"\\])+", re.DOTALL
)
# Each part of a delimiter word, by which Bash removes its quotes: an escaped character, a $'...' string, a '...' one,
# a "..." or $"..." one, or any other character.
DELIMITER_PART = re.compile(r"\\(.)|\$'((?:\\.|[^'\\])*)'|'([^']*)'|\$?\"((?:\\.|[^\"\\])*)\"|(.)", re.DOTALL)
# The characters a backslash escapes inside double quotes, where any other backslash stays.
DOUBLE_QUOTE_ESCAPES = re.compile(r"\\([$`\"\\\n])")


@dataclass(frozen=True)
class HereDocument:
    """A here-document a command line opens: the delimiter its body ends at, after Bash's quote removal; whether
    `<<-` strips its lines' leading tabs; and whether any part of the delimiter was quoted or escaped, which leaves
    the body as written, with no expansion and no line continuation."""

    delimiter: str
    strip_tabs: bool
    quoted: bool


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
        # The case, in, and esac a text is read by are reserved words, which Bash recognizes only unquoted.
        words = [spelling for _, spelling in spelled_words(command)]
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
        program = words[program_start(words, reserved=True) :]
        if program[:1] == ["case"]:
            self.state = "patterns" if program[2:3] == ["in"] else "case-word"
            return False
        return True


def bash_git_subcommands(text: str, read_powershell: PowerShellReader) -> set[str]:
    """The subcommand of each git command a Bash script runs, outside comments and here-document bodies: one where a
    command starts, a substitution's, a case item's, and a function body's included, one a wrapper, find's actions,
    eval, a shell's -c script, or a PowerShell start-up runs, and one a substitution in an unquoted here-document's
    body runs.

    A line ends at LF alone, as Bash ends one, so a line separator, a form feed, or another character
    str.splitlines() breaks at stays inside its line; a backslash an odd count of them ends a line with continues it,
    unless a comment holds it, since a comment ends at the LF.
    A here-document's body, the lines after the line that opens it up to its delimiter, is data, as read_line
    reads it. A body the text never closes is read as commands, so a delimiter read otherwise than Bash reads it,
    such as a $'...' one holding an escape, hides no command; Bash would run none there.

    Beyond what it reads: a here-document a shell reads as its script, as in `bash <<EOF`, `bash -s <<'EOF'`, or
    `source /dev/stdin <<EOF`, is read as data, so a git command in its body goes unread; and a `<<` that Bash reads
    inside a quote or a parameter expansion, such as one in a single-quoted string spanning lines or in
    `${x//<<a/b}`, opens a here-document here, so lines up to one equal to its word go unread; and a line inside a
    quote spanning lines whose word starts with `#` and that ends in a backslash is read as a comment, so the line
    after it is read as a command Bash joins as arguments. No script here writes any of these."""
    found: set[str] = set()
    after_pipe = False
    starts = CommandStarts()
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line, index = command_line(lines, index)
        documents, _ = read_line(line)
        if not line.lstrip(BASH_BLANKS).startswith("#"):
            start, command = "", ""
            for match in SHELL_COMMAND.finditer(line):
                # After a pipe, or at the start of the line right after one, `time` is the time program, not the
                # keyword.
                reserved = match.group(1) not in PIPES and not (match.group(1) == "" and after_pipe)
                start, command = match.groups()
                if not starts.runs(start, command):
                    continue
                spelled = spelled_words(command)
                words, spellings = [word for word, _ in spelled], [spelling for _, spelling in spelled]
                found |= command_git_subcommands(words, read_powershell, reserved, spellings)
            # A line whose last operator is a pipe with no command after it hands its output to the next line's
            # command.
            after_pipe = start in PIPES and not command.strip()
        for document in documents:
            body = here_document_body(lines, index, document)
            if body is None:
                break
            text_read, index = body
            for inner in substitutions(text_read):
                found |= bash_git_subcommands(inner, read_powershell)
    return found


def escapes_its_end(line: str) -> bool:
    """Whether a line ends in an odd count of backslashes, the last of which escapes the LF after it."""
    return (len(line) - len(line.rstrip("\\"))) % 2 == 1


def command_line(lines: Sequence[str], index: int) -> tuple[str, int]:
    """The command line at `index`, each line its backslash continues joined to it by a space, and the index after
    the last line read. A backslash inside a comment continues nothing, since the comment ends at the LF, so a line
    whose comment ends in one is a line of its own."""
    line = lines[index]
    index += 1
    while index < len(lines) and escapes_its_end(line) and not read_line(line)[1]:
        line = line[:-1] + " " + lines[index]
        index += 1
    return line, index


def continued_line(lines: Sequence[str], index: int) -> tuple[str, int]:
    """The line at `index` of an unquoted here-document's body with each line its backslash continues joined to it,
    as Bash removes that backslash and the LF, and the index after the last line read."""
    line = lines[index]
    index += 1
    while index < len(lines) and escapes_its_end(line):
        line = line[:-1] + lines[index]
        index += 1
    return line, index


def read_line(line: str) -> tuple[list[HereDocument], bool]:
    """The here-documents a command line opens, in order, and whether a comment ends it: each `<<` or `<<-` outside
    quotes, escapes, a comment, and arithmetic, `$((...))` or `((...))`, where `<<` shifts, with the delimiter word
    after it and any blanks; a `<<<` here-string opens none, and neither does a `<<` with no word after it, which Bash
    refuses. A comment is a `#` that starts a word outside quotes and escapes."""
    found: list[HereDocument] = []
    index = 0
    word_start = True
    while index < len(line):
        character = line[index]
        after = index + 1
        if character == "\\":
            after = index + 2
        elif character == "'":
            closing = line.find("'", index + 1)
            after = len(line) if closing < 0 else closing + 1
        elif character == '"':
            quoted = DOUBLE_QUOTED.match(line, index)
            after = len(line) if quoted is None else quoted.end()
        elif character == "#" and word_start:
            return found, True
        elif line.startswith("((", index) or line.startswith("$((", index):
            after = closing_parenthesis(line, line.index("(", index) + 1) + 1
        elif line.startswith("<<<", index):
            after = index + 3
        elif line.startswith("<<", index):
            strip_tabs = line.startswith("<<-", index)
            position = index + (3 if strip_tabs else 2)
            while position < len(line) and line[position] in BASH_BLANKS:
                position += 1
            word = DELIMITER_WORD.match(line, position)
            if word is not None:
                found.append(
                    HereDocument(delimiter_text(word.group()), strip_tabs, any(c in word.group() for c in "'\"\\"))
                )
                position = word.end()
            after = position
        word_start = character in WORD_ENDS
        index = after
    return found, False


def delimiter_text(word: str) -> str:
    """A delimiter word after Bash's quote removal: an escaped character as itself, a quoted string's content, in
    double quotes with a backslash removed only before the characters it escapes there, and any other character as
    written; a $'...' string's escapes are kept as written."""
    parts: list[str] = []
    for escaped, ansi, single, double, plain in DELIMITER_PART.findall(word):
        if double:
            parts.append(DOUBLE_QUOTE_ESCAPES.sub(r"\1", double))
        else:
            parts.append(escaped or ansi or single or plain)
    return "".join(parts)


def here_document_body(lines: Sequence[str], index: int, document: HereDocument) -> tuple[str, int] | None:
    """A here-document's body read from the line at `index`, as the text Bash expands, and the index after its
    delimiter line; None when no line is its delimiter. A line is the delimiter when it is the delimiter text exactly,
    after its leading tabs are removed for `<<-`; an unquoted body first joins each line its backslash continues, as
    Bash removes that backslash and the LF, so a delimiter after one is body text. A quoted body is never expanded,
    so its text is empty."""
    body: list[str] = []
    while index < len(lines):
        if document.quoted:
            line, index = lines[index], index + 1
        else:
            line, index = continued_line(lines, index)
        compared = line.lstrip("\t") if document.strip_tabs else line
        if compared == document.delimiter:
            return ("" if document.quoted else "\n".join(body)), index
        body.append(compared)
    return None


def substitutions(text: str) -> list[str]:
    """The script of each command substitution in an unquoted here-document's body, `$(...)` or backquoted, outside
    the backslash escapes; quotes are text in a body, so neither kind hides one. An arithmetic `$((...))` is read as
    one too, whose script runs nothing."""
    found: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text.startswith("$(", index):
            end = closing_parenthesis(text, index + 2)
            found.append(text[index + 2 : end])
            index = end + 1
        elif text[index] == "`":
            end = backquote_end(text, index + 1)
            found.append(text[index + 1 : end])
            index = end + 1
        else:
            index += 1
    return found


def closing_parenthesis(text: str, start: int) -> int:
    """Where the `)` closing a parenthesis opened before `start` stands, past nested pairs, quoted strings, and
    escapes, or the text's length when none closes it."""
    depth = 1
    index = start
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == "'":
            closing = text.find("'", index + 1)
            index = len(text) if closing < 0 else closing + 1
            continue
        if character == '"':
            quoted = DOUBLE_QUOTED.match(text, index)
            index = len(text) if quoted is None else quoted.end()
            continue
        depth += {"(": 1, ")": -1}.get(character, 0)
        if depth == 0:
            return index
        index += 1
    return len(text)


def backquote_end(text: str, start: int) -> int:
    """Where the backquote closing one opened before `start` stands, past escapes, or the text's length."""
    index = start
    while index < len(text) and text[index] != "`":
        index += 2 if text[index] == "\\" else 1
    return min(index, len(text))


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


def spelled_words(command: str) -> list[tuple[str, str]]:
    """A command's words as Bash splits them, each with its quotes removed beside its spelling as written, so a
    reader can tell `if` from `'if'` and `\\!`; a command shlex cannot split, as one with an unclosed quote, is split
    at whitespace with each word's outer quotes stripped, beside the word as written."""
    spellings = SHELL_WORD.findall(command)
    try:
        if SHELL_WORD.sub("", command).strip(WORD_BLANKS):
            raise ValueError(command)
        return [(shlex.split(spelling)[0], spelling) for spelling in spellings]
    except ValueError:
        return [(word.strip("\"'"), word) for word in command.split()]


def command_git_subcommands(
    words: list[str], read_powershell: PowerShellReader, reserved: bool = True, spellings: list[str] | None = None
) -> set[str]:
    """The git subcommand a simple command runs: past its leading keywords and assignments, Bash's time keyword and the
    -p and -- it takes, through each wrapper of command_wrappers' catalogue, find's actions, eval, a shell's -c
    script, and a PowerShell start-up's script, to a git command's own words. `reserved` says `time` in the command's
    place is Bash's keyword, false where a pipe or find's action runs it, and in PowerShell, which has no such
    keyword; after an assignment it is the time program, which the catalogue reads. `spellings`, given, are the
    words as written, by which a keyword or an assignment is read only unquoted; without them each word is its own
    spelling."""
    words = words[program_start(words if spellings is None else spellings, reserved) :]
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


def program_start(spellings: Sequence[str], reserved: bool) -> int:
    """Where a command's program stands among its words, given each as written: past its leading keywords and
    assignments, a function's `function` and name, and Bash's time keyword where `reserved` holds and no assignment
    stands before it, with the -p and then the --, each optional, it takes before the pipeline it times. As Bash reads
    them, a keyword is one only spelled with no quote or escape, and an assignment only with its name and `=`
    unquoted, so `'if' git` runs the program `if`, and `"A=1" git` the program `A=1`."""
    index = 0
    while index < len(spellings):
        word = spellings[index]
        if word == "function" and reserved:
            index += 2
        elif word == "time" and reserved:
            index += 1
            for option in TIME_KEYWORD_OPTIONS:
                if index < len(spellings) and spellings[index] == option:
                    index += 1
        elif word in LEADING_WORDS:
            index += 1
        elif ASSIGNMENT.match(word):
            reserved = False
            index += 1
        else:
            break
    return min(index, len(spellings))
