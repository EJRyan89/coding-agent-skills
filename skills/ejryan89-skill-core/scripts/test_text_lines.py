"""Regression tests for the one splitter of a file an author writes into its lines, and of a program's output."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from text_lines import LINE_FEED, line_index, line_starts, lines_and_breaks, output_lines, program_lines, text_lines

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

    def test_keepends_keeps_each_break_with_its_line(self) -> None:
        self.assertEqual(["a\r\n", "b\r", "c\n", "d"], text_lines("a\r\nb\rc\nd", keepends=True))
        self.assertEqual(["a\r", "\r\n", "b"], text_lines("a\r\r\nb", keepends=True))
        self.assertEqual(["a\n", "\n"], text_lines("a\n\n", keepends=True))
        self.assertEqual(["\n"], text_lines("\n", keepends=True))
        self.assertEqual([], text_lines("", keepends=True))


class LinesAndBreaksTests(unittest.TestCase):
    def test_each_line_is_beside_the_break_that_ends_it(self) -> None:
        self.assertEqual((["a", "b", "c", "d"], ["\r\n", "\r", "\n", ""]), lines_and_breaks("a\r\nb\rc\nd"))
        self.assertEqual((["a", "", "b"], ["\r", "\r\n", ""]), lines_and_breaks("a\r\r\nb"))
        self.assertEqual((["a", ""], ["\n", "\n"]), lines_and_breaks("a\n\n"))
        self.assertEqual(([], []), lines_and_breaks(""))

    def test_the_lines_and_breaks_rejoin_to_the_text(self) -> None:
        for text in ("a\r\nb\rc\nd", "a\r\r\nb\n", "\n\r", f"a{''.join(KEPT)}\rb"):
            with self.subTest(text=text):
                lines, breaks = lines_and_breaks(text)
                self.assertEqual(text, "".join(line + end for line, end in zip(lines, breaks, strict=True)))
                self.assertEqual(text_lines(text), lines)


@pytest.mark.parametrize("character", KEPT, ids=[f"U+{ord(character):04X}" for character in KEPT])
def test_every_other_character_splitlines_breaks_at_is_kept_in_its_line(character: str) -> None:
    text = f"description: a{character}disable-model-invocation: true\nname: x"
    assert len(text.splitlines()) == 3  # the premise: str.splitlines breaks here
    assert text_lines(text) == [f"description: a{character}disable-model-invocation: true", "name: x"]
    assert text_lines(text, keepends=True) == [f"description: a{character}disable-model-invocation: true\n", "name: x"]


class LineStartsTests(unittest.TestCase):
    def test_each_line_starts_after_a_crlf_a_cr_or_an_lf(self) -> None:
        self.assertEqual([0, 3, 5, 7, 8], line_starts("a\r\nb\rc\n\nd"))
        self.assertEqual([0, 2, 4, 6], line_starts("a\r\r\nb\n"))
        self.assertEqual([0], line_starts(""))

    def test_line_feed_starts_a_line_after_an_lf_alone(self) -> None:
        self.assertEqual([0, 3, 7], line_starts("a\r\nb\rc\nd", LINE_FEED))
        self.assertEqual(["\n"], LINE_FEED.findall("a\r\u2028\x85\n"))

    def test_a_position_s_index_counts_the_breaks_that_end_at_or_before_it(self) -> None:
        text = "ab\r\ncd\re\n"
        starts = line_starts(text)
        # Each character of the text, and its end: a break stands on the line it ends.
        self.assertEqual(
            [0, 0, 0, 0, 1, 1, 1, 2, 2, 3], [line_index(starts, position) for position in range(len(text) + 1)]
        )


class ProgramLinesTests(unittest.TestCase):
    def test_it_breaks_at_lf_and_takes_off_the_cr_before_it(self) -> None:
        self.assertEqual(["a", "b", "c"], program_lines("a\r\nb\nc"))

    def test_a_lone_cr_stays_in_its_line(self) -> None:
        # A progress line a program rewrites in place, or a path holding a CR, is one line.
        self.assertEqual(["10%\r20%", "done"], program_lines("10%\r20%\ndone\n"))
        self.assertEqual(["a\r", "b"], program_lines("a\r\r\nb"))

    def test_a_last_break_ends_the_last_line_and_empty_output_has_none(self) -> None:
        self.assertEqual(["a", ""], program_lines("a\n\n"))
        self.assertEqual(["a"], program_lines("a\r\n"))
        self.assertEqual([], program_lines(""))
        self.assertEqual([""], program_lines("\n"))


class OutputLinesTests(unittest.TestCase):
    def test_it_breaks_at_lf_alone_and_keeps_every_cr(self) -> None:
        # git ends a line with LF alone, so a CR before it is the last character of a name or a message.
        self.assertEqual(["a\r", "b"], output_lines("a\r\nb\n"))
        self.assertEqual(["10%\r20%", "done"], output_lines("10%\r20%\ndone"))

    def test_a_last_break_ends_the_last_line_and_empty_output_has_none(self) -> None:
        self.assertEqual(["a", ""], output_lines("a\n\n"))
        self.assertEqual([], output_lines(""))
        self.assertEqual([""], output_lines("\n"))

    def test_program_lines_is_output_lines_without_the_cr_before_each_lf(self) -> None:
        for output in ("", "\n", "a\r\n", "a\r\r\nb", "a\rb\r\n\r\n", "a\u2028b\r\nc\r"):
            with self.subTest(output=output):
                self.assertEqual([line.removesuffix("\r") for line in output_lines(output)], program_lines(output))


@pytest.mark.parametrize("character", [*KEPT, "\r"], ids=[f"U+{ord(character):04X}" for character in [*KEPT, "\r"]])
def test_an_output_line_keeps_every_character_splitlines_breaks_at_but_lf(character: str) -> None:
    output = f"refs/heads/a{character}b\nnext"
    assert len(output.splitlines()) == 3  # the premise: str.splitlines breaks here
    assert output_lines(output) == [f"refs/heads/a{character}b", "next"]


@pytest.mark.parametrize("character", KEPT, ids=[f"U+{ord(character):04X}" for character in KEPT])
def test_a_program_line_keeps_every_other_character_splitlines_breaks_at(character: str) -> None:
    output = f"error: cannot read docs/a{character}b.md\nnext"
    assert len(output.splitlines()) == 3  # the premise: str.splitlines breaks here
    assert program_lines(output) == [f"error: cannot read docs/a{character}b.md", "next"]
