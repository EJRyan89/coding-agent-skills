"""Regression tests for the one parse of JSON the skills, the deployer, and the tools share."""

from __future__ import annotations

import collections
import errno
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import json_reader
import json_writer
import nesting_depth
import whole_number
from json_reader import CONTENT_ERRORS, JsonRefused, loads, raw_decode, read_file
from validation_notes import note_line

# The refusal of a number whose float is an infinity, spelled here rather than read from the module.
OVERFLOW = "the JSON holds a number too large for a float"


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

    def test_nesting_one_level_past_the_shared_limit_is_refused_though_the_scanner_reads_it(self) -> None:
        past = nesting_depth.DEPTH_LIMIT + 1
        for text in ("[" * past + "]" * past, '{"a": ' * past + "1" + "}" * past, "[" * 2000 + "]" * 2000):
            with self.subTest(opening=text[:6], length=len(text)):
                self.assertEqual("the JSON nests deeper than the parser can read", self.refusal(text))

    def test_nesting_at_the_shared_limit_reads(self) -> None:
        limit = nesting_depth.DEPTH_LIMIT
        value = loads("[" * limit + "]" * limit)
        for _ in range(limit - 1):
            value = value[0]
        self.assertEqual([], value)
        value = loads('{"a": ' * (limit - 1) + "{}" + "}" * (limit - 1))
        for _ in range(limit - 1):
            value = value["a"]
        self.assertEqual({}, value)

    def test_a_scalar_is_no_level_so_one_at_the_limit_reads_as_an_empty_container_does(self) -> None:
        limit = nesting_depth.DEPTH_LIMIT
        for inner in ("1", '"text"', "null", "true", "1.5"):
            with self.subTest(inner=inner):
                value = loads("[" * limit + inner + "]" * limit)
                for _ in range(limit - 1):
                    value = value[0]
                self.assertEqual([json.loads(inner)], value)
                past = limit + 1
                self.assertEqual(
                    "the JSON nests deeper than the parser can read", self.refusal("[" * past + inner + "]" * past)
                )
        value = loads('{"a": ' * limit + "1" + "}" * limit)
        for _ in range(limit):
            value = value["a"]
        self.assertEqual(1, value)

    def test_a_lone_surrogate_is_refused_in_a_string_or_a_key_at_any_depth(self) -> None:
        for text, code in (
            ('"\\ud800"', "D800"),
            ('"\\udfff"', "DFFF"),
            ('{"\\udc00": 1}', "DC00"),
            ('[{"a": ["x", "\\ud83d"]}]', "D83D"),
            ('"\\ude00\\ud83d"', "DE00"),
            ('"\ud800"', "D800"),
        ):
            with self.subTest(text=ascii(text)):
                self.assertEqual(
                    f"the JSON holds a lone surrogate U+{code}, which UTF-8 cannot encode", self.refusal(text)
                )

    def test_a_lone_surrogate_is_kept_only_when_the_caller_asks_and_every_other_refusal_still_holds(self) -> None:
        kept = loads('{"branch": "caf\\udce9", "\\ud800": ["\\udfff"]}', surrogates=True)
        self.assertEqual({"branch": "caf\udce9", "\ud800": ["\udfff"]}, kept)
        for text, refusal in (
            ('{"k": "\\ud800", "k": 2}', "the JSON repeats the key 'k' in one object"),
            ("[" * 101 + '"\\ud800"' + "]" * 101, "the JSON nests deeper than the parser can read"),
        ):
            with self.subTest(text=text[:12]):
                with self.assertRaises(JsonRefused) as raised:
                    loads(text, surrogates=True)
                self.assertEqual(refusal, str(raised.exception))

    def test_a_surrogate_pair_reads_as_the_character_it_spells(self) -> None:
        self.assertEqual(["\U0001f600"], loads('["\\ud83d\\ude00"]'))
        self.assertEqual({"\U0001f600": 1}, loads('{"\\ud83d\\ude00": 1}'))

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

    def test_a_number_that_overflows_to_an_infinity_is_refused_whatever_its_sign_or_nesting(self) -> None:
        for text in ("1e400", "-1e400", '{"value": 1e400}', "[[-1.5e309]]"):
            with self.subTest(text=text):
                self.assertEqual(OVERFLOW, self.refusal(text))

    def test_a_finite_float_reads_as_json_loads_reads_it_subnormals_and_negative_zero_included(self) -> None:
        text = "[1.5, -0.0, 5e-324, 1e-400, 1.7976931348623157e308, -2.5E+3]"
        self.assertEqual([repr(number) for number in json.loads(text)], [repr(number) for number in loads(text)])

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

    def test_a_number_that_overflows_is_refused_and_a_finite_float_reads_to_its_end(self) -> None:
        self.assertEqual((-0.0, 4), raw_decode("-0.0 rest"))
        with self.assertRaises(JsonRefused) as raised:
            raw_decode("1e400 rest")
        self.assertEqual(OVERFLOW, str(raised.exception))

    def test_each_shape_loads_refuses_is_refused(self) -> None:
        for text, refusal in (
            ('{"k": 1, "k": 2} rest', "the JSON repeats the key 'k' in one object"),
            ('{"value": NaN} rest', "the JSON holds NaN, which JSON does not define"),
            ('{"value": -1e400} rest', OVERFLOW),
            ("[" * 100_000 + "]" * 100_000, "the JSON nests deeper than the parser can read"),
            ("[" * 101 + "]" * 101 + " rest", "the JSON nests deeper than the parser can read"),
            ('["\\ud800"] rest', "the JSON holds a lone surrogate U+D800, which UTF-8 cannot encode"),
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

    def test_with_byte_order_mark_one_leading_mark_is_passed_over_and_text_that_is_not_utf8_still_refused(
        self,
    ) -> None:
        self.path.write_bytes(b'\xef\xbb\xbf{"title": "x"}')
        self.assertEqual({"title": "x"}, read_file(self.path, byte_order_mark=True))
        for content in ('{"title": "café"}'.encode("cp1252"), b'\xef\xbb\xbf\xef\xbb\xbf{"title": "x"}'):
            with self.subTest(content=content):
                self.path.write_bytes(content)
                with self.assertRaises(ValueError):
                    read_file(self.path, byte_order_mark=True)

    def test_with_byte_order_mark_a_file_without_one_reads_as_without_the_option(self) -> None:
        self.path.write_bytes('{"title": "café 設計"}'.encode())
        self.assertEqual({"title": "café 設計"}, read_file(self.path, byte_order_mark=True))

    def test_a_refused_shape_is_refused_as_loads_refuses_it(self) -> None:
        self.path.write_bytes(b'{"k": 1, "k": 2}')
        with self.assertRaises(JsonRefused):
            read_file(self.path)

    def test_a_number_that_overflows_to_an_infinity_is_refused_as_loads_refuses_it(self) -> None:
        self.path.write_bytes(b'{"value": 1e400}')
        with self.assertRaises(JsonRefused) as raised:
            read_file(self.path)
        self.assertEqual(OVERFLOW, str(raised.exception))

    def test_a_missing_file_is_an_os_error(self) -> None:
        with self.assertRaises(OSError):
            read_file(self.path)


# A writer in a process of its own, as the code-review pipeline's `check` replaces a run's run.json: it puts the folder
# its first argument names first on its path, replaces the file its second names through atomic_file.replace_bytes for
# as many seconds as its third, each time with an object whose `replace` counts the replaces, says when it starts, and
# prints the replaces it made and the PermissionErrors that outlasted replace_bytes' own waits, as one JSON object.
WRITER = """import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import atomic_file
target, seconds = Path(sys.argv[2]), float(sys.argv[3])
made = refused = 0
padding = "x" * 4096
print("ready", flush=True)
end = time.monotonic() + seconds
while time.monotonic() < end:
    try:
        atomic_file.replace_bytes(target, json.dumps({"replace": made + 1, "padding": padding}).encode("utf-8"))
    except PermissionError:
        refused += 1
    else:
        made += 1
print(json.dumps({"made": made, "refused": refused}), flush=True)
"""

# How long the writer replaces the file while the reader reads it, in seconds.
RACE_SECONDS = 5


@unittest.skipUnless(sys.platform == "win32", "only Windows refuses an open that meets a replace of its file")
class ReadMeetingAReplaceTests(unittest.TestCase):
    """A read of a file another process replaces through atomic_file while it reads, as a reviewer guard reads a run's
    run.json while `check` replaces it. On Windows, an open that meets the replace may be refused with
    PermissionError, as CI's Windows runner showed for Python's own open, so read_file tries a refused read again."""

    def test_every_read_file_of_a_file_another_process_keeps_replacing_reads_one_whole_file(self) -> None:
        # Holds on Windows: the reader loops over Python's open and json_reader.read_file, pausing a millisecond after
        # each pair so the writer is not starved, while the writer loops over atomic_file.replace_bytes. Every read
        # either gives an object some replace wrote, whole, or is refused; read_file is never refused. A refusal of
        # Python's own open, which read_file tries again, is counted and printed, with the reads and replaces, as a
        # validation note, so each run shows the race the retry answers.
        reads: collections.Counter[str] = collections.Counter()
        refusals: collections.Counter[str] = collections.Counter()
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            path.write_bytes(b'{"replace": 0}')
            writer = subprocess.Popen(
                [sys.executable, "-c", WRITER, str(Path(json_reader.__file__).parent), str(path), str(RACE_SECONDS)],
                stdout=subprocess.PIPE,
                encoding="utf-8",
            )
            assert writer.stdout is not None
            try:
                self.assertEqual("ready\n", writer.stdout.readline(), "the writer did not start")
                while writer.poll() is None:
                    for name, read in (("open", self.read_through_open), ("read_file", read_file)):
                        try:
                            value = read(path)
                        except OSError as error:
                            refusals[self.refusal(name, error)] += 1
                        else:
                            self.assertIsInstance(value, dict, f"{name} read part of a file")
                            self.assertIsInstance(value.get("replace"), int, f"{name} read part of a file")
                            reads[name] += 1
                    time.sleep(0.001)
                summary = loads(writer.stdout.read())
            finally:
                if writer.poll() is None:
                    writer.kill()
                writer.wait(timeout=30)
                writer.stdout.close()
        print(
            note_line(
                f"json_reader race on Windows: {reads['open']} reads through open and {reads['read_file']} through "
                f"read_file met {summary['made']} replaces ({summary['refused']} replaces refused past their waits); "
                f"refused reads: {dict(refusals) or 'none'}"
            )
        )
        self.assertGreater(summary["made"], 0, "the writer replaced the file")
        self.assertEqual(
            {}, {key: count for key, count in refusals.items() if key.startswith("read_file:")}, "read_file was refused"
        )

    @staticmethod
    def read_through_open(path: Path) -> Any:
        with path.open("rb") as held:
            return loads(held.read())

    @staticmethod
    def refusal(name: str, error: OSError) -> str:
        return f"{name}: {type(error).__name__} errno {error.errno} winerror {getattr(error, 'winerror', None)}"


class ReadRefusedForAMomentTests(unittest.TestCase):
    """A read of a file whose open another process refuses for a moment, as Windows refuses one that meets a replace
    through atomic_file."""

    def read(
        self, refusals: int | None, failure: BaseException | None = None, interrupt: BaseException | None = None
    ) -> tuple[Any, list[float], int]:
        """read_file of a file whose read is refused with PermissionError `refusals` times, or every time for None,
        then fails with `failure` if given, with each wait raising `interrupt` if given. The file is replaced with a
        new one at the first refusal, as the replace a refused read meets puts it in place. Returns what read_file gave
        or raised, the waits, and the tries."""
        real_read_bytes = Path.read_bytes
        tries: list[Path] = []
        waits: list[float] = []

        def read_bytes(self: Path) -> bytes:
            tries.append(self)
            if refusals is None or len(tries) <= refusals:
                self.write_bytes(b'{"replace": 2}')
                raise PermissionError(errno.EACCES, "Permission denied", str(self))
            if failure is not None:
                raise failure
            return real_read_bytes(self)

        def wait(seconds: float) -> None:
            waits.append(seconds)
            if interrupt is not None:
                raise interrupt

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run.json"
            path.write_bytes(b'{"replace": 1}')
            with mock.patch.object(Path, "read_bytes", read_bytes), mock.patch.object(time, "sleep", wait):
                try:
                    value: Any = read_file(path)
                except (OSError, KeyboardInterrupt) as error:
                    value = error
        self.assertEqual(1, len(set(tries)), "every try reads the one file")
        return value, waits, len(tries)

    def test_a_read_refused_for_a_while_reads_the_new_file_whole_after_short_waits(self) -> None:
        # Holds on every platform, by injected refusals.
        self.assertEqual(({"replace": 2}, [0.01, 0.02, 0.04], 4), self.read(3))
        self.assertEqual(({"replace": 1}, [], 1), self.read(0), "a read not refused never waits")

    def test_a_read_refused_throughout_raises_the_last_refusal_after_a_bounded_back_off(self) -> None:
        # Holds on every platform. The back-off is pinned against literals: five waits of 10, 20, 40, 80, and 160 ms,
        # 0.31 s in all, and a try after each.
        raised, waits, tries = self.read(None)
        self.assertIs(PermissionError, type(raised), "the refusal is raised as it was")
        self.assertEqual("Permission denied", getattr(raised, "strerror", None))
        self.assertEqual(([0.01, 0.02, 0.04, 0.08, 0.16], 6), (waits, tries))
        self.assertAlmostEqual(0.31, sum(waits), msg="about a third of a second in all")

    def test_any_other_failure_is_raised_as_it_was_and_never_tried_again(self) -> None:
        # Holds on every platform: only a PermissionError, the refusal a replace in progress explains, is tried again,
        # so no other failure is retried or masked, whether it comes first or after refusals.
        for failure in (
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
            IsADirectoryError(errno.EISDIR, "Is a directory"),
            OSError("synthetic read"),
        ):
            with self.subTest(failure=repr(failure)):
                self.assertEqual((failure, [], 1), self.read(0, failure))
                self.assertEqual((failure, [0.01, 0.02], 3), self.read(2, failure))

    def test_an_interrupt_in_a_wait_is_raised(self) -> None:
        interrupt = KeyboardInterrupt()
        self.assertEqual((interrupt, [0.01], 1), self.read(None, interrupt=interrupt))


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

    def test_what_the_reader_accepts_the_writer_writes(self) -> None:
        limit = nesting_depth.DEPTH_LIMIT
        for text in (
            '"\\ud800"',
            '{"\\udfff": 1}',
            '["\\ud83d\\ude00", "\\u0000", "\\uffff"]',
            "[" * limit + "1" + "]" * limit,
            "[" * limit + "]" * limit,
            '{"a": ' * limit + '"x"' + "}" * limit,
            "9" * whole_number.DIGIT_LIMIT,
            "1.7976931348623157e308",
            "-0.0",
        ):
            with self.subTest(text=ascii(text[:24])):
                try:
                    value = loads(text)
                except CONTENT_ERRORS:
                    continue
                json_writer.dumps(value)


class ContentErrorsTests(unittest.TestCase):
    def test_the_family_is_the_refusal_the_parse_error_and_the_codec_error(self) -> None:
        self.assertEqual((JsonRefused, json.JSONDecodeError, UnicodeDecodeError), CONTENT_ERRORS)

    def test_each_member_is_what_one_kind_of_bad_content_raises(self) -> None:
        for text, member in (
            ('{"k": 1, "k": 2}', JsonRefused),
            ("{", json.JSONDecodeError),
            (b'"\xff"', UnicodeDecodeError),
        ):
            with self.subTest(text=text), self.assertRaises(CONTENT_ERRORS) as raised:
                loads(text)
            self.assertIs(member, type(raised.exception))
        for text, member in (('{"k": 1, "k": 2}', JsonRefused), ("{", json.JSONDecodeError)):
            with self.subTest(text=text), self.assertRaises(CONTENT_ERRORS) as raised:
                raw_decode(text)
            self.assertIs(member, type(raised.exception))

    def test_a_file_that_is_not_utf_8_raises_a_member_of_the_family(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "value.json"
            path.write_bytes(b'"\xff"')
            with self.assertRaises(CONTENT_ERRORS) as raised:
                read_file(path)
        self.assertIs(UnicodeDecodeError, type(raised.exception))


class AnswersRequestTests(unittest.TestCase):
    def test_a_refused_object_with_the_request_id_among_its_id_members_answers_it(self) -> None:
        for text in (
            '{"id": 3, "result": {"n": NaN}}',
            '{"id": 3, "result": {"n": 1e400}}',
            '{"id": 3, "result": {"s": "\\ud800"}}',
            '{"id": 3, "result": {"n": ' + "9" * (whole_number.DIGIT_LIMIT + 1) + "}}",
            '{"id": 3, "result": ' + "[" * (deep := nesting_depth.DEPTH_LIMIT + 1) + "]" * deep + "}",
            '{"id": 3, "id": 3, "result": {}}',
            '{"id": 8, "id": 3, "result": {}}',
            b'{"id": 3, "result": {"n": NaN}}',
            '{"id": 3.0, "result": {"n": NaN}}',
            '{"id": 3e0, "result": {"n": NaN}}',
            '{"id": 30E-1, "result": {"n": NaN}}',
            '{"\\u0069d": 3, "result": {"n": NaN}}',
            ' {\n"s": "}]\\"{[", "id" : 3 , "n": NaN}',
        ):
            with self.subTest(text=text[:60]):
                self.assertRaises(JsonRefused, loads, text)
                self.assertTrue(json_reader.answers_request(text, 3))

    def test_a_response_nested_past_the_recursion_limit_is_told_by_its_id_wherever_the_id_stands(self) -> None:
        # The id was read by a parse that recursed into every value, so a response nested past the interpreter's
        # recursion limit answered nothing and its reader waited out its time limit.
        deep = "[" * (sys.getrecursionlimit() * 20) + "]" * (sys.getrecursionlimit() * 20)
        for text in ('{"id": 3, "result": ' + deep + "}", '{"result": {"x": ' + deep + '}, "id": 3}'):
            with self.subTest(at=text[:8]):
                self.assertRaises(JsonRefused, loads, text)
                self.assertTrue(json_reader.answers_request(text, 3))
        self.assertFalse(json_reader.answers_request('{"result": ' + deep + ', "id": 4}', 3))

    def test_a_request_id_is_a_number_equal_to_it_whatever_its_spelling_and_nothing_else(self) -> None:
        # The one rule both a taken response and a refused one are matched by.
        for value in (3, 3.0, loads("3e0"), loads("30E-1")):
            with self.subTest(value=value):
                self.assertTrue(json_reader.is_request_id(value, 3))
        for value in ("3", True, None, 4, 3.5, [3], {"id": 3}):
            with self.subTest(value=value):
                self.assertFalse(json_reader.is_request_id(value, 3))
        self.assertFalse(json_reader.is_request_id(True, 1))
        self.assertFalse(json_reader.is_request_id(False, 0))

    def test_a_refused_line_with_no_id_equal_to_the_request_answers_nothing(self) -> None:
        for text in (
            '{"method": "notifications/message", "params": {"n": NaN}}',
            '{"id": 4, "id": 4, "result": {}}',
            '{"id": "3", "result": {"n": NaN}}',
            '{"id": 3.5, "result": {"n": NaN}}',
            '{"id": true, "result": {"n": NaN}}',
            '{"id": null, "result": {"n": NaN}}',
            '{"id": NaN, "result": {}}',
            '{"id": [3], "result": {"n": NaN}}',
            '{"ids": 3, "result": {"n": NaN}}',
            '{"result": {"id": 3, "n": NaN}}',
            '[{"id": 3, "n": NaN}]',
            "not json",
            "[" * 100000 + "]" * 100000,
        ):
            with self.subTest(text=text[:60]):
                self.assertFalse(json_reader.answers_request(text, 3))

    def test_the_scan_keeps_the_ids_read_before_a_member_that_does_not_scan_and_none_after_it(self) -> None:
        # The scan stops at the first member it cannot read: an id before it still answers, and one after it, or
        # one only a scan that read on past the fault would find, does not.
        for text in (
            '{"id": 3, "x": NaN, "y": [1',
            '{"id": 3, "x": NaN, "y": [1, "a]}',
            '{"id": 3, "x": NaN, "y": "a',
            '{"id": 3, "x',
            '{"id": 3, "x" 1}',
            '{"id": 3 "x": 1}',
            '{"id": 3, "x": , "y": NaN}',
        ):
            with self.subTest(text=text):
                self.assertRaises(CONTENT_ERRORS, loads, text)
                self.assertTrue(json_reader.answers_request(text, 3))
        for text in (
            '{"x": [1, "a], "id": 3}',
            '{"id" 3}',
            '{"id" 33}',
            '{"x": [1, "a]}, "id": 3}',
            '{"x": , "id": 3}',
            '{"x": 1 "id": 3}',
            '{"x": 1 x"id": 3}',
            '{"x" 1, "id": 3}',
            '{"x": [1, "id": 3',
        ):
            with self.subTest(text=text):
                self.assertRaises(CONTENT_ERRORS, loads, text)
                self.assertFalse(json_reader.answers_request(text, 3))

    def test_bytes_are_read_in_the_encoding_json_detects_and_bytes_that_do_not_decode_answer_nothing(self) -> None:
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-8-sig"):
            with self.subTest(encoding=encoding):
                self.assertTrue(json_reader.answers_request('{"id": 3, "n": NaN}'.encode(encoding), 3))
        for data in (b"\xff\xfe\x00\xd8", b'\xff{"id": 3}', b'{"id": 3, "s": "\xff"}'):
            with self.subTest(data=data):
                self.assertFalse(json_reader.answers_request(data, 3))


def test_the_readers_that_raise_the_family_are_named() -> None:
    assert json_reader.CONTENT_READERS == ("loads", "raw_decode", "read_file")
