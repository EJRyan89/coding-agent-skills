"""Regression tests for the reader of PowerShell's start-up parameters that audit-skill and validation share, held to
pwsh's own start-up parser, which each test runs in process."""

from __future__ import annotations

import base64
import functools
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import powershell_commands
import powershell_startup
from powershell_startup import StartupCommand, UnknownWord, parameter_kind, parameter_name, startup_command


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["-Command", "jq ."], StartupCommand(1)),
        (["-c", "jq", "."], StartupCommand(1)),
        (["-NoProfile", "-com", "jq ."], StartupCommand(2)),
        (["-ExecutionPolicy", "Bypass", "-Command", "jq ."], StartupCommand(3)),
        (["-ep", "-Command", "-c", "jq ."], StartupCommand(3)),
        (["-cwa", "jq $args", "."], StartupCommand(1, with_arguments=True)),
        (["-CommandWithArgs", "jq $args"], StartupCommand(1, with_arguments=True)),
        # pwsh takes -CommandWithArgs whole or as -cwa alone: -commandw names no parameter, and passes over.
        (["-commandw", "jq $args"], None),
        # A slash, a doubled dash, an en dash, and whitespace around a parameter, as pwsh reads them.
        (["--command", "jq ."], StartupCommand(1)),
        (["/c", "jq ."], StartupCommand(1)),
        (["\u2013c", "jq ."], StartupCommand(1)),
        ([" -c ", "jq ."], StartupCommand(1)),
        (["-co", "jq ."], StartupCommand(1)),
        # -File - reads a script from standard input and reads on, so a -Command after it runs; any other file ends it.
        (["-File", "-", "-c", "jq ."], StartupCommand(3)),
        (["-f", "-", "-NoProfile", "-c", "jq ."], StartupCommand(4)),
        (["-File", None, "-c", "jq ."], UnknownWord(1)),
        (["-File", " -", "-c", "jq ."], None),
        # -EncodedArguments takes a value, so a -Command after it runs.
        (["-ea", "AAAA", "-c", "jq ."], StartupCommand(3)),
        (["-EncodedCommand", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-e", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-ec", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-en", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-Command"], StartupCommand(1)),
        (["-File", "a.ps1", "-Command", "jq ."], None),
        (["-f", "a.ps1"], None),
        # -f and every prefix of -File from -fi end the reading, so a later -Command is the script's argument.
        (["-f", "a.ps1", "-c", "jq ."], None),
        (["-fi", "a.ps1", "-c", "jq ."], None),
        (["-FIL", "a.ps1", "-c", "jq ."], None),
        (["-NoProfile"], None),
        # A value parameter given as the last word, with no value after it, runs nothing.
        (["-ExecutionPolicy"], None),
        ([], None),
    ],
)
def test_a_start_up_runs_the_command_its_parameters_name(words: list[str], expected: object) -> None:
    assert startup_command(words) == expected


def test_a_command_is_plain_unless_its_parameter_says_it_is_encoded_or_takes_arguments() -> None:
    # Each field read itself, so a default that changed would not pass by changing the expected value with it.
    assert [
        (found.start, found.encoded, found.with_arguments)
        for words in (["-c", "x"], ["-e", "x"], ["-cwa", "x"])
        if isinstance(found := startup_command(words), StartupCommand)
    ] == [(1, False, False), (1, True, False), (1, False, True)]


# Every start-up parameter of pwsh and Windows PowerShell that takes a value, by its full name and by its alias, as
# `pwsh -?` and Windows PowerShell's start-up help list them; each takes the next word, so the command comes after it.
@pytest.mark.parametrize(
    "parameter",
    [
        "-ConfigurationFile",
        "-ConfigurationName",
        "-cfn",
        "-CustomPipeName",
        "-ExecutionPolicy",
        "-ep",
        "-InputFormat",
        "-if",
        "-OutputFormat",
        "-of",
        "-PSConsoleFile",
        "-SettingsFile",
        "-Version",
        "-WindowStyle",
        "-WorkingDirectory",
        "-wd",
    ],
)
def test_each_value_parameter_takes_the_next_word(parameter: str) -> None:
    assert startup_command([parameter, "value", "jq ."], positional_command=True) == StartupCommand(2)


def test_a_first_positional_word_is_the_command_the_file_or_passed_over_as_the_caller_asks() -> None:
    words = ["-NoProfile", "-wd", "C:/w", "jq .", "-Command", "rg x"]
    # Windows PowerShell runs it as -Command's; pwsh runs it as -File's; a prose reading passes over it.
    assert startup_command(words, positional_command=True) == StartupCommand(3)
    assert startup_command(words, positional_file=True) is None
    assert startup_command(words) == StartupCommand(5)


