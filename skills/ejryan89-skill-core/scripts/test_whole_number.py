"""Regression tests for the one reader of a whole number from text."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import whole_number


class Refused(ValueError):
    """The caller's own error, which the reader raises as given."""


class ReadTests(unittest.TestCase):
    def refused(self, text: str, **limit: int) -> None:
        error = Refused(f"field {text!r} is not a whole number")
        with self.assertRaises(Refused) as raised:
            whole_number.read(text, error, **limit)
        self.assertIs(error, raised.exception)

    def test_the_cap_is_the_interpreters_default_integer_string_limit(self) -> None:
        # A pinned contract value: json_reader and every reader of a number from text share it.
        self.assertEqual(4300, whole_number.DIGIT_LIMIT)
        self.assertEqual(sys.int_info.default_max_str_digits, whole_number.DIGIT_LIMIT)

    def test_ascii_digits_read_as_their_number(self) -> None:
        for text, number in (("0", 0), ("7", 7), ("007", 7), ("1234567890", 1234567890)):
            with self.subTest(text=text):
                self.assertEqual(number, whole_number.read(text, Refused("unused")))

    def test_what_int_reads_beyond_ascii_digits_is_refused(self) -> None:
        # int() reads, or str.isdigit() passes, each of these, and none is a whole number in ASCII digits.
        # The non-ASCII ones are an Arabic-Indic three, a fullwidth one, and a superscript two.
        unicode_digits = (chr(0x0663), chr(0xFF11), chr(0x00B2), "1" + chr(0x00B2))
        for text in ("", " 1", "1 ", "+1", "-1", "1_000", "1.0", "0x1", *unicode_digits):
            with self.subTest(text=ascii(text)):
                self.refused(text)

    def test_a_run_up_to_the_cap_is_read_and_one_past_it_is_refused(self) -> None:
        cap = whole_number.DIGIT_LIMIT
        self.assertEqual(10 ** (cap - 1), whole_number.read("1" + "0" * (cap - 1), Refused("unused")))
        self.refused("1" * (cap + 1))

    def test_a_lower_limit_the_caller_gives_is_held(self) -> None:
        self.assertEqual(1234567890, whole_number.read("1234567890", Refused("unused"), limit=10))
        self.refused("12345678901", limit=10)

    def test_a_limit_above_the_cap_does_not_raise_it(self) -> None:
        self.refused("1" * (whole_number.DIGIT_LIMIT + 1), limit=whole_number.DIGIT_LIMIT * 2)

    def test_parse_gives_none_where_read_raises(self) -> None:
        cap = whole_number.DIGIT_LIMIT
        for text, number in (("42", 42), (chr(0x00B2), None), ("", None), ("1" * (cap + 1), None), ("123", None)):
            with self.subTest(text=text[:8]):
                self.assertEqual(number, whole_number.parse(text, limit=2 if text == "123" else cap))
        self.assertEqual(10 ** (cap - 1), whole_number.parse("1" + "0" * (cap - 1)))

    def test_an_interpreter_limit_set_below_the_cap_ends_in_the_callers_error(self) -> None:
        configured = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(640)
        try:
            self.assertEqual(int("9" * 640), whole_number.read("9" * 640, Refused("unused")))
            self.refused("9" * 641)
        finally:
            sys.set_int_max_str_digits(configured)
