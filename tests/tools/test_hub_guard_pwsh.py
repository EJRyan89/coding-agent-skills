"""The hub guard's PowerShell catalogue, held to PowerShell itself: every AST node type reflection finds in
System.Management.Automation.Language is understood or in NOT_READ, each has a sample that PowerShell's parser makes
it from, and the reader refuses every sample of a type it does not understand. A type a later PowerShell adds fails
here until a row decides it."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import Finished

from deployer import platform_support
from tools import hub_guard

# Prints, for the JSON list of commands on stdin, each one's node types, or the parser's first error.
PARSE_SAMPLES = r"""
$samples = [Console]::In.ReadToEnd() | ConvertFrom-Json
$out = foreach ($sample in $samples) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput($sample, [ref] $tokens, [ref] $errors)
    if ($errors.Count -gt 0) { , @("error: $($errors[0].Message)") }
    else { , @($ast.FindAll({ $true }, $true) | ForEach-Object { $_.GetType().Name } | Sort-Object -Unique) }
}
ConvertTo-Json -InputObject @($out) -Depth 4 -Compress
"""
NODE_TYPES = r"""
$language = [System.Management.Automation.Language.Ast]
$language.Assembly.GetTypes() |
    Where-Object { $_.IsPublic -and -not $_.IsAbstract -and $_.IsSubclassOf($language) } |
    ForEach-Object Name | Sort-Object | ConvertTo-Json -Compress
"""
# A command PowerShell's parser makes each node type from.
NODE_SAMPLES = {
    "ScriptBlockAst": "Get-Date",
    "NamedBlockAst": "Get-Date",
    "PipelineAst": "Get-Date | Out-String",
    "PipelineChainAst": "Get-Date && Get-Date",
    "CommandAst": "Get-Date",
    "CommandParameterAst": "Get-ChildItem -Force",
    "CommandExpressionAst": "'text'",
    "StringConstantExpressionAst": "Get-Item 'x'",
    "ConstantExpressionAst": "Start-Sleep 1",
    "ExpandableStringExpressionAst": 'Write-Output "x$y"',
    "FileRedirectionAst": "Get-Date > out.txt",
    "MergingRedirectionAst": "Get-Date 2>&1",
    "VariableExpressionAst": "Write-Output $x",
    "ParenExpressionAst": "Write-Output (1)",
    "SubExpressionAst": "Write-Output $(1)",
    "ArrayExpressionAst": "Write-Output @(1)",
    "ArrayLiteralAst": "Write-Output 1,2",
    "HashtableAst": "Write-Output @{a=1}",
    "BinaryExpressionAst": "1 + 1",
    "UnaryExpressionAst": "-not 1",
    "TernaryExpressionAst": "$true ? 1 : 2",
    "IndexExpressionAst": "$a[0]",
    "ConvertExpressionAst": "[int]'1'",
    "TypeExpressionAst": "[int]",
    "AttributedExpressionAst": "[ValidateNotNull()]$x = 1",
    "UsingExpressionAst": "Write-Output $using:x",
    "MemberExpressionAst": "$x.Length",
    "InvokeMemberExpressionAst": "$x.ToString()",
    "BaseCtorInvokeMemberExpressionAst": "",
    "ScriptBlockExpressionAst": "Write-Output { 1 }",
    "AssignmentStatementAst": "$x = 1",
    "FunctionDefinitionAst": "function f { 1 }",
    "TypeDefinitionAst": "class C { }",
    "FunctionMemberAst": "class C { [void] M() { } }",
    "PropertyMemberAst": "class C { [int] $p }",
    "ConfigurationDefinitionAst": "Configuration C { }",
    "DynamicKeywordStatementAst": "",
    "UsingStatementAst": "using namespace System",
    "ParamBlockAst": "param($x)",
    "ParameterAst": "param($x)",
    "AttributeAst": "param([Parameter()] $x)",
    "NamedAttributeArgumentAst": "param([Parameter(Mandatory)] $x)",
    "TypeConstraintAst": "param([int] $x)",
    "IfStatementAst": "if ($true) { }",
    "SwitchStatementAst": "switch (1) { 1 { } }",
    "ForStatementAst": "for (;;) { break }",
    "ForEachStatementAst": "foreach ($x in 1) { }",
    "WhileStatementAst": "while ($false) { }",
    "DoWhileStatementAst": "do { } while ($false)",
    "DoUntilStatementAst": "do { } until ($true)",
    "TryStatementAst": "try { } catch { }",
    "CatchClauseAst": "try { } catch { }",
    "TrapStatementAst": "trap { }",
    "DataStatementAst": "data { 'x' }",
    "BlockStatementAst": "",
    "StatementBlockAst": "if ($true) { }",
    "BreakStatementAst": "break",
    "ContinueStatementAst": "continue",
    "ReturnStatementAst": "return",
    "ExitStatementAst": "exit",
    "ThrowStatementAst": "throw 'x'",
    "ErrorStatementAst": "",
    "ErrorExpressionAst": "",
}
# The types no sample here makes, and why each is refused all the same: the reader refuses every type
# POWERSHELL_UNDERSTOOD does not name, which the generic row below holds.
UNSAMPLED = {
    "BaseCtorInvokeMemberExpressionAst": "made for a class constructor's call to its base, which a tree search does "
    "not reach; the class around it is a TypeDefinitionAst, refused",
    "DynamicKeywordStatementAst": "made only for a keyword a DSC resource module defines",
    "BlockStatementAst": "made only for a workflow's parallel or sequence block, which PowerShell 7 refuses to parse",
    "ErrorStatementAst": "made only for text PowerShell cannot parse, which runs nothing",
    "ErrorExpressionAst": "made only for text PowerShell cannot parse, which runs nothing",
    # Off Windows, pwsh parses a Configuration only where PowerShell DSC for Linux has installed its schema store, so
    # the sample is a parse error there, which runs nothing; Windows parses it, and the row holds the type there.
    **(
        {}
        if platform_support.WINDOWS
        else {"ConfigurationDefinitionAst": "parsed only where PowerShell DSC's schema store is installed"}
    ),
}


def pwsh(script: str, stdin: str = "") -> str:
    found = platform_support.find_pwsh("")
    if found is None:
        found = platform_support.find_pwsh(__import__("os").environ.get("PATH", ""))
    if found is None:
        raise AssertionError("pwsh is not installed, and PowerShell lists the catalogue the guard is held to")
    result = subprocess.run(
        [found, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr)
    return result.stdout


def understood() -> set[str]:
    return set(hub_guard.POWERSHELL_UNDERSTOOD)


def refused() -> set[str]:
    return {entry.removeprefix("PowerShell ") for entry in hub_guard.NOT_READ if entry.startswith("PowerShell ")}


class NodeCatalogueTests(unittest.TestCase):
    def test_every_node_type_powershell_has_is_understood_or_not_read_and_never_both(self) -> None:
        types = set(json.loads(pwsh(NODE_TYPES)))
        self.assertTrue(types)
        self.assertEqual(set(), understood() & refused())
        self.assertEqual(set(), types - understood() - refused())
        self.assertEqual(understood() | refused(), set(NODE_SAMPLES))

    def test_each_sample_makes_its_type_and_the_reader_decides_it_by_its_table(self) -> None:
        sampled = {name: sample for name, sample in NODE_SAMPLES.items() if name not in UNSAMPLED}
        self.assertEqual(set(), {name for name, sample in NODE_SAMPLES.items() if not sample} - set(UNSAMPLED))
        results = json.loads(pwsh(PARSE_SAMPLES, json.dumps(list(sampled.values()))))
        for (name, sample), nodes in zip(sampled.items(), results, strict=True):
            with self.subTest(type=name, sample=sample):
                self.assertIn(name, nodes)
                reading = hub_guard.Reading({}, sample)
                self.read(reading, nodes)
                if name in refused():
                    self.assertIn(hub_guard.NOT_READ[f"PowerShell {name}"], " ".join(reading.unread))

    def test_a_node_type_neither_table_names_is_not_read(self) -> None:
        reading = hub_guard.Reading({}, "x")
        self.read(reading, ["ScriptBlockAst", "SomeNewAst"])
        self.assertEqual(["SomeNewAst: a node type the guard does not know"], reading.unread)

    def read(self, reading: hub_guard.Reading, nodes: list[str]) -> None:
        """The reader's decision on a tree of these node types, with no statement it could place."""
        output = json.dumps({"nodes": nodes, "commands": [], "statements": None}).encode("utf-8")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, output, b"")),
        ):
            hub_guard.read_powershell(reading.text, Path.cwd(), reading)


