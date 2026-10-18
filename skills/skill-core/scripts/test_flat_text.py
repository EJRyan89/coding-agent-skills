"""Regression tests for the one whitespace flattener the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flat_text import flat_text, single_line


class FlatTextTests(unittest.TestCase):
    def test_each_run_of_whitespace_becomes_one_space_and_the_ends_are_trimmed(self) -> None:
        self.assertEqual("a b c", flat_text(" \ta\r\n\n b\u2028\u00a0c \x0c"))

    def test_every_line_separator_splitlines_breaks_on_is_flattened(self) -> None:
        for separator in ("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"):
            with self.subTest(separator=repr(separator)):
                self.assertEqual(["a", "b"], f"a{separator}b".splitlines())
                self.assertEqual("a b", flat_text(f"a{separator}b"))

    def test_a_control_character_never_reaches_the_terminal(self) -> None:
        for control in ("\x00", "\x07", "\x08", "\x1b", "\x7f", "\x80", "\x9b", "\x9f"):
            with self.subTest(control=repr(control)):
                self.assertEqual("a b", flat_text(f"a{control}b"))
        self.assertEqual("x [31mred [0m", flat_text("x \x1b[31mred\x1b[0m"))

    def test_other_characters_are_kept(self) -> None:
        self.assertEqual('$x "q" `b` \u00e9 \u00a1 \ufffd', flat_text('$x "q" `b` \u00e9 \u00a1 \ufffd'))
        self.assertEqual("", flat_text(" \n\t\x1b "))


class SingleLineTests(unittest.TestCase):
    def test_spaces_and_other_whitespace_are_kept_and_only_the_end_is_trimmed(self) -> None:
        self.assertEqual("  Name:    value \u00a0x", single_line("  Name:    value \u00a0x  \t"))

    def test_each_run_of_line_breaks_and_controls_becomes_one_space(self) -> None:
        for unsafe in ("\n", "\r\n", "\x1b", "\x7f", "\x85", "\x9b", "\u2028", "\u2029", "\t\x00"):
            with self.subTest(unsafe=repr(unsafe)):
                self.assertEqual("a b", single_line(f"a{unsafe}b"))
        self.assertEqual("caf\ufffd  x", single_line("caf\ufffd  x\n"))

    def test_it_flattens_the_characters_flat_text_does(self) -> None:
        text = " a\x1b\u2028 b  \u0085c\x7f "
        self.assertEqual(flat_text(text), " ".join(single_line(text).split()))
