"""Regression tests for the one parse of JSON the skills, the deployer, and the tools share."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from json_reader import JsonRefused, loads


class RefusalTests(unittest.TestCase):
    def refusal(self, text: str | bytes) -> str:
        with self.assertRaises(JsonRefused) as raised:
            loads(text)
        return str(raised.exception)

    def test_a_repeated_key_is_refused_by_name(self) -> None:
        self.assertEqual("the JSON repeats the key 'value' in one object", self.refusal('{"value": 1, "value": 2}'))

    def test_a_key_repeated_in_a_nested_object_is_refused(self) -> None:
        self.assertEqual("the JSON repeats the key 'b' in one object", self.refusal('[{"a": {"b": 1, "b": 1}}]'))

    def test_a_repeated_key_in_bytes_is_refused(self) -> None:
        self.assertEqual("the JSON repeats the key 'k' in one object", self.refusal(b'{"k": 1, "k": 2}'))

    def test_each_non_finite_constant_is_refused_by_name(self) -> None:
        for name in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(name=name):
                self.assertEqual(
                    f"the JSON holds {name}, which JSON does not define", self.refusal(f'{{"value": {name}}}')
                )

    def test_a_refusal_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(JsonRefused, ValueError))

    def test_malformed_json_raises_what_json_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            loads("{")


class AcceptanceTests(unittest.TestCase):
    def test_well_formed_json_reads_as_json_loads_reads_it(self) -> None:
        text = '{"a": [1, 2.5, -3e2, true, null, "x"], "b": {"a": {}}, "c": []}'
        self.assertEqual(json.loads(text), loads(text))

    def test_the_same_key_in_two_objects_is_not_a_repeat(self) -> None:
        self.assertEqual([{"k": 1}, {"k": 2}], loads('[{"k": 1}, {"k": 2}]'))

    def test_a_string_spelling_a_constant_is_text(self) -> None:
        self.assertEqual({"value": "NaN"}, loads('{"value": "NaN"}'))

    def test_keys_keep_their_order(self) -> None:
        self.assertEqual(["b", "a"], list(loads('{"b": 1, "a": 2}')))
