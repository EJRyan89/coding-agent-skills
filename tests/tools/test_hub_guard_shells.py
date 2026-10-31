"""Regression suite holding tools/hub_guard.py's readings to the shells themselves: each spelling of pwsh's
inline-script and standard-input parameters the guard reads, run by pwsh, and each Bash builtin that sets a variable,
run by Bash, so that a release of either that reads one otherwise fails here.

It starts pwsh once for each spelling and Bash once in all; tests/tools/test_hub_guard_pwsh.py and
tests/tools/test_hub_guard_bash.py hold the same readings without starting either.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from bounded_process import run_bounded

from deployer import platform_support
from tools import hub_guard

INLINE = "Write-Output ('INLINE' + '-RAN')"
STANDARD_INPUT = "Write-Output ('STDIN' + '-RAN')\n"
# Each pwsh command line, with INLINE standing for the inline script.
PWSH_COMMAND_LINES = (
    *(
        (spelling, INLINE)
        for spelling in (
            "-c",
            "-Command",
            "--c",
            "--command",
            "/c",
            "/Command",
            "\u2013c",
            "\u2014command",
            "\u2015co",
            "\u2013\u2013c",
            "\u2014\u2014command",
            "\u2015\u2015c",
            " -c ",
            "\t--command\n",
            "\u3000-c",
            "-cwa",
            "--cwa",
            "/cwa",
            "\u2013cwa",
            "--CommandWithArgs",
            "---c",
            "-\u2013c",
            "//c",
            "/-c",
            "-commandx",
        )
    ),
    ("-c", "-"),
    ("--command", "-"),
    ("/c", "-"),
    ("--file", "-"),
    ("/f", "-"),
    ("\u2014file", "-"),
    ("-",),
    ("--",),
    ("", "-c", "-"),
    ("-", "-c", INLINE),
    ("-NoProfile", "-", "-c", INLINE),
)
# Each Bash command that may set GIT_DIR, exported as `before` when it starts.
BASH_COMMANDS = (
    "read GIT_DIR <<< after",
    "read -r other GIT_DIR <<< 'one after'",
    "read -a GIT_DIR <<< after",
    "read -rd, GIT_DIR <<< after,",
    "read -p GIT_DIR other <<< after",
    "read <<< after",
    "read -- GIT_DIR <<< after",
    "printf -v GIT_DIR %s after",
    "printf -vGIT_DIR %s after",
    "printf -v 'GIT_DIR[0]' %s after",
    "printf -- -v GIT_DIR",
    "printf -v other %s GIT_DIR",
    "getopts ab GIT_DIR -a",
    "mapfile GIT_DIR < /dev/null",
    "declare -n MAPFILE=GIT_DIR; mapfile < /dev/null",
    "declare -n REPLY=GIT_DIR; read <<< after",
    "read -N GIT_DIR other <<< after",
    "read -p GIT_DIR -- other <<< after",
    "read -- -aGIT_DIR <<< after",
    "export -n GIT_DIR; read GIT_DIR <<< after",
    "getopts ab other GIT_DIR",
    "sleep 0 & wait -p GIT_DIR",
    "wait -p other",
    "wait -p other GIT_DIR",
    "printf -v other GIT_DIR",
    "declare -n ref=GIT_DIR; unset ref; export GIT_DIR",
    "unset GIT_DIR; declare -nx ref=GIT_DIR; ref=after",
    "unset GIT_DIR; declare -n ref=GIT_DIR; export ref; ref=after",
    "declare -n ref=GIT_DIR; for ref in one two; do :; done",
    "declare -n ref=GIT_DIR; unset ref; GIT_DIR=after",
    "declare -n GIT_DIR=other; unset -n GIT_DIR",
    "declare -n GIT_DIR=other; unset -n GIT_DIR; GIT_DIR=after",
    "declare -n GIT_DIR=other; unset -n GIT_DIR; export GIT_DIR",
    "unset GIT_DIR; export GIT_DIR; declare -n GIT_DIR",
    "declare -n ref; declare ref=GIT_DIR GIT_DIR=after",
    "declare -n ref; ref=GIT_DIR; declare -n ref; ref=after",
    "if [ -n x ]; then declare -n ref=GIT_DIR; else declare -n ref=GIT_DIR; fi; ref=after",
    "readarray -t other < /dev/null",
    "ref=GIT_DIR; declare -n ref; ref=after",
    "declare -n ref=GIT_DIR; declare -n ref; ref=after",
    "declare -n ref=GIT_DIR; f() { true; }; f; ref=after",
    "f() { local -n ref=GIT_DIR; }; f; ref=after",
    "declare -n ref=GIT_DIR; ref=after",
    "typeset -n ref=GIT_DIR; ref=after",
    "f() { local -n ref=GIT_DIR; ref=after; }; f",
    "declare -n ref=GIT_DIR; ref=after true",
    "declare -n ref=GIT_DIR; export ref=after",
    "declare -n ref=GIT_DIR; declare ref=after",
    "declare -n ref; ref=GIT_DIR; ref=after",
    "declare -n first=second second=GIT_DIR; first=after",
    "declare -n r1=r2 r2=r3 r3=r4 r4=r5 r5=r6 r6=r7 r7=r8 r8=GIT_DIR; r1=after",
    "declare -n r0=r1 r1=r2 r2=r3 r3=r4 r4=r5 r5=r6 r6=r7 r7=r8 r8=GIT_DIR; r0=after",
    "declare -n ref=GIT_DIR; read ref <<< after",
    "declare -n ref=GIT_DIR; printf -v ref %s after",
    "declare -n ref=GIT_DIR; ref+=after",
    "declare -n ref=GIT_DIR; unset ref",
    "declare -n ref=GIT_DIR; export -n ref",
    "declare -n ref=GIT_DIR; unset -n ref; ref=after",
    "declare -n ref=GIT_DIR; declare +n ref; ref=after",
    "declare -n ref=GIT_DIR; declare -n ref=other; ref=after",
    "declare -n ref; for ref in GIT_DIR; do ref=after; done",
    "declare -n ref=other; for ref in GIT_DIR; do ref=after; done",
    "declare -n GIT_DIR=other",
    "readonly -n ref=GIT_DIR; ref=after",
    # set takes the option each o in a bundle names from the next word.
    "unset GIT_DIR; set -o allexport; GIT_DIR=after",
    "unset GIT_DIR; set -eo allexport; GIT_DIR=after",
    "unset GIT_DIR; set -oe allexport; GIT_DIR=after",
    "unset GIT_DIR; set -oo pipefail allexport; GIT_DIR=after",
    "unset GIT_DIR; set -ao pipefail; GIT_DIR=after",
    "unset GIT_DIR; set -eo pipefail; GIT_DIR=after",
    "unset GIT_DIR; set -o allexport; set +eo allexport; GIT_DIR=after",
    "unset GIT_DIR; set -o pipefail allexport; GIT_DIR=after",
    "unset GIT_DIR; set -o allexport +e; GIT_DIR=after",
    "unset GIT_DIR; set -e -o allexport; GIT_DIR=after",
    "unset GIT_DIR; set -o pipefail -o allexport; GIT_DIR=after",
)
UNSET = "<unset>"


def pwsh_runs(words: tuple[str, ...]) -> tuple[bool, bool]:
    """Whether pwsh, given these words and a script on its standard input, runs that script and the inline one."""
    pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
    if pwsh is None:
        raise AssertionError("pwsh was not found on PATH; pwsh checks the guard's reading of its command line")
    result = run_bounded(
        [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", *words],
        120,
        input_bytes=STANDARD_INPUT.encode("utf-8"),
        env=os.environ,
    )
    lines = {line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines()}
    return "STDIN-RAN" in lines, "INLINE-RAN" in lines


def bash_values(commands: tuple[str, ...], start: Path) -> list[str]:
    """The value of GIT_DIR in the environment after each command, run by Bash in a subshell of its own in start, or
    UNSET."""
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the Bash reader's verdicts")
    # Each value is printed on a line of its own after what the command printed, as `<index> <value>`.
    script = "".join(
        f'(export GIT_DIR=before; {command}\necho; echo "=== {index} $(printenv GIT_DIR || echo "{UNSET}")") '
        "2>/dev/null\n"
        for index, command in enumerate(commands)
    )
    path = start / "commands.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    result = platform_support.run_tool([bash, "--norc", "--noprofile", platform_support.normalize(path)])
    values: dict[int, str] = {}
    for line in result.output.splitlines():
        if line.startswith("=== "):
            index, _, value = line[4:].partition(" ")
            values[int(index)] = value
    return [values.get(index, "") for index in range(len(commands))]


def guard_value(command: str, start: Path) -> Path | str | None:
    """The repository GIT_DIR names for a git call after the command, as the guard reads it: None when the text
    does not settle it, and UNSET when no GIT_DIR reaches the call."""
    text = f"export GIT_DIR=before; {command}\ngit status"
    reading = hub_guard.Reading({}, text)
    hub_guard.read_bash(text, start, reading, 0, {})
    place = reading.calls[-1].place
    return place.repository if place.repository_named else UNSET


class PwshDifferentialTests(unittest.TestCase):
    """The guard reads each command line as pwsh runs it: the script on its standard input, the inline script, or
    neither."""

    def test_the_guard_reads_each_spelling_as_pwsh_runs_it(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            runs = list(pool.map(pwsh_runs, PWSH_COMMAND_LINES))
        for words, (reads_input, runs_inline) in zip(PWSH_COMMAND_LINES, runs, strict=True):
            arguments: list[hub_guard.Word] = list(words)
            read = [None] if hub_guard.powershell_reads_input(arguments) else hub_guard.powershell_script(arguments)
            with self.subTest(words=words, reads_input=reads_input, runs_inline=runs_inline):
                # A script pwsh reads from standard input is refused, whatever else runs; pwsh on Linux reads no
                # script from the input this suite hands it, so there only the inline script tells the forms apart.
                if reads_input:
                    self.assertEqual([None], read)
                elif runs_inline:
                    self.assertIn(read, ([INLINE], [None]))
                # Where pwsh runs neither, as after an empty word, which it takes for a missing script file, the guard
                # may read more than runs; it reads the inline script only where pwsh runs one.
                if read == [INLINE]:
                    self.assertTrue(runs_inline)

    def test_the_powershell_reader_follows_a_child_pwsh_by_each_spelling(self) -> None:
        spellings = [words[0] for words in PWSH_COMMAND_LINES if words[1:] == (INLINE,)]
        # Each spelling quoted, and each without white space bare, as PowerShell's parser reads a parameter.
        bare = [spelling for spelling in spellings if spelling.strip() == spelling]
        statements = [f"pwsh '{spelling}' 'Write-Output {index}'" for index, spelling in enumerate(spellings)]
        statements += [
            f"pwsh {spelling} 'Write-Output {index + len(spellings)}'" for index, spelling in enumerate(bare)
        ]
        reading = hub_guard.Reading(dict(os.environ), "")
        events = hub_guard.powershell_events("; ".join(statements), reading, 0)
        self.assertIsNotNone(events, reading.unread)
        followed = set()
        inside = False
        for event in events or []:
            if event["kind"] == "open" and event["region"] == "child":
                inside = True
            elif event["kind"] == "close":
                inside = False
            elif inside and event["kind"] == "command":
                followed.add(int(event["words"][1]["text"]))
        expected = {
            index
            for index, spelling in enumerate([*spellings, *bare])
            if hub_guard.powershell_script([spelling, INLINE]) is not None
        }
        self.assertEqual(expected, followed)


class FunctionCallDifferentialTests(unittest.TestCase):
    """The guard reads a function's body wherever pwsh runs it, whatever words the call passes."""

    def test_the_guard_reads_each_function_body_pwsh_runs(self) -> None:
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if pwsh is None:
            raise AssertionError("pwsh was not found on PATH; pwsh checks the guard's reading of a function call")
        # Each definition, with BODY standing for its body, and the call after it.
        forms = (
            ("function f { BODY }", "f"),
            ("function f { BODY }", "f extra"),
            ("function f { BODY }", "f -Name extra"),
            ("function f($a) { BODY }", "f one two"),
            ("function f { param($a) BODY }", "f -b one"),
            ("function f { [CmdletBinding()] param() BODY }", "f"),
            ("function f { [CmdletBinding()] param() BODY }", "f extra"),
            ("function f { param([Parameter()] $a) BODY }", "f one two"),
            # -WhatIf reaches only the cmdlets in an advanced function's body that honour it.
            ("function f { [CmdletBinding(SupportsShouldProcess)] param() BODY }", "f -WhatIf"),
            ("function f { [CmdletBinding(SupportsShouldProcess)] param() BODY }", "f -wi"),
            ("function f { [CmdletBinding()] param() BODY }", "f -WhatIf"),
            # A function that takes a cmdlet's name is the one called, and the binder reads -WhatIf by the cmdlet's.
            ("function Remove-Item { [CmdletBinding(SupportsShouldProcess)] param() BODY }", "Remove-Item -WhatIf"),
            # A scope in the definition is not part of the name the function is called by.
            ("function global:f { BODY }", "f"),
            ("function script:f { BODY }", "f extra"),
            ("function local:f { BODY }", "f"),
            ("function private:f { BODY }", "f"),
            ("function Global:f { BODY }", "global:f"),
            ("function f { BODY }", "script:f"),
        )
        for definition, call in forms:
            ran_script = f"{definition.replace('BODY', 'Write-Output BODY-RAN')}; {call}"
            result = run_bounded(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-c", ran_script], 120, env=os.environ
            )
            ran = "BODY-RAN" in result.stdout.decode("utf-8", "replace")
            text = f"{definition.replace('BODY', 'git status')}; {call}"
            reading = hub_guard.Reading(dict(os.environ), text)
            hub_guard.read_powershell(text, None, reading, 0, {})
            with self.subTest(definition=definition, call=call, ran=ran):
                self.assertEqual([], reading.unread)
                if ran:
                    self.assertEqual(["status"], [call.subcommand for call in reading.calls])


