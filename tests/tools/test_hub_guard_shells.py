"""Regression suite holding tools/hub_guard.py's readings to the shells themselves: each spelling of pwsh's
inline-script and standard-input parameters the guard reads, run by pwsh, and each Bash builtin that sets a variable,
run by Bash, so that a release of either that reads one otherwise fails here.

It starts pwsh once for each spelling and Bash once in all; tests/tools/test_hub_guard_pwsh.py and
tests/tools/test_hub_guard_bash.py hold the same readings without starting either.
"""

from __future__ import annotations

import collections
import itertools
import os
import sys
import tempfile
import unittest
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest

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
# Each expansion that assigns GIT_DIR, as `<before>` leaves it, in each place a word is expanded: before (export
# GIT_DIR=before), unset and exported (so the assignment is exported), set empty, or through a reference.
ASSIGNING_EXPANSIONS = tuple(
    f"{before}{place.format(expansion=expansion.format(operator=operator))}"
    for before in ("", "unset GIT_DIR; export GIT_DIR; ", "GIT_DIR=; ")
    for operator in (":=", "=")
    for expansion in (
        "${{GIT_DIR{operator}after}}",
        "${{ref{operator}after}}",
        "${{!name{operator}after}}",
        "${{other:-${{GIT_DIR{operator}after}}}}",
    )
    for place in (
        "declare -n ref=GIT_DIR; name=GIT_DIR; : {expansion}",
        'declare -n ref=GIT_DIR; name=GIT_DIR; : "{expansion}"',
        "declare -n ref=GIT_DIR; name=GIT_DIR; x={expansion}",
        "declare -n ref=GIT_DIR; name=GIT_DIR; [[ -n {expansion} ]]",
        "declare -n ref=GIT_DIR; name=GIT_DIR; case {expansion} in *) ;; esac",
        "declare -n ref=GIT_DIR; name=GIT_DIR; for x in {expansion}; do :; done",
        "declare -n ref=GIT_DIR; name=GIT_DIR; : < /dev/null > /dev/null 2> /dev/null; echo > /dev/null {expansion}",
        "declare -n ref=GIT_DIR; name=GIT_DIR; (( ${{#GIT_DIR}} >= 0 )); : $(: {expansion})",
        # A subshell, and an arithmetic command and for's header, which the reader reads as the same.
        "declare -n ref=GIT_DIR; name=GIT_DIR; ( : {expansion} )",
        "declare -n ref=GIT_DIR; name=GIT_DIR; (( {expansion} )) || :",
        "declare -n ref=GIT_DIR; name=GIT_DIR; for (( i = {expansion}; i < 0; )); do :; done",
    )
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
    # A word the text does not settle, alone, may expand to nothing, and the shell goes on.
    "unset x; $x",
    # An expansion that assigns, in each operator, place, and spelling of its name, generated below.
    *ASSIGNING_EXPANSIONS,
)
UNSET = "<unset>"
# The part the guard cannot read of a SHELLOPTS a PowerShell session hands bash, which holds names folded where the
# platform folds environment names; None where it reads the name as Bash does.
FOLDED_SHELLOPTS = "SHELLOPTS" if platform_support.ENVIRONMENT_NAMES_FOLD_CASE else None
# Each spelling of unset's options: every bundle of -f, -n, and -v Bash reads, in one word or apart, and spellings it
# refuses or reads as names.
UNSET_OPTIONS = (
    "",
    "--",
    "-",
    "+f",
    "-x",
    "-fx",
    "--f",
    "other",
    "-- -f",
    *dict.fromkeys(
        spelling
        for count in (1, 2, 3)
        for letters in itertools.product("fnv", repeat=count)
        for spelling in ("-" + "".join(letters), " ".join(f"-{letter}" for letter in letters))
    ),
)
# Each state a name may be in before unset: a function alone, beside a variable, or a name reference to a variable
# that is set or is not.
UNSET_BEFORE = ("", "NAME=1; ", "declare -n NAME=other; ", "other=1; declare -n NAME=other; ")
# Each command before a bare unset after which a variable by the name may not be set on every path Bash takes.
UNSET_BEFORE_BARE = (
    "false && NAME=1; ",
    'if [ -n "$z" ]; then NAME=1; fi; ',
    "local NAME=1; ",
    "f() { local NAME=1; }; f; ",
    "f() { declare NAME=1; }; f; ",
    "for NAME in; do :; done; ",
    "read NAME < /dev/null; ",
    "( NAME=1 ); ",
    "NAME=1 true; ",
    "NAME=1; unset -v NAME; ",
)


