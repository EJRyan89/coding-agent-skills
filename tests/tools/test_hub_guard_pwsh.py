"""The hub guard's PowerShell catalogue, held to PowerShell itself: every AST node type reflection finds in
System.Management.Automation.Language has a sample that PowerShell's parser makes it from, and the reader reads a
command statement by statement only where every node type in it is one POWERSHELL_UNDERSTOOD names, and otherwise
only its git calls and redirections, from a location left unknown once any command may move it. A type a later
PowerShell adds fails here until a sample makes it."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import bounded_process
import resident_pwsh
from bounded_process import CUT_NOTE, Finished
from guard_limits import process_limits_held

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
        found = platform_support.find_pwsh(os.environ.get("PATH", ""))
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


def not_followed(types: set[str]) -> set[str]:
    return types - understood()


class NodeCatalogueTests(unittest.TestCase):
    def test_every_node_type_powershell_has_has_a_sample_and_each_understood_one_is_a_type_it_has(self) -> None:
        types = set(json.loads(pwsh(NODE_TYPES)))
        self.assertTrue(types)
        self.assertEqual(set(), types - set(NODE_SAMPLES))
        self.assertEqual(set(), understood() - types)
        self.assertTrue(not_followed(types), "some node types are never followed statement by statement")

    def test_each_sample_makes_its_type(self) -> None:
        sampled = {name: sample for name, sample in NODE_SAMPLES.items() if name not in UNSAMPLED}
        self.assertEqual(set(), {name for name, sample in NODE_SAMPLES.items() if not sample} - set(UNSAMPLED))
        results = json.loads(pwsh(PARSE_SAMPLES, json.dumps(list(sampled.values()))))
        for (name, sample), nodes in zip(sampled.items(), results, strict=True):
            with self.subTest(type=name, sample=sample):
                self.assertIn(name, nodes)

    def test_a_node_type_the_table_does_not_name_has_only_its_git_calls_read_from_a_location_left_unknown(self) -> None:
        hub = Path.cwd()
        commands = [
            {"elements": [{"kind": "word", "text": "Set-Location"}, {"kind": "word", "text": str(hub)}]},
            {"elements": [{"kind": "word", "text": "git"}, {"kind": "word", "text": "commit"}]},
        ]
        statements = [
            {"pipelines": [[0]], "operators": [], "nested": []},
            {"pipelines": [[1]], "operators": [], "nested": []},
        ]
        for nodes, place in ((["ScriptBlockAst", "CommandAst"], hub), (["ScriptBlockAst", "SomeNewAst"], None)):
            with self.subTest(nodes=nodes):
                reading = self.read(nodes, commands, statements)
                self.assertEqual([], reading.unread)
                [call] = reading.calls
                self.assertEqual(place, call.place.start)

    def read(self, nodes: list[str], commands: list[Any], statements: list[Any]) -> hub_guard.Reading:
        """The reader's reading of a tree of these node types, commands, and statements."""
        reading = hub_guard.Reading({}, "x")
        output = json.dumps({"nodes": nodes, "commands": commands, "statements": statements}).encode("utf-8")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, output, b"")),
        ):
            hub_guard.read_powershell(reading.text, Path("/elsewhere"), reading)
        return reading


class RedirectionReachTests(unittest.TestCase):
    def test_a_file_redirection_no_statement_reaches_is_noted(self) -> None:
        # The tree holds one file redirection, and the statements reach no command that carries it.
        output = json.dumps(
            {"nodes": ["ScriptBlockAst"], "commands": [], "statements": [], "fileRedirections": 1}
        ).encode("utf-8")
        reading = hub_guard.Reading({}, "x")
        with (
            mock.patch.object(platform_support, "find_pwsh", return_value="pwsh"),
            mock.patch.object(hub_guard, "run_bounded", return_value=Finished(0, output, b"")),
        ):
            hub_guard.read_powershell("x", Path.cwd(), reading)
        self.assertEqual(["a file redirection the reader did not reach"], reading.notes)
        self.assertEqual([], reading.unread)


