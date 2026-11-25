"""Regression tests for how powershell_commands.read names a pwsh that listed no commands, and for how COMMANDS_RUN
binds the arguments of the commands it binds, held to pwsh's own binder and to what pwsh lists of each command."""

from __future__ import annotations

import functools
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import powershell_commands
import powershell_startup
from bounded_process import Finished
from flat_text import REASON_CHARACTERS
from powershell_commands import PowerShellUnread

PREFIX = "pwsh could not list the commands: "


def unread_reason(finished: Finished) -> str:
    """The reason `read` refuses one source with when pwsh leaves `finished`, run with no pwsh started."""
    with (
        mock.patch.object(powershell_commands, "find_executable", return_value="pwsh"),
        mock.patch.object(powershell_commands, "run_bounded", return_value=finished),
        pytest.raises(PowerShellUnread) as raised,
    ):
        powershell_commands.read(["git status"])
    message = str(raised.value)
    assert message.startswith(PREFIX), message
    return message.removeprefix(PREFIX)


@pytest.mark.parametrize(
    ("finished", "reason"),
    [
        pytest.param(
            Finished(1, b"", b"ParserError:\r\nLine |\r\n   1 |  bad\x1b[0m\r\n     | Missing closing '}'\r\n"),
            "ParserError: Line | 1 | bad [0m | Missing closing '}'",
            id="error-lines",
        ),
        pytest.param(Finished(1, b"", b""), "exit code 1", id="nothing-printed"),
        pytest.param(Finished(3, b"partial\n", b" \r\n\t"), "exit code 3", id="whitespace-only-stderr"),
        pytest.param(Finished(0, b"", b""), "it exited 0 and printed nothing", id="exit-0-nothing"),
        pytest.param(
            Finished(0, b'[{"source": 0}\nnot json\r\n', b""),
            "it exited 0 and its last line is not a list of commands: not json",
            id="exit-0-not-a-list",
        ),
        pytest.param(
            Finished(0, b'{"a": 1}\n', b"WARNING: a module could not load\n"),
            'it exited 0 and its last line is not a list of commands: {"a": 1}; '
            "its stderr: WARNING: a module could not load",
            id="exit-0-with-errors",
        ),
    ],
)
def test_a_pwsh_that_lists_no_commands_is_named_by_its_errors_on_one_line_or_its_exit_code(
    finished: Finished, reason: str
) -> None:
    assert reason == unread_reason(finished)


def test_long_errors_are_cut_to_one_bounded_line() -> None:
    reason = unread_reason(Finished(1, b"", b"Error: " + b"x" * 5000 + b"\n"))
    assert reason.startswith("Error: xxx")
    assert reason.endswith("...")
    assert len(reason) == REASON_CHARACTERS + 3


