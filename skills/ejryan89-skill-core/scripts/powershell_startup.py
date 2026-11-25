"""How Windows PowerShell and pwsh read their start-up parameters, to find the command a start-up runs.

`powershell -Command "jq ."`, `pwsh -c "jq ."`, `pwsh -CommandWithArgs "jq $args" .`, and `pwsh -EncodedCommand ...`
each run a PowerShell script the command line holds, and Windows PowerShell runs as a command the first word that is
neither a parameter nor a parameter's value, where pwsh runs it as a script file. `startup_command` reads the words
after the program's name as both read them, by the catalogue below, and says where the command it runs begins. The
audit-skill scan reads it to give a start-up that runs inline code its cue, and validation's "Commands skills may run"
policy reads it to read that command's programs.

pwsh binds a start-up word to a parameter by its name alone, never by a pipeline or a splat: a word that, trimmed of
whitespace, starts with a dash (DASHES) or a slash names a parameter, the dash or slash dropped and then a second dash
like the first, and the rest names the first of PARAMETERS it is a long enough prefix of; the first word that names
none, or names -File, is the script file, and the words after it are its arguments, as the words after -Command are
its script. test_powershell_startup.py holds this reading to pwsh's own parser, run in process, for every prefix of
every parameter in every spelling, and holds PARAMETERS to the parameters pwsh lists of itself.

A stated limit: Windows PowerShell's parser, which no runner can run in process, is read as pwsh's, with its own
-PSConsoleFile and -cfn, and a -Version that takes a value, added from its help.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# What each start-up parameter does with the words after it: a SWITCH takes none, a VALUE takes the next word, COMMAND
# runs every later word joined by spaces as its script, COMMAND_WITH_ARGUMENTS runs the next word as its script and
# passes it the later words, ENCODED_COMMAND runs the next word as a script in Base64, and FILE runs the next word as a
# script file, passing it the later words.
SWITCH = "switch"
VALUE = "value"
COMMAND = "command"
COMMAND_WITH_ARGUMENTS = "command with arguments"
ENCODED_COMMAND = "encoded command"
FILE = "file"
# Every start-up parameter, each by its case-folded name, the shortest prefix of the name that names it, and what it
# does, in the order pwsh's parser (CommandLineParameterParser.ParseHelper) tries them, so a prefix names the first
# parameter it is a long enough prefix of. pwsh's Windows-only parameters are among them, since a script may start
# pwsh on Windows; on another system pwsh takes such a word as its script file, so reading it as the parameter reads
# more than pwsh runs there. Windows PowerShell's own -PSConsoleFile and -cfn follow them, as its help names them, and
# its -Version takes a value where pwsh's shows the version and ends the start-up, so reading on past it reads more
# than pwsh runs. Two of an entry's names may be one parameter's, as pwsh's -outputformat and -of are.
PARAMETERS: tuple[tuple[str, str, str], ...] = (
    ("version", "v", VALUE),
    ("help", "h", SWITCH),
    ("?", "?", SWITCH),
    ("login", "l", SWITCH),
    ("noexit", "noe", SWITCH),
    ("noprofile", "nop", SWITCH),
    ("nologo", "nol", SWITCH),
    ("noninteractive", "noni", SWITCH),
    ("socketservermode", "so", SWITCH),
    ("v2socketservermode", "v2so", SWITCH),
    ("servermode", "s", SWITCH),
    ("namedpipeservermode", "nam", SWITCH),
    ("sshservermode", "sshs", SWITCH),
    ("noprofileloadtime", "noprofileloadtime", SWITCH),
    ("interactive", "i", SWITCH),
    ("configurationfile", "configurationfile", VALUE),
    ("configurationname", "config", VALUE),
    ("custompipename", "cus", VALUE),
    ("commandwithargs", "commandwithargs", COMMAND_WITH_ARGUMENTS),
    ("cwa", "cwa", COMMAND_WITH_ARGUMENTS),
    ("command", "c", COMMAND),
    ("windowstyle", "w", VALUE),
    ("file", "f", FILE),
    ("outputformat", "o", VALUE),
    ("of", "o", VALUE),
    ("inputformat", "inp", VALUE),
    ("if", "if", VALUE),
    ("executionpolicy", "ex", VALUE),
    ("ep", "ep", VALUE),
    ("encodedcommand", "e", ENCODED_COMMAND),
    ("ec", "e", ENCODED_COMMAND),
    ("encodedarguments", "encodeda", VALUE),
    ("ea", "ea", VALUE),
    ("settingsfile", "settings", VALUE),
    ("sta", "sta", SWITCH),
    ("mta", "mta", SWITCH),
    ("workingdirectory", "wo", VALUE),
    ("wd", "wd", VALUE),
    ("removeworkingdirectorytrailingcharacter", "removeworkingdirectorytrailingcharacter", SWITCH),
    ("token", "to", VALUE),
    ("utctimestamp", "utc", VALUE),
    ("psconsolefile", "p", VALUE),
    ("cfn", "cfn", VALUE),
)
# Each way PowerShell binds an argument to a parameter, as powershell_commands.BINDING_MODES names them, that pwsh's
# start-up parameters take, with how startup_command reads it.
READ_MODES = {
    "position": "the first word that names no parameter: pwsh's script file, or Windows PowerShell's command",
    "name": "a parameter's whole name, by PARAMETERS, after any dash, en dash, em dash, horizontal bar, or slash",
    "abbreviated name": "a prefix of a name at least as long as its shortest, naming the first such of PARAMETERS",
    "alias": "each alias pwsh's parser takes, as -ep and -cwa, is an entry of PARAMETERS",
    "remaining arguments": "the words after the command's script, or a file, are its arguments and run no command",
}
# Each binding mode, and each start-up parameter, the reader leaves unread, with why.
NOT_READ = {
    "colon-joined": (
        "pwsh's start-up takes no value after a colon: -Command:x names no parameter, so pwsh runs it as its script "
        "file and the reader reads on past it, reading no less than pwsh runs"
    ),
    "end of parameters": (
        "pwsh's start-up has no --: it names no parameter, so pwsh runs it as its script file and the reader reads on "
        "past it, reading no less than pwsh runs"
    ),
    "pipeline value": "a start-up's command line has no pipeline",
    "pipeline property name": "a start-up's command line has no pipeline",
    "splatting": "a start-up's command line has no variable to splat",
    "isswait": "only a debug build of pwsh takes it, as a switch; a released pwsh runs it as its script file",
}
# The characters pwsh takes as a parameter's dash: the hyphen-minus, the en dash, the em dash, and the horizontal bar,
# as its CharExtensions.IsDash; a slash also starts a parameter, on every system.
DASHES = frozenset({"-", chr(0x2013), chr(0x2014), chr(0x2015)})
PARAMETER_STARTS = DASHES | {"/"}
# The programs whose start-up parameters these are, by the name a command calls them, without .exe.
POWERSHELL_PROGRAMS = frozenset({"powershell", "pwsh"})


@dataclass(frozen=True)
class StartupCommand:
    """The command a start-up runs: it begins at `start` among the words after the program's name. `encoded` marks an
    -EncodedCommand, whose one word is the script in Base64; `with_arguments` a -CommandWithArgs, whose one word is the
    script and whose later words its arguments; any other runs every word from `start` on, joined by spaces, as its
    script. `start` is the number of words when the parameter that names the command is the last word."""

    start: int
    encoded: bool = False
    with_arguments: bool = False


@dataclass(frozen=True)
class UnknownWord:
    """A word whose text the caller does not know stands at `index`, where the reading needed it to tell a parameter
    from a value or a command."""

    index: int


def parameter_name(word: str) -> str | None:
    """The case-folded name a start-up word gives a parameter, as pwsh reads it: the word trimmed of whitespace, its
    first character a dash or a slash, dropped, and a second dash like the first dropped too; None for a word that
    names no parameter. Python's whitespace holds every character .NET's Trim removes."""
    text = word.strip()
    if not text or text[0] not in PARAMETER_STARTS:
        return None
    first, text = text[0], text[1:]
    if first in DASHES and text.startswith(first):
        text = text[1:]
    return text.casefold()


def parameter_kind(name: str) -> str | None:
    """What the start-up parameter a case-folded name gives does, as PARAMETERS names it: the first parameter the name
    is a prefix of, at least as long as its shortest prefix. None for a name that names no parameter."""
    for parameter, shortest, kind in PARAMETERS:
        if len(name) >= len(shortest) and parameter.startswith(name):
            return kind
    return None


def startup_command(
    words: Sequence[str | None], positional_command: bool = False, positional_file: bool = False
) -> StartupCommand | UnknownWord | None:
    """The command PowerShell's start-up parameters run: -Command, -CommandWithArgs, or -EncodedCommand, in any
    spelling PowerShell reads as one, before any -File; given `positional_command`, the first word that names no
    parameter, which Windows PowerShell runs as -Command's. Given `positional_file`, that word ends the reading with no
    command, since pwsh runs it as -File's; given neither, it is passed over, as is a word whose dash names no
    parameter. None for a start-up that runs no command the words hold. A parameter holds the next word where
    PARAMETERS names it a VALUE. A word given as None, one whose text the caller cannot read, gives UnknownWord where
    it is reached."""
    rest = iter(enumerate(words))
    for index, word in rest:
        if word is None:
            return UnknownWord(index)
        name = parameter_name(word)
        kind = None if name is None else parameter_kind(name)
        if name is None:
            if positional_command:
                return StartupCommand(index)
            if positional_file:
                return None
        elif kind == FILE:
            return None
        elif kind == COMMAND_WITH_ARGUMENTS:
            return StartupCommand(index + 1, with_arguments=True)
        elif kind == COMMAND:
            return StartupCommand(index + 1)
        elif kind == ENCODED_COMMAND:
            return StartupCommand(index + 1, encoded=True)
        elif kind == VALUE:
            next(rest, None)
    return None
