"""Regression tests for the one list of the names Windows opens as a device rather than a file."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from device_names import RESERVED_DEVICE_NAMES, is_reserved_device_name


class DeviceNameTests(unittest.TestCase):
    def test_the_list_is_the_consoles_the_printer_the_null_device_and_every_port(self) -> None:
        self.assertEqual(
            {
                "con",
                "prn",
                "aux",
                "nul",
                "conin$",
                "conout$",
                "com0",
                "com1",
                "com2",
                "com3",
                "com4",
                "com5",
                "com6",
                "com7",
                "com8",
                "com9",
                "com¹",
                "com²",
                "com³",
                "lpt0",
                "lpt1",
                "lpt2",
                "lpt3",
                "lpt4",
                "lpt5",
                "lpt6",
                "lpt7",
                "lpt8",
                "lpt9",
                "lpt¹",
                "lpt²",
                "lpt³",
            },
            RESERVED_DEVICE_NAMES,
        )

    def test_a_device_name_in_any_case_alone_or_before_an_extension_is_reserved(self) -> None:
        for name in (
            "con",
            "PRN",
            "Aux",
            "nul.txt",
            "COM1.tar.gz",
            "lpt9",
            "aux .txt",
            "CONIN$",
            "conout$.log",
            "COM¹.py",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_reserved_device_name(name))

    def test_a_name_that_only_starts_like_one_is_not(self) -> None:
        for name in ("console", "con-alpha", "nul-check", "com", "com10", "lpt10", "aux2", ".con", " con", "con_.md"):
            with self.subTest(name=name):
                self.assertFalse(is_reserved_device_name(name))
