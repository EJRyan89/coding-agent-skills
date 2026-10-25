"""Regression tests for the one writer of JSON for a file the skills, the deployer, and the tools share."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_writer
from json_reader import loads
from json_writer import CONTENT_ERRORS, JsonWriteRefused, dumps, write

# A record of every JSON type, with a character past ASCII and one past the Basic Multilingual Plane.
RECORD: dict[str, Any] = {"z": ["café", "\U0001f600"], "a": {"n": 1, "f": 1.5, "t": True, "none": None}}


def nested(depth: int) -> list[Any]:
    value: list[Any] = []
    for _ in range(depth):
        value = [value]
    return value


class RefusalTests(unittest.TestCase):
    def refusal(self, value: Any, **options: Any) -> str:
        with self.assertRaises(JsonWriteRefused) as raised:
            dumps(value, **options)
        return str(raised.exception)

    def test_each_non_finite_number_is_refused_by_name_whatever_the_options(self) -> None:
        for number in (float("nan"), float("inf"), float("-inf")):
            for options in ({}, {"indent": 2, "sort_keys": True}, {"ensure_ascii": False}):
                with self.subTest(number=number, options=options):
                    self.assertEqual(
                        "the value holds NaN or an infinity, which JSON does not define",
                        self.refusal({"record": [1, number]}, **options),
                    )

    def test_a_lone_surrogate_is_refused_by_name_even_where_the_text_would_escape_it(self) -> None:
        for value, ensure_ascii in (("caf\udce9", False), ("caf\udce9", True), ({"\ud800 key": 1}, True)):
            with self.subTest(value=value, ensure_ascii=ensure_ascii):
                self.assertRegex(
                    self.refusal(value, ensure_ascii=ensure_ascii),
                    r"^the value holds a lone surrogate U\+D[8C][0-9A-F]{2}, which UTF-8 cannot encode$",
                )

    def test_a_key_two_spellings_write_alike_is_refused_by_the_read_back(self) -> None:
        self.assertEqual(
            "the value cannot be read back: the JSON repeats the key '1' in one object",
            self.refusal({1: "a", "1": "b"}),
        )

    def test_nesting_past_the_recursion_limit_is_refused(self) -> None:
        self.assertEqual("the value nests deeper than the writer can write", self.refusal(nested(100_000)))

    def test_a_refusal_is_a_value_error_and_a_value_of_no_json_type_is_still_a_type_error(self) -> None:
        self.assertTrue(issubclass(JsonWriteRefused, ValueError))
        with self.assertRaises(TypeError):
            dumps({"value": object()})

    def test_a_circular_value_is_the_json_modules_own_error_not_a_refusal(self) -> None:
        circular: list[Any] = []
        circular.append(circular)
        with self.assertRaisesRegex(ValueError, "^Circular reference detected$") as raised:
            dumps(circular)
        self.assertNotIsInstance(raised.exception, JsonWriteRefused)


class WriteTests(unittest.TestCase):
    def test_text_is_what_json_dumps_writes_with_the_same_options(self) -> None:
        for options in (
            {},
            {"indent": 2, "sort_keys": True},
            {"ensure_ascii": False},
            {"separators": (",", ":")},
        ):
            with self.subTest(options=options):
                self.assertEqual(json.dumps(RECORD, **options), dumps(RECORD, **options))

    def test_by_default_keys_keep_their_order_and_text_past_ascii_is_escaped(self) -> None:
        self.assertEqual('{"z": ["caf\\u00e9"], "a": 1}', dumps({"z": ["café"], "a": 1}))
        self.assertEqual('{"z": ["café"], "a": 1}', dumps({"z": ["café"], "a": 1}, ensure_ascii=False))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            write(path, {"z": ["café"], "a": 1})
            self.assertEqual(b'{"z": ["caf\\u00e9"], "a": 1}\n', path.read_bytes())

    def test_a_surrogate_pair_is_a_character_and_is_written(self) -> None:
        self.assertEqual('"\\ud83d\\ude00"', dumps("\U0001f600"))
        self.assertEqual('"\U0001f600"', dumps("\U0001f600", ensure_ascii=False))

    def test_write_replaces_the_file_with_the_text_and_a_newline_as_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "a folder" / "record.json"
            write(path, RECORD, indent=2, sort_keys=True, ensure_ascii=False)
            self.assertEqual(
                (json.dumps(RECORD, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8"),
                path.read_bytes(),
            )
            self.assertEqual(RECORD, loads(path.read_bytes().decode("utf-8")))

    @unittest.skipIf(os.name == "nt", "Windows keeps no permission bits but read-only")
    def test_write_gives_the_file_the_mode_it_is_asked_for_and_the_owners_alone_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for mode, expected in ((None, 0o600), (0o644, 0o644)):
                with self.subTest(mode=mode):
                    path = Path(temporary) / f"record {expected:o}.json"
                    if mode is None:
                        write(path, RECORD)
                    else:
                        write(path, RECORD, mode=mode)
                    self.assertEqual(expected, path.stat().st_mode & 0o777)

    def test_a_refused_value_leaves_the_file_as_it_was_and_no_copy_beside_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "record.json"
            path.write_bytes(b'{"previous": true}\n')
            for value in ({"value": float("nan")}, {"value": "\udc80"}):
                with self.subTest(value=value), self.assertRaises(JsonWriteRefused):
                    write(path, value)
            self.assertEqual(b'{"previous": true}\n', path.read_bytes())
            self.assertEqual(["record.json"], sorted(item.name for item in Path(temporary).iterdir()))


class ContentErrorsTests(unittest.TestCase):
    def test_the_family_is_the_refusal(self) -> None:
        self.assertEqual((JsonWriteRefused,), CONTENT_ERRORS)

    def test_a_value_no_json_file_can_hold_raises_a_member_and_a_value_of_no_json_type_does_not(self) -> None:
        for value in (float("nan"), "\ud800", {1: 1, "1": 2}):
            with self.subTest(value=value), self.assertRaises(CONTENT_ERRORS):
                dumps(value)
        with self.assertRaises(TypeError):
            dumps(object())


def test_the_readers_that_raise_the_family_are_named() -> None:
    assert json_writer.CONTENT_READERS == ("dumps", "write")