def persistent_environment() -> dict[str, object]:
    """What outlives a process and reaches later ones on this machine: on Windows the user's and the machine's
    environment in the registry, and elsewhere the shell profiles in the home, which .NET leaves alone, since it keeps
    a user or machine target to the process there."""
    if sys.platform == "win32":
        import winreg

        found: dict[str, object] = {}
        for root, key in (
            (winreg.HKEY_CURRENT_USER, "Environment"),
            (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ):
            with winreg.OpenKey(root, key) as opened:
                for index in range(winreg.QueryInfoKey(opened)[1]):
                    name, value, _ = winreg.EnumValue(opened, index)
                    found[f"{key}\\{name}"] = value
        return found
    home = Path.home()
    profiles = (".profile", ".bashrc", ".bash_profile", ".config/powershell/Microsoft.PowerShell_profile.ps1")
    return {name: (home / name).read_bytes() for name in profiles if (home / name).is_file()}


@pytest.fixture(autouse=True)
def leaves_the_persistent_environment_alone() -> Iterator[None]:
    """Every test here runs pwsh or Bash on generated forms, so each is held to leaving what outlives it as it was:
    a form that sets a user or machine variable fails here, whatever spelling it takes."""
    before = persistent_environment()
    yield
    changed = persistent_changes(before, persistent_environment())
    assert not changed, f"a test changed the persistent environment: {changed}"


def persistent_changes(before: Mapping[str, object], after: Mapping[str, object]) -> list[str]:
    """Each entry of the persistent environment one snapshot holds that the other does not hold, or holds otherwise."""
    return sorted(
        name for name in before.keys() | after.keys() if before.get(name, _MISSING) != after.get(name, _MISSING)
    )


_MISSING = object()


class PersistentEnvironmentTests(unittest.TestCase):
    """The snapshot this suite takes around each test reads what outlives a process, and a change to it fails."""

    def test_an_added_removed_or_changed_entry_is_a_change(self) -> None:
        before = {"Environment\\PATH": "a", "Environment\\TEMP": "t", "Environment\\EMPTY": ""}
        for after, expected in (
            (dict(before), []),
            ({**before, "Environment\\GIT_WORK_TREE": "after"}, ["Environment\\GIT_WORK_TREE"]),
            ({**before, "Environment\\PATH": "b"}, ["Environment\\PATH"]),
            ({"Environment\\PATH": "a", "Environment\\TEMP": "t"}, ["Environment\\EMPTY"]),
        ):
            with self.subTest(after=after):
                self.assertEqual(expected, persistent_changes(before, after))

    def test_the_snapshot_reads_what_outlives_a_process_here(self) -> None:
        snapshot = persistent_environment()
        if sys.platform == "win32":
            # Every Windows profile holds a user TEMP and a machine Path.
            self.assertTrue(any(name.startswith("Environment\\") for name in snapshot))
            self.assertTrue(any(name.startswith("SYSTEM\\") for name in snapshot))


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


def find_bash() -> str:
    bash = platform_support.find_bash()
    if bash is None:
        raise AssertionError(f"{platform_support.BASH_MISSING} Bash checks the guard's reading of bash's options")
    return bash


def guard_value(command: str, start: Path) -> Path | str | None:
    """The repository GIT_DIR names for a git call after the command, as the guard reads it: None when the text
    does not settle it, and UNSET when no GIT_DIR reaches the call."""
    text = f"export GIT_DIR=before; {command}\ngit status"
    reading = hub_guard.Reading({}, text)
    hub_guard.read_bash(text, start, reading, 0, {})
    return guard_place(reading.calls[-1])


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

    def test_the_guard_reads_every_body_the_branches_give_a_function_that_runs(self) -> None:
        # Each command, with ONE and TWO standing for two bodies and Y for a condition that holds or does not.
        bash_forms = (
            'if test -n "Y"; then f() { ONE; }; else f() { TWO; }; fi; f',
            'if test -n "Y"; then f() { ONE; }; else f() { TWO; }; fi; export -f f; bash -c f',
            'f() { ONE; }; if test -n "Y"; then f() { TWO; }; fi; f',
            'if test -n "Y"; then f() { ONE; }; elif true; then f() { TWO; }; fi; f',
            'case "Y" in y) f() { ONE; } ;; *) f() { TWO; } ;; esac; f',
            'f() { ONE; }; test -n "Y" && f() { TWO; }; f',
        )
        powershell_forms = (
            "if ('Y') { function f { ONE } } else { function f { TWO } }; f",
            "function f { ONE }; if ('Y') { function f { TWO } }; f",
            "function f { ONE }; foreach ($i in @('Y') -ne '') { function f { TWO } }; f",
        )
        bash = platform_support.find_bash()
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if bash is None or pwsh is None:
            raise AssertionError("Bash and pwsh check the guard's reading of a function the branches define")
        for tool, forms in (("Bash", bash_forms), ("PowerShell", powershell_forms)):
            for form in forms:
                ran = set()
                for condition in ("y", ""):
                    script = form.replace("Y", condition).replace("ONE", "echo ONE-RAN").replace("TWO", "echo TWO-RAN")
                    shell = [bash, "--norc", "--noprofile", "-c"] if tool == "Bash" else [pwsh, "-NoProfile", "-c"]
                    result = run_bounded([*shell, script], 120, env=os.environ)
                    output = result.stdout.decode("utf-8", "replace")
                    ran |= {body for body in ("ONE", "TWO") if f"{body}-RAN" in output}
                text = form.replace("Y", "$y").replace("ONE", "git status").replace("TWO", "git log")
                reading = hub_guard.Reading(dict(os.environ), text)
                read = hub_guard.read_bash if tool == "Bash" else hub_guard.read_powershell
                read(text, None, reading, 0, {})
                read_calls = {call.subcommand for call in reading.calls}
                with self.subTest(tool=tool, form=form, ran=ran):
                    self.assertEqual({"ONE", "TWO"}, ran)
                    self.assertEqual({"status", "log"}, read_calls)


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

    def test_an_option_word_the_text_does_not_settle_makes_a_script_the_guard_cannot_read(self) -> None:
        # Each command line, with x unset, where Bash runs the -c script, and the guard cannot tell it does.
        forms = (
            "bash -c\"$x\" 'echo MARKER-RAN'",
            "bash $x -c 'echo MARKER-RAN'",
            "bash -e $x -c 'echo MARKER-RAN'",
            "bash $x -lc 'echo MARKER-RAN'",
        )
        with tempfile.TemporaryDirectory() as folder:
            for form in forms:
                path = Path(folder) / "form.sh"
                path.write_text(f"unset x; {form}\n", encoding="utf-8", newline="\n")
                result = platform_support.run_tool(
                    [find_bash(), "--norc", "--noprofile", platform_support.normalize(path)]
                )
                text = form.replace("echo MARKER-RAN", "git status")
                reading = hub_guard.Reading({}, text)
                hub_guard.read_bash(text, Path(folder), reading, 0, {})
                with self.subTest(form=form):
                    self.assertIn("MARKER-RAN", result.output)
                    self.assertIn("could not read the command passed to bash", reading.unread)

    def test_a_word_the_text_does_not_settle_may_vanish_and_leave_the_next_word_the_command(self) -> None:
        # Each command, with x unset, where Bash runs PROGRAM, and the guard reads the call as one that may run.
        forms = (
            "$x PROGRAM status",
            "$x $x PROGRAM status",
            "command $x PROGRAM status",
            "command -- $x PROGRAM status",
            "exec $x PROGRAM status",
            "builtin $x command PROGRAM status",
            "$x PROGRAM status; true",
            "$x bash <<< 'PROGRAM status'",
        )
        with tempfile.TemporaryDirectory() as folder:
            # command and exec pass over functions, so the marker is a program, as git is.
            program = Path(folder) / "marker"
            program.write_text("#!/bin/bash\necho MARKER-RAN\n", encoding="utf-8", newline="\n")
            program.chmod(0o755)
            for form in forms:
                path = Path(folder) / "form.sh"
                ran = form.replace("PROGRAM", platform_support.normalize(program))
                path.write_text(f"unset x; {ran}\n", encoding="utf-8", newline="\n")
                result = platform_support.run_tool(
                    [find_bash(), "--norc", "--noprofile", platform_support.normalize(path)]
                )
                text = form.replace("PROGRAM", "git")
                reading = hub_guard.Reading({}, text)
                hub_guard.read_bash(text, Path(folder), reading, 0, {})
                with self.subTest(form=form):
                    self.assertIn("MARKER-RAN", result.output)
                    self.assertEqual(["status"], [call.subcommand for call in reading.calls])

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
        # Each command, with PROGRAM standing for git and PATH for the program's path, and the part the guard cannot
        # read that it refuses, or None where it follows the definition into the body that runs.
        aliases = f"an alias the command defines ({hub_guard.BASH_ALIASES})"
        names = f"a command name the command defines ({hub_guard.BASH_CMDS})"
        exported = "f() { PROGRAM status; }"
        forms = (
            ("shopt -s expand_aliases; BASH_ALIASES[g]=PROGRAM\ng status", aliases),
            ("shopt -s expand_aliases; declare -A BASH_ALIASES=([g]=PROGRAM)\ng status", aliases),
            ("shopt -s expand_aliases; BASH_ALIASES+=([g]=PROGRAM)\ng status", aliases),
            ("shopt -s expand_aliases; declare -n ref=BASH_ALIASES; ref[g]=PROGRAM\ng status", aliases),
            ("shopt -s expand_aliases; printf -v 'BASH_ALIASES[g]' PROGRAM\ng status", aliases),
            ("shopt -s expand_aliases; : ${BASH_ALIASES[g]:=PROGRAM}\ng status", aliases),
            ("shopt -s expand_aliases; : ${BASH_ALIASES[g]=PROGRAM}\ng status", aliases),
            # hash keeps BASH_CMDS, whose elements name the program a command name runs.
            ("hash -p PATH g; g status", "a command name the command defines (hash -p)"),
            ("hash -lp PATH g; g status", "a command name the command defines (hash -p)"),
            ("BASH_CMDS[g]=PATH; g status", names),
            ("BASH_CMDS+=([g]=PATH); g status", names),
            ("declare -A BASH_CMDS=([g]=PATH); g status", names),
            ("printf -v 'BASH_CMDS[g]' %s PATH; g status", names),
            ("declare -n ref=BASH_CMDS; ref[g]=PATH; g status", names),
            (": ${BASH_CMDS[g]:=PATH}; g status", names),
            (f"{exported}; export -f f; bash -c f", None),
            # sh is Bash under Git Bash and may be dash elsewhere, which imports no function, so only bash is run.
            (f"{exported}; declare -fx f; bash -c f", None),
            (f"{exported}; typeset -xf f; bash -euo pipefail -c f", None),
            (f"{exported}; export -f f; env bash -c f", None),
            (f"{exported}; export -f f; bash -c 'bash -c f'", None),
            ("env 'BASH_FUNC_f%%=() { PROGRAM status; }' bash -c f", None),
            # -F names functions too, in each case and order, and an option the text does not settle may be -fx.
            (f"{exported}; declare -Fx f; bash -c f", None),
            (f"{exported}; declare -xF f; bash -c f", None),
            (f"{exported}; declare -F -x f; bash -c f", None),
            (f"{exported}; typeset -Fx f; bash -c f", None),
            (f"{exported}; typeset -xF f; bash -c f", None),
            (f"o=-fx; {exported}; declare $o f; bash -c f", None),
            # allexport exports a function as it is defined, in each spelling that turns it on.
            (f"set -a; {exported}; bash -c f", None),
            (f"set -o allexport; {exported}; bash -c f", None),
            (f"set -eo allexport; {exported}; bash -c f", None),
            (f"set -ae; {exported}; bash -c f", None),
            (f"shopt -so allexport; {exported}; bash -c f", None),
            (f"shopt -s -o allexport; {exported}; bash -c f", None),
            (f"bash -ac '{exported}; bash -c f'", None),
            (f"bash -o allexport -c '{exported}; bash -c f'", None),
            # A pwsh between the two shells hands the function on in its environment.
            (f"{exported}; export -f f; pwsh -NoProfile -c 'bash -c f'", None),
            (f"set -a; {exported}; pwsh -NoProfile -c 'bash -c f'", None),
            # Bash runs the handler for a program it does not find.
            ("command_not_found_handle() { PROGRAM status; }; no-such-program", None),
        )
        with tempfile.TemporaryDirectory() as folder:
            for form, unread in forms:
                path = Path(folder) / "form.sh"
                # The marker is a function, so a nested shell finds it only where it is exported, as git is on PATH.
                marker = "marker() { echo MARKER-RAN; }; export -f marker\n"
                # A program by PATH runs the marker as git would run: a script that calls it.
                program = Path(folder) / "program"
                program.write_text("#!/bin/bash\nmarker\n", encoding="utf-8", newline="\n")
                program.chmod(0o755)
                ran = form.replace("PROGRAM", "marker").replace("PATH", platform_support.normalize(program))
                path.write_text(marker + ran + "\n", encoding="utf-8", newline="\n")
                result = platform_support.run_tool([bash, "--norc", "--noprofile", platform_support.normalize(path)])
                text = form.replace("PROGRAM", "git").replace("PATH", "/usr/bin/git")
                reading = hub_guard.Reading(dict(os.environ), text)
                hub_guard.read_bash(text, Path(folder), reading, 0, {})
                with self.subTest(form=form):
                    self.assertIn("MARKER-RAN", result.output)
                    if unread is not None:
                        self.assertIn(unread, reading.unread)
                    else:
                        self.assertEqual([], reading.unread)
                        self.assertEqual(["status"], [call.subcommand for call in reading.calls])

    def test_the_guard_reads_a_function_powershell_hands_a_bash_it_starts(self) -> None:
        bash = platform_support.find_bash()
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if bash is None or pwsh is None:
            raise AssertionError("Bash and pwsh check the guard's reading of a function pwsh hands bash")
        # Each PowerShell command, with BODY standing for the function's body and STARTS for bash, and the part the
        # guard cannot read that it refuses, or None where it follows the function into its body.
        forms = (
            ("${env:BASH_FUNC_f%%} = '() { BODY; }'; STARTS -c f", None),
            ("${Env:BASH_FUNC_f%%} = '() { BODY; }'; STARTS -c f", None),
            ("Set-Item 'env:BASH_FUNC_f%%' '() { BODY; }'; STARTS -c f", "BASH_FUNC_"),
            ("[Environment]::SetEnvironmentVariable('BASH_FUNC_f%%', '() { BODY; }'); STARTS -c f", "BASH_FUNC_"),
            # A SHELLOPTS listing allexport exports the function the bash defines to the bash it starts; Bash reads it
            # by its exact name, which the session holds folded where the platform folds environment names.
            ("$env:SHELLOPTS = 'allexport'; STARTS -c 'f() { BODY; }; bash -c f'", FOLDED_SHELLOPTS),
            ("${env:SHELLOPTS} = 'allexport'; STARTS -c 'f() { BODY; }; bash -c f'", FOLDED_SHELLOPTS),
            ("Set-Item env:SHELLOPTS 'allexport'; STARTS -c 'f() { BODY; }; bash -c f'", "SHELLOPTS"),
        )
        for form, unread in forms:
            ran = form.replace("BODY", "echo MARKER-RAN").replace("STARTS", f"& '{bash}'")
            result = run_bounded([pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-c", ran], 120, env=os.environ)
            text = form.replace("BODY", "git status").replace("STARTS", "bash")
            reading = hub_guard.Reading(dict(os.environ), text)
            hub_guard.read_powershell(text, None, reading, 0, {})
            with self.subTest(form=form):
                self.assertIn("MARKER-RAN", result.stdout.decode("utf-8", "replace"))
                if unread is not None:
                    self.assertTrue(any(unread in part for part in reading.unread), reading.unread)
                else:
                    self.assertEqual([], reading.unread)
                    self.assertEqual(["status"], [call.subcommand for call in reading.calls])


class ShellOptionsDifferentialTests(unittest.TestCase):
    """A nested bash handed a SHELLOPTS that lists allexport exports the functions it defines and the variables it
    assigns; the guard reads both where Bash runs them, by each way the variable reaches it."""

    def test_the_guard_reads_allexport_where_the_shellopts_a_nested_bash_is_handed_turns_it_on(self) -> None:
        bash = find_bash()
        # Each start of a nested bash, with SCRIPT standing for its script.
        starts = (
            "env SHELLOPTS=allexport bash -c 'SCRIPT'",
            "env SHELLOPTS=errexit:allexport bash -c 'SCRIPT'",
            "env SHELLOPTS=allexport bash +a -c 'SCRIPT'",
            "env SHELLOPTS=allexport env bash -c 'SCRIPT'",
            "env SHELLOPTS=pipefail bash -c 'SCRIPT'",
            "env SHELLOPTS=ALLEXPORT bash -c 'SCRIPT'",
            "env shellopts=allexport bash -c 'SCRIPT'",
            "env SHELLOPTS=allexport env -u SHELLOPTS bash -c 'SCRIPT'",
            "SHELLOPTS=allexport bash -c 'SCRIPT'",
            "export SHELLOPTS; set -a; bash -c 'SCRIPT'",
            "declare -x SHELLOPTS; set -o allexport; bash -c 'SCRIPT'",
            "set -a; export SHELLOPTS; bash -c 'SCRIPT'",
            "export SHELLOPTS=x; set -a; bash -c 'SCRIPT'",
            "declare -x SHELLOPTS=x; set -a; bash -c 'SCRIPT'",
            "declare -n ref=SHELLOPTS; export ref; set -a; bash -c 'SCRIPT'",
            "set -a; bash -c 'SCRIPT'",
            "set -a; export SHELLOPTS; set +a; bash -c 'SCRIPT'",
            "export SHELLOPTS; set -a; export -n SHELLOPTS; bash -c 'SCRIPT'",
            "export SHELLOPTS; set -a; unset SHELLOPTS; bash -c 'SCRIPT'",
            "export SHELLOPTS; set -a; env -u SHELLOPTS bash -c 'SCRIPT'",
            "export SHELLOPTS; set -a; env -i PATH=/usr/bin:/bin bash -c 'SCRIPT'",
            "env SHELLOPTS=allexport bash -c 'set +a; SCRIPT'",
        )
        # Each script, with PROGRAM standing for git, and the subcommand the guard reads where Bash runs PROGRAM:
        # a function the nested bash exports to the next, and a variable it exports to the program.
        scripts = (("f() { PROGRAM status; }; bash -c f", "status"), ("GIT_DIR=after; PROGRAM log", "log"))
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder).resolve()
            program = start / "program"
            # The program prints the GIT_DIR it sees, as git would act on it.
            program.write_text('#!/bin/bash\necho "PROGRAM-RAN [$GIT_DIR]"\n', encoding="utf-8", newline="\n")
            program.chmod(0o755)
            forms = [(form, script, read) for form in starts for script, read in scripts]
            texts = [form.replace("SCRIPT", script) for form, script, _ in forms]
            # Each form runs in a subshell of its own, and a line `=== <index>` follows what it printed.
            body = "".join(
                f"( {text.replace('PROGRAM', platform_support.normalize(program))} ) 2>/dev/null; echo '=== {index}'\n"
                for index, text in enumerate(texts)
            )
            path = start / "forms.sh"
            path.write_text(body, encoding="utf-8", newline="\n")
            output = platform_support.run_tool([bash, "--norc", "--noprofile", platform_support.normalize(path)]).output
            printed: list[list[str]] = [[]]
            for line in output.splitlines():
                if line.startswith("=== "):
                    printed.append([])
                elif line.startswith("PROGRAM-RAN"):
                    printed[-1].append(line)
            self.assertEqual(len(texts) + 1, len(printed))
            for (form, script, subcommand), text, lines in zip(forms, texts, printed, strict=False):
                # A function that runs, or a GIT_DIR the program sees, shows allexport held in the nested bash.
                exported = bool(lines) if "f()" in script else "PROGRAM-RAN [after]" in lines
                guarded = text.replace("PROGRAM", "git")
                reading = hub_guard.Reading({}, guarded)
                hub_guard.read_bash(guarded, start, reading, 0, {})
                calls = [call for call in reading.calls if call.subcommand == subcommand]
                with self.subTest(form=form, script=script, exported=exported):
                    # An option no table names, as ALLEXPORT, which Bash passes over, may be one a later Bash reads.
                    unknown = "ALLEXPORT" in form
                    unread = [UNKNOWN_OPTION.format("ALLEXPORT", "on", ENVIRONMENT_SOURCE)] if unknown else []
                    self.assertEqual(unread, reading.unread)
                    if "f()" in script:
                        self.assertEqual(exported, bool(calls))
                    else:
                        [call] = calls
                        self.assertEqual(start / "after" if exported else UNSET, guard_place(call))


UNKNOWN_OPTION = "the option {0} is turned {1} by {2}, and the guard does not read it"
ENVIRONMENT_SOURCE = "the environment bash starts with"
# Each shell option the guard cannot read a shell in, by what shows Bash holds it, as a test that prints ON.
OPTION_PROBES = {
    "keyword": "[[ -o keyword ]] && echo ON",
    "posix": "[[ -o posix ]] && echo ON",
    # lastpipe holds only while job control is off.
    "lastpipe": "shopt -q lastpipe && [[ ! -o monitor ]] && echo ON",
    "cdable_vars": "shopt -q cdable_vars && echo ON",
    "physical": "[[ -o physical ]] && echo ON",
}
# Each spelling that turns an option on or leaves it off, with PROBE standing for what is run where it holds, the
# option, and the verdict: "holds" where Bash holds it and the guard refuses the shell, "refused" where the guard
# refuses a shell Bash need not hold it in, and "off" where it holds nowhere and the guard reads the shell.
OPTION_SPELLINGS: tuple[tuple[str, str, str], ...] = (
    *(
        (spelling, "keyword", "holds")
        for spelling in (
            "set -k; PROBE",
            "set -ek; PROBE",
            "set -o keyword; PROBE",
            "set -eo keyword; PROBE",
            "set -o errexit -o keyword; PROBE",
            "shopt -so keyword; PROBE",
            "shopt -s -o keyword; PROBE",
            "bash -k -c 'PROBE'",
            "bash -ek -c 'PROBE'",
            "bash -o keyword -c 'PROBE'",
            "bash -oc keyword 'PROBE'",
            "env SHELLOPTS=keyword bash -c 'PROBE'",
            "env SHELLOPTS=braceexpand:keyword bash -c 'PROBE'",
            "export SHELLOPTS; set -k; bash -c 'PROBE'",
        )
    ),
    *(
        (spelling, "posix", "holds")
        for spelling in (
            "set -o posix; PROBE",
            "set -eo posix; PROBE",
            "shopt -so posix; PROBE",
            "bash --posix -c 'PROBE'",
            "bash -o posix -c 'PROBE'",
            "exec -a sh bash -c 'PROBE'",
            "exec -a -sh bash -c 'PROBE'",
            "exec -la sh bash -c 'PROBE'",
            "env SHELLOPTS=posix bash -c 'PROBE'",
            "POSIXLY_CORRECT=1; PROBE",
            "POSIXLY_CORRECT=; PROBE",
            "POSIXLY_CORRECT=1 :; PROBE",
            "declare POSIXLY_CORRECT=1; PROBE",
            "read -r POSIXLY_CORRECT <<< 1; PROBE",
            "(( POSIXLY_CORRECT = 1 )); PROBE",
            "let POSIXLY_CORRECT=1; PROBE",
            "printf -v POSIXLY_CORRECT 1; PROBE",
            "export POSIXLY_CORRECT=1; bash -c 'PROBE'",
            "env POSIXLY_CORRECT= bash -c 'PROBE'",
            "POSIXLY_CORRECT=1 bash -c 'PROBE'",
            # A name the guard settles, and names it does not, each of which Bash assigns.
            'v=POSIXLY_; export "${v}CORRECT=1"; PROBE',
            'read -r v <<< POSIXLY_; export "${v}CORRECT=1"; PROBE',
            'read -r v <<< POSIXLY_; declare "${v}CORRECT=1"; PROBE',
            "read -r v <<< POSIXLY_; declare -n r=${v}CORRECT; r=1; PROBE",
            'read -r v <<< POSIXLY_; printf -v "${v}CORRECT" 1; PROBE',
        )
    ),
    *(
        (spelling, "lastpipe", "holds")
        for spelling in (
            "shopt -s lastpipe; PROBE",
            "shopt -s extglob lastpipe; PROBE",
            "shopt -s nosuch lastpipe; PROBE",
            "bash -O lastpipe -c 'PROBE'",
            "bash -O extglob -O lastpipe -c 'PROBE'",
            "env BASHOPTS=lastpipe bash -c 'PROBE'",
            "env BASHOPTS=extglob:lastpipe bash -c 'PROBE'",
            "export BASHOPTS; shopt -s lastpipe; bash -c 'PROBE'",
        )
    ),
    *(
        (spelling, "cdable_vars", "holds")
        for spelling in (
            "shopt -s cdable_vars; PROBE",
            "bash -O cdable_vars -c 'PROBE'",
            "env BASHOPTS=cdable_vars bash -c 'PROBE'",
        )
    ),
    *(
        (spelling, "physical", "holds")
        for spelling in ("set -P; PROBE", "set -o physical; PROBE", "bash -P -c 'PROBE'")
    ),
    # The guard refuses sh, which Bash runs in POSIX mode and a system may provide as another shell, such as dash,
    # which has no [[; and lastpipe turned on beside job control, which keeps it from holding.
    ("sh -c 'PROBE'", "posix", "refused"),
    ("set -m; shopt -s lastpipe; PROBE", "lastpipe", "refused"),
    # Spellings that leave each option off.
    *(
        (spelling, option, "off")
        for spelling, option in (
            ("set +k; PROBE", "keyword"),
            ("set +o keyword; PROBE", "keyword"),
            ("shopt -uo keyword; PROBE", "keyword"),
            ("bash +k -c 'PROBE'", "keyword"),
            ("env SHELLOPTS=braceexpand:hashall bash -c 'PROBE'", "keyword"),
            ("set -euo pipefail; PROBE", "keyword"),
            ("set +o posix; PROBE", "posix"),
            ("echo PS4 POSIXLY_CORRECT $BASH_COMPAT; unset POSIXLY_CORRECT; PROBE", "posix"),
            ("echo $(( POSIXLY_CORRECT == 1 )); PROBE", "posix"),
            ("bash -c 'PROBE'", "posix"),
            ("bash -lc 'PROBE'", "posix"),
            ("bash --norc --noprofile -c 'PROBE'", "posix"),
            ("exec -a bash bash -c 'PROBE'", "posix"),
            ("shopt -u lastpipe; PROBE", "lastpipe"),
            ("shopt lastpipe; shopt -p lastpipe; shopt -q lastpipe; PROBE", "lastpipe"),
            ("bash +O lastpipe -c 'PROBE'", "lastpipe"),
            ("env BASHOPTS=extglob bash -c 'PROBE'", "lastpipe"),
            ("shopt -u cdable_vars; PROBE", "cdable_vars"),
            ("set +P; PROBE", "physical"),
        )
    ),
)


class ShellOptionSpellingTests(unittest.TestCase):
    """Each spelling that turns on a shell option under which the guard cannot read a shell, run by Bash with a
    probe that shows the option holds, and read by the guard, which refuses each shell where Bash holds it."""

    def test_the_guard_refuses_each_shell_where_bash_holds_an_option_it_cannot_read(self) -> None:
        bash = find_bash()
        texts = [spelling.replace("PROBE", OPTION_PROBES[option]) for spelling, option, _ in OPTION_SPELLINGS]
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder).resolve()
            # Each spelling runs in a subshell of its own, and a line `=== <index>` follows what it printed.
            body = "".join(f"( {text} ) 2>/dev/null; echo '=== {index}'\n" for index, text in enumerate(texts))
            path = start / "spellings.sh"
            path.write_text(body, encoding="utf-8", newline="\n")
            environment = {name: value for name, value in os.environ.items() if name not in ("SHELLOPTS", "BASHOPTS")}
            output = platform_support.run_tool(
                [bash, "--norc", "--noprofile", platform_support.normalize(path)], environment
            ).output
            printed: list[set[str]] = [set()]
            for line in output.splitlines():
                if line.startswith("=== "):
                    printed.append(set())
                else:
                    printed[-1].add(line.strip())
            self.assertEqual(len(texts) + 1, len(printed))
            for (spelling, option, verdict), lines in zip(OPTION_SPELLINGS, printed, strict=False):
                text = spelling.replace("PROBE", "git status")
                reading = hub_guard.Reading({}, text)
                hub_guard.read_bash(text, start, reading, 0, {})
                with self.subTest(spelling=spelling, option=option, bash="ON" in lines):
                    # Bash holds the option where the row says so, and not where it says off; the guard refuses each
                    # shell but those that leave it off, and reads git in each.
                    if verdict != "refused":
                        self.assertEqual(verdict == "holds", "ON" in lines)
                    self.assertEqual(verdict != "off", bool(reading.unread), reading.unread)
                    self.assertEqual(["status"], [call.subcommand for call in reading.calls])

    def test_every_option_bash_lists_is_one_the_guard_reads(self) -> None:
        bash = find_bash()
        result = platform_support.run_tool([bash, "--norc", "--noprofile", "-c", "set -o; echo ===; shopt"])
        set_options, _, shopt_options = result.output.partition("===")
        self.assertEqual(
            set(), {line.split()[0] for line in set_options.splitlines() if line} - set(hub_guard.SET_OPTIONS)
        )
        self.assertEqual(
            set(),
            {line.split()[0] for line in shopt_options.splitlines() if line.strip()} - set(hub_guard.SHOPT_OPTIONS),
        )


def guard_place(call: hub_guard.GitCall) -> Path | str | None:
    """The repository GIT_DIR names for a git call, as the guard reads it, or UNSET when none reaches it."""
    return call.place.repository if call.place.repository_named else UNSET


class EnvironmentSetDifferentialTests(unittest.TestCase):
    """Each spelling of PSVariable.Set and SetEnvironmentVariable that pwsh runs and that sets the session's
    GIT_WORK_TREE leaves it unsettled in the guard's reading, or set to the value pwsh gives it."""

    def test_the_guard_reads_each_method_that_sets_a_variable_pwsh_sets(self) -> None:
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if pwsh is None:
            raise AssertionError("pwsh was not found on PATH; pwsh checks the guard's reading of PSVariable.Set")
        receivers = (
            "$ExecutionContext.SessionState.PSVariable",
            "$ExecutionContext.SessionState.'PSVariable'",
            '$ExecutionContext.SessionState."PSVariable"',
            "$ExecutionContext.SessionState.('PSVariable')",
            "$ExecutionContext.'SessionState'.PSVariable",
            "$global:ExecutionContext.SessionState.'PSVariable'",
            "($ExecutionContext.SessionState.PSVariable)",
            "([Management.Automation.PSVariableIntrinsics]$p)",
        )
        forms = (
            *(
                f"{receiver}{member}('env:GIT_WORK_TREE', 'after')"
                for receiver in receivers
                for member in (".Set", ".'Set'")
            ),
            "function f { [CmdletBinding()] param()"
            " $PSCmdlet.SessionState.'PSVariable'.Set('env:GIT_WORK_TREE', 'after') }; f",
            *(
                f"[Environment]::SetEnvironmentVariable('GIT_WORK_TREE', 'after', {target})"
                for target in ("'0'", "'process'", "0", "[EnvironmentVariableTarget]'0'")
            ),
            # Each setter reached another way than by calling it, by an [object] receiver, or by its name.
            "[Environment]::SetEnvironmentVariable.Invoke('GIT_WORK_TREE', 'after')",
            "$m = [Environment]::SetEnvironmentVariable; $m.Invoke('GIT_WORK_TREE', 'after')",
            "([object]$v.SessionState.PSVariable).Set('env:GIT_WORK_TREE', 'after')",
            "$v.SessionState.PSVariable.Set.Invoke('env:GIT_WORK_TREE', 'after')",
            "$p | ForEach-Object -MemberName Set -ArgumentList 'env:GIT_WORK_TREE', 'after'",
            "$p | % Set 'env:GIT_WORK_TREE' 'after'",
            "$p.psobject.Methods['Set'].Invoke('env:GIT_WORK_TREE', 'after')",
            "@($p).ForEach('Set', 'env:GIT_WORK_TREE', 'after')",
        )
        # Each form runs after GIT_WORK_TREE is removed, and a line `=== <index> [<value>]` follows it.
        header = "$p = $ExecutionContext.SessionState.PSVariable\n$v = Get-Variable ExecutionContext -ValueOnly\n"
        script = header + "".join(
            "Remove-Item env:GIT_WORK_TREE -ErrorAction SilentlyContinue\n"
            f"try {{ {form} }} catch {{ }}\n"
            f'Write-Output "=== {index} [$env:GIT_WORK_TREE]"\n'
            for index, form in enumerate(forms)
        )
        with tempfile.TemporaryDirectory() as folder:
            start = Path(folder).resolve()
            path = start / "forms.ps1"
            path.write_text(script, encoding="utf-8")
            result = run_bounded(
                [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(path)], 300, env=os.environ
            )
            values: dict[int, str] = {}
            for line in result.stdout.decode("utf-8", "replace").splitlines():
                if line.startswith("=== "):
                    number, _, value = line[4:].partition(" ")
                    values[int(number)] = value.strip("[]")
            self.assertEqual(len(forms), len(values), result.stdout)
            environment = {name: value for name, value in os.environ.items() if name.upper() != "GIT_WORK_TREE"}
            for index, form in enumerate(forms):
                text = f"{form}; git status"
                reading = hub_guard.Reading(environment, text)
                hub_guard.read_powershell(text, start, reading, 0, {})
                tree = reading.calls[-1].place.tree
                with self.subTest(form=form, pwsh=values[index], unread=reading.unread):
                    # Where pwsh set the variable, the guard refuses the form as a part it cannot read, or leaves the
                    # variable unsettled, or holds the value pwsh gave it.
                    if values[index] and not reading.unread:
                        self.assertIn(tree, (None, start / values[index]))
                    # Every form here sets the variable in pwsh; a release that stops doing so leaves the row moot.
                    self.assertEqual("after", values[index])


# Lists each reference to a node's Extent in a PowerShell script, by PowerShell's own parser: the function that holds
# it and the member read from it, or nothing when the extent itself is kept or handed on.
EXTENT_REFERENCES = r"""
param([string] $Path)
$tokens = $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Path, [ref] $tokens, [ref] $errors)
foreach ($node in $ast.FindAll({ param($n) $n -is [Management.Automation.Language.MemberExpressionAst] -and
            $n.Member -is [Management.Automation.Language.StringConstantExpressionAst] -and
            $n.Member.Value -eq 'Extent' }, $true)) {
    $function = $node.Parent
    while ($null -ne $function -and $function -isnot [Management.Automation.Language.FunctionDefinitionAst]) {
        $function = $function.Parent
    }
    $read = if ($node.Parent -is [Management.Automation.Language.MemberExpressionAst] -and
        [object]::ReferenceEquals($node.Parent.Expression, $node)) { $node.Parent.Member.Extent.Text } else { '' }
    "$(if ($function) { $function.Name })`t$read"
}
"""
# Each reference to a node's Extent the reader may hold, by function and the member read, with how many and why. None
# decides what a node does: that comes from the syntax tree and the resolver, so another spelling cannot change it.
SANCTIONED_EXTENTS = {
    # A switch parameter's word as written, and an expansion's token, which hub_guard.py replaces with its value.
    ("Get-ElementWord", "Text"): 2,
    # The member's text, named in the part the guard reports it cannot read.
    ("Add-MemberEvent", "Text"): 1,
    # Where a node stands, to place it in the region that runs it.
    ("Get-Region", "StartOffset"): 1,
    ("Get-Region", "EndOffset"): 1,
    ("Read-CommandEvent", "StartOffset"): 2,
    ("Read-CommandEvent", "EndOffset"): 2,
}


class ReaderSourceTests(unittest.TestCase):
    """The PowerShell reader decides what a node does by its syntax tree and the resolver, never by its source text,
    which another spelling of the same node does not match: every reference to a node's Extent, read by PowerShell's
    own parser, is one SANCTIONED_EXTENTS names."""

    def test_every_reference_to_a_nodes_extent_in_the_reader_is_sanctioned(self) -> None:
        pwsh = platform_support.find_pwsh(os.environ.get("PATH", ""))
        if pwsh is None:
            raise AssertionError("pwsh was not found on PATH; pwsh parses the PowerShell reader")
        with tempfile.TemporaryDirectory() as folder:
            script = Path(folder) / "extents.ps1"
            script.write_text(EXTENT_REFERENCES, encoding="utf-8")
            command = [pwsh, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)]
            result = run_bounded([*command, "-Path", str(hub_guard.POWERSHELL_READER)], 120, env=os.environ)
        found = collections.Counter(
            tuple(line.split("\t", 1)) for line in result.stdout.decode("utf-8", "replace").splitlines() if line
        )
        self.assertEqual(SANCTIONED_EXTENTS, dict(found))