class CommandNameTests(unittest.TestCase):
    def test_every_alias_function_and_verb_powershell_lists_is_one_the_guard_keeps_for_its_own_commands(self) -> None:
        script = (
            "ConvertTo-Json -Compress -InputObject @("
            "@(Get-Alias | ForEach-Object Name), "
            "@(Get-Command -CommandType Function | Where-Object { -not $_.ModuleName } | ForEach-Object Name), "
            "@(Get-Verb | ForEach-Object Verb))"
        )
        aliases, functions, verbs = json.loads(pwsh(script))
        self.assertEqual(set(), {name.lower() for name in aliases} - hub_guard.POWERSHELL_ALIASES)
        self.assertEqual(
            set(),
            {name.lower() for name in functions if "-" not in name} - hub_guard.POWERSHELL_FUNCTIONS,
        )
        self.assertEqual(set(), {name.lower() for name in verbs} - hub_guard.POWERSHELL_VERBS)
        for name in ("iex", "Invoke-Expression", "Start-Process", "help", "C:", "exec"):
            with self.subTest(name=name):
                self.assertTrue(hub_guard.powershell_command(name))
        for name in ("git", "python", "dotnet-format", "shellcheck"):
            with self.subTest(name=name):
                self.assertFalse(hub_guard.powershell_command(name))

    def test_each_alias_the_table_gives_a_cmdlet_is_the_one_powershell_gives_it(self) -> None:
        aliases = [name for name, cmdlet in hub_guard.POWERSHELL_COMMANDS.items() if name != cmdlet]
        script = (
            "$names = [Console]::In.ReadToEnd() | ConvertFrom-Json; "
            "ConvertTo-Json -Compress -InputObject @(foreach ($name in $names) { "
            # By its exact name: Get-Alias -Name reads ? and % as wildcards.
            "$alias = Get-Alias | Where-Object { $_.Name -eq $name } | Select-Object -First 1; "
            "if ($null -eq $alias) { '' } else { $alias.Definition } })"
        )
        resolved = json.loads(pwsh(script, json.dumps(aliases)))
        for name, definition in zip(aliases, resolved, strict=True):
            with self.subTest(alias=name):
                if definition:
                    self.assertEqual(hub_guard.POWERSHELL_COMMANDS[name], definition.lower())
                else:
                    # Where PowerShell gives the name no alias, as ls and cat on Linux, it runs the program, one the
                    # guard reads as harmless either way: none of them runs another command.
                    self.assertNotIn(name, hub_guard.RUNNERS)
