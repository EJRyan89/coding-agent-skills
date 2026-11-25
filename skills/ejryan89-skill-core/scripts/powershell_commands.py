"""The one reading of the external commands a PowerShell source runs, by PowerShell's own parser.

COMMANDS_RUN is the script that asks pwsh's parser what each source runs: repository validation's "Commands skills
may run" policy runs it through its toolchain, and audit-skill's runtime-support reader through `read`, so the two
read a PowerShell script's commands the same way. `command_words` turns one command the script lists into its words,
the program's name first, as both read them.

Where a command's arguments name the program or the script it runs, as Start-Process's, Get-Command's, and
Invoke-Expression's do, COMMANDS_RUN binds them to its parameters as pwsh's parameter binder does, by BOUND_COMMANDS,
in every way READ_MODES and REFUSED_MODES name, which NOT_READ completes. test_powershell_commands.py
binds every form of every parameter of each with pwsh's own binder and holds the reading to it.
"""

from __future__ import annotations

import itertools
import shlex
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import json_reader
import json_writer
import private_folder
from bounded_process import Finished, non_interactive_environment, run_bounded
from executable_locator import find_executable, program_name
from flat_text import reason_line
from text_lines import program_lines

# The environment variable naming the JSON file that holds the sources COMMANDS_RUN reads.
SOURCES_VARIABLE = "COMMAND_SOURCES"
# How long `read` lets pwsh take to list the commands of the sources it is given, start-up included.
READ_SECONDS = 120.0

# Start-Process's parameters, by name, each with whether it is a switch, which takes no value, and the aliases
# PowerShell binds, as `(Get-Command Start-Process).Parameters` lists them; tests/validation/test_shell_readers.py
# holds the table to that listing.
START_PROCESS_PARAMETERS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "FilePath": (False, ("PSPath", "Path")),
    "ArgumentList": (False, ("Args",)),
    "Credential": (False, ("RunAs",)),
    "WorkingDirectory": (False, ()),
    "LoadUserProfile": (True, ("Lup",)),
    "NoNewWindow": (True, ("nnw",)),
    "PassThru": (True, ()),
    "RedirectStandardError": (False, ("RSE",)),
    "RedirectStandardInput": (False, ("RSI",)),
    "RedirectStandardOutput": (False, ("RSO",)),
    "Verb": (False, ()),
    "WindowStyle": (False, ()),
    "Wait": (True, ()),
    "UseNewEnvironment": (True, ()),
    "Environment": (False, ()),
    "Verbose": (True, ("vb",)),
    "Debug": (True, ("db",)),
    "ErrorAction": (False, ("ea",)),
    "WarningAction": (False, ("wa",)),
    "InformationAction": (False, ("infa",)),
    "ProgressAction": (False, ("proga",)),
    "ErrorVariable": (False, ("ev",)),
    "WarningVariable": (False, ("wv",)),
    "InformationVariable": (False, ("iv",)),
    "OutVariable": (False, ("ov",)),
    "OutBuffer": (False, ("ob",)),
    "PipelineVariable": (False, ("pv",)),
    "WhatIf": (True, ("wi",)),
    "Confirm": (True, ("cf",)),
}
# The common parameters every cmdlet takes, as START_PROCESS_PARAMETERS holds them; Get-Command and Invoke-Expression
# take them too.
COMMON_PARAMETERS = {
    name: START_PROCESS_PARAMETERS[name]
    for name in (
        "Verbose",
        "Debug",
        "ErrorAction",
        "WarningAction",
        "InformationAction",
        "ProgressAction",
        "ErrorVariable",
        "WarningVariable",
        "InformationVariable",
        "OutVariable",
        "OutBuffer",
        "PipelineVariable",
    )
}
# Get-Command's and Invoke-Expression's parameters, as START_PROCESS_PARAMETERS holds Start-Process's: those of the
# newest pwsh, which test_powershell_commands.py holds to the listing of the pwsh it runs.
GET_COMMAND_PARAMETERS: dict[str, tuple[bool, tuple[str, ...]]] = {
    "Name": (False, ()),
    "Verb": (False, ()),
    "Noun": (False, ()),
    "Module": (False, ("PSSnapin",)),
    "ExcludeModule": (False, ()),
    "FullyQualifiedModule": (False, ()),
    "CommandType": (False, ("Type",)),
    "TotalCount": (False, ()),
    "Syntax": (True, ()),
    "ShowCommandInfo": (True, ()),
    "ArgumentList": (False, ("Args",)),
    "All": (True, ()),
    "ListImported": (True, ()),
    "ParameterName": (False, ()),
    "ParameterType": (False, ()),
    "UseFuzzyMatching": (True, ()),
    "FuzzyMinimumDistance": (False, ()),
    "UseAbbreviationExpansion": (True, ()),
    **COMMON_PARAMETERS,
}
INVOKE_EXPRESSION_PARAMETERS: dict[str, tuple[bool, tuple[str, ...]]] = {"Command": (False, ()), **COMMON_PARAMETERS}


