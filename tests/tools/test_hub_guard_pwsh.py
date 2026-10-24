"""Regression suite for tools/hub_guard.py's reading of the pwsh command line: which forms read a script from standard
input, and which parameters take the next word as their value.

It starts no program and holds no fixture, so it runs in under a second, apart from tests/tools/test_hub_guard.py,
whose suites start git and pwsh.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

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