# What pwsh lists of the commands BOUND_COMMANDS binds and of its binding modes, as one JSON array: [version, the
# properties of [Parameter()], {command: [{name, switch, aliases, positions, pipeline, remaining}]}].
LISTING = (
    "$ErrorActionPreference = 'Stop'; "
    "$names = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$commands = [ordered]@{}; "
    "foreach ($name in $names) { "
    "$commands[$name] = @(foreach ($parameter in (Get-Command $name -CommandType Cmdlet).Parameters.Values) { "
    "$sets = @($parameter.ParameterSets.Values); "
    "[ordered]@{ name = $parameter.Name; switch = [bool]$parameter.SwitchParameter; "
    "aliases = @($parameter.Aliases); positions = @($sets | ForEach-Object Position | Where-Object { $_ -ge 0 }); "
    "pipeline = [bool]@($sets | Where-Object { $_.ValueFromPipeline -or $_.ValueFromPipelineByPropertyName }).Count; "
    "remaining = [bool]@($sets | Where-Object ValueFromRemainingArguments).Count } }) }; "
    "$properties = @([Management.Automation.ParameterAttribute].GetProperties() | ForEach-Object Name); "
    "$version = @($PSVersionTable.PSVersion.Major, $PSVersionTable.PSVersion.Minor); "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($version, $properties, $commands) -Compress -Depth 5))"
)
# Binds each row's source with pwsh's own binder and prints, for each, {bound} or {error}: each command BOUND_COMMANDS
# binds is replaced by a function of its parameters, as pwsh lists them, whose body only reports what pwsh bound, so
# nothing runs. A parameter of a type other than a string, a string array, or a switch takes any value, and none
# validates or transforms it, so a row's placeholder value binds wherever pwsh places it.
BINDER = (
    "$ErrorActionPreference = 'Stop'; "
    "$rows = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$plain = @([string], [string[]], [Management.Automation.SwitchParameter]); "
    "$report = 'process { $bound = [ordered]@{}; foreach ($key in $PSBoundParameters.Keys) { "
    "$value = $PSBoundParameters[$key]; "
    "$bound[$key] = if ($value -is [Management.Automation.SwitchParameter]) { [bool]$value } else { $value } }; "
    "[pscustomobject]@{ bound = $bound } }'; "
    "foreach ($name in @(" + ", ".join(f"'{name}'" for name in powershell_commands.BOUND_COMMANDS) + ")) { "
    "$cmdlet = Microsoft.PowerShell.Core\\Get-Command $name -CommandType Cmdlet; "
    "$metadata = [Management.Automation.CommandMetadata]::new($cmdlet); "
    "foreach ($parameter in $metadata.Parameters.Values) { "
    "if ($plain -notcontains $parameter.ParameterType) { "
    "$parameter.ParameterType = [object]; $parameter.Attributes.Clear() } }; "
    "$block = [Management.Automation.ProxyCommand]::GetParamBlock($metadata); "
    "$binding = [Management.Automation.ProxyCommand]::GetCmdletBindingAttribute($metadata); "
    '$function = [scriptblock]::Create("$binding`nparam($block)`n$report"); '
    'Set-Item -LiteralPath "function:global:$name" -Value $function }; '
    "$results = foreach ($row in $rows) { "
    "try { $out = @(& ([scriptblock]::Create($row)) | Where-Object { $_.PSObject.Properties.Name -contains 'bound' }); "
    "if ($out.Count -eq 1) { [ordered]@{ bound = $out[0].bound } } "
    "else { [ordered]@{ error = 'bound ' + $out.Count } } } "
    "catch { [ordered]@{ error = $_.Exception.Message } } }; "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject @($results) -Compress -Depth 5))"
)
# The ways a command's words name a parameter, from about_Parameters, about_Splatting, and about_Parsing, beside the
# ways ATTRIBUTE_MODES reads from pwsh.
LANGUAGE_MODES = frozenset({"name", "abbreviated name", "alias", "colon-joined", "splatting", "end of parameters"})
# Each property of [Parameter()] that lets a parameter take an argument, with the binding mode it is.
ATTRIBUTE_MODES = {
    "Position": "position",
    "ValueFromPipeline": "pipeline value",
    "ValueFromPipelineByPropertyName": "pipeline property name",
    "ValueFromRemainingArguments": "remaining arguments",
}
# Every other property of [Parameter()], with why it is no way to bind an argument.
NOT_BINDING_PROPERTIES = {
    "ParameterSetName": "chooses among a command's parameter sets, which the binder rows reach through pwsh's binder",
    "Mandatory": "prompts for an argument none gave, which a non-interactive script cannot answer",
    "HelpMessage": "help text",
    "HelpMessageBaseName": "help text",
    "HelpMessageResourceId": "help text",
    "DontShow": "hides the parameter from completion, not from the binder",
    "ExperimentName": "makes the parameter exist or not, which the parameters tables follow",
    "ExperimentAction": "makes the parameter exist or not, which the parameters tables follow",
    "TypeId": "every attribute's type, no property of a parameter",
}
# The pwsh release the parameters tables are those of; a runner's pwsh at least as new lists exactly them.
PINNED_FROM_PWSH = (7, 7)
# What a reading of a read parameter is when the reader leaves it unread.
UNKNOWN = "<unknown>"


