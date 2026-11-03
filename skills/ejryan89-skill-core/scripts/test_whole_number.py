"""Regression tests for the one reader of a whole number from text."""

from __future__ import annotations

import argparse
import contextlib
import io
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


# What int() reads, or str.isdigit() passes, that is no whole number in ASCII digits: the non-ASCII ones are an
# Arabic-Indic three, a fullwidth one, and a superscript two.
NOT_ASCII_DIGITS = (
    "",
    " 1",
    "1 ",
    "+1",
    "1_0",
    "1.0",
    "0x1",
    "1e3",
    chr(0x0663),
    chr(0xFF11),
    chr(0x00B2),
    "1" + chr(0x00B2),
)


class ArgumentTests(unittest.TestCase):
    def refused(self, read: object, value: str, message: str) -> None:
        assert callable(read)
        with self.assertRaises(argparse.ArgumentTypeError) as raised:
            read(value)
        self.assertEqual(message, str(raised.exception))

    def test_a_whole_number_within_the_bounds_is_read(self) -> None:
        read = whole_number.argument(1, 300)
        for value, number in (("1", 1), ("007", 7), ("300", 300)):
            with self.subTest(value=value):
                self.assertEqual(number, read(value))
        self.assertEqual(0, whole_number.argument()("0"))
        self.assertEqual(10**50, whole_number.argument(1)("1" + "0" * 50))

    def test_what_int_reads_beyond_ascii_digits_is_refused_naming_the_value(self) -> None:
        for value in (*NOT_ASCII_DIGITS, "-1", "twelve"):
            with self.subTest(value=ascii(value)):
                # The message is pinned here once against a literal; the sites' suites take it from a refusal.
                self.refused(
                    whole_number.argument(1),
                    value,
                    f"must be a whole number of at least 1 in ASCII digits, not {value!r}",
                )

    def test_a_number_outside_the_bounds_is_refused(self) -> None:
        for value in ("0", "301", "1000"):
            with self.subTest(value=value):
                self.refused(
                    whole_number.argument(1, 300),
                    value,
                    f"must be a whole number from 1 to 300 in ASCII digits, not {value!r}",
                )
        self.refused(whole_number.argument(5), "4", "must be a whole number of at least 5 in ASCII digits, not '4'")

    def test_the_digits_are_capped_at_the_larger_bounds_and_otherwise_at_the_cap(self) -> None:
        # "0300" has four digits, one more than 300; the cap holds an unbounded option to DIGIT_LIMIT digits.
        self.refused(
            whole_number.argument(1, 300), "0300", "must be a whole number from 1 to 300 in ASCII digits, not '0300'"
        )
        self.assertEqual(300, whole_number.argument(1, 300)("300"))
        self.assertEqual(9, whole_number.argument(-10, 9)("09"))
        cap = whole_number.DIGIT_LIMIT
        self.assertEqual(10 ** (cap - 1), whole_number.argument()("1" + "0" * (cap - 1)))
        with self.assertRaises(argparse.ArgumentTypeError):
            whole_number.argument()("1" * (cap + 1))

    def test_a_sign_is_read_only_where_the_minimum_is_below_zero(self) -> None:
        read = whole_number.argument(-(2**31), 2**31 - 1)
        for value, number in (("-1073741819", -1073741819), ("-0", 0), ("0", 0), ("2147483647", 2**31 - 1)):
            with self.subTest(value=value):
                self.assertEqual(number, read(value))
        span = "an integer from -2147483648 to 2147483647 in ASCII digits"
        for value in ("-", "--1", "+1", "- 1", "-1_0", "-" + chr(0x0663), "-2147483649", "2147483648"):
            with self.subTest(value=value):
                self.refused(read, value, f"must be {span}, not {value!r}")
        self.refused(whole_number.argument(0), "-0", "must be a whole number of at least 0 in ASCII digits, not '-0'")

    def test_a_maximum_below_the_minimum_is_a_programming_error(self) -> None:
        with self.assertRaises(ValueError):
            whole_number.argument(2, 1)
        self.assertEqual(3, whole_number.argument(3, 3)("3"))

    def test_argparse_ends_a_refused_value_with_a_usage_error(self) -> None:
        parser = argparse.ArgumentParser(prog="tool")
        parser.add_argument("--pull", type=whole_number.argument(1))
        self.assertEqual(12, parser.parse_args(["--pull", "12"]).pull)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as exited:
            parser.parse_args(["--pull", "1_0"])
        self.assertEqual(2, exited.exception.code)
        self.assertEqual(
            "tool: error: argument --pull: must be a whole number of at least 1 in ASCII digits, not '1_0'",
            errors.getvalue().splitlines()[-1],
        )