def test_a_word_the_caller_cannot_read_is_named_where_the_reading_needs_it() -> None:
    assert startup_command(["-NoProfile", None, "-Command", "jq ."]) == UnknownWord(1)
    # A value parameter takes the unknown word as its value, and a word past the command's start is never read here.
    assert startup_command(["-ExecutionPolicy", None, "-Command", "jq ."]) == StartupCommand(3)
    assert startup_command(["-Command", None]) == StartupCommand(1)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("executionpolicy", powershell_startup.VALUE),
        ("ex", powershell_startup.VALUE),
        ("ep", powershell_startup.VALUE),
        ("wd", powershell_startup.VALUE),
        ("v", powershell_startup.VALUE),
        ("noprofile", powershell_startup.SWITCH),
        ("no", None),
        ("i", powershell_startup.SWITCH),
        # pwsh tries -servermode, whose shortest prefix is -s, before -settingsfile, whose is -settings.
        ("s", powershell_startup.SWITCH),
        ("se", powershell_startup.SWITCH),
        ("settings", powershell_startup.VALUE),
        ("co", powershell_startup.COMMAND),
        ("commandw", None),
        ("ea", powershell_startup.VALUE),
        ("", None),
        ("zzz", None),
    ],
)
def test_a_parameter_names_the_first_parameter_it_is_a_long_enough_prefix_of(name: str, expected: str | None) -> None:
    assert parameter_kind(name) == expected


@pytest.mark.parametrize(
    ("word", "name"),
    [
        ("-Command", "command"),
        ("--command", "command"),
        ("/c", "c"),
        ("\u2013c", "c"),
        ("\u2014c", "c"),
        ("\u2015c", "c"),
        (" -c\t", "c"),
        ("\u00a0-c", "c"),
        # Only a second dash like the first is dropped, and a slash takes none.
        ("-\u2013c", "\u2013c"),
        ("//c", "/c"),
        ("-", ""),
        ("c", None),
        ("", None),
    ],
)
def test_a_word_names_a_parameter_after_a_dash_or_a_slash_as_pwsh_trims_it(word: str, name: str | None) -> None:
    assert parameter_name(word) == name


# pwsh's start-up parser, run in process for each list of words in the JSON file COMMAND_SOURCES names: for each, what
# it runs, {command, encoded, withArguments, stdin, abort}, or {threw} where it throws; then the parameters it lists
# of itself, the characters it takes as dashes, and those its Trim removes.
PARSER = (
    "$ErrorActionPreference = 'Stop'; "
    "$lists = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$flags = [Reflection.BindingFlags]'NonPublic,Public,Instance'; "
    "$assembly = [Microsoft.PowerShell.ConsoleShell].Assembly; "
    "$type = $assembly.GetType('Microsoft.PowerShell.CommandLineParameterParser', $true); "
    "$results = foreach ($list in $lists) { $parser = [Activator]::CreateInstance($type, $true); "
    "try { [void]$type.GetMethod('Parse', $flags).Invoke($parser, @(,[string[]]@($list))) } "
    "catch { [ordered]@{ threw = $_.Exception.InnerException.Message }; continue }; "
    "[ordered]@{ command = $type.GetProperty('InitialCommand', $flags).GetValue($parser); "
    "encoded = $type.GetProperty('WasInitialCommandEncoded', $flags).GetValue($parser); "
    "withArguments = $type.GetField('_commandHasArgs', $flags).GetValue($parser); "
    "stdin = $type.GetProperty('ExplicitReadCommandsFromStdin', $flags).GetValue($parser); "
    "abort = $type.GetProperty('AbortStartup', $flags).GetValue($parser) } }; "
    "$listed = @($type.GetField('s_validParameters', [Reflection.BindingFlags]'NonPublic,Static').GetValue($null)); "
    "$dash = $assembly.GetType('System.Management.Automation.Language.CharExtensions', $false); "
    "if ($null -eq $dash) { $dash = [Management.Automation.Language.Parser].Assembly.GetType("
    "'System.Management.Automation.Language.CharExtensions', $true) }; "
    "$isDash = $dash.GetMethod('IsDash', [Reflection.BindingFlags]'NonPublic,Public,Static'); "
    "$dashes = @(foreach ($code in 0..0xFFFF) { if ($isDash.Invoke($null, @([char]$code))) { $code } }); "
    "$spaces = @(foreach ($code in 0..0xFFFF) { if ([char]::IsWhiteSpace([char]$code)) { $code } }); "
    "$version = @($PSVersionTable.PSVersion.Major, $PSVersionTable.PSVersion.Minor); "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject @(@($results), $listed, $dashes, $spaces, $version) "
    "-Compress -Depth 4))"
)
# The parameters pwsh's start-up parser takes, as the newest pwsh's CommandLineParameterParser.ParseHelper tries them,
# those it lists of itself and those it does not, Windows-only and hidden ones included; and the release that is.
PINNED_PARAMETERS = frozenset(
    {"version", "help", "?", "login", "noexit", "noprofile", "nologo", "noninteractive", "socketservermode"}
    | {"v2socketservermode", "servermode", "namedpipeservermode", "sshservermode", "noprofileloadtime", "interactive"}
    | {"configurationfile", "configurationname", "custompipename", "commandwithargs", "cwa", "command"}
    | {"windowstyle", "file", "isswait", "outputformat", "of", "inputformat", "if", "executionpolicy", "ep"}
    | {"encodedcommand", "ec", "encodedarguments", "ea", "settingsfile", "sta", "mta", "workingdirectory", "wd"}
    | {"removeworkingdirectorytrailingcharacter", "token", "utctimestamp"}
)
PINNED_FROM_PWSH = (7, 7)
# Windows PowerShell's own start-up parameters PARAMETERS adds, from its help.
WINDOWS_POWERSHELL_PARAMETERS = frozenset({"psconsolefile", "cfn"})
# The word each value parameter is given: Base64 of the UTF-16 CLIXML of an argument list, which -EncodedArguments
# takes, as every other value parameter but those that take a word from a list, and -EncodedCommand decodes as its
# script.
ENCODED = base64.b64encode(
    (
        '<Objs Version="1.1.0.1" xmlns="http://schemas.microsoft.com/powershell/2004/04"><Obj RefId="0"><TN RefId="0">'
        "<T>System.Object[]</T></TN><LST><S>a</S></LST></Obj></Objs>"
    ).encode("utf-16-le")
).decode()


