"""Regression tests for the one typed-field reader every record, configuration, manifest, and fixture validator uses."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from typed_fields import FieldError, FieldReader, describe

READ = FieldReader()


class DomainError(RuntimeError):
    """A skill's own error type, which its callers catch."""


class DomainFieldError(FieldError, DomainError):
    """A field error that is also the skill's error."""


class FieldReaderTests(unittest.TestCase):
    def refused(self, message: str, read: Any, *args: Any, **kwargs: Any) -> FieldError:
        with self.assertRaises(FieldError) as caught:
            read(*args, **kwargs)
        self.assertEqual(message, str(caught.exception))
        return caught.exception

    def test_the_error_carries_the_path_the_kind_expected_and_what_was_found(self) -> None:
        error = self.refused('a.b[2] must be an integer, found string "7"', READ.integer, "7", "a.b[2]")
        self.assertEqual(("a.b[2]", "an integer", 'string "7"'), (error.path, error.expected, error.found))
        self.assertIsInstance(error, ValueError)

    def test_what_was_found_names_the_json_type_and_a_short_value(self) -> None:
        cases = [
            (None, "null"),
            (True, "boolean true"),
            (False, "boolean false"),
            (7, "integer 7"),
            (-3, "integer -3"),
            (10**40, "an integer too long to quote"),
            (1.0, "number 1.0"),
            (float("nan"), "number nan"),
            ("a\nb", 'string "a\\nb"'),
            ("é", 'string "\\u00e9"'),
            ("x" * 40, 'string "' + "x" * 40 + '"'),
            ("x" * 41, "a string of 41 characters"),
            ([1], "an array"),
            ({"a": 1}, "an object"),
            (b"x", "a bytes, not a JSON value"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, describe(value))

    def test_a_string_is_non_empty_unless_empty_is_allowed(self) -> None:
        self.assertEqual(" x ", READ.string(" x ", "s"))
        self.assertEqual("", READ.string("", "s", empty=True))
        self.refused("s must be a non-empty string, found an empty string", READ.string, "", "s")
        self.refused("s must be a non-empty string, found a blank string", READ.string, " \t", "s")
        for value, found in ((1, "integer 1"), (["x"], "an array"), ({"x": 1}, "an object"), (None, "null")):
            with self.subTest(value=value):
                self.refused(f"s must be a non-empty string, found {found}", READ.string, value, "s")
        self.refused("s must be a string, found boolean true", READ.string, True, "s", empty=True)

    def test_a_single_line_trimmed_bounded_string(self) -> None:
        self.assertEqual("a b", READ.string("a b", "s", single_line=True, trimmed=True, maximum=3))
        for value in ("a\nb", "a\rb", "a\u2028b", "a\n"):
            with self.subTest(value=value):
                self.refused(
                    "s must be a non-empty single-line string, found a string of more than one line",
                    READ.string,
                    value,
                    "s",
                    single_line=True,
                )
        self.refused(
            "s must be a non-empty trimmed string, found a string with whitespace around it",
            READ.string,
            " a",
            "s",
            trimmed=True,
        )
        self.refused(
            "s must be a non-empty string of at most 3 characters, found a string of 4 characters",
            READ.string,
            "abcd",
            "s",
            maximum=3,
        )

    def test_an_integer_is_never_a_boolean_or_a_float(self) -> None:
        self.assertEqual(0, READ.integer(0, "n"))
        for value, found in ((True, "boolean true"), (1.0, "number 1.0"), ("1", 'string "1"'), ([1], "an array")):
            with self.subTest(value=value):
                self.refused(f"n must be an integer, found {found}", READ.integer, value, "n")

    def test_an_integer_within_its_bounds(self) -> None:
        self.assertEqual(1, READ.integer(1, "n", minimum=1))
        self.refused("n must be an integer of at least 1, found integer 0", READ.integer, 0, "n", minimum=1)
        self.refused("n must be an integer of at most 5, found integer 6", READ.integer, 6, "n", maximum=5)
        self.refused(
            "n must be an integer from 1 to 5, found boolean false", READ.integer, False, "n", minimum=1, maximum=5
        )

    def test_a_number_is_finite_and_never_a_boolean(self) -> None:
        self.assertEqual(0.5, READ.number(0.5, "x"))
        self.assertEqual(2, READ.number(2, "x"))
        for value, found in (
            (True, "boolean true"),
            (float("inf"), "number inf"),
            (float("nan"), "number nan"),
            ("0.5", 'string "0.5"'),
        ):
            with self.subTest(value=value):
                self.refused(f"x must be a finite number, found {found}", READ.number, value, "x")

    def test_a_boolean(self) -> None:
        self.assertIs(False, READ.boolean(False, "b"))
        self.refused("b must be a boolean, found integer 0", READ.boolean, 0, "b")
        self.refused('b must be a boolean, found string "true"', READ.boolean, "true", "b")

    def test_a_hex_digest_has_one_of_its_lengths_and_is_lowercase(self) -> None:
        sha256 = "0123456789abcdef" * 4
        self.assertEqual(sha256, READ.hex_digest(sha256, "h", 64))
        self.assertEqual(sha256[:40], READ.hex_digest(sha256[:40], "h", 40, 64))
        self.refused(
            "h must be a lowercase hexadecimal digest of 64 characters, found a string of 40 characters",
            READ.hex_digest,
            sha256[:40],
            "h",
            64,
        )
        self.refused(
            "h must be a lowercase hexadecimal digest of 64 characters, found a string with a character that is not "
            "a lowercase hexadecimal digit",
            READ.hex_digest,
            sha256.upper(),
            "h",
            64,
        )
        self.refused(
            "h must be a lowercase hexadecimal digest of 40 or 64 characters, found a string of 3 characters",
            READ.hex_digest,
            "abc",
            "h",
            40,
            64,
        )
        self.refused(
            "h must be a lowercase hexadecimal digest of 64 characters, found an array", READ.hex_digest, [], "h", 64
        )
        with self.assertRaises(ValueError):
            READ.hex_digest(sha256, "h")

    def test_one_of_matches_the_type_as_well_as_the_value(self) -> None:
        self.assertEqual("b", READ.one_of("b", "e", ("a", "b")))
        self.assertEqual(1, READ.one_of(1, "v", {1}))
        self.refused('e must be one of "a", "b", found string "c"', READ.one_of, "c", "e", ("a", "b"))
        self.refused('e must be one of "a", "b", found string "c"', READ.one_of, "c", "e", {"b", "a"})
        self.refused('e must be one of "a", "b", found an array', READ.one_of, ["a"], "e", ("a", "b"))
        self.refused("v must be one of 1, found boolean true", READ.one_of, True, "v", {1})
        self.refused("v must be one of 1, found number 1.0", READ.one_of, 1.0, "v", {1})

    def test_an_array_reads_each_item_at_its_index(self) -> None:
        self.assertEqual([], READ.array([], "a"))
        self.assertEqual(["x", "y"], READ.array(["x", "y"], "a", READ.string))
        self.refused("a must be an array, found an object", READ.array, {}, "a")
        self.refused("a must be a non-empty array, found an empty array", READ.array, [], "a", nonempty=True)
        self.refused(
            "a must be an array of at most 1 items, found an array of 2 items", READ.array, [1, 2], "a", maximum=1
        )
        self.refused("a[1] must be a non-empty string, found integer 2", READ.array, ["x", 2], "a", READ.string)

    def test_an_object_holds_its_required_fields_and_no_unknown_one(self) -> None:
        self.assertEqual({"a": 1}, READ.json_object({"a": 1}, "o"))
        self.assertEqual({"a": 1}, READ.json_object({"a": 1}, "o", required={"a"}, optional={"b"}))
        self.assertEqual({"b": 1}, READ.json_object({"b": 1}, "o", optional={"a", "b"}))
        self.refused("o must be an object, found an array", READ.json_object, [], "o")
        self.refused(
            "o must be an object with field a, found an object without it",
            READ.json_object,
            {"b": 1},
            "o",
            required={"a"},
            optional={"b"},
        )
        self.refused(
            "o must be an object with fields a, c, found an object without them",
            READ.json_object,
            {},
            "o",
            required={"c", "a"},
        )
        self.refused(
            'o must be an object of known fields, found unknown fields "x", "y"',
            READ.json_object,
            {"a": 1, "y": 2, "x": 3},
            "o",
            required={"a"},
        )
        self.refused(
            'o must be an object of known fields, found unknown field "x"',
            READ.json_object,
            {"x": 1},
            "o",
            optional={"a"},
        )

    def test_optional_allows_null_and_expects_the_kind_or_null(self) -> None:
        self.assertIsNone(READ.optional(READ.string, None, "s"))
        self.assertEqual("x", READ.optional(READ.string, "x", "s", single_line=True))
        self.refused("s must be a non-empty string or null, found integer 1", READ.optional, READ.string, 1, "s")
        self.refused(
            "s must be a lowercase hexadecimal digest of 64 characters or null, found a string of 1 character",
            READ.optional,
            READ.hex_digest,
            "x",
            "s",
            64,
        )
        self.refused(
            "s[0] must be a non-empty string, found integer 1",
            READ.optional,
            READ.array,
            [1],
            "s",
            READ.string,
        )

    def test_either_reads_the_first_kind_that_accepts_the_value(self) -> None:
        self.assertEqual(2, READ.either(2, "r", READ.integer, READ.string))
        self.assertEqual("v1:F001", READ.either("v1:F001", "r", READ.integer, READ.string))
        self.refused(
            "r must be an integer or a non-empty string, found boolean true",
            READ.either,
            True,
            "r",
            READ.integer,
            READ.string,
        )
        self.refused(
            'r must be an integer or a non-empty string, found string ""',
            READ.either,
            "",
            "r",
            READ.integer,
            READ.string,
        )
        self.refused(
            "r[0] must be a non-empty string, found integer 1",
            READ.either,
            [1],
            "r",
            lambda value, path: READ.array(value, path, READ.string),
            READ.integer,
        )

    def test_a_reader_raises_the_error_class_it_was_built_with(self) -> None:
        reader = FieldReader(DomainFieldError)
        for read in (
            lambda: reader.string(1, "f"),
            lambda: reader.integer("1", "f"),
            lambda: reader.number(None, "f"),
            lambda: reader.boolean(1, "f"),
            lambda: reader.hex_digest(1, "f", 64),
            lambda: reader.one_of(1, "f", ("a",)),
            lambda: reader.array(1, "f"),
            lambda: reader.json_object(1, "f"),
            lambda: reader.optional(reader.string, 1, "f"),
            lambda: reader.array([1], "f", reader.string),
            lambda: reader.either(None, "f", reader.integer, reader.string),
        ):
            with self.subTest(read=read), self.assertRaises(DomainError) as caught:
                read()
            self.assertIsInstance(caught.exception, DomainFieldError)
            self.assertTrue(str(caught.exception).startswith("f"))


if __name__ == "__main__":
    unittest.main()
