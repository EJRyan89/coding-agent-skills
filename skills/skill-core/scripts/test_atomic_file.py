"""Regression tests for the one atomic replace of a file the skills' scripts share."""

from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import atomic_file
from atomic_file import UnfinishedCopyError, replace_bytes, replace_text


def names_in(folder: str) -> list[str]:
    return sorted(item.name for item in Path(folder).iterdir())


class ReplaceTests(unittest.TestCase):
    @contextlib.contextmanager
    def watched_descriptors(self) -> Iterator[tuple[list[int], list[int], list[int]]]:
        """The descriptors replace_bytes opens, each close of one, and each close that found its descriptor already
        closed. Such a close first gets an unrelated file at that number, as another thread's open would, so the
        close lands where a second close would land."""
        opened: list[int] = []
        closes: list[int] = []
        reused: list[int] = []
        real_mkstemp = tempfile.mkstemp
        real_close = os.close

        def mkstemp(*args: Any, **kwargs: Any) -> tuple[int, str]:
            descriptor, name = real_mkstemp(*args, **kwargs)
            opened.append(descriptor)
            return descriptor, name

        def close(descriptor: int) -> None:
            if descriptor in opened:
                closes.append(descriptor)
                try:
                    os.fstat(descriptor)
                except OSError:
                    unrelated = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(unrelated, descriptor)
                    real_close(unrelated)
                    reused.append(descriptor)
            real_close(descriptor)

        with (
            mock.patch.object(atomic_file.tempfile, "mkstemp", mkstemp),
            mock.patch.object(atomic_file.os, "close", close),
        ):
            yield opened, closes, reused

    def test_each_failure_closes_the_descriptor_once_leaves_the_target_and_removes_the_copy(self) -> None:
        for name, failure in (
            ("chmod", mock.patch.object(Path, "chmod", side_effect=OSError("synthetic chmod"))),
            ("write", mock.patch.object(atomic_file.os, "write", side_effect=OSError("synthetic write"))),
            ("fsync", mock.patch.object(atomic_file.os, "fsync", side_effect=OSError("synthetic fsync"))),
            ("replace", mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace"))),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with (
                    self.watched_descriptors() as (opened, closes, reused),
                    failure,
                    self.assertRaisesRegex(OSError, f"^synthetic {name}$") as raised,
                ):
                    replace_text(path, "next\n")
                self.assertIs(OSError, type(raised.exception), "the failure is raised as it was")
                self.assertEqual(1, len(opened))
                self.assertEqual(opened, closes)
                self.assertEqual([], reused)
                with self.assertRaises(OSError):
                    os.fstat(opened[0])
                self.assertEqual(b"previous\n", path.read_bytes())
                self.assertEqual(["state.json"], names_in(temporary))

    def test_a_failure_that_is_not_an_oserror_removes_the_copy_too(self) -> None:
        for name, failure, raised in (
            (
                "interrupted write",
                mock.patch.object(atomic_file.os, "write", side_effect=KeyboardInterrupt),
                KeyboardInterrupt,
            ),
            (
                "interrupted replace",
                mock.patch.object(Path, "replace", side_effect=KeyboardInterrupt),
                KeyboardInterrupt,
            ),
            ("defect", mock.patch.object(atomic_file.os, "fsync", side_effect=ValueError("synthetic")), ValueError),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with self.watched_descriptors() as (opened, closes, _reused), failure, self.assertRaises(raised):
                    replace_text(path, "next\n")
                self.assertEqual(opened, closes)
                self.assertEqual(b"previous\n", path.read_bytes())
                self.assertEqual(["state.json"], names_in(temporary), "no copy is left beside the target")

    def test_success_closes_the_descriptor_once_and_writes_every_byte(self) -> None:
        real_write = os.write

        def short_write(descriptor: int, data: Any) -> int:
            return real_write(descriptor, bytes(data[:1]))

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                self.watched_descriptors() as (opened, closes, reused),
                mock.patch.object(atomic_file.os, "write", short_write),
            ):
                replace_text(path, "café\nnext\n")
            self.assertEqual(1, len(opened))
            self.assertEqual(opened, closes)
            self.assertEqual([], reused)
            self.assertEqual("café\nnext\n".encode(), path.read_bytes())
            self.assertEqual(["state.json"], names_in(temporary))

    def test_bytes_that_are_not_utf8_replace_the_file_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "condition.txt"
            path.write_bytes(b"previous\n")
            with self.watched_descriptors() as (opened, closes, _reused):
                replace_bytes(path, b"caf\xc3\xa9 \xff\xc3")
            self.assertEqual(opened, closes)
            self.assertEqual(b"caf\xc3\xa9 \xff\xc3", path.read_bytes())
            self.assertEqual(["condition.txt"], names_in(temporary))

    def test_a_missing_folder_is_made(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "inner" / "deeper" / "state.json"
            replace_text(path, "next\n")
            self.assertEqual(b"next\n", path.read_bytes())

    def test_a_folder_that_cannot_be_made_or_text_utf8_cannot_encode_fails_before_a_copy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            blocker = Path(temporary) / "blocker"
            blocker.write_bytes(b"a file where the folder would go\n")
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            for name, target, content, error in (
                ("mkdir", blocker / "inner" / "state.json", "next\n", OSError),
                ("lone surrogate", path, "next \ud800\n", UnicodeEncodeError),
            ):
                with (
                    self.subTest(failure=name),
                    self.watched_descriptors() as (opened, _closes, _reused),
                    self.assertRaises(error),
                ):
                    replace_text(target, content)
                self.assertEqual([], opened, "no copy was made")
            self.assertEqual(b"previous\n", path.read_bytes())
            self.assertEqual(["blocker", "state.json"], names_in(temporary))

    def test_the_replacement_has_the_mode_it_is_asked_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for mode in (0o600, 0o644):
                with self.subTest(mode=oct(mode)):
                    path = Path(temporary) / f"state-{mode:o}.json"
                    replace_text(path, "next\n", mode=mode)
                    self.assertEqual(b"next\n", path.read_bytes())
                    if os.name != "nt":  # Windows keeps no permission bits but read-only
                        self.assertEqual(mode, path.stat().st_mode & 0o777)

    def test_a_copy_that_cannot_be_removed_is_named_in_an_oserror(self) -> None:
        for name, failure in (
            ("refused replace", OSError("synthetic replace")),
            ("interrupted replace", KeyboardInterrupt()),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with (
                    mock.patch.object(Path, "replace", side_effect=failure),
                    mock.patch.object(Path, "unlink", side_effect=OSError("synthetic unlink")),
                    self.assertRaises(UnfinishedCopyError) as raised,
                ):
                    replace_text(path, "next\n")
                copies = [item for item in Path(temporary).iterdir() if item.name != "state.json"]
                self.assertEqual(1, len(copies), "the copy stays where the error says")
                error = raised.exception
                self.assertIsInstance(error, OSError)
                self.assertEqual(
                    f"Cannot remove {copies[0]}, the unfinished copy of {path}: synthetic unlink", str(error)
                )
                self.assertEqual((path, copies[0]), (error.target, error.copy))
                self.assertIs(failure, error.failure)
                self.assertIs(error.removal, error.__cause__)
                self.assertRegex(str(error.removal), re.escape("synthetic unlink"))
                self.assertIs(failure, error.removal.__context__)
                self.assertEqual(b"previous\n", path.read_bytes())