class UnsetDifferentialTests(unittest.TestCase):
    """After each spelling of unset's options, the guard reads the function's body wherever Bash runs it, and the
    program by its name wherever Bash runs that."""

    def test_the_guard_reads_what_bash_runs_by_a_name_after_each_unset(self) -> None:
        bash = find_bash()
        # Each command, with NAME standing for the function's name and BODY for its body.
        forms = tuple(
            f"{before}NAME() {{ BODY; }}; unset {options} NAME; NAME status"
            for before in UNSET_BEFORE
            for options in UNSET_OPTIONS
        ) + tuple(f"{before}NAME() {{ BODY; }}; unset NAME; NAME status" for before in UNSET_BEFORE_BARE)
        with tempfile.TemporaryDirectory() as folder:
            program = Path(folder) / "g"
            program.write_text("#!/bin/bash\necho PROGRAM-RAN\n", encoding="utf-8", newline="\n")
            program.chmod(0o755)
            # Each form runs in a subshell of its own, and a line `=== <index>` follows what it printed.
            script = f'PATH="{platform_support.to_shell_path(str(folder))}:$PATH"\n' + "".join(
                f"( {form.replace('NAME', 'g').replace('BODY', 'echo BODY-RAN')} ) 2>/dev/null; echo '=== {index}'\n"
                for index, form in enumerate(forms)
            )
            path = Path(folder) / "forms.sh"
            path.write_text(script, encoding="utf-8", newline="\n")
            output = platform_support.run_tool([bash, "--norc", "--noprofile", platform_support.normalize(path)]).output
            ran: list[set[str]] = [set()]
            for line in output.splitlines():
                if line.startswith("=== "):
                    ran.append(set())
                elif line in ("BODY-RAN", "PROGRAM-RAN"):
                    ran[-1].add(line)
            for form, outputs in zip(forms, ran, strict=False):
                text = form.replace("NAME", "git").replace("BODY", "command git log")
                reading = hub_guard.Reading({}, text)
                hub_guard.read_bash(text, Path(folder), reading, 0, {})
                read = {call.subcommand for call in reading.calls}
                with self.subTest(form=form, ran=outputs):
                    self.assertEqual(len(forms) + 1, len(ran))
                    self.assertTrue(outputs)
                    self.assertEqual([], reading.unread)
                    if "BODY-RAN" in outputs:
                        self.assertIn("log", read)
                    if "PROGRAM-RAN" in outputs:
                        self.assertIn("status", read)


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