@functools.cache
def listing() -> tuple[tuple[int, int], list[str], dict[str, list[dict[str, Any]]]]:
    """The runner's pwsh release, the properties of [Parameter()], and the parameters of each bound command."""
    version, properties, commands = powershell_commands.listed(list(powershell_commands.BOUND_COMMANDS), script=LISTING)
    return (version[0], version[1]), properties, commands


def test_every_binding_mode_is_read_refused_or_not_read_and_every_parameter_property_is_decided() -> None:
    properties = set(listing()[1])
    assert sorted(properties - ATTRIBUTE_MODES.keys() - NOT_BINDING_PROPERTIES.keys()) == [], (
        "decide each new property of [Parameter()]: a binding mode in ATTRIBUTE_MODES, or why it is none in "
        "NOT_BINDING_PROPERTIES"
    )
    modes = LANGUAGE_MODES | set(ATTRIBUTE_MODES.values())
    for name, tables in (
        ("powershell_commands", (powershell_commands.READ_MODES, powershell_commands.REFUSED_MODES)),
        ("powershell_startup", (powershell_startup.READ_MODES,)),
    ):
        not_read = vars(sys.modules[name])["NOT_READ"]
        decided = [mode for table in (*tables, not_read) for mode in table if mode in modes]
        assert sorted(decided) == sorted(modes), f"{name}: decide each mode once: read, refused, or NOT_READ"
        assert set().union(*tables) <= modes, f"{name}: a mode table holds a mode no catalogue names"


@pytest.mark.parametrize("command", list(powershell_commands.BOUND_COMMANDS))
def test_each_bound_command_s_table_is_what_pwsh_lists(command: str) -> None:
    release, _, commands = listing()
    bound = powershell_commands.BOUND_COMMANDS[command]
    listed = {row["name"]: row for row in commands[command]}
    assert sorted(listed.keys() - bound.parameters.keys()) == [], f"add each to {command}'s parameters table"
    if release >= PINNED_FROM_PWSH:
        assert sorted(bound.parameters.keys() - listed.keys()) == [], f"pwsh {release} no longer lists these"
    for name, row in listed.items():
        assert bound.parameters[name] == (row["switch"], tuple(row["aliases"])), name
    positions = sorted((min(row["positions"]), name) for name, row in listed.items() if row["positions"])
    assert all(len(set(row["positions"])) <= 1 for row in listed.values()), "a parameter has two positions"
    assert tuple(name for _, name in positions) == bound.positions
    assert {name for name, row in listed.items() if row["remaining"]} == ({bound.remaining} - {None})
    assert {name for name, row in listed.items() if row["pipeline"]} == bound.pipeline & listed.keys()
    assert set(bound.read) | set(bound.refused) <= bound.parameters.keys()


@dataclass(frozen=True)
class Row:
    """One way to bind a bound command's arguments: the mode it shows, the read parameter it binds, the statements
    before the command, and the command; `unread` where the reader must leave that parameter unread, as a splat whose
    table the source does not settle, whatever the mode's table says."""

    mode: str
    target: str
    setup: str
    call: str
    unread: bool = False

    @property
    def command(self) -> str:
        """The bound command the row calls, by its name, past any pipeline that feeds it."""
        called = self.call.rpartition("| ")[2].split()[0]
        return next(
            name for name, bound in powershell_commands.BOUND_COMMANDS.items() if called in (name, *bound.aliases)
        )

    def source(self) -> str:
        """What the reader reads: Get-Command's call as the program `&` runs."""
        call = f"& ({self.call})" if self.command == "Get-Command" else self.call
        return f"{self.setup}; {call}" if self.setup else call


# The words each bound command is given after the parameter a row spells, and its read parameter's value: Start-Process
# starts prog with other, Get-Command names prog, and Invoke-Expression runs the script prog.
TAILS = {"Start-Process": "prog other", "Get-Command": "prog", "Invoke-Expression": "prog"}
TARGETS = {"Start-Process": "FilePath", "Get-Command": "Name", "Invoke-Expression": "Command"}