class CommandNameTests(unittest.TestCase):
    def test_every_alias_function_and_verb_powershell_lists_is_one_the_guard_keeps_for_its_own_commands(self) -> None:
        script = (
            "ConvertTo-Json -Compress -InputObject @("
            "@(Get-Alias | ForEach-Object Name), "
            "@(Get-Command -CommandType Function | Where-Object { -not $_.ModuleName } | ForEach-Object Name), "
            "@(Get-Command -CommandType Cmdlet,Function | Where-Object { $_.Name -like '*-*' } | ForEach-Object Name))"
        )
        aliases, functions, commands = json.loads(pwsh(script))
        self.assertEqual(set(), {name.lower() for name in aliases} - hub_guard.POWERSHELL_ALIASES)
        self.assertEqual(
            set(),
            {name.lower() for name in functions if "-" not in name} - hub_guard.POWERSHELL_FUNCTIONS,
        )
        # Every cmdlet and function a hyphenated name gives, whatever its verb and whichever modules this machine
        # holds, as Tee-Object or a Windows module's Backup-BitLockerKeyProtector, is a command the guard reads only
        # where POWERSHELL_COMMANDS names it.
        self.assertTrue(commands)
        self.assertEqual(set(), {name for name in commands if not hub_guard.powershell_command(name)})
        for name in (
            *("iex", "Invoke-Expression", "Start-Process", "help", "C:", "exec", "Tee-Object"),
            *("BackupToAAD-BitLockerKeyProtector", "Delete-DeliveryOptimizationCache", "dotnet-format", "pre-commit"),
        ):
            with self.subTest(name=name):
                self.assertTrue(hub_guard.powershell_command(name))
        for name in ("git", "python", "shellcheck", "dotnet-format.exe", "/usr/local/bin/pre-commit", "C:\\x\\a-b"):
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
                    # Where PowerShell gives the name no alias, as ls and cat on Linux, it runs the program, which,
                    # as the cmdlet would, moves no location, so the guard reads it alike either way.
                    self.assertNotIn(
                        hub_guard.POWERSHELL_COMMANDS[name], {"set-location", "push-location", "pop-location"}
                    )


def found_pwsh() -> str:
    found = platform_support.find_pwsh(os.environ.get("PATH", ""))
    if found is None:
        raise AssertionError("pwsh is not installed, and the guard's reader runs in it")
    return found


# Commands beyond the node samples whose reading takes another path: a parse error, which the reader answers with
# its message, text past ASCII, which travels base64-encoded, and a lone surrogate, which has no JSON form.
EDGE_SAMPLES = ("if (", "Get-Date |", "git commit -m 'café ü€'", "git commit -m \ud800", "")


