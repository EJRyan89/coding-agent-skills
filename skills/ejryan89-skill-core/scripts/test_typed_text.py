"""Regression tests for the one read of a text file as its author typed it."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typed_text
from typed_text import CONTENT_ERRORS, TYPED_ENCODING, read_typed

# The bytes a Windows editor may save before UTF-8 text, written out so the read is held to them.
BYTE_ORDER_MARK = b"\xef\xbb\xbf"


class TypedTextTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "title.txt"

    def test_the_codec_is_utf_8_with_its_signature(self) -> None:
        self.assertEqual("utf-8-sig", TYPED_ENCODING)

    def test_a_leading_byte_order_mark_is_dropped(self) -> None:
        self.path.write_bytes(BYTE_ORDER_MARK + b"fix(deployer): keep a thing\n")
        self.assertEqual("fix(deployer): keep a thing\n", read_typed(self.path))

    def test_text_without_a_mark_reads_as_typed_and_only_one_mark_is_dropped(self) -> None:
        self.path.write_bytes("café ✓\r\n".encode())
        self.assertEqual("café ✓\n", read_typed(self.path))
        self.path.write_bytes(BYTE_ORDER_MARK * 2 + b"x")
        self.assertEqual("﻿x", read_typed(self.path))

    def test_text_that_is_not_utf_8_and_a_missing_file_are_refused(self) -> None:
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaises(UnicodeDecodeError):
            read_typed(self.path)
        with self.assertRaises(FileNotFoundError):
            read_typed(self.path.with_name("missing.txt"))


class ContentErrorsTests(unittest.TestCase):
    def test_the_family_is_the_codec_error_and_covers_text_that_is_not_utf_8(self) -> None:
        self.assertEqual((UnicodeDecodeError,), CONTENT_ERRORS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "body.md"
            path.write_bytes(b"\xff")
            with self.assertRaises(CONTENT_ERRORS):
                read_typed(path)


def test_the_readers_that_raise_the_family_are_named() -> None:
    assert typed_text.CONTENT_READERS == ("read_typed",)