@dataclass(frozen=True)
class BoundCommand:
    """A command whose arguments COMMANDS_RUN binds to its parameters as pwsh's binder does: the aliases it is also
    called by; its parameters; those it binds by position, in order; the one that takes the remaining arguments, if
    any; those that take pipeline input, by value or by property name; the ones COMMANDS_RUN reads, each with what it
    names (the program, the program's arguments, or the script to run); and those whose binding leaves the command
    unread, each with why. Every other parameter is passed over, with its value."""

    aliases: tuple[str, ...]
    parameters: Mapping[str, tuple[bool, tuple[str, ...]]]
    positions: tuple[str, ...]
    remaining: str | None
    pipeline: frozenset[str]
    read: Mapping[str, str]
    refused: Mapping[str, str] = field(default_factory=dict)


# Each command COMMANDS_RUN binds, by its name. test_powershell_commands.py holds each one's positions, remaining
# arguments, and pipeline parameters to what pwsh lists of it.
BOUND_COMMANDS = {
    "Start-Process": BoundCommand(
        aliases=("saps", "start"),
        parameters=START_PROCESS_PARAMETERS,
        positions=("FilePath", "ArgumentList"),
        remaining=None,
        pipeline=frozenset(),
        read={"FilePath": "program", "ArgumentList": "arguments"},
    ),
    "Get-Command": BoundCommand(
        aliases=("gcm",),
        parameters=GET_COMMAND_PARAMETERS,
        positions=("Name", "ArgumentList"),
        remaining="ArgumentList",
        pipeline=frozenset(
            {"Name", "Verb", "Noun", "Module", "FullyQualifiedModule", "CommandType", "TotalCount", "Syntax"}
            | {"All", "ListImported", "UseAbbreviationExpansion"}
        ),
        read={"Name": "program"},
        refused={
            "Verb": "finds a command by its verb, not by the name it is given",
            "Noun": "finds a command by its noun, not by the name it is given",
            "UseAbbreviationExpansion": "finds a command whose name the name it is given abbreviates, as i-e does iex",
            "UseFuzzyMatching": "finds a command whose name resembles the name it is given",
        },
    ),
    "Invoke-Expression": BoundCommand(
        aliases=("iex",),
        parameters=INVOKE_EXPRESSION_PARAMETERS,
        positions=("Command",),
        remaining=None,
        pipeline=frozenset({"Command"}),
        read={"Command": "script"},
    ),
}
# Every way pwsh binds an argument to a parameter: the ways a command's words name one, and the ways the properties
# of a parameter's [Parameter()] attribute let it take an argument (Position, ValueFromPipeline,
# ValueFromPipelineByPropertyName, and ValueFromRemainingArguments), which test_powershell_commands.py reads from pwsh.
# Each is read, refused, which leaves the program or the script a bound command names unread, or named in NOT_READ.
READ_MODES = {
    "position": "each positional argument binds the next of the command's positions not bound by name",
    "name": "a parameter's whole name, in any case",
    "abbreviated name": (
        "a prefix of the name or an alias of one parameter alone, or of the one among several that is no common "
        "parameter, as pwsh's binder places it; a prefix it cannot place leaves the command unread"
    ),
    "alias": "each alias the command's parameters table holds",
    "colon-joined": "-Name:value binds value, a switch's included; a switch takes no value otherwise",
    "remaining arguments": "the positional arguments past the command's positions bind its remaining parameter",
    "splatting": (
        "a splat of a variable assigned once, before it in its script block, from a hashtable literal of constants, "
        "with no other mention, binds each key by name (Get-Splat); any other splat may bind any parameter by name, so "
        "each parameter otherwise bound only by position is unread, and a command with a refused parameter is unread"
    ),
}
REFUSED_MODES = {
    "pipeline value": (
        "a command a pipeline feeds leaves each parameter that takes pipeline input and is not otherwise bound unread, "
        "and the command unread where that is a refused parameter"
    ),
    "pipeline property name": "as a pipeline value",
    "end of parameters": "-- names no parameter, so the command is unread",
}
# The binding modes the reader leaves unread, each with why; every one is read or refused today.
NOT_READ: dict[str, str] = {}