def rows() -> list[Row]:
    """A row for every spelling of every parameter of each bound command, by its whole name, an alias, or a prefix of
    either, with a placeholder value and without, and joined by a colon; and for each other binding mode."""
    found: list[Row] = []
    for name, bound in powershell_commands.BOUND_COMMANDS.items():
        tail, target = TAILS[name], TARGETS[name]
        found.append(Row("position", target, "", f"{name} {tail}"))
        found += [Row("position", target, "", f"{alias} {tail}") for alias in bound.aliases]
        for parameter, (switch, aliases) in bound.parameters.items():
            for spelling in (parameter, *aliases):
                mode = "name" if spelling == parameter else "alias"
                for length in range(1, len(spelling) + 1):
                    prefix = spelling[:length]
                    shown = mode if length == len(spelling) else "abbreviated name"
                    found += [
                        Row(shown, target, "", f"{name} -{prefix} '1' {tail}"),
                        Row(shown, target, "", f"{name} -{prefix} {tail}"),
                    ]
            colon = "$true" if switch else "'1'"
            found.append(Row("colon-joined", target, "", f"{name} -{parameter}:{colon} {tail}"))
            found.append(Row("splatting", target, f"$s = @{{ {parameter} = {colon} }}", f"{name} {tail} @s"))
            if parameter in bound.read:
                value = tail.split()[0] if parameter == target else "more"
                found.append(
                    Row(
                        "colon-joined",
                        parameter,
                        "",
                        f"{name} -{parameter}:{value} {' '.join(tail.split()[1:])}".strip(),
                    )
                )
                found.append(Row("splatting", parameter, f"$s = @{{ {parameter} = '{value}' }}", f"{name} @s"))
                found.append(Row("splatting", parameter, f"$s = @{{ {parameter} = 'other' }}", f"{name} {tail} @s"))
            if parameter in bound.pipeline:
                literal = "$true" if switch else "'prog'"
                found.append(
                    Row(
                        "pipeline property name",
                        target,
                        "",
                        f"[pscustomobject]@{{ {parameter} = {literal} }} | {name} {tail}",
                    )
                )
        # A splat whose table the source does not settle may bind any parameter by name, so the read one is unread: an
        # array, a table changed after it is assigned, and one assigned in a branch.
        first = tail.split()[0]
        found += [
            Row("splatting", target, "$s = @('prog', 'other')", f"{name} @s", unread=True),
            Row(
                "splatting",
                target,
                f"$s = @{{ Verbose = $true }}; $s['{target}'] = 'other'",
                f"{name} {first} @s",
                True,
            ),
            Row("splatting", target, f"if ($true) {{ $s = @{{ {target} = 'other' }} }}", f"{name} {first} @s", True),
        ]
        found.append(Row("end of parameters", target, "", f"{name} -- {tail}"))
        if bound.remaining:
            found.append(Row("remaining arguments", target, "", f"{name} {tail} more words"))
        for parameter in bound.pipeline & set(bound.read):
            found.append(Row("pipeline value", parameter, "", f"'prog' | {name}"))
            found.append(
                Row("pipeline property name", parameter, "", f"[pscustomobject]@{{ {parameter} = 'prog' }} | {name}")
            )
    return found


def pwsh_readings(bound: dict[str, Any]) -> dict[str, object]:
    """What pwsh bound each read parameter to: a text, or a list of texts for an argument list."""
    readings: dict[str, object] = {}
    for parameter, value in bound.items():
        items = value if isinstance(value, list) else [value]
        readings[parameter] = (
            [str(item) for item in items] if parameter == "ArgumentList" else " ".join(map(str, items))
        )
    return readings


def reader_readings(command: str, items: list[dict[str, Any]]) -> dict[str, object]:
    """What the reader read each read parameter of a row's command as, UNKNOWN for one it left unread."""
    bound = powershell_commands.BOUND_COMMANDS[command]
    if any("unread" in item for item in items) or not items:
        return dict.fromkeys(bound.read, UNKNOWN)
    item = items[0]
    if command == "Start-Process":
        arguments = item["arguments"]
        return {"FilePath": item["name"], "ArgumentList": UNKNOWN if None in arguments else arguments}
    words = [item["name"], *item["words"]]
    return {next(iter(bound.read)): " ".join(word if word is not None else UNKNOWN for word in words)}


