"""The one reading of the external commands a PowerShell source runs, by PowerShell's own parser.

COMMANDS_RUN is the script that asks pwsh's parser what each source runs: repository validation's "Commands skills
may run" policy runs it through its toolchain, and audit-skill's runtime-support reader through `read`, so the two
read a PowerShell script's commands the same way. `command_words` turns one command the script lists into its words,
the program's name first, as both read them.
"""

from __future__ import annotations

import itertools
import shlex
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import json_reader
import json_writer
from bounded_process import non_interactive_environment, run_bounded
from executable_locator import find_executable, program_name

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


def _start_process_table() -> str:
    """START_PROCESS_PARAMETERS as a PowerShell array of entries, each its name, whether it is a switch, and an array
    of its aliases."""
    entries = []
    for name, (switch, aliases) in START_PROCESS_PARAMETERS.items():
        quoted = ", ".join(f"'{alias}'" for alias in aliases)
        entries.append(f"@('{name}', ${'true' if switch else 'false'}, @({quoted}))")
    return f"@({', '.join(entries)})"


# Prints one JSON array of {source, name} for each external command run by the PowerShell sources in the JSON file
# SOURCES_VARIABLE names, as PowerShell's own parser reads them, and {source, unread} for each program it cannot read. A
# function a source defines is its own, and so is what PowerShell ships: its aliases and the commands of its core and
# of the modules under its home, such as Get-ChildItem and ForEach-Object. A module installed elsewhere, such as
# PSScriptAnalyzer, is not standard. What Start-Process starts, by -FilePath or by its first positional argument, what
# `& (Get-Command name)` runs, and every command of a string Invoke-Expression runs are read as commands too; a
# program any of these, or `&` or `.`, names other than by a literal is unread, but for a .ps1 script a path names,
# whose own commands the policy reads in its file. Start-Process's parameters are bound as PowerShell binds them, by
# START_PROCESS_PARAMETERS: a name, an alias, or a prefix of one name alone, a switch taking no value; one it does not
# place leaves the program unread. Each external command carries its words after its name, a literal's text or null for
# any other, and one Start-Process starts carries instead the items of its -ArgumentList, or of the positional argument
# after the program, each a literal's text or null for any other, a list or array of them read item by item, so
# a reader can read the script a pwsh or Windows PowerShell start-up runs.
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
    "function Get-Literal($element) { "
    "if ($element -is [Management.Automation.Language.StringConstantExpressionAst]) { return $element.Value }; "
    "if ($element -is [Management.Automation.Language.ExpandableStringExpressionAst] -and "
    "$element.NestedExpressions.Count -eq 0) { return $element.Value }; "
    "return $null }; "
    "function Get-Argument($elements, $named, $switches) { "
    "for ($at = 1; $at -lt $elements.Count; $at++) { $element = $elements[$at]; "
    "if ($element -is [Management.Automation.Language.CommandParameterAst]) { "
    "if ($named -contains $element.ParameterName) { "
    "if ($element.Argument) { return $element.Argument }; return $elements[$at + 1] }; "
    "if (-not ($switches -contains $element.ParameterName) -and -not $element.Argument) { $at++ }; continue }; "
    "return $element }; "
    "return $null }; "
    "function Find-StartParameter($parameter) { "
    "foreach ($entry in $startParameters) { "
    "if ($entry[0] -eq $parameter -or $entry[2] -contains $parameter) { return ,$entry } }; "
    "$prefixed = @(foreach ($entry in $startParameters) { "
    "if ($entry[0].StartsWith($parameter, [StringComparison]::OrdinalIgnoreCase)) { ,$entry } }); "
    "if ($prefixed.Count -eq 1) { return ,$prefixed[0] }; return $null }; "
    "function Get-Started($elements) { "
    "$bound = @{}; $positional = [Collections.Generic.List[object]]::new(); "
    "for ($at = 1; $at -lt $elements.Count; $at++) { $element = $elements[$at]; "
    "if ($element -isnot [Management.Automation.Language.CommandParameterAst]) { "
    "$positional.Add($element); continue }; "
    "$entry = Find-StartParameter $element.ParameterName; "
    "if ($null -eq $entry) { $bound.unread = $true; return $bound }; "
    "$value = if ($element.Argument) { $element.Argument } elseif ($entry[1]) { $null } "
    "else { $at++; $elements[$at] }; "
    "if ($entry[0] -eq 'FilePath') { $bound.program = $value } "
    "elseif ($entry[0] -eq 'ArgumentList') { $bound.arguments = $value } }; "
    "foreach ($key in @('program', 'arguments')) { "
    "if (-not $bound.ContainsKey($key) -and $positional.Count) { "
    "$bound[$key] = $positional[0]; $positional.RemoveAt(0) } }; "
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
    f"$startParameters = {_start_process_table()}; "
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
    "if ($inner.Count -eq 1 -and @('Get-Command', 'gcm') -contains $inner[0].GetCommandName()) { "
    "$name = Get-Literal (Get-Argument $inner[0].CommandElements @('Name') @()) }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $first.Extent.Text }); continue } }; "
    "$arguments = $null; "
    "if (@('Start-Process', 'saps', 'start') -contains $name) { $started = Get-Started $elements; "
    "$name = if ($started.program -and -not $started.unread) { Get-Literal $started.program } else { $null }; "
    "if (-not $name) { $found.Add([ordered]@{ source = $index; unread = $command.Extent.Text }); continue }; "
    "$arguments = Get-Literals $started.arguments }; "
    "if (@('Invoke-Expression', 'iex') -contains $name) { "
    "$script = Get-Argument $elements @('Command') @(); $text = if ($script) { Get-Literal $script } else { $null }; "
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


