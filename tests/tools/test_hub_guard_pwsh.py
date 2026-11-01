"""Regression suite for tools/hub_guard.py's reading of the pwsh command line: which forms read a script from standard
input or inline, how pwsh spells a parameter, and which parameters take the next word as their value.

It starts no program and holds no fixture, so it runs in under a second, apart from tests/tools/test_hub_guard.py,
whose suites start git and pwsh; tests/tools/test_hub_guard_shells.py holds these readings to what pwsh does.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from tools import hub_guard


class StandardInputTests(unittest.TestCase):
    """The pwsh forms that read a script from standard input, judged without starting pwsh."""

    def test_pwsh_reads_its_input_only_without_an_inline_command_a_file_or_an_encoded_command(self) -> None:
        for arguments, expected in (
            ([], True),
            (["-NoProfile", "-NonInteractive"], True),
            (["-Command", "-"], True),
            (["-c", "Get-Date"], False),
            (["-File", "-"], True),
            (["-fi", "-"], True),
            # A file named like a switch is still a file, under any spelling of -File.
            (["-File", "-NoProfile"], False),
            (["-FILE", "-NoProfile"], False),
            (["-f", "-NoProfile"], False),
            (["-cwa", "Get-Date"], False),
            (["-EncodedCommand"], False),
            (["-ENCODEDCOMMAND"], False),
            (["-ec"], False),
            (["-EC"], False),
            (["-e"], False),
            (["-enc"], False),
            (["build.ps1"], False),
            # A switch that takes no value is one word, and the word after it is read on its own.
            (["-NoProfile", "build.ps1"], False),
            (["-NoProfile", "-Command", "-"], True),
            (["-NoProfile", "-File", "-"], True),
            (["-ExecutionPolicy", "Bypass"], True),
            (["-ex", "Bypass", "build.ps1"], False),
            ([None], True),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, hub_guard.powershell_reads_input(list(arguments)))

    def test_pwsh_parameters_that_take_a_value_by_name_shortest_form_and_alias(self) -> None:
        for word, expected in (
            ("-ConfigurationFile", True),
            ("-ConfigurationName", True),
            ("-config", True),
            ("-conf", False),
            ("-CustomPipeName", True),
            ("-custom", False),
            ("-ExecutionPolicy", True),
            ("-ex", True),
            ("-ep", True),
            ("-InputFormat", True),
            ("-inp", True),
            ("-if", True),
            ("-in", False),
            ("-OutputFormat", True),
            ("-o", True),
            ("-of", True),
            ("-SettingsFile", True),
            ("-settings", True),
            ("-set", False),
            ("-WindowStyle", True),
            ("-w", True),
            ("-WorkingDirectory", True),
            ("-wd", True),
            ("-NoProfile", False),
        ):
            with self.subTest(word=word):
                self.assertEqual(expected, hub_guard.powershell_takes_value(word))


class SpellingTests(unittest.TestCase):
    """Every spelling pwsh takes for a parameter: a leading hyphen, en dash, em dash, or horizontal bar, that dash
    doubled, white space around the word, and a leading slash, on every platform."""

    def test_pwsh_inline_script_parameters_in_every_spelling(self) -> None:
        for word, expected in (
            ("-c", "command"),
            ("-Command", "command"),
            ("-COMMAND", "command"),
            ("-co", "command"),
            ("--c", "command"),
            ("--command", "command"),
            ("--Command", "command"),
            ("\u2013c", "command"),
            ("\u2014command", "command"),
            ("\u2015co", "command"),
            ("\u2013\u2013c", "command"),
            ("\u2014\u2014command", "command"),
            (" -c ", "command"),
            ("\t--command\n", "command"),
            ("\u3000-c", "command"),
            ("-cwa", "commandwithargs"),
            ("-commandw", "commandwithargs"),
            ("--cwa", "commandwithargs"),
            ("--CommandWithArgs", "commandwithargs"),
            ("\u2013cwa", "commandwithargs"),
            ("/c", "command"),
            ("/Command", "command"),
            (" /c", "command"),
            ("/cwa", "commandwithargs"),
            ("/CommandWithArgs", "commandwithargs"),
            ("---c", None),
            ("-\u2013c", None),
            ("\u2013-c", None),
            ("//c", None),
            ("/-c", None),
            ("-/c", None),
            ("-commandx", None),
            ("-cw", None),
            ("c", None),
            ("", None),
            ("-", None),
            ("--", None),
            ("/", None),
            ("Xc", None),
            (None, None),
        ):
            with self.subTest(word=word):
                self.assertEqual(expected, hub_guard.powershell_inline(word))

    def test_pwsh_script_is_read_after_every_spelling_of_its_parameter(self) -> None:
        for arguments, expected in (
            (["--command", "Get-Date", "-Format", "o"], ["Get-Date", "-Format", "o"]),
            (["\u2014c", "Get-Date"], ["Get-Date"]),
            (["-NoProfile", "--cwa", "Get-Date", "one"], ["Get-Date"]),
            (["/Command", "Get-Date"], ["Get-Date"]),
            (["/cwa", "Get-Date", "one"], ["Get-Date"]),
            (["---c", "Get-Date"], None),
            ([None, "-c", "Get-Date"], ["Get-Date"]),
            (["build.ps1"], None),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, hub_guard.powershell_script(list(arguments)))

    def test_pwsh_reads_its_input_by_every_spelling_and_after_a_lone_dash(self) -> None:
        for arguments, expected in (
            (["--command", "-"], True),
            (["--command", "Get-Date"], False),
            # The inline script is the words after -Command, and the one after -CommandWithArgs, whatever they say.
            (["-c", "-NoProfile"], False),
            (["-cwa", "-"], False),
            (["Xc"], False),
            (["\u2013\u2013file", "-"], True),
            (["--file", "build.ps1"], False),
            (["--ec"], False),
            (["\u2014cwa", "Get-Date"], False),
            (["--ex", "Bypass"], True),
            (["\u2013wd", "C:/", "-c", "-"], True),
            ([" -c ", "-"], True),
            (["/c", "-"], True),
            (["/f", "-"], True),
            (["/ec"], False),
            (["/NoProfile"], True),
            (["/ex", "Bypass", "-c", "Get-Date"], False),
            # A lone dash reads standard input whatever follows it.
            (["-"], True),
            (["-", "-c", "Get-Date"], True),
            (["-NoProfile", "-", "build.ps1"], True),
            (["--"], True),
            # An empty word is a script file's name, which pwsh cannot find.
            (["", "-c", "-"], False),
            # A word after a slash that names no parameter, such as an absolute path, is not settled.
            (["/scripts/build.ps1"], True),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, hub_guard.powershell_reads_input(list(arguments)))

    def test_pwsh_parameters_that_take_a_value_in_every_spelling(self) -> None:
        for word, expected in (
            ("--ExecutionPolicy", True),
            ("\u2013ep", True),
            ("\u2015\u2015wd", True),
            (" -o ", True),
            ("/ex", True),
            ("/WorkingDirectory", True),
            ("---ex", False),
            ("ex", False),
        ):
            with self.subTest(word=word):
                self.assertEqual(expected, hub_guard.powershell_takes_value(word))


class WorkingDirectoryTests(unittest.TestCase):
    """pwsh starts in the last -WorkingDirectory it is given, a leading ~ read as home, as Set-Location reads it."""

    def test_the_last_working_directory_counts_and_a_leading_tilde_is_home(self) -> None:
        start, home = Path("start").absolute(), Path("home").absolute()
        with mock.patch.object(Path, "home", return_value=home):
            for arguments, expected in (
                (["-c", "x"], start),
                (["-wd", "inner", "-c", "x"], start / "inner"),
                (["-wd", "~", "-c", "x"], home),
                (["-wd", "~/hub", "-c", "x"], home / "hub"),
                (["-wd", "~\\hub", "-c", "x"], home / "hub"),
                (["-wd", "~hub", "-c", "x"], start / "~hub"),
                (["-wd", "one", "-wd", "two", "-c", "x"], start / "two"),
                (["-wd", "~/hub", "-WorkingDirectory", "two", "-c", "x"], start / "two"),
                (["-wd", "two", "-wd", "~/hub", "-c", "x"], home / "hub"),
                (["-wd", "one", None, "-wd", "two", "-c", "x"], None),
                (["-wd", "one", "-c", "x", "-wd", "two"], start / "one"),
                (["-wd", "one", "-wd"], None),
            ):
                with self.subTest(arguments=arguments):
                    self.assertEqual(expected, hub_guard.powershell_directory(list(arguments), start))


class DefinitionTests(unittest.TestCase):
    """The names a PowerShell command defines for a later word: aliases and functions, at a path on their drive or
    provider, and functions under a scope."""

    def test_a_path_on_the_alias_or_function_drive_or_provider_is_known_by_each_spelling(self) -> None:
        for path, expected in (
            ("alias:g", "alias"),
            ("Alias:g", "alias"),
            ("alias:", "alias"),
            ("Alias::g", "alias"),
            ("\\Alias::g", "alias"),
            ("Microsoft.PowerShell.Core\\Alias::g", "alias"),
            ("function:f", "function"),
            ("Function::f", "function"),
            ("microsoft.powershell.core\\function::f", "function"),
            ("function:global:f", "function"),
            ("env:GIT_DIR", None),
            ("variable:x", None),
            ("aliases:g", None),
            ("C:\\alias:g", None),
            ("Other\\Alias::g", None),
        ):
            with self.subTest(path=path):
                self.assertEqual(expected, hub_guard.definition_drive(path))

    def test_each_command_that_writes_a_definition_drive_is_known_by_each_of_its_names(self) -> None:
        writers: tuple[str, ...] = ("set-item", "si", "new-item", "ni", "copy-item", "copy", "cp", "cpi")
        writers += (
            "rename-item",
            "ren",
            "rni",
            "set-content",
            "sc",
            "add-content",
            "ac",
            "move-item",
            "mi",
            "move",
            "mv",
        )
        for name in writers:
            for drive, path in (("alias", "Alias::g"), ("function", "function:f")):
                with self.subTest(name=name, drive=drive):
                    self.assertTrue(hub_guard.writes_definition_drive(name, [path, "git"], drive))
                    self.assertFalse(hub_guard.writes_definition_drive(name, [path, "git"], "other"))
                    self.assertFalse(hub_guard.writes_definition_drive(name, ["a.txt", None], drive))
                    # A path the cmdlet takes from its pipeline may be on either drive; another piped parameter names
                    # no item it writes.
                    for piped in ({"path"}, {"literalpath"}, {"destination"}, {"value", "path"}):
                        self.assertTrue(hub_guard.writes_definition_drive(name, ["-Value", "x"], drive, piped))
                    self.assertFalse(hub_guard.writes_definition_drive(name, ["-Value", "x"], drive, {"value"}))
        self.assertFalse(hub_guard.writes_definition_drive("get-item", ["function:f"], "function"))
        self.assertFalse(hub_guard.writes_definition_drive("get-item", [], "function", {"path"}))
        self.assertTrue(hub_guard.defines_powershell_alias("si", [], {"path"}))
        self.assertTrue(hub_guard.defines_powershell_alias("sc", ["alias:g", "git"]))
        self.assertFalse(hub_guard.defines_powershell_alias("sc", ["function:g", "git"]))

    def test_a_drive_on_the_alias_or_function_provider_is_known_unless_another_provider_is_named(self) -> None:
        cases: tuple[tuple[list[hub_guard.Word] | None, bool], ...] = (
            (["Alias"], True),
            (["function"], True),
            (["Microsoft.PowerShell.Core\\Alias"], True),
            ([None], True),
            (None, True),
            (["FileSystem"], False),
            (["Environment"], False),
            (["Aliases"], False),
        )
        for providers, expected in cases:
            with self.subTest(providers=providers):
                self.assertEqual(expected, hub_guard.definition_provider(providers))

    def test_the_reader_pwsh_is_handed_fits_a_windows_command_line_with_every_line_that_runs(self) -> None:
        source = hub_guard.POWERSHELL_READER.read_text(encoding="utf-8")
        handed = hub_guard.powershell_reader()
        # A here-string could hold a line that begins with #, which the comment lines' removal would take out.
        self.assertNotIn("@'", source)
        self.assertNotIn('@"', source)
        code = [line for line in source.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(code, [line for line in handed.splitlines() if line.strip()])
        # Windows starts no command line longer than 32767 characters; the rest of pwsh's is far under 2048.
        self.assertLess(len(handed), 32767 - 2048)

    def test_a_function_is_named_without_its_scope(self) -> None:
        for name, expected in (
            ("f", "f"),
            ("F", "f"),
            ("global:f", "f"),
            ("Script:F", "f"),
            ("local:f", "f"),
            ("private:f", "f"),
            ("using:f", "using:f"),
            ("global:global:f", "global:f"),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, hub_guard.function_name(name))
