"""The hub guard's PowerShell catalogue, held to PowerShell itself: every AST node type reflection finds in
System.Management.Automation.Language is understood or in NOT_READ, each has a sample that PowerShell's parser makes
it from, and the reader refuses every sample of a type it does not understand. A type a later PowerShell adds fails
here until a row decides it."""

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
import option_reader
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


class ProviderPathTests(unittest.TestCase):
    def test_every_path_cmdlet_refuses_a_provider_path_by_position_or_attached_and_a_writer_an_unsettled_one(
        self,
    ) -> None:
        for cmdlet in sorted(hub_guard.POWERSHELL_PATH_COMMANDS):
            writes = cmdlet in hub_guard.POWERSHELL_WRITING_COMMANDS
            rows: tuple[tuple[list[option_reader.Word], bool], ...] = (
                (["env:GIT_DIR"], True),
                (["alias:git", "x"], True),
                (["x", "Environment::GIT_DIR"], True),
                ([None], writes),
                (["README.md", "C:/x"], False),
            )
            for values, refused in rows:
                with self.subTest(cmdlet=cmdlet, values=values):
                    self.assertEqual(refused, hub_guard.provider_refusal(cmdlet, cmdlet, values) is not None)
        self.assertIsNone(hub_guard.provider_refusal("Write-Output", "write-output", ["env:GIT_DIR"]))

    def test_a_value_attached_to_a_parameter_is_judged_as_the_cmdlet_s_value(self) -> None:
        reader = hub_guard.PowerShellReader(hub_guard.Reading({}, ""), [])
        command = {
            "elements": [
                {"kind": "word", "text": "Set-Content"},
                {"kind": "parameter", "text": "-Path:", "argument": {"kind": "word", "text": "env:GIT_DIR"}},
                {"kind": "parameter", "text": "-Value"},
                {"kind": "word", "text": "x"},
                {"kind": "parameter", "text": "-LiteralPath:", "argument": {"kind": "other", "type": "X"}},
            ]
        }
        self.assertEqual(["env:GIT_DIR", "x", None], reader.values(command))
        self.assertEqual(["Set-Content", "-Path:env:GIT_DIR", "-Value", "x", None], reader.words(command))


class RedirectionReachTests(unittest.TestCase):
    def test_a_file_redirection_no_statement_reaches_is_refused(self) -> None:
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
        self.assertEqual(["a file redirection the reader did not reach, which may write in a hub"], reading.unread)


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
                    # guard reads as harmless either way: none of them runs another command. One whose options
                    # may, as sort's, is judged by its catalogue there.
                    self.assertNotIn(name, hub_guard.RUNNERS)
                    if name in hub_guard.PROGRAM_OPTIONS:
                        self.assertFalse(platform_support.POWERSHELL_ALIASES_SHADOW_PROGRAMS)


def powershell_reading(command: str) -> hub_guard.Reading:
    """The guard's reading of a PowerShell command, as data: pwsh only parses it, in this process's resident pwsh,
    which ResidentReaderTests holds to a real start."""
    reading = hub_guard.Reading({"PATH": os.environ.get("PATH", "")}, command)
    with process_limits_held(), resident_pwsh.resident_reads():
        hub_guard.read_powershell(command, Path.cwd(), reading)
    return reading


def bash_reading(command: str) -> hub_guard.Reading:
    """The guard's reading of a Bash command, as data: nothing is run."""
    reading = hub_guard.Reading({}, command)
    with process_limits_held():
        hub_guard.read_bash(command, Path.cwd(), reading)
    return reading


