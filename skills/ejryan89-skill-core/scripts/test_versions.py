"""Regression tests for the one reader of a version number."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import versions
import whole_number


class FindTests(unittest.TestCase):
    def test_the_first_version_a_command_prints_is_read_as_its_numbers(self) -> None:
        for output, version in (
            ("git version 2.45.1.windows.1", (2, 45, 1)),
            ("Python 3.11.4", (3, 11, 4)),
            ("7.4", (7, 4)),
            ("ShellCheck - version: 0.10.0", (0, 10, 0)),
            ("tool 1 then 1.0.98 and 2.0", (1, 0, 98)),
            ("v007.010.000", (7, 10, 0)),
        ):
            with self.subTest(output=output):
                self.assertEqual(version, versions.find(output))

    def test_a_version_is_read_up_to_most_numbers_and_needs_at_least_least(self) -> None:
        self.assertEqual((1, 2, 3), versions.find("1.2.3.4"))
        self.assertEqual((1, 2, 3, 4), versions.find("1.2.3.4", most=None))
        self.assertEqual((1, 0, 88), versions.find("GitHub Copilot CLI 1.2 then 1.0.88", least=3, most=3))
        self.assertIsNone(versions.find("GitHub Copilot CLI 1.2", least=3, most=3))
        self.assertEqual((1, 2), versions.find("1.2.", least=2))

    def test_text_holding_no_version_reads_none(self) -> None:
        # The non-ASCII ones are Arabic-Indic one and two, and fullwidth one and two.
        arabic, fullwidth = f"{chr(0x0661)}.{chr(0x0662)}", f"{chr(0xFF11)}.{chr(0xFF12)}"
        for output in ("", "version unknown", "12", "1.", ".1", "1..2", arabic, fullwidth):
            with self.subTest(output=ascii(output)):
                self.assertIsNone(versions.find(output))

    def test_a_number_past_the_digit_limit_is_no_version(self) -> None:
        longest = "1" + "0" * (whole_number.DIGIT_LIMIT - 1)
        self.assertEqual((10 ** (whole_number.DIGIT_LIMIT - 1), 0), versions.find(f"{longest}.0"))
        self.assertIsNone(versions.find(f"{longest}0.0"))


class ReadTests(unittest.TestCase):
    def test_a_text_that_is_a_version_is_read(self) -> None:
        self.assertEqual((2, 1, 295), versions.read("2.1.295"))
        self.assertEqual((1, 2, 3, 4, 5), versions.read("1.2.3.4.5"))
        self.assertEqual((1, 0, 0), versions.read("1.0.0", least=3, most=3))

    def test_anything_around_or_after_the_version_is_refused(self) -> None:
        for text in ("v1.0.0", "1.0.0-rc.1", "1.0.0\n", " 1.0", "1", "1.0.0.0"):
            with self.subTest(text=ascii(text)):
                self.assertIsNone(versions.read(text, least=2, most=3))
        self.assertIsNone(versions.read("1.0", least=3, most=3))


class OrderTests(unittest.TestCase):
    def test_versions_order_by_number_never_as_text(self) -> None:
        self.assertEqual("1.10", versions.latest(["1.10", "1.9"], versions.read))
        self.assertEqual("1.10", versions.latest(["1.9", "1.10"], versions.read))
        self.assertNotEqual(versions.read("1.0.9"), versions.read("1.0.98"))
        self.assertEqual(versions.read("1.01"), versions.read("1.1"))

    def test_latest_is_the_highest_version_and_leaves_out_names_holding_none(self) -> None:
        def tagged(name: str) -> versions.Version | None:
            return versions.read(name.removeprefix("v")) if name.startswith("v") else None

        self.assertEqual("v0.10.0", versions.latest(["v0.9.0", "v0.10.0", "nightly", "v0.2.0"], tagged))
        self.assertEqual("v1.0.0", versions.latest(["v01.0.0", "v1.0.0"], tagged))
        self.assertEqual("v1.0.0", versions.latest(["v1.0.0", "v01.0.0"], tagged))
        self.assertIsNone(versions.latest(["nightly"], tagged))
        self.assertIsNone(versions.latest([], tagged))

    def test_a_version_is_written_dot_separated(self) -> None:
        self.assertEqual("3.11.4", versions.text((3, 11, 4)))
        self.assertEqual("10", versions.text((10,)))