def read(sources: Sequence[str], timeout: float = READ_SECONDS) -> list[list[list[str]]]:
    """The words of each external command each PowerShell source runs, as command_words gives them, read by pwsh's
    parser in one call for them all. Raises PowerShellUnread when pwsh is not found, does not finish within `timeout`
    seconds, exits with a status other than 0, or prints no list of commands."""
    if not sources:
        return []
    program = find_executable("pwsh")
    if program is None:
        raise PowerShellUnread("pwsh was not found")
    with tempfile.TemporaryDirectory(prefix="powershell-commands-") as directory:
        request = Path(directory) / "sources.json"
        try:
            json_writer.write(request, list(sources))
        except json_writer.CONTENT_ERRORS as exc:
            raise PowerShellUnread(f"the sources cannot be handed to pwsh: {exc}") from exc
        environment = {**non_interactive_environment(), SOURCES_VARIABLE: str(request)}
        try:
            finished = run_bounded(
                [program, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", COMMANDS_RUN],
                timeout,
                env=environment,
            )
        except OSError as exc:
            raise PowerShellUnread(f"pwsh could not start: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise PowerShellUnread(f"pwsh did not finish within {timeout:g} seconds") from exc
    # pwsh ends a line with "\n" or "\r\n", and a JSON string may hold U+0085 or U+2028 unescaped, which
    # str.splitlines() would break the document on, so the output is split on "\n" alone.
    output = finished.stdout.decode("utf-8", "replace")
    lines = [line.removesuffix("\r") for line in output.split("\n") if line.strip(" \t\r")]
    try:
        found = json_reader.loads(lines[-1]) if finished.returncode == 0 and lines else None
    except json_reader.CONTENT_ERRORS:
        found = None
    if not isinstance(found, list):
        raise PowerShellUnread(f"pwsh could not list the commands (exit code {finished.returncode})")
    commands: list[list[list[str]]] = [[] for _ in sources]
    for item in found:
        if (words := command_words(item)) is not None:
            commands[item["source"]].append(words)
    return commands