def _powershell_list(items: Sequence[str]) -> str:
    """Texts as a PowerShell array of single-quoted strings."""
    return "@({})".format(", ".join(f"'{item}'" for item in items))


def _parameter_table(parameters: Mapping[str, tuple[bool, tuple[str, ...]]]) -> str:
    """A parameters table as a PowerShell array of entries, each its name, whether it is a switch, and an array of its
    aliases."""
    entries = [
        f"@('{name}', ${'true' if switch else 'false'}, {_powershell_list(aliases)})"
        for name, (switch, aliases) in parameters.items()
    ]
    return f"@({', '.join(entries)})"


def _bound_commands() -> str:
    """BOUND_COMMANDS as a PowerShell hashtable from each name and alias to its entry: its name, its parameters table,
    its positions, its remaining parameter or an empty text, its pipeline parameters, and its refused parameters."""
    entries = []
    for name, command in BOUND_COMMANDS.items():
        entry = (
            f"@('{name}', {_parameter_table(command.parameters)}, {_powershell_list(command.positions)}, "
            f"'{command.remaining or ''}', {_powershell_list(sorted(command.pipeline))}, "
            f"{_powershell_list(list(command.refused))})"
        )
        entries += [f"'{called}' = {entry}" for called in (name, *command.aliases)]
    return f"@{{ {'; '.join(entries)} }}"


