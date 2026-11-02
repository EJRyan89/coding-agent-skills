"""Regression tests for the one whitespace flattener the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flat_text import fact_line, flat_text, single_line


def code_points(text: str) -> str:
    """A test ID naming each character of `text` by its code point, since most are invisible."""
    return "+".join(f"U+{ord(character):04X}" for character in text)


class FlatTextTests(unittest.TestCase):
    def test_each_run_of_whitespace_becomes_one_space_and_the_ends_are_trimmed(self) -> None:
        self.assertEqual("a b c", flat_text(" \ta\r\n\n b\u2028\u00a0c \x0c"))

    def test_other_characters_are_kept(self) -> None:
        self.assertEqual('$x "q" `b` \u00e9 \u00a1 \ufffd', flat_text('$x "q" `b` \u00e9 \u00a1 \ufffd'))
        self.assertEqual("", flat_text(" \n\t\x1b "))


class SingleLineTests(unittest.TestCase):
    def test_spaces_and_other_whitespace_are_kept_and_only_the_end_is_trimmed(self) -> None:
        self.assertEqual("  Name:    value \u00a0x", single_line("  Name:    value \u00a0x  \t"))

    def test_it_flattens_the_characters_flat_text_does(self) -> None:
        text = " a\x1b\u2028 b  \u0085c\x7f "
        self.assertEqual(flat_text(text), " ".join(single_line(text).split()))


class FactLineTests(unittest.TestCase):
    def test_each_field_is_flattened_and_the_key_and_fields_are_joined_by_tabs(self) -> None:
        self.assertEqual("FILE\tsrc/a b.cs\t3\tNone", fact_line("FILE", " src/a\tb.cs\u2028", 3, None))
        self.assertEqual("SUMMARY", fact_line("SUMMARY"))

    def test_a_field_holding_any_line_or_field_break_stays_one_field_on_one_line(self) -> None:
        breaks = [chr(code) for code in range(0x3000) if len(f"a{chr(code)}b".splitlines()) == 2]
        for character in [*breaks, "\t"]:
            with self.subTest(character=code_points(character)):
                line = fact_line("FILE", f"x{character}STOP", f"y{character}z")
                self.assertEqual([line], line.splitlines())
                self.assertEqual(["FILE", "x STOP", "y z"], line.split("\t"))


@pytest.mark.parametrize(
    "separator", ["\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"], ids=code_points
)
def test_every_line_separator_splitlines_breaks_on_is_flattened(separator: str) -> None:
    assert f"a{separator}b".splitlines() == ["a", "b"]
    assert flat_text(f"a{separator}b") == "a b"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        *(
            pytest.param(f"a{control}b", "a b", id=code_points(control))
            for control in ("\x00", "\x07", "\x08", "\x1b", "\x7f", "\x80", "\x9b", "\x9f")
        ),
        pytest.param("x \x1b[31mred\x1b[0m", "x [31mred [0m", id="a-colour-sequence"),
    ],
)
def test_a_control_character_never_reaches_the_terminal(text: str, expected: str) -> None:
    assert expected == flat_text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        *(
            pytest.param(f"a{unsafe}b", "a b", id=code_points(unsafe))
            for unsafe in ("\n", "\r\n", "\x1b", "\x7f", "\x85", "\x9b", "\u2028", "\u2029", "\t\x00")
        ),
        pytest.param("caf\ufffd  x\n", "caf\ufffd  x", id="inner-spaces-and-a-final-newline"),
    ],
)
def test_each_run_of_line_breaks_and_controls_becomes_one_space(text: str, expected: str) -> None:
    assert expected == single_line(text)