def spellings(name: str) -> list[str]:
    """Each word that spells a prefix of a start-up parameter's name: after each dash or a slash, after a doubled dash,
    and padded with whitespace pwsh trims."""
    starts = [*sorted(powershell_startup.PARAMETER_STARTS), *(dash * 2 for dash in sorted(powershell_startup.DASHES))]
    words = [start + name[:length] for length in range(1, len(name) + 1) for start in starts]
    return [*words, f" -{name}", f"-{name}\t", f"-{name}:{ENCODED}"]


def word_lists() -> list[list[str]]:
    """Every spelling of every start-up parameter, given a value and a later -c, given a script alone, and given - and
    a later -c; and the words that name no parameter."""
    names = sorted({name for name, _, _ in powershell_startup.PARAMETERS} | PINNED_PARAMETERS)
    tails = ([ENCODED, "-c", "X"], ["X"], ["-", "-c", "X"])
    lists = [[word, *tail] for name in names for word in spellings(name) for tail in tails]
    return [*lists, ["--", "-c", "X"], ["-", "-c", "X"], ["X", "-c", "Y"], ["-c", "-"], ["-File", " -", "-c", "X"]]


@functools.cache
def parsed() -> tuple[list[list[str]], list[dict[str, Any]], list[str], set[int], set[int], tuple[int, int]]:
    """Each word list, what pwsh's parser runs for each, the parameters it lists, its dashes, and its whitespace."""
    lists = word_lists()
    results, listed, dashes, spaces, version = powershell_commands.listed(lists, script=PARSER)
    return lists, results, listed, set(dashes), set(spaces), (version[0], version[1])


def pwsh_runs(words: list[str], result: dict[str, Any]) -> StartupCommand | None:
    """The command pwsh's parser runs for the words, where the reader's start and kind must be the same, or None where
    it runs none: it threw, it stopped the start-up, or it runs a file, or only what its standard input holds."""
    if "threw" in result or result["abort"] or result["command"] is None:
        return None
    if result["encoded"]:
        return StartupCommand(words.index(ENCODED), encoded=True)
    if result["withArguments"]:
        return StartupCommand(words.index(result["command"], 1), with_arguments=True)
    return StartupCommand(next(start for start in range(1, len(words)) if " ".join(words[start:]) == result["command"]))


def test_the_reader_runs_the_command_pwsh_s_parser_runs_for_every_spelling_of_every_parameter() -> None:
    lists, results, *_ = parsed()
    problems = [
        f"{words!r}: pwsh runs {runs}, the reader reads {read}"
        for words, result in zip(lists, results, strict=True)
        if (runs := pwsh_runs(words, result)) is not None
        and (read := startup_command(words, positional_file=True)) != runs
    ]
    assert problems == []


def test_every_parameter_pwsh_takes_is_decided() -> None:
    _, _, listed, _, _, release = parsed()
    assert sorted(set(listed) - PINNED_PARAMETERS) == [], "pin each in PINNED_PARAMETERS, and decide it in PARAMETERS"
    if release >= PINNED_FROM_PWSH:
        assert {name for name, _, _ in powershell_startup.PARAMETERS} | {
            name for name in powershell_startup.NOT_READ if name in PINNED_PARAMETERS
        } == PINNED_PARAMETERS | WINDOWS_POWERSHELL_PARAMETERS


def test_the_reader_takes_the_dashes_pwsh_takes_and_trims_the_whitespace_it_trims() -> None:
    _, _, _, dashes, spaces, _ = parsed()
    assert {ord(dash) for dash in powershell_startup.DASHES} == dashes
    assert [code for code in sorted(spaces) if not chr(code).isspace()] == []