@functools.cache
def judged_rows() -> list[tuple[Row, dict[str, Any], dict[str, object]]]:
    """Each row, with what pwsh's binder bound or why it refused, and what the reader read of it."""
    found = rows()
    bindings = powershell_commands.listed(
        [row.setup + "; " + row.call if row.setup else row.call for row in found], script=BINDER
    )
    listed = powershell_commands.listed([row.source() for row in found])
    items: list[list[dict[str, Any]]] = [[] for _ in found]
    for item in listed:
        items[item["source"]].append(item)
    return [
        (row, binding, reader_readings(row.command, row_items))
        for row, binding, row_items in zip(found, bindings, items, strict=True)
    ]


def binding_problems(row: Row, binding: dict[str, Any], reading: dict[str, object]) -> list[str]:
    """How the reader's reading of a row pwsh bound disagrees with pwsh: a read parameter read as other than pwsh bound
    it, or read where pwsh bound none or bound a refused parameter, or the row's parameter read where its mode is
    refused or left unread where its mode is read. A row pwsh refuses to bind runs nothing, so any reading of it
    passes."""
    if "bound" not in binding:
        return []
    bound = powershell_commands.BOUND_COMMANDS[row.command]
    theirs = pwsh_readings(binding["bound"])
    if refused := [parameter for parameter in bound.refused if parameter in binding["bound"]]:
        return [] if all(value == UNKNOWN for value in reading.values()) else [f"read though pwsh bound {refused}"]
    problems = []
    for parameter in bound.read:
        expected = theirs.get(parameter, [] if parameter == "ArgumentList" else None)
        if reading[parameter] != UNKNOWN and reading[parameter] != expected:
            problems.append(f"{parameter}: read {reading[parameter]!r} where pwsh bound {expected!r}")
    if row.unread:
        return (
            [*problems, f"{row.target} read, though the row's splat is unread"]
            if reading[row.target] != UNKNOWN
            else problems
        )
    if row.mode in powershell_commands.READ_MODES and reading[row.target] == UNKNOWN and row.target in theirs:
        problems.append(f"{row.target} unread, though {row.mode} is read")
    if row.mode in powershell_commands.REFUSED_MODES and reading[row.target] != UNKNOWN:
        problems.append(f"{row.target} read, though {row.mode} is refused")
    return problems


def test_the_reader_binds_every_spelling_of_every_parameter_as_pwsh_s_binder_does() -> None:
    problems = [
        f"{row.source()} ({row.mode}): {problem}"
        for row, binding, reading in judged_rows()
        for problem in binding_problems(row, binding, reading)
    ]
    assert problems == []


@pytest.mark.parametrize("mode", sorted(LANGUAGE_MODES | set(ATTRIBUTE_MODES.values())))
def test_each_binding_mode_pwsh_binds_has_a_row_pwsh_binds(mode: str) -> None:
    # A mode no bound command's parameter takes has no row, and a mode none of whose rows pwsh binds shows nothing.
    if mode in powershell_commands.NOT_READ:
        return
    assert any(row.mode == mode and "bound" in binding for row, binding, _ in judged_rows())


def test_a_splat_the_source_settles_keeps_the_command_read_for_audit_skill() -> None:
    # audit-skill reads a script's git calls through `read`, which drops what is unread, so a splat of options the
    # source settles must leave the command it binds read, and one it does not settle must leave it out.
    assert powershell_commands.read(
        [
            "$o = @{ Wait = $true; NoNewWindow = $true }; Start-Process git -ArgumentList 'push' @o",
            "$o = @{ Wait = $true }; Start-Process git 'push' @o",
            "$o = @{ Verbose = $true }; iex 'git push' @o",
            "$o = @{ Wait = $true }; $o.FilePath = 'rm'; Start-Process git 'push' @o",
        ]
    ) == [[["git", "push"]], [["git", "push"]], [["git", "push"]], []]


def test_only_the_bare_call_in_parentheses_runs_the_command_get_command_names() -> None:
    assert powershell_commands.read(
        ["& (Get-Command git) push", "& (Get-Command git).Source push", "& (Get-Command git)[0] push"]
    ) == [[["git", "push"]], [], []]