class ResidentReaderTests(unittest.TestCase):
    """The resident pwsh the in-process tests read PowerShell through reads every command as the guard's own pwsh
    start does, keeps nothing from one read to the next, and fails the test that meets it misbehaving."""

    def read(self, command: str) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        reading = hub_guard.Reading({"PATH": os.environ.get("PATH", "")}, command)
        with process_limits_held():
            events = hub_guard.powershell_events(command, reading)
        return events, reading.notes, reading.unread

    def test_the_resident_reads_every_node_sample_as_a_real_pwsh_start_does(self) -> None:
        samples = [*NODE_SAMPLES.values(), *EDGE_SAMPLES]
        # No read below starts a pwsh of its own: were powershell_events to start its reader in a way
        # resident_pwsh.reads_its_reader no longer knows, every in-process read would quietly start pwsh again.
        started = AssertionError("a read started a pwsh of its own rather than reading in the resident")
        with (
            mock.patch.object(bounded_process, "run_bounded", side_effect=started),
            resident_pwsh.resident_reads(),
        ):
            resident = {sample: self.read(sample) for sample in samples}
            # The same samples read again in the other order: no read is shaped by the ones before it.
            again = {sample: self.read(sample) for sample in reversed(samples)}
        self.assertEqual(resident, again)
        for name, sample in [*NODE_SAMPLES.items(), *(("edge", sample) for sample in EDGE_SAMPLES)]:
            with self.subTest(type=name, sample=sample):
                self.assertEqual(self.read(sample), resident[sample])
                events = resident[sample][0]
                if name in NODE_SAMPLES and name not in UNSAMPLED:
                    self.assertIsNotNone(events)
                    self.assertIn(name, (events or {}).get("nodes", []))
        self.assertIn("PowerShell would not parse the command", " ".join(resident["if ("][1]))
        self.assertIn("could not hand pwsh the command", " ".join(resident["git commit -m \ud800"][1]))

    def test_only_the_reader_s_own_start_goes_to_the_resident(self) -> None:
        reader = [found_pwsh(), *resident_pwsh.READER_OPTIONS, hub_guard.powershell_reader()]
        self.assertTrue(resident_pwsh.reads_its_reader(reader))
        self.assertFalse(resident_pwsh.reads_its_reader([*reader[:-1], "Get-Date"]))
        self.assertFalse(resident_pwsh.reads_its_reader([reader[0], "-NoProfile", *reader[2:]]))
        real = Finished(0, b"real", b"")
        rows: tuple[tuple[list[str], dict[str, Any]], ...] = (
            (["git", "status"], {"input_bytes": b"x"}),
            ([*reader[:-1], "Get-Date"], {"input_bytes": b"x"}),
            # A start with an option powershell_events does not pass runs as it is.
            (reader, {"input_bytes": b"x", "cwd": Path.cwd()}),
        )
        for command, options in rows:
            with (
                self.subTest(command=command[-1][:20], options=sorted(options)),
                mock.patch.object(bounded_process, "run_bounded", return_value=real) as bounded,
                mock.patch.object(resident_pwsh, "resident", side_effect=AssertionError("not the reader")),
                resident_pwsh.resident_reads() as reads,
            ):
                self.assertEqual(real, reads.run_bounded(command, 5.0, **options))
                bounded.assert_called_once_with(command, 5.0, **options)

    def test_a_read_that_leaves_a_global_behind_is_a_malfunction(self) -> None:
        for reader, kept in (("$global:kept = 1", "variable kept"), ("function global:Kept { }", "function Kept")):
            with self.subTest(reader=reader):
                resident = resident_pwsh.ResidentPwsh(found_pwsh(), reader)
                self.addCleanup(resident.close)
                with self.assertRaisesRegex(resident_pwsh.Malfunction, kept):
                    resident.read(b"", 600.0, None)

    def test_a_reader_s_own_variables_and_errors_end_with_its_read(self) -> None:
        # The reader's own preference makes its error stop it, as the guard's reader sets it; what it wrote before
        # the error is kept on stdout, and the error is on stderr, as pwsh writes them.
        reader = "$ErrorActionPreference = 'Stop'; $mine = [Console]::In.ReadToEnd(); Write-Output $mine; Write-Error x"
        resident = resident_pwsh.ResidentPwsh(found_pwsh(), reader)
        self.addCleanup(resident.close)
        for payload in (b"first", b"second"):
            with self.subTest(payload=payload):
                finished = resident.read(payload, 600.0, None)
                self.assertEqual((1, payload + b"\n"), (finished.returncode, finished.stdout))
                self.assertIn(b"x", finished.stderr)
        silent = resident_pwsh.ResidentPwsh(found_pwsh(), "")
        self.addCleanup(silent.close)
        self.assertEqual(Finished(0, b"", b""), silent.read(b"", 600.0, None))

    def test_a_resident_that_does_not_answer_in_time_is_ended(self) -> None:
        resident = resident_pwsh.ResidentPwsh(found_pwsh(), "Start-Sleep 60")
        self.addCleanup(resident.close)
        with self.assertRaises(subprocess.TimeoutExpired):
            resident.read(b"", 1.0, None)
        self.assertIsNotNone(resident.process.poll())

    def test_an_answer_that_is_not_one_is_a_malfunction(self) -> None:
        for answer, said in ((b"garbage\n", "answered b'garbage"), (b"READ x\n", "answered b'READ x"), (b"", "ended")):
            with self.subTest(answer=answer):
                resident = resident_pwsh.ResidentPwsh(found_pwsh(), "")
                self.addCleanup(resident.close)
                resident.answers.put(answer)
                with self.assertRaisesRegex(resident_pwsh.Malfunction, said):
                    resident.read(b"", 600.0, None)

    def test_a_resident_that_takes_no_input_is_a_malfunction(self) -> None:
        resident = resident_pwsh.ResidentPwsh(found_pwsh(), "")
        resident.close()
        with self.assertRaisesRegex(resident_pwsh.Malfunction, "took no input"):
            resident.read(b"", 600.0, None)

    def test_a_malfunction_or_a_time_out_fails_the_test_that_met_it_whatever_the_guard_decided(self) -> None:
        class Broken:
            def __init__(self, failure: Exception) -> None:
                self.failure = failure

            def read(self, payload: bytes, timeout: float, output_limit: int | None) -> Finished:
                raise self.failure

        failures: tuple[tuple[Exception, str], ...] = (
            (resident_pwsh.Malfunction("it broke"), "it broke"),
            (subprocess.TimeoutExpired("pwsh", 5), "did not answer within"),
        )
        for failure, met in failures:
            with self.subTest(failure=type(failure).__name__):
                reads: list[tuple[dict[str, Any] | None, list[str], list[str]]] = []
                with (
                    self.assertRaisesRegex(AssertionError, f"the resident pwsh malfunctioned: .*{met}"),
                    mock.patch.object(resident_pwsh, "resident", return_value=Broken(failure)),
                    resident_pwsh.resident_reads(),
                ):
                    reads.append(self.read("git status"))
                # The guard read nothing, as it reads nothing from a pwsh that failed, and the test fails anyway.
                [(events, notes, _)] = reads
                self.assertIsNone(events)
                self.assertTrue(notes)

    def test_a_resident_that_ended_is_started_again_at_the_next_read(self) -> None:
        program = found_pwsh()
        first = resident_pwsh.resident(program)
        first.close()
        second = resident_pwsh.resident(program)
        self.assertIsNot(first, second)
        self.assertIs(second, resident_pwsh.resident(program))
        with resident_pwsh.resident_reads():
            self.assertIsNotNone(self.read("Get-Date")[0])

    def test_a_folder_the_first_read_stood_in_can_be_removed_while_the_resident_runs(self) -> None:
        # The in-process guard reads from inside a test's temporary folder, which the test removes as it ends.
        resident_pwsh.close_all()
        with tempfile.TemporaryDirectory() as folder, contextlib.chdir(folder):
            running = resident_pwsh.resident(found_pwsh())
        self.assertIsNone(running.process.poll())
        self.assertFalse(Path(folder).exists())

    def test_the_process_ending_ends_every_resident(self) -> None:
        running = resident_pwsh.resident(found_pwsh())
        resident_pwsh.close_all()
        self.assertIsNotNone(running.process.poll())
        self.assertIsNot(running, resident_pwsh.resident(found_pwsh()))

    def test_each_stream_is_kept_to_the_output_limit_as_run_bounded_keeps_it(self) -> None:
        self.assertEqual(b"abc", resident_pwsh.kept(b"abc", None))
        self.assertEqual(b"abc", resident_pwsh.kept(b"abc", 3))
        self.assertEqual(b"ab" + CUT_NOTE.format(cut=1, limit=2).encode(), resident_pwsh.kept(b"abc", 2))
