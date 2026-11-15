"""Regression tests for the one splitter of a file an author writes into its lines."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from text_lines import text_lines

# Every character str.splitlines breaks at besides CR and LF, written out rather than derived from it.
KEPT = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


class TextLinesTests(unittest.TestCase):
    def test_it_breaks_at_crlf_cr_and_lf_alone(self) -> None:
        self.assertEqual(["a", "b", "c", "d"], text_lines("a\r\nb\rc\nd"))

    def test_a_last_break_ends_the_last_line_and_an_empty_text_has_none(self) -> None:
        self.assertEqual(["a", ""], text_lines("a\n\n"))
        self.assertEqual(["a"], text_lines("a\r\n"))
        self.assertEqual([], text_lines(""))
        self.assertEqual([""], text_lines("\n"))

    def test_a_lone_cr_before_lf_is_one_break(self) -> None:
        self.assertEqual(["a", "", "b"], text_lines("a\r\r\nb"))


@pytest.mark.parametrize("character", KEPT, ids=[f"U+{ord(character):04X}" for character in KEPT])
def test_every_other_character_splitlines_breaks_at_is_kept_in_its_line(character: str) -> None:
    text = f"description: a{character}disable-model-invocation: true\nname: x"
    assert len(text.splitlines()) == 3  # the premise: str.splitlines breaks here
    assert text_lines(text) == [f"description: a{character}disable-model-invocation: true", "name: x"]
