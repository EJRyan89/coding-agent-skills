"""Regression tests for the one whitespace flattener the skills share."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flat_text import flat_text


class FlatTextTests(unittest.TestCase):
    def test_each_run_of_whitespace_becomes_one_space_and_the_ends_are_trimmed(self) -> None:
        self.assertEqual("a b c", flat_text(" \ta\r\n\n b\u2028\u00a0c \x0c"))

    def test_other_characters_are_kept(self) -> None:
        self.assertEqual('$x "q" `b` \x1b', flat_text('$x "q" `b` \x1b'))
        self.assertEqual("", flat_text(" \n\t "))


if __name__ == "__main__":
    unittest.main()