# Prints one JSON array of {source, name} for each external command run by the PowerShell sources in the JSON file
# SOURCES_VARIABLE names, as PowerShell's own parser reads them, and {source, unread} for each program it cannot read. A
# function a source defines is its own, and so is what PowerShell ships: its aliases and the commands of its core and
# of the modules under its home, such as Get-ChildItem and ForEach-Object. A module installed elsewhere, such as
# PSScriptAnalyzer, is not standard. What Start-Process starts, what `& (Get-Command name)` runs, and every command of
# a string Invoke-Expression runs are read as commands too, each command's arguments bound by Get-Bound as pwsh binds
# them (BOUND_COMMANDS); a program any of these, or `&` or `.`, names other than by a literal is unread, as is a
# Get-Command name holding a wildcard, but for a .ps1 script a path names, whose own commands the policy reads in its
# file. Each external command carries its words after its name, a literal's text or null for any other, and one
# Start-Process starts carries instead the items of its -ArgumentList, each a literal's text or null for any other, a
# list or array of them read item by item, so a reader can read the script a pwsh or Windows PowerShell start-up runs.
# A stated limit: a cmdlet called by its module-qualified name, as Microsoft.PowerShell.Management\Start-Process, is
# read as a program of that name, which no skill declares, so the command policy refuses it unbound.
COMMANDS_RUN = (
    "$ErrorActionPreference = 'Stop'; "
    "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
    "$sources = @(Get-Content -LiteralPath $env:COMMAND_SOURCES -Raw -Encoding utf8 | ConvertFrom-Json); "
    "$shipped = @(Get-Module -ListAvailable | Where-Object { "
    "$_.ModuleBase.StartsWith($PSHOME, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object Name); "
    "$own = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); "
    "foreach ($command in Get-Command -Module @($shipped + 'Microsoft.PowerShell.Core')) { "
    "[void]$own.Add($command.Name) }; "
    "foreach ($alias in Get-Alias) { [void]$own.Add($alias.Name) }; "
    "$common = [Collections.Generic.HashSet[string]]::new([string[]]@("
    "[Management.Automation.Cmdlet]::CommonParameters + [Management.Automation.Cmdlet]::OptionalCommonParameters), "
    "[StringComparer]::OrdinalIgnoreCase); "
    "$unknown = [object]::new(); "
    "$variableCommands = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase); "
    "foreach ($command in Get-Command -Noun Variable) { [void]$variableCommands.Add($command.Name) }; "
    "foreach ($alias in Get-Alias -Definition *-Variable) { [void]$variableCommands.Add($alias.Name) }; "
    "function Get-Literal($element) { "
    "if ($element -is [Management.Automation.Language.StringConstantExpressionAst]) { return $element.Value }; "
    "if ($element -is [Management.Automation.Language.ExpandableStringExpressionAst] -and "
    "$element.NestedExpressions.Count -eq 0) { return $element.Value }; "
    "return $null }; "
    # The parameter a name binds, as pwsh's binder places it: a whole name or alias, else the one parameter a name or
    # alias of which it prefixes, or the one of several that is no common parameter; null for any other.
    "function Find-Parameter($table, $name) { "
    "foreach ($entry in $table) { if ($entry[0] -eq $name -or $entry[2] -contains $name) { return ,$entry } }; "
    "$matched = @(foreach ($entry in $table) { "
    "if (@(@($entry[0]) + $entry[2] | Where-Object { "
    "$_.StartsWith($name, [StringComparison]::OrdinalIgnoreCase) }).Count) { ,$entry } }); "
    "if ($matched.Count -gt 1) { $matched = @(foreach ($entry in $matched) { "
    "if (-not $common.Contains($entry[0])) { ,$entry } }) }; "
    "if ($matched.Count -eq 1) { return ,$matched[0] }; return $null }; "
    # The constant an expression statement holds, as its AST, or null: a string, a number, $true, $false, or $null.
    "function Get-Constant($statement) { "
    "if ($statement -is [Management.Automation.Language.PipelineAst] -and $statement.PipelineElements.Count -eq 1) { "
    "$statement = $statement.PipelineElements[0] }; "
    "if ($statement -is [Management.Automation.Language.CommandExpressionAst]) { $statement = $statement.Expression }; "
    "if ($statement -is [Management.Automation.Language.ConstantExpressionAst] -or "
    "($statement -is [Management.Automation.Language.ExpandableStringExpressionAst] -and "
    "$statement.NestedExpressions.Count -eq 0) -or "
    "($statement -is [Management.Automation.Language.VariableExpressionAst] -and "
    "@('true', 'false', 'null') -contains $statement.VariablePath.UserPath)) { return $statement }; return $null }; "
    # The keys and constants of the hashtable a splatted variable holds, as pairs, where the source it stands in, $item
    # and $tree, settles them: the variable, unqualified, is assigned once, by =, from a hashtable literal of constants
    # under literal keys, as a statement of the script block the splat runs in, before it; every other mention of it is
    # an unqualified splat, by the AST and by its text; and the source reaches no variable by name, through a command
    # of pwsh's Variable noun or its alias ($variableCommands), the variable: drive, or PSVariable or SessionState, and
    # dot-sources nothing. Null for any other splat.
    "function Get-Splat($variable) { "
    "$name = $variable.VariablePath.UserPath; $text = [string]$item[1]; "
    "if (-not $variable.VariablePath.IsUnqualified -or $text -match '(?i)variable:|PSVariable|SessionState') { "
    "return $null }; "
    "if (@($tree.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] -and "
    "($node.InvocationOperator -eq 'Dot' -or $variableCommands.Contains([string]$node.GetCommandName())) }, "
    "$true)).Count) { return $null }; "
    "$pattern = '(?i)[$@]\\{?(\\w+:)?' + [regex]::Escape($name) + '(?![\\w])'; "
    "$mentions = @($tree.FindAll({ param($node) $node -is [Management.Automation.Language.VariableExpressionAst] -and "
    "$node.VariablePath.UserPath -match ('(?i)^(\\w+:)?' + [regex]::Escape($name) + '$') }, $true)); "
    "if ([regex]::Matches($text, $pattern).Count -ne $mentions.Count) { return $null }; "
    "$assignments = @($mentions | Where-Object { -not $_.Splatted }); "
    "if ($assignments.Count -ne 1 -or -not $assignments[0].VariablePath.IsUnqualified -or "
    "@($mentions | Where-Object { $_.Splatted -and -not $_.VariablePath.IsUnqualified }).Count) { return $null }; "
    "$assignment = $assignments[0].Parent; "
    "if ($assignment -isnot [Management.Automation.Language.AssignmentStatementAst] -or "
    "$assignment.Left -ne $assignments[0] -or $assignment.Operator -ne 'Equals' -or "
    "$assignment.Parent -isnot [Management.Automation.Language.NamedBlockAst] -or "
    "$assignment.Extent.EndOffset -gt $variable.Extent.StartOffset) { return $null }; "
    "$block = $variable; while ($block -isnot [Management.Automation.Language.ScriptBlockAst]) { "
    "$block = $block.Parent }; "
    "if ($assignment.Parent.Parent -ne $block) { return $null }; "
    "$table = $assignment.Right; "
    "if ($table -is [Management.Automation.Language.PipelineAst] -and $table.PipelineElements.Count -eq 1) { "
    "$table = $table.PipelineElements[0] }; "
    "if ($table -is [Management.Automation.Language.CommandExpressionAst]) { $table = $table.Expression }; "
    "if ($table -isnot [Management.Automation.Language.HashtableAst]) { return $null }; "
    "$pairs = [Collections.Generic.List[object]]::new(); "
    "foreach ($pair in $table.KeyValuePairs) { $value = Get-Constant $pair.Item2; "
    "if ($pair.Item1 -isnot [Management.Automation.Language.StringConstantExpressionAst] -or $null -eq $value) { "
    "return $null }; $pairs.Add(@($pair.Item1.Value, $value)) }; "
    "return ,$pairs.ToArray() }; "
    # What a bound command's arguments bind each parameter to, by the entry _bound_commands gives it, or null where the
    # command is unread: a parameter it cannot place, or one of its refused parameters bound, or perhaps bound by a
    # splat Get-Splat cannot settle or by the pipeline, which holds $unknown in each parameter it may bind. A splat
    # Get-Splat settles binds each of its keys by name, as pwsh does before it binds by position.
    "function Get-Bound($command, $spec) { "
    "$elements = $command.CommandElements; $bound = @{}; "
    "$positional = [Collections.Generic.List[object]]::new(); $splatted = $false; "
    "for ($at = 1; $at -lt $elements.Count; $at++) { $element = $elements[$at]; "
    "if ($element -is [Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) { "
    "$pairs = Get-Splat $element; if ($null -eq $pairs) { $splatted = $true; continue }; "
    "foreach ($pair in $pairs) { $entry = Find-Parameter $spec[1] $pair[0]; "
    "if ($null -eq $entry) { return $null }; $bound[$entry[0]] = $pair[1] }; continue }; "
    "if ($element -isnot [Management.Automation.Language.CommandParameterAst]) { "
    "$positional.Add($element); continue }; "
    "$entry = Find-Parameter $spec[1] $element.ParameterName; "
    "if ($null -eq $entry) { return $null }; "
    "$bound[$entry[0]] = if ($element.Argument) { $element.Argument } elseif ($entry[1]) { $true } "
    "else { $at++; $elements[$at] } }; "
    "foreach ($name in $spec[2]) { if ($bound.ContainsKey($name)) { continue }; "
    "if ($splatted) { $bound[$name] = $unknown } "
    "elseif ($positional.Count) { $bound[$name] = $positional[0]; $positional.RemoveAt(0) } }; "
    "if ($spec[3] -and $positional.Count -and -not $bound.ContainsKey($spec[3])) { "
    "$bound[$spec[3]] = $positional.ToArray() }; "
    "$piped = $command.Parent -is [Management.Automation.Language.PipelineAst] -and "
    "$command.Parent.PipelineElements.IndexOf($command) -gt 0; "
    "if ($piped) { foreach ($name in $spec[4]) { if (-not $bound.ContainsKey($name)) { $bound[$name] = $unknown } } }; "
    "if ($splatted -and $spec[5].Count) { return $null }; "
    "foreach ($name in $spec[5]) { if ($bound.ContainsKey($name)) { return $null } }; "
    "return $bound }; "
    "function Get-Literals($element) { "
    "while ($element -is [Management.Automation.Language.ParenExpressionAst] -or "
    "$element -is [Management.Automation.Language.ArrayExpressionAst]) { "
    "$statements = if ($element -is [Management.Automation.Language.ParenExpressionAst]) { @($element.Pipeline) } "
    "else { @($element.SubExpression.Statements) }; "
    "if ($statements.Count -eq 0) { return ,@() }; "
    "$pipeline = $statements[0]; "
    "if ($statements.Count -ne 1 -or $pipeline -isnot [Management.Automation.Language.PipelineAst] -or "
    "$pipeline.PipelineElements.Count -ne 1 -or "
    "$pipeline.PipelineElements[0] -isnot [Management.Automation.Language.CommandExpressionAst]) { "
    "return ,@($null) }; "
    "$element = $pipeline.PipelineElements[0].Expression }; "
    "if ($null -eq $element) { return ,@() }; "
    "$items = if ($element -is [Management.Automation.Language.ArrayLiteralAst]) { @($element.Elements) } "
    "else { @($element) }; "
    "return ,@(foreach ($item in $items) { Get-Literal $item }) }; "
    f"$specs = {_bound_commands()}; "
    "$found = [Collections.Generic.List[object]]::new(); "
    "$queue = [Collections.Generic.Queue[object]]::new(); "
    "for ($index = 0; $index -lt $sources.Count; $index++) { $queue.Enqueue(@($index, $sources[$index])) }; "
    "while ($queue.Count) { $item = $queue.Dequeue(); $index = $item[0]; "
    "$tokens = $null; $errors = $null; "
    "$tree = [Management.Automation.Language.Parser]::ParseInput($item[1], [ref]$tokens, [ref]$errors); "
    "$functions = @($tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name); "
    "foreach ($command in $tree.FindAll({ param($node) "
    "$node -is [Management.Automation.Language.CommandAst] }, $true)) { "
    "$name = $command.GetCommandName(); $elements = $command.CommandElements; "
    "if (-not $name -and $elements[0].Extent.Text -match '\\.ps1\\W*$') { continue }; "
    "if (-not $name) { $first = $elements[0]; "
    "$inner = @($first.FindAll({ param($node) $node -is [Management.Automation.Language.CommandAst] }, $true)); "
    # Only the bare call in parentheses, `& (Get-Command name)`, runs the command it names; a member or an index taken
    # off its result, or any other expression around it, is unread.
    "$innerSpec = if ($inner.Count -eq 1 -and $first -is [Management.Automation.Language.ParenExpressionAst] -and "
    "$first.Pipeline -is [Management.Automation.Language.PipelineAst] -and "
    "$first.Pipeline.PipelineElements.Count -eq 1 -and $first.Pipeline.PipelineElements[0] -eq $inner[0]) { "
    "$specs[[string]$inner[0].GetCommandName()] } else { $null }; "
    "if ($innerSpec -and $innerSpec[0] -eq 'Get-Command') { "
    "$bound = Get-Bound $inner[0] $specs['Get-Command']; "
    "$name = if ($null -ne $bound) { Get-Literal $bound.Name } else { $null }; "
    "if ($name -and [Management.Automation.WildcardPattern]::ContainsWildcardCharacters($name)) { $name = $null } }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $first.Extent.Text }); continue } }; "
    "$arguments = $null; $spec = $specs[$name]; "
    "if ($spec -and $spec[0] -eq 'Start-Process') { $bound = Get-Bound $command $spec; "
    "$name = if ($null -ne $bound) { Get-Literal $bound.FilePath } else { $null }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $command.Extent.Text }); continue }; "
    "$arguments = Get-Literals $bound.ArgumentList }; "
    "if ($spec -and $spec[0] -eq 'Invoke-Expression') { $bound = Get-Bound $command $spec; "
    "$text = if ($null -ne $bound) { Get-Literal $bound.Command } else { $null }; "
    "if ($null -eq $text) { $found.Add([ordered]@{ source = $index; unread = $command.Extent.Text }) } "
    "else { $queue.Enqueue(@($index, $text)) }; continue }; "
    "if ($functions -contains $name -or $own.Contains($name)) { continue }; "
    "if ($null -ne $arguments) { "
    "$found.Add([ordered]@{ source = $index; name = $name; arguments = $arguments }); continue }; "
    "$words = @(for ($at = 1; $at -lt $elements.Count; $at++) { $element = $elements[$at]; "
    "if ($element -isnot [Management.Automation.Language.CommandParameterAst]) { Get-Literal $element } "
    "elseif ($element.Argument) { $null } else { $element.Extent.Text } }); "
    "$found.Add([ordered]@{ source = $index; name = $name; words = $words }) } }; "
    "[Console]::Out.WriteLine((ConvertTo-Json -InputObject $found.ToArray() -Compress -Depth 3))"
)