class PowerShellDefinitionDifferentialTests(unittest.TestCase):
    """Each way PowerShell runs a name or text the parser never read as a command, run by pwsh with a marker for git,
    is refused by the guard; and a native program is handed its words whatever the binder makes of them."""

    def pwsh_output(self, script: str) -> str:
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if pwsh is None:
            raise AssertionError("pwsh was not found on PATH; pwsh checks the guard's reading of definitions")
        result = run_bounded([pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-c", script], 120, env=os.environ)
        return result.stdout.decode("utf-8", "replace")

    def test_each_definition_from_text_pwsh_runs_is_a_part_the_guard_cannot_read(self) -> None:
        # Each command, with PROGRAM standing for git and ARG for its arguments.
        forms = (
            "$alias:g = 'PROGRAM'; g ARG",
            "${alias:g} = 'PROGRAM'; g ARG",
            "${Alias::g} = 'PROGRAM'; g ARG",
            "Set-Content alias:g PROGRAM; g ARG",
            "Set-Item -Path Microsoft.PowerShell.Core\\Alias::g -Value PROGRAM; g ARG",
            "Set-Item -Path Alias::g -Value PROGRAM; g ARG",
            "New-PSDrive -Name a -PSProvider Alias -Root '' | Out-Null; Set-Item a:g PROGRAM; g ARG",
            "Set-Item function:f 'PROGRAM ARG'; f",
            "$function:f = 'PROGRAM ARG'; f",
            "${Function::f} = 'PROGRAM ARG'; f",
            "Set-Content function:f 'PROGRAM ARG'; f",
            "New-Item -Path function: -Name f -Value 'PROGRAM ARG' | Out-Null; f",
            "[scriptblock]::Create('PROGRAM ARG').Invoke()",
            "([scriptblock])::Create('PROGRAM ARG').Invoke()",
            "[System.Management.Automation.ScriptBlock]::Create('PROGRAM ARG').Invoke()",
            "[scriptblock]::Create.Invoke('PROGRAM ARG').Invoke()",
            "$ExecutionContext.InvokeCommand.NewScriptBlock('PROGRAM ARG').Invoke()",
            "$ExecutionContext.InvokeCommand.NewScriptBlock.Invoke('PROGRAM ARG').Invoke()",
            "$ExecutionContext.InvokeCommand.InvokeScript('PROGRAM ARG')",
            "[powershell]::Create().AddScript('PROGRAM ARG').Invoke()",
            "[powershell]::Create().AddCommand('PROGRAM').AddArgument('ARG').Invoke()",
            "$ExecutionContext.InvokeCommand.ExpandString('$(PROGRAM ARG)')",
            "$ExecutionContext.SessionState.InvokeProvider.Item.Set('function:g', 'PROGRAM ARG'); g",
            "$ExecutionContext.SessionState.InvokeProvider.Item.Set('alias:g', 'PROGRAM'); g ARG",
            "$ExecutionContext.SessionState.InvokeProvider.Item.New('function:', 'g', 'Function', 'PROGRAM ARG') |"
            " Out-Null; g",
            "[scriptblock]::('Create')('PROGRAM ARG').Invoke()",
            "$ExecutionContext.InvokeCommand.('NewScriptBlock')('PROGRAM ARG').Invoke()",
            "([type]'scriptblock')::Create('PROGRAM ARG').Invoke()",
            "[System.Management.Automation.Language.Parser]::ParseInput('PROGRAM ARG', [ref]$null, [ref]$null)"
            ".GetScriptBlock().Invoke()",
            "$Host.Runspace.CreateNestedPipeline('PROGRAM ARG', $false).Invoke()",
            "[scriptblock].GetMethod('Create').Invoke($null, @('PROGRAM ARG')).Invoke()",
        )
        for form in forms:
            output = self.pwsh_output(form.replace("PROGRAM", "Write-Output").replace("ARG", "MARKER-RAN"))
            text = form.replace("PROGRAM", "git").replace("ARG", "status")
            reading = hub_guard.Reading(dict(os.environ), text)
            hub_guard.read_powershell(text, None, reading, 0, {})
            with self.subTest(form=form):
                self.assertIn("MARKER-RAN", output)
                self.assertTrue(
                    reading.unread, "a definition or script block from text is a part the guard cannot read"
                )

    def test_a_path_each_cmdlet_takes_from_its_pipeline_is_one_the_guard_does_not_settle(self) -> None:
        # Each path parameter of each cmdlet that takes pipeline input, by value or by property name, as its metadata
        # in pwsh says, with the parts of the guard's reading it reaches: a location, a definition, and a variable.
        cmdlets = {
            "Set-Location": ("location",),
            "Push-Location": ("location",),
            "Set-Item": ("definition", "variable"),
            "New-Item": ("definition", "variable"),
            "Set-Content": ("definition", "variable"),
            "Remove-Item": ("removal",),
            "Clear-Item": ("removal",),
        }
        names = "', '".join(cmdlets)
        metadata = self.pwsh_output(
            f"foreach ($name in '{names}') {{ foreach ($parameter in 'Path', 'LiteralPath') {{"
            " $sets = (Get-Command $name).Parameters[$parameter].ParameterSets.Values;"
            " '{0} {1} {2} {3}' -f $name, $parameter, [bool]($sets | Where-Object ValueFromPipeline),"
            " [bool]($sets | Where-Object ValueFromPipelineByPropertyName) } }"
        )
        rows = []
        for line in metadata.splitlines():
            cmdlet, parameter, by_value, by_name = line.split()
            for piped, taken in ((by_value, "'TARGET'"), (by_name, f"[pscustomobject]@{{{parameter}='TARGET'}}")):
                if piped == "True":
                    rows += [(cmdlet, f"{taken} | {cmdlet}", part) for part in cmdlets[cmdlet]]
        self.assertEqual(set(cmdlets), {cmdlet for cmdlet, _, _ in rows})
        with tempfile.TemporaryDirectory() as temporary:
            start, moved = Path(temporary, "start dir"), Path(temporary, "moved dir")
            start.mkdir()
            moved.mkdir()
            for cmdlet, piped, part in rows:
                if part == "location":
                    ran = self.pwsh_output(f"{piped.replace('TARGET', str(moved))}; (Get-Location).Path")
                    # pwsh may name the directory by another spelling, as a long name for a short one.
                    lines = ran.strip().splitlines()
                    took = bool(lines) and Path(lines[-1].strip()).is_dir() and Path(lines[-1].strip()).samefile(moved)
                    text = f"{piped.replace('TARGET', str(moved))}; git status"
                elif part == "definition":
                    ran = self.pwsh_output(f"{piped.replace('TARGET', 'function:g')} -Value 'Write-Output TOOK';g")
                    took = "TOOK" in ran
                    text = f"{piped.replace('TARGET', 'function:g')} -Value 'git status'; g"
                elif part == "variable":
                    ran = self.pwsh_output(f"{piped.replace('TARGET', 'env:GIT_DIR')} -Value TOOK; $env:GIT_DIR")
                    took = "TOOK" in ran
                    text = f"$env:GIT_DIR = 'before'; {piped.replace('TARGET', 'env:GIT_DIR')} -Value x; git status"
                else:
                    removal = piped.replace("TARGET", "env:GIT_DIR")
                    ran = self.pwsh_output(f"$env:GIT_DIR = 'before'; {removal}; \"[$env:GIT_DIR]\"")
                    took = "[]" in ran
                    text = f"$env:GIT_DIR = 'before'; {removal}; git status"
                reading = hub_guard.Reading(dict(os.environ), text)
                hub_guard.read_powershell(text, start, reading, 0, {})
                with self.subTest(cmdlet=cmdlet, piped=piped, part=part):
                    self.assertTrue(took, "pwsh takes the path from the pipeline")
                    if part == "definition":
                        self.assertTrue(reading.unread)
                    elif part == "location":
                        self.assertIsNone(reading.calls[-1].place.start)
                    else:
                        self.assertIsNone(reading.calls[-1].place.repository)

    def test_a_native_program_runs_its_words_where_the_binder_refuses_them_or_takes_whatif(self) -> None:
        # Each argument list, which pwsh hands a native program as written, and the binder refuses or reads -WhatIf in.
        native = f"& '{sys.executable}' -c \"print('MARKER-RAN')\""
        for arguments in ("-WhatIf", "-c x -c y", "-wi"):
            output = self.pwsh_output(f"{native} {arguments}")
            text = f"git status {arguments}"
            reading = hub_guard.Reading(dict(os.environ), text)
            events = hub_guard.powershell_events(text, reading, 0) or []
            with self.subTest(arguments=arguments):
                self.assertIn("MARKER-RAN", output)
                [event] = [event for event in events if event["kind"] == "command"]
                self.assertEqual((False, False), (event["failed"], event["whatIf"]))


class StartDifferentialTests(unittest.TestCase):
    """The guard reads a nested shell as it starts: the operand Bash runs as its -c script, the directory pwsh's
    -WorkingDirectory starts it in, and the script Windows PowerShell runs from a leading word that names no
    parameter."""

    def test_the_guard_reads_the_operand_bash_runs_as_its_c_script(self) -> None:
        bash = platform_support.find_bash()
        if bash is None:
            raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the guard's reading of bash -c")
        forms = (
            ("-c", "echo first"),
            ("-c", "-e", "echo first"),
            ("-c", "--", "echo first"),
            ("-ec", "-o", "pipefail", "echo first"),
            ("-c", "-u", "--", "echo first", "echo second"),
            ("-lc", "echo first", "echo second"),
            # Each o or O in a bundle takes the next word, wherever it stands in the bundle and the bundle stands.
            ("-c", "-euo", "pipefail", "echo first"),
            ("-euo", "pipefail", "-c", "echo first"),
            ("-co", "pipefail", "echo first"),
            ("-oc", "pipefail", "echo first"),
            ("-ceo", "pipefail", "echo first"),
            ("-oo", "pipefail", "errexit", "-c", "echo first"),
            ("-Oc", "extglob", "echo first"),
            ("+Oc", "extglob", "echo first"),
            ("-c", "+eo", "pipefail", "echo first", "echo second"),
        )
        for words in forms:
            ran = platform_support.run_tool([bash, "--norc", "--noprofile", *words]).output
            script = hub_guard.bash_script(list(words), [], hub_guard.Shell(None, {}, set()))
            with self.subTest(words=words, ran=ran):
                self.assertIsNotNone(script)
                [text] = script or [None]
                self.assertIsNotNone(text)
                self.assertEqual(
                    ran, platform_support.run_tool([bash, "--norc", "--noprofile", "-c", str(text)]).output
                )

    def test_the_guard_starts_a_nested_pwsh_where_working_directory_names(self) -> None:
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if pwsh is None:
            raise AssertionError("pwsh was not found on PATH; pwsh checks the guard's reading of -WorkingDirectory")
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder).resolve()
            (start / "inner").mkdir()
            script = "Write-Output (Get-Location).ProviderPath"
            (start / "home" / "inner").mkdir(parents=True)
            home = start / "home"
            forms = (
                ("-c", script),
                ("-wd", "inner", "-c", script),
                ("-WorkingDirectory", str(start / "inner"), "-Command", script),
                ("-NoProfile", "-wo", "inner", "-c", script),
                # A leading ~ is home, by either separator, and the last -WorkingDirectory counts.
                ("-wd", "~", "-c", script),
                ("-wd", "~/inner", "-c", script),
                ("-wd", "~\\inner", "-c", script),
                ("-wd", str(start), "-wd", "inner", "-c", script),
                ("-wd", "inner", "-wd", str(start), "-c", script),
                ("-wd", "inner", "-WorkingDirectory", "~/inner", "-c", script),
            )
            # pwsh takes home from USERPROFILE on Windows and HOME elsewhere, as Path.home() does.
            environment = {**os.environ, "USERPROFILE": str(home), "HOME": str(home)}
            for words in forms:
                result = run_bounded([pwsh, "-NoLogo", "-NonInteractive", *words], 120, cwd=start, env=environment)
                ran = Path(result.stdout.decode("utf-8", "replace").strip())
                with self.subTest(words=words, ran=ran), mock.patch.dict(os.environ, environment):
                    self.assertEqual(ran.resolve(), hub_guard.powershell_directory(list(words), start))

    if platform_support.WINDOWS:

        def test_the_guard_reads_a_leading_word_as_windows_powershell_runs_it(self) -> None:
            powershell = platform_support.find_executable("powershell")
            if powershell is None:
                raise AssertionError("powershell was not found on PATH; it checks the guard's reading of its words")
            for words in ((INLINE,), ("-NoProfile", INLINE), ("-ExecutionPolicy", "Bypass", INLINE)):
                result = run_bounded([powershell, "-NoLogo", "-NonInteractive", *words], 120, env=os.environ)
                lines = {line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines()}
                arguments = hub_guard.powershell_arguments("powershell", list(words))
                with self.subTest(words=words):
                    self.assertIn("INLINE-RAN", lines)
                    self.assertEqual([INLINE], hub_guard.powershell_script(arguments))


class BashDefinitionDifferentialTests(unittest.TestCase):
    """Each way Bash defines a name a later word runs, an alias in BASH_ALIASES or a function a nested shell is handed,
    run by Bash with a marker for git, and read by the guard with git: refused as an alias, or followed into its body.
    """

    def test_the_guard_reads_each_definition_bash_runs_a_later_word_by(self) -> None:
        bash = platform_support.find_bash()
        if bash is None:
            raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the guard's reading of definitions")
        # Each command, with PROGRAM standing for git, and whether the guard refuses it as an alias it cannot read.
        forms = (
            ("shopt -s expand_aliases; BASH_ALIASES[g]=PROGRAM\ng status", True),
            ("shopt -s expand_aliases; declare -A BASH_ALIASES=([g]=PROGRAM)\ng status", True),
            ("shopt -s expand_aliases; BASH_ALIASES+=([g]=PROGRAM)\ng status", True),
            ("shopt -s expand_aliases; declare -n ref=BASH_ALIASES; ref[g]=PROGRAM\ng status", True),
            ("shopt -s expand_aliases; printf -v 'BASH_ALIASES[g]' PROGRAM\ng status", True),
            ("f() { PROGRAM status; }; export -f f; bash -c f", False),
            # sh is Bash under Git Bash and may be dash elsewhere, which imports no function, so only bash is run.
            ("f() { PROGRAM status; }; declare -fx f; bash -c f", False),
            ("f() { PROGRAM status; }; typeset -xf f; bash -euo pipefail -c f", False),
            ("f() { PROGRAM status; }; export -f f; env bash -c f", False),
            ("f() { PROGRAM status; }; export -f f; bash -c 'bash -c f'", False),
            ("env 'BASH_FUNC_f%%=() { PROGRAM status; }' bash -c f", False),
        )
        with tempfile.TemporaryDirectory() as folder:
            for form, alias in forms:
                path = Path(folder) / "form.sh"
                # The marker is a function, so a nested shell finds it only where it is exported, as git is on PATH.
                marker = "marker() { echo MARKER-RAN; }; export -f marker\n"
                path.write_text(marker + form.replace("PROGRAM", "marker") + "\n", encoding="utf-8", newline="\n")
                result = platform_support.run_tool([bash, "--norc", "--noprofile", platform_support.normalize(path)])
                text = form.replace("PROGRAM", "git")
                reading = hub_guard.Reading({}, text)
                hub_guard.read_bash(text, Path(folder), reading, 0, {})
                with self.subTest(form=form):
                    self.assertIn("MARKER-RAN", result.output)
                    if alias:
                        self.assertIn(f"an alias the command defines ({hub_guard.BASH_ALIASES})", reading.unread)
                    else:
                        self.assertEqual([], reading.unread)
                        self.assertEqual(["status"], [call.subcommand for call in reading.calls])


class BashDifferentialTests(unittest.TestCase):
    """After each builtin that may set GIT_DIR, the guard reads the value Bash gives it, or leaves it unsettled."""

    def test_the_guard_reads_each_builtin_as_bash_runs_it_or_leaves_its_variable_unsettled(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder).resolve()
            values = bash_values(BASH_COMMANDS, start)
            for command, value in zip(BASH_COMMANDS, values, strict=True):
                read = guard_value(command, start)
                with self.subTest(command=command, bash=value):
                    if read is not None:
                        self.assertEqual(UNSET if value == UNSET else start / value, read)
