"""How Windows PowerShell and pwsh read their start-up parameters, to find the command a start-up runs.

`powershell -Command "jq ."`, `pwsh -c "jq ."`, `pwsh -CommandWithArgs "jq $args" .`, and `pwsh -EncodedCommand ...`
each run a PowerShell script the command line holds, and Windows PowerShell runs as a command the first word that is
neither a parameter nor a parameter's value, where pwsh runs it as a script file. `startup_command` reads the words
after the program's name as both read them, by the catalogue below, and says where the command it runs begins. The
audit-skill scan reads it to give a start-up that runs inline code its cue, and validation's "Commands skills may run"
policy reads it to read that command's programs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# The start-up parameters of Windows PowerShell and pwsh that take the next word as their value, besides -Command,
# -CommandWithArgs, -EncodedCommand, and -File, case-folded; the aliases of some; and the switches, which take none,
# so a prefix that one of them shares names no value-taking parameter.
POWERSHELL_VALUE_PARAMETERS = (
    "configurationfile",
    "configurationname",
    "custompipename",
    "executionpolicy",
    "inputformat",
    "outputformat",
    "psconsolefile",
    "settingsfile",
    "version",
    "windowstyle",
    "workingdirectory",
)
POWERSHELL_VALUE_ALIASES = frozenset({"ep", "if", "of", "wd", "cfn"})
POWERSHELL_SWITCHES = (
    "help",
    "interactive",
    "login",
    "mta",
    "noexit",
    "nologo",
    "noninteractive",
    "noprofile",
    "noprofileloadtime",
    "sta",
)
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


def startup_command(
    words: Sequence[str | None], positional_command: bool = False, positional_file: bool = False
) -> StartupCommand | UnknownWord | None:
    """The command PowerShell's start-up parameters run: -Command, -CommandWithArgs, or -EncodedCommand, by any prefix
    PowerShell reads as one, before any -File; given `positional_command`, the first word that is neither a parameter
    nor a parameter's value, which Windows PowerShell runs as -Command's. Given `positional_file`, that word ends the
    reading with no command, since pwsh runs it as -File's; given neither, it is passed over. None for a start-up that
    runs no command the words hold. A parameter is taken to hold the next word only where it names one of
    POWERSHELL_VALUE_PARAMETERS and no switch, so a word after a parameter this reader does not know is read as the
    command. A word given as None, one whose text the caller cannot read, gives UnknownWord where it is reached."""
    index = 0
    while index < len(words):
        word = words[index]
        if word is None:
            return UnknownWord(index)
        if not word.startswith("-"):
            if positional_command:
                return StartupCommand(index)
            if positional_file:
                return None
            index += 1
            continue
        name = word.casefold()[1:]
        if name == "f" or (len(name) >= 2 and "file".startswith(name)):
            return None
        if name == "cwa" or (len(name) >= 8 and "commandwithargs".startswith(name)):
            return StartupCommand(index + 1, with_arguments=True)
        if name == "c" or (len(name) >= 3 and "command".startswith(name)):
            return StartupCommand(index + 1)
        if name in ("e", "ec") or (len(name) >= 2 and "encodedcommand".startswith(name)):
            return StartupCommand(index + 1, encoded=True)
        index += 2 if takes_powershell_value(name) else 1
    return None


def takes_powershell_value(name: str) -> bool:
    """Whether a PowerShell start-up parameter, by its case-folded name without the dash, takes the next word as its
    value: a prefix of one of POWERSHELL_VALUE_PARAMETERS and of no switch of POWERSHELL_SWITCHES, or its alias."""
    if name in POWERSHELL_VALUE_ALIASES:
        return True
    return (
        bool(name)
        and any(parameter.startswith(name) for parameter in POWERSHELL_VALUE_PARAMETERS)
        and not any(switch.startswith(name) for switch in POWERSHELL_SWITCHES)
    )