# What command_words records for a word of a command a PowerShell source runs that is no literal.
UNREAD_WORD = "<value>"


def started_process_words(arguments: list[str | None]) -> list[str | None]:
    """The words a program Start-Process starts is given, from the items of its -ArgumentList, each a literal's text or
    None: Start-Process joins the items with spaces into one command line, which the program splits at whitespace
    outside double quotes, as the Windows C runtime does. The items before the first one that is no literal are read
    so, and None stands for that item and every later one; a line whose double quote is never closed is None too."""
    known = [item for item in itertools.takewhile(lambda item: item is not None, arguments) if item is not None]
    lexer = shlex.shlex(" ".join(known), posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.escape = ""
    lexer.commenters = ""
    try:
        words: list[str | None] = list(lexer)
    except ValueError:
        return [None]
    return [*words, None] if len(known) < len(arguments) else words


def command_words(item: Mapping[str, Any]) -> list[str] | None:
    """The words of one external command COMMANDS_RUN lists, its program's name first, without a .exe suffix, and
    UNREAD_WORD for each word that is no literal: the words after its name, or the words Start-Process gives the
    program it starts, as started_process_words reads its argument list. None for a program the parser could not
    read, which names no command."""
    if "unread" in item:
        return None
    words = item["words"] if "words" in item else started_process_words(item["arguments"])
    return [program_name(item["name"]), *(word if word is not None else UNREAD_WORD for word in words)]


class PowerShellUnread(Exception):
    """pwsh could not be found or could not list the commands of the sources it was given."""


def unlisted_reason(finished: Finished, lines: Sequence[str]) -> str:
    """Why a COMMANDS_RUN that printed no list of commands failed, on one line: after a nonzero exit, what pwsh
    printed to stderr, where its errors go, or its exit code when it printed none; after an exit of 0, what its last
    line held, then anything it printed to stderr."""
    errors = reason_line(finished.stderr.decode("utf-8", "replace"))
    if finished.returncode != 0:
        return errors or f"exit code {finished.returncode}"
    if lines:
        found = f"it exited 0 and its last line is not a list of commands: {reason_line(lines[-1])}"
    else:
        found = "it exited 0 and printed nothing"
    return f"{found}; its stderr: {errors}" if errors else found


def listed(sources: Sequence[object], timeout: float = READ_SECONDS, script: str | None = None) -> list[Any]:
    """The JSON array `script` prints as its last line, run by pwsh with `sources` in the JSON file SOURCES_VARIABLE
    names. `script` defaults to COMMANDS_RUN, as the module holds it at the call, whose array lists each command the
    sources run, {source, name, words or arguments} or {source, unread}, read by pwsh's parser in one call for them all.
    Raises PowerShellUnread when pwsh is not found, does not finish within `timeout` seconds, exits with a status other
    than 0, or prints no list, naming the reason pwsh printed on one line, or its exit code when it printed none."""
    program = find_executable("pwsh")
    if program is None:
        raise PowerShellUnread("pwsh was not found")
    with tempfile.TemporaryDirectory(
        prefix="powershell-commands-", dir=private_folder.temporary_directory()
    ) as directory:
        request = Path(directory) / "sources.json"
        try:
            json_writer.write(request, list(sources))
        except json_writer.CONTENT_ERRORS as exc:
            raise PowerShellUnread(f"the sources cannot be handed to pwsh: {exc}") from exc
        environment = {**non_interactive_environment(), SOURCES_VARIABLE: str(request)}
        try:
            finished = run_bounded(
                [program, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script or COMMANDS_RUN],
                timeout,
                env=environment,
            )
        except OSError as exc:
            raise PowerShellUnread(f"pwsh could not start: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PowerShellUnread(f"pwsh did not finish within {timeout:g} seconds") from exc
    # pwsh ends a line with "\n" or "\r\n", and a JSON string may hold U+0085 or U+2028 unescaped, which
    # str.splitlines() would break the document on, so the output is split as program_lines splits it.
    lines = [line for line in program_lines(finished.stdout.decode("utf-8", "replace")) if line.strip(" \t\r")]
    try:
        found = json_reader.loads(lines[-1]) if finished.returncode == 0 and lines else None
    except json_reader.CONTENT_ERRORS:
        found = None
    if not isinstance(found, list):
        raise PowerShellUnread(f"pwsh could not list the commands: {unlisted_reason(finished, lines)}")
    return found


def read(sources: Sequence[str], timeout: float = READ_SECONDS) -> list[list[list[str]]]:
    """The words of each external command each PowerShell source runs, as command_words gives them, read by pwsh's
    parser in one call for them all, as `listed` lists them; raises PowerShellUnread as it does."""
    if not sources:
        return []
    commands: list[list[list[str]]] = [[] for _ in sources]
    for item in listed(sources, timeout):
        if (words := command_words(item)) is not None:
            commands[item["source"]].append(words)
    return commands
