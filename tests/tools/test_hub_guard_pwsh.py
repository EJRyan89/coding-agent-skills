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

from deployer import platform_support
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
    doubled, white space around the word, and, where pwsh takes it, a leading slash."""

    def test_pwsh_inline_script_parameters_in_every_spelling(self) -> None:
        for slash in (True, False):
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
                ("/c", "command" if slash else None),
                ("/Command", "command" if slash else None),
                (" /c", "command" if slash else None),
                ("/cwa", "commandwithargs" if slash else None),
                ("/CommandWithArgs", "commandwithargs" if slash else None),
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
                (None, None),
            ):
                with (
                    self.subTest(word=word, slash=slash),
                    mock.patch.object(platform_support, "POWERSHELL_SLASH_PARAMETERS", slash),
                ):
                    self.assertEqual(expected, hub_guard.powershell_inline(word))

    def test_pwsh_script_is_read_after_every_spelling_of_its_parameter(self) -> None:
        for slash in (True, False):
            for arguments, expected in (
                (["--command", "Get-Date", "-Format", "o"], ["Get-Date", "-Format", "o"]),
                (["\u2014c", "Get-Date"], ["Get-Date"]),
                (["-NoProfile", "--cwa", "Get-Date", "one"], ["Get-Date"]),
                (["/Command", "Get-Date"], ["Get-Date"] if slash else None),
                (["/cwa", "Get-Date", "one"], ["Get-Date"] if slash else None),
                (["---c", "Get-Date"], None),
                ([None, "-c", "Get-Date"], ["Get-Date"]),
                (["build.ps1"], None),
            ):
                with (
                    self.subTest(arguments=arguments, slash=slash),
                    mock.patch.object(platform_support, "POWERSHELL_SLASH_PARAMETERS", slash),
                ):
                    self.assertEqual(expected, hub_guard.powershell_script(list(arguments)))

    def test_pwsh_reads_its_input_by_every_spelling_and_after_a_lone_dash(self) -> None:
        for slash in (True, False):
            for arguments, expected in (
                (["--command", "-"], True),
                (["--command", "Get-Date"], False),
                (["\u2013\u2013file", "-"], True),
                (["--file", "build.ps1"], False),
                (["--ec"], False),
                (["\u2014cwa", "Get-Date"], False),
                (["--ex", "Bypass"], True),
                (["\u2013wd", "C:/", "-c", "-"], True),
                ([" -c ", "-"], True),
                # A lone dash reads standard input whatever follows it.
                (["-"], True),
                (["-", "-c", "Get-Date"], True),
                (["-NoProfile", "-", "build.ps1"], True),
                (["--"], True),
                # An empty word is a script file's name, which pwsh cannot find.
                (["", "-c", "-"], False),
                (["/c", "-"], slash),
                (["/f", "-"], slash),
                (["/ec"], False),
                (["/NoProfile"], slash),
                (["/ex", "Bypass", "-c", "-"], slash),
            ):
                with (
                    self.subTest(arguments=arguments, slash=slash),
                    mock.patch.object(platform_support, "POWERSHELL_SLASH_PARAMETERS", slash),
                ):
                    self.assertEqual(expected, hub_guard.powershell_reads_input(list(arguments)))

    def test_pwsh_parameters_that_take_a_value_in_every_spelling(self) -> None:
        for slash in (True, False):
            for word, expected in (
                ("--ExecutionPolicy", True),
                ("\u2013ep", True),
                ("\u2015\u2015wd", True),
                (" -o ", True),
                ("/ex", slash),
                ("/WorkingDirectory", slash),
                ("---ex", False),
                ("ex", False),
            ):
                with (
                    self.subTest(word=word, slash=slash),
                    mock.patch.object(platform_support, "POWERSHELL_SLASH_PARAMETERS", slash),
                ):
                    self.assertEqual(expected, hub_guard.powershell_takes_value(word))
