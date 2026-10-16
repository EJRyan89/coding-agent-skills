"""Regression tests for the one whitespace flattener the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flat_text import flat_text


class FlatTextTests(unittest.TestCase):
    def test_each_run_of_whitespace_becomes_one_space_and_the_ends_are_trimmed(self) -> None:
        self.assertEqual("a b c", flat_text(" \ta\r\n\n b  c \x0c"))

    def test_every_line_separator_splitlines_breaks_on_is_flattened(self) -> None:
        for separator in ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(["a", "b"], f"a{separator}b".splitlines())
                self.assertEqual("a b", flat_text(f"a{separator}b"))

    def test_a_control_character_never_reaches_the_terminal(self) -> None:
        for control in ("\x00", "\x07", "\x08", "\x1b", "\x7f", "\x80", "\x9b", "\x9f"):
            with self.subTest(control=repr(control)):
                self.assertEqual("a b", flat_text(f"a{control}b"))
        self.assertEqual("x [31mred [0m", flat_text("x \x1b[31mred\x1b[0m"))

    def test_other_characters_are_kept(self) -> None:
        self.assertEqual('$x "q" `b` é ¡ �', flat_text('$x "q" `b` é ¡ �'))
        self.assertEqual("", flat_text(" \n\t\x1b "))


if __name__ == "__main__":
    unittest.main()
