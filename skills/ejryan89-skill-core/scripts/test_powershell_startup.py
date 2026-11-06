"""Regression tests for the reader of PowerShell's start-up parameters that audit-skill and validation share."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powershell_startup import StartupCommand, UnknownWord, startup_command, takes_powershell_value


@pytest.mark.parametrize(
    ("words", "expected"),
    [
        (["-Command", "jq ."], StartupCommand(1)),
        (["-c", "jq", "."], StartupCommand(1)),
        (["-NoProfile", "-com", "jq ."], StartupCommand(2)),
        (["-ExecutionPolicy", "Bypass", "-Command", "jq ."], StartupCommand(3)),
        (["-ep", "-Command", "-c", "jq ."], StartupCommand(3)),
        (["-cwa", "jq $args", "."], StartupCommand(1, with_arguments=True)),
        (["-CommandWithArgs", "jq $args"], StartupCommand(1, with_arguments=True)),
        (["-commandw", "jq $args"], StartupCommand(1, with_arguments=True)),
        (["-EncodedCommand", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-e", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-ec", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-en", "agBxAA=="], StartupCommand(1, encoded=True)),
        (["-Command"], StartupCommand(1)),
        (["-File", "a.ps1", "-Command", "jq ."], None),
        (["-f", "a.ps1"], None),
        (["-NoProfile"], None),
        ([], None),
    ],
)
def test_a_start_up_runs_the_command_its_parameters_name(words: list[str], expected: object) -> None:
    assert startup_command(words) == expected


def test_a_first_positional_word_is_the_command_the_file_or_passed_over_as_the_caller_asks() -> None:
    words = ["-NoProfile", "-wd", "C:/w", "jq .", "-Command", "rg x"]
    # Windows PowerShell runs it as -Command's; pwsh runs it as -File's; a prose reading passes over it.
    assert startup_command(words, positional_command=True) == StartupCommand(3)
    assert startup_command(words, positional_file=True) is None
    assert startup_command(words) == StartupCommand(5)


def test_a_word_the_caller_cannot_read_is_named_where_the_reading_needs_it() -> None:
    assert startup_command(["-NoProfile", None, "-Command", "jq ."]) == UnknownWord(1)
    # A value parameter takes the unknown word as its value, and a word past the command's start is never read here.
    assert startup_command(["-ExecutionPolicy", None, "-Command", "jq ."]) == StartupCommand(3)
    assert startup_command(["-Command", None]) == StartupCommand(1)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("executionpolicy", True),
        ("ex", True),
        ("ep", True),
        ("wd", True),
        ("v", True),
        ("noprofile", False),
        ("no", False),
        ("i", False),
        ("", False),
        ("zzz", False),
    ],
)
def test_a_parameter_takes_a_value_only_where_no_switch_shares_its_prefix(name: str, expected: bool) -> None:
    assert takes_powershell_value(name) is expected