class RunnerOptionTests(unittest.TestCase):
    """The PowerShell reader judges a runner's and a program's options by the allow-lists the Bash reader does, and
    reads gh after a runner; tests/tools/test_prefix_programs.py holds the allow-lists themselves to each catalogue."""

    def test_a_runner_s_or_a_program_s_option_not_known_to_be_safe_is_refused_in_every_spelling(self) -> None:
        for command, refused in (
            ("tar --to-com=git -xf a.tar", True),
            ("tar -xIgit -f a.tar", True),
            ("tar xIf a.tar", True),
            # sort alone is PowerShell's alias of Sort-Object; sort.exe is the program.
            ("sort.exe --compress=x f", True),
            ("sort.exe -o out.txt f", True),
            ("sort.exe -r f", False),
            ("rg --pre x y", True),
            ("find . -exec ls '{}' ';'", True),
            ("find . -delete", True),
            ("tar $x a.tar", True),
            ("Write-Output git | xargs --replac=X X status", True),
            ("Write-Output git | xargs -0IX X status", True),
            ("env --split='git status'", True),
            ("env -C d ls", True),
            # The Rust coreutils' env reads variables from a file by --file and names what its command runs as by
            # --argv0, and ubuntu-26.04 ships that env.
            ("env --file x git commit", True),
            ("env -fx ls", True),
            ("env --argv0 name git status", True),
            ("env -aname ls", True),
            ("timeout -p 5 ls", False),
            ("timeout 5 tar --to-command=git -xf a.tar", True),
            ("Write-Output a | xargs rg x", True),
            ("timeout 5 tar -tf a.tar", False),
            ("env 'TAR_OPTIONS=--to-command=git' tar -tf a.tar", True),
            ("env -u X 'TAR_OPTIONS=--to-command=git' tar -tf a.tar", True),
            ("env RIPGREP_CONFIG_PATH=c rg x", True),
            ("sudo 'TAR_OPTIONS=x' tar -tf a.tar", True),
            ("tar -tf host:a.tar", True),
            ("tar -tf host:a.tar -- --force-local", True),
            ("tar -tf host:a.tar --exclude --force-local", True),
            ("tar -tf a.tar -H --use-compress-program=git", True),
            ("tar -tf a.tar -W use-compress-program=git", True),
            ("env GIT_DIR=x gh pr checkout 1", True),
            ("env -u X GIT_DIR=x ls", True),
            ("tar -xf a.tar", True),
            ("tar -czf a.tar d", False),
            ("sort -u -k 2 f", False),
            ("find . -name x -mtime -1", False),
            ("rg -n --glob '*.py' x", False),
            ("timeout -s TERM 60 ls -la", False),
            ("Write-Output x | xargs -0 grep -n x", False),
        ):
            with self.subTest(command=command):
                self.assertEqual(refused, bool(powershell_reading(command).unread))

    def test_a_variable_a_program_reads_options_from_is_refused_from_the_environment(self) -> None:
        for command in ("tar -xf a.tar", "nice tar -xf a.tar"):
            with self.subTest(command=command):
                reading = hub_guard.Reading({"PATH": os.environ.get("PATH", ""), "TAR_OPTIONS": "x"}, command)
                with process_limits_held():
                    hub_guard.read_powershell(command, Path.cwd(), reading)
                self.assertIn("TAR_OPTIONS", " ".join(reading.unread))

    def test_sort_is_read_as_the_program_where_pwsh_gives_it_no_alias(self) -> None:
        self.assertEqual(platform_support.WINDOWS, platform_support.POWERSHELL_ALIASES_SHADOW_PROGRAMS)
        for aliased, command, refused in (
            (False, "sort --compress=x f", True),
            (False, "sort -o out.txt f", True),
            (False, "sort -r f", False),
            # On Windows sort is Sort-Object, whose parameters run nothing.
            (True, "sort --compress=x f", False),
            (True, "sort -Descending", False),
        ):
            with (
                self.subTest(aliased=aliased, command=command),
                mock.patch.object(platform_support, "POWERSHELL_ALIASES_SHADOW_PROGRAMS", aliased),
            ):
                self.assertEqual(refused, bool(powershell_reading(command).unread))
        here = powershell_reading("sort --compress=x f")
        self.assertEqual(not platform_support.WINDOWS, bool(here.unread), "this platform's own reading")

    def test_a_windows_build_s_slash_option_is_read_where_pwsh_reaches_that_build(self) -> None:
        for windows, command, refused in (
            (True, "sort.exe /o out.txt f", True),
            (True, "sort.exe /OUTPUT out.txt f", True),
            (True, "sort.exe /t d f", True),
            (True, "sort.exe /r f", False),
            (True, "find.exe /c x f", False),
            (True, "find.exe /x x f", True),
            (True, "timeout 5 sort.exe /o out.txt f", True),
            (True, "sort.exe -- /o out.txt f", True),
            (True, "C:\\WINDOWS\\SysWOW64\\SORT.EXE /O out.txt f", True),
            # A joined pair, an attached value, and a slash word another option takes as its value.
            (True, "sort.exe /r/o out.txt f", True),
            (True, "sort.exe /L C /O out.txt f", True),
            (True, "sort.exe /L /O out.txt f", True),
            (True, "sort.exe /Oout.txt f", True),
            (True, "sort.exe /O:out.txt f", True),
            (True, "sort.exe /Tdir f", True),
            (True, "sort.exe -t /O out.txt f", True),
            (True, "find.exe /c/v x f", True),
            (True, "sort.exe /L C /r f", False),
            # sort.exe may read a dash as a slash; find.exe has no option that writes or runs anything.
            (True, "sort.exe -u f", True),
            (True, "sort.exe -t x f", True),
            (True, "sort.exe -temporary d f", True),
            (True, "sort.exe -r f", False),
            (False, "sort.exe -u f", False),
            (True, "find.exe . -name x", False),
            (True, "sort.exe /reve\u017fe f", True),
            (False, "sort.exe /o out.txt f", False),
        ):
            with (
                self.subTest(windows=windows, command=command),
                mock.patch.object(platform_support, "SYSTEM_PROGRAMS_TAKE_SLASH_OPTIONS", windows),
            ):
                self.assertEqual(refused, bool(powershell_reading(command).unread))

    def test_gh_behind_xargs_is_refused(self) -> None:
        for command in ("Write-Output checkout 1 | xargs gh pr", "Write-Output x | xargs -E gh gh pr checkout 1"):
            with self.subTest(command=command):
                self.assertIn("words from its input", " ".join(powershell_reading(command).unread))

    def test_bash_and_pwsh_are_refused_whatever_they_run(self) -> None:
        for command in ("bash -c 'ls'", "bash script.sh", "pwsh -NoProfile -Command 'Get-Date'", "pwsh -File x.ps1"):
            with self.subTest(command=command):
                program = command.split()[0]
                self.assertIn(hub_guard.RUNNERS[program], " ".join(powershell_reading(command).unread))

    def test_gh_behind_chroot_is_read_where_the_text_does_not_settle(self) -> None:
        unsettled = powershell_reading("chroot /srv gh pr checkout 1")
        self.assertEqual([("gh", None)], [(call.program, call.place.start) for call in unsettled.calls])
        placed = powershell_reading("timeout 60 gh pr checkout 1")
        self.assertEqual([("gh", Path.cwd())], [(call.program, call.place.start) for call in placed.calls])

    def test_gh_after_a_runner_is_read_as_the_checkout_it_makes(self) -> None:
        for command in (
            "timeout 60 gh pr checkout 1",
            "nice gh repo sync",
            "gh pr checkout 1",
            # gh as an option's value first, then as the command.
            "env -u gh gh pr checkout 1",
            "sudo -u gh gh pr checkout 1",
        ):
            with self.subTest(command=command):
                reading = powershell_reading(command)
                self.assertEqual([], reading.unread)
                self.assertEqual(["gh"], [call.program for call in reading.calls])
        self.assertEqual([], powershell_reading("timeout 60 gh pr view 1").calls)

    def test_both_readers_read_options_through_one_helper(self) -> None:
        for command, read in (
            ("timeout -s TERM 60 ls", powershell_reading),
            ("tar -czf a.tar d", powershell_reading),
            ("timeout -s TERM 60 ls", bash_reading),
            ("tar -czf a.tar d", bash_reading),
        ):
            with (
                self.subTest(command=command, reader=read.__name__),
                mock.patch.object(hub_guard, "option_refusal", wraps=option_reader.option_refusal) as helper,
            ):
                self.assertEqual([], read(command).unread)
                self.assertTrue(helper.called)


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
        self.assertIn("could not hand pwsh the command", " ".join(resident["git commit -m \ud800"][2]))

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
                resident_pwsh.resident_reads(),
            ):
                self.assertEqual(real, hub_guard.run_bounded(command, 5.0, **options))
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
                [(events, _, unread)] = reads
                self.assertIsNone(events)
                self.assertTrue(unread)

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
