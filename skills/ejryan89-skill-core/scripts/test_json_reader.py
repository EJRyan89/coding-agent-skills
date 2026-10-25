"""Regression tests for the one parse of JSON the skills, the deployer, and the tools share."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import whole_number
from json_reader import JsonRefused, loads, raw_decode, read_file


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

    def test_nesting_past_the_recursion_limit_is_refused(self) -> None:
        for text in ("[" * 100_000 + "]" * 100_000, '{"a": ' * 100_000 + "1" + "}" * 100_000):
            with self.subTest(opening=text[:6]):
                self.assertEqual("the JSON nests deeper than the parser can read", self.refusal(text))

    def test_a_refusal_is_a_value_error(self) -> None:
        self.assertTrue(issubclass(JsonRefused, ValueError))

    def test_malformed_json_raises_what_json_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            loads("{")

    def test_an_integer_past_the_shared_digit_cap_is_refused_whatever_its_sign(self) -> None:
        refusal = f"the JSON holds an integer of more than {whole_number.DIGIT_LIMIT} digits"
        past = "1" * (whole_number.DIGIT_LIMIT + 1)
        for text in (past, f'{{"a": -{past}}}', f"[{past}]"):
            with self.subTest(text=text[:8]):
                self.assertEqual(refusal, self.refusal(text))

    def test_an_integer_up_to_the_cap_reads_with_its_sign(self) -> None:
        widest = "1" + "0" * (whole_number.DIGIT_LIMIT - 1)
        self.assertEqual([-int(widest), int(widest)], loads(f"[-{widest}, {widest}]"))
        self.assertEqual([0, 0, 12, -12, 1.5], loads("[0, -0, 12, -12, 1.5]"))

    def test_the_cap_holds_whatever_limit_the_interpreter_is_configured_with(self) -> None:
        configured = sys.get_int_max_str_digits()
        sys.set_int_max_str_digits(0)
        try:
            refusal = self.refusal("1" * (whole_number.DIGIT_LIMIT + 1))
        finally:
            sys.set_int_max_str_digits(configured)
        self.assertEqual(f"the JSON holds an integer of more than {whole_number.DIGIT_LIMIT} digits", refusal)


class RawDecodeTests(unittest.TestCase):
    def test_a_value_followed_by_other_text_reads_with_the_index_past_it(self) -> None:
        self.assertEqual(({"a": 1}, 8), raw_decode('{"a": 1} {"b": 2}'))
        self.assertEqual(({"b": 2}, 17), raw_decode('{"a": 1} {"b": 2}', 9))

    def test_an_integer_past_the_shared_digit_cap_is_refused_and_one_within_it_reads_to_its_end(self) -> None:
        widest = "1" + "0" * (whole_number.DIGIT_LIMIT - 1)
        self.assertEqual(([int(widest), -7], len(widest) + 6), raw_decode(f"[{widest}, -7] rest"))
        self.assertEqual((int(widest), 4 + len(widest)), raw_decode(f"[1] {widest} rest", 4))
        with self.assertRaises(JsonRefused) as raised:
            raw_decode(f'{{"a": 1}} [{widest}0] rest', 9)
        self.assertEqual(
            f"the JSON holds an integer of more than {whole_number.DIGIT_LIMIT} digits", str(raised.exception)
        )

    def test_each_shape_loads_refuses_is_refused(self) -> None:
        for text, refusal in (
            ('{"k": 1, "k": 2} rest', "the JSON repeats the key 'k' in one object"),
            ('{"value": NaN} rest', "the JSON holds NaN, which JSON does not define"),
            ("[" * 100_000 + "]" * 100_000, "the JSON nests deeper than the parser can read"),
        ):
            with self.subTest(text=text[:12]):
                with self.assertRaises(JsonRefused) as raised:
                    raw_decode(text)
                self.assertEqual(refusal, str(raised.exception))


class ReadFileTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "value.json"

    def test_a_utf8_file_reads_its_non_ascii_text_on_every_platform(self) -> None:
        self.path.write_bytes('{"title": "café 設計"}'.encode())
        self.assertEqual({"title": "café 設計"}, read_file(self.path))

    def test_a_file_that_is_not_utf8_or_starts_with_a_byte_order_mark_is_a_value_error(self) -> None:
        for content in ('{"title": "café"}'.encode("cp1252"), b'\xef\xbb\xbf{"title": "x"}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_file(self.path)

    def test_a_refused_shape_is_refused_as_loads_refuses_it(self) -> None:
        self.path.write_bytes(b'{"k": 1, "k": 2}')
        with self.assertRaises(JsonRefused):
            read_file(self.path)

    def test_a_missing_file_is_an_os_error(self) -> None:
        with self.assertRaises(OSError):
            read_file(self.path)


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
