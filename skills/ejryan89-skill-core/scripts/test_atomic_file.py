"""Regression tests for the one atomic replace of a file the skills' scripts share."""

from __future__ import annotations

import contextlib
import os
import re
import stat
import sys
import tempfile
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_file import (
    ReadOnlyTargetError,
    UnfinishedCopyError,
    create_bytes,
    is_read_only,
    replace_bytes,
    replace_text,
)


def names_in(folder: str) -> list[str]:
    return sorted(item.name for item in Path(folder).iterdir())


@contextlib.contextmanager
def watched_descriptors() -> Iterator[tuple[list[int], list[int], list[int]]]:
    """The descriptors replace_bytes and create_bytes open, each close of one, and each close that found its
    descriptor already closed. Such a close first gets an unrelated file at that number, as another thread's open
    would, so the close lands where a second close would land."""
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
        mock.patch.object(tempfile, "mkstemp", mkstemp),
        mock.patch.object(os, "close", close),
    ):
        yield opened, closes, reused


class ReplaceTests(unittest.TestCase):
    def test_each_failure_closes_the_descriptor_once_leaves_the_target_and_removes_the_copy(self) -> None:
        for name, failure in (
            ("chmod", mock.patch.object(Path, "chmod", side_effect=OSError("synthetic chmod"))),
            ("write", mock.patch.object(os, "write", side_effect=OSError("synthetic write"))),
            ("fsync", mock.patch.object(os, "fsync", side_effect=OSError("synthetic fsync"))),
            ("replace", mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace"))),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with (
                    watched_descriptors() as (opened, closes, reused),
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
                mock.patch.object(os, "write", side_effect=KeyboardInterrupt),
                KeyboardInterrupt,
            ),
            (
                "interrupted replace",
                mock.patch.object(Path, "replace", side_effect=KeyboardInterrupt),
                KeyboardInterrupt,
            ),
            ("defect", mock.patch.object(os, "fsync", side_effect=ValueError("synthetic")), ValueError),
        ):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "state.json"
                path.write_bytes(b"previous\n")
                with watched_descriptors() as (opened, closes, _reused), failure, self.assertRaises(raised):
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
                watched_descriptors() as (opened, closes, reused),
                mock.patch.object(os, "write", short_write),
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
            with watched_descriptors() as (opened, closes, _reused):
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
                    watched_descriptors() as (opened, _closes, _reused),
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


class CreateTests(unittest.TestCase):
    """create_bytes makes a file whole or not at all, and never replaces what is already at its target."""

    def test_a_new_file_holds_every_byte_with_its_mode_and_no_copy_beside_it(self) -> None:
        real_write = os.write

        def short_write(descriptor: int, data: Any) -> int:
            return real_write(descriptor, bytes(data[:1]))

        with tempfile.TemporaryDirectory() as temporary:
            for mode in (0o600, 0o644):
                with self.subTest(mode=oct(mode)):
                    path = Path(temporary) / "inner" / f"report-{mode:o}.md"
                    with (
                        watched_descriptors() as (opened, closes, reused),
                        mock.patch.object(os, "write", short_write),
                    ):
                        create_bytes(path, b"caf\xc3\xa9 \xff\n", mode=mode)
                    self.assertEqual(1, len(opened))
                    self.assertEqual(opened, closes)
                    self.assertEqual([], reused)
                    self.assertEqual(b"caf\xc3\xa9 \xff\n", path.read_bytes())
                    self.assertEqual(1, path.stat().st_nlink, "the copy's own name is gone")
                    if os.name != "nt":  # Windows keeps no permission bits but read-only
                        self.assertEqual(mode, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(["report-600.md", "report-644.md"], names_in(str(Path(temporary) / "inner")))

    def test_an_entry_already_at_the_target_is_never_replaced_and_no_copy_is_left(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / "file.md").write_bytes(b"mine\n")
            (folder / "folder.md").mkdir()
            entries = ["file.md", "folder.md"]
            with contextlib.suppress(OSError, NotImplementedError):  # a link only where the platform lets one be made
                (folder / "nowhere.md").symlink_to(folder / "missing")
                entries.append("nowhere.md")
            for name in entries:
                with self.subTest(entry=name), self.assertRaises(FileExistsError):
                    create_bytes(folder / name, b"next\n")
                self.assertEqual(sorted(entries), names_in(temporary), "no copy is left beside the target")
            self.assertEqual(b"mine\n", (folder / "file.md").read_bytes())
            self.assertTrue((folder / "folder.md").is_dir())

    def test_each_failure_before_the_file_is_made_leaves_nothing_at_the_target_and_removes_the_copy(self) -> None:
        real_write = os.write
        writes: list[int] = []

        def write_then_fail(descriptor: int, data: Any) -> int:
            # The first write lands part of the bytes; the next fails, as a disk that fills part way does.
            writes.append(descriptor)
            if len(writes) > 1:
                raise OSError(28, "No space left on device")
            return real_write(descriptor, bytes(data[:3]))

        for name, failure, raised in (
            ("chmod", mock.patch.object(Path, "chmod", side_effect=OSError("synthetic chmod")), OSError),
            ("write part way", mock.patch.object(os, "write", write_then_fail), OSError),
            ("fsync", mock.patch.object(os, "fsync", side_effect=OSError("synthetic fsync")), OSError),
            ("link", mock.patch.object(os, "link", side_effect=OSError("synthetic link")), OSError),
            ("interrupted write", mock.patch.object(os, "write", side_effect=KeyboardInterrupt), KeyboardInterrupt),
            ("interrupted link", mock.patch.object(os, "link", side_effect=KeyboardInterrupt), KeyboardInterrupt),
        ):
            writes.clear()
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with watched_descriptors() as (opened, closes, _reused), failure, self.assertRaises(raised):
                    create_bytes(path, b"the whole report\n")
                self.assertEqual(1, len(opened))
                self.assertEqual(opened, closes)
                self.assertEqual([], names_in(temporary), "nothing at the target and no copy beside it")
                create_bytes(path, b"the whole report\n")
                self.assertEqual(b"the whole report\n", path.read_bytes(), "the next create makes it")

    def test_a_copy_whose_name_cannot_be_removed_is_named_in_an_oserror(self) -> None:
        real_unlink = Path.unlink
        for name, linked in (("before the link", False), ("after the link", True)):
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                link_failure = mock.patch.object(os, "link", side_effect=OSError("synthetic link"))
                with (
                    contextlib.nullcontext() if linked else link_failure,
                    mock.patch.object(Path, "unlink", side_effect=OSError("synthetic unlink")),
                    mock.patch.object(Path, "chmod", wraps=Path.chmod, autospec=True) as chmod,
                    self.assertRaises(UnfinishedCopyError) as raised,
                ):
                    create_bytes(path, b"the whole report\n", mode=0o644)
                copies = [item for item in Path(temporary).iterdir() if item.name != "report.md"]
                self.assertEqual(1, len(copies), "the copy stays where the error says")
                error = raised.exception
                self.assertEqual(
                    f"Cannot remove {copies[0]}, the unfinished copy of {path}: synthetic unlink", str(error)
                )
                self.assertEqual((path, copies[0]), (error.target, error.copy))
                self.assertEqual(1, chmod.call_count, "the copy's mode is set once and never changed after")
                if linked:
                    self.assertEqual(b"the whole report\n", path.read_bytes(), "the file made keeps its bytes")
                    if os.name != "nt":  # Windows keeps no permission bits but read-only
                        self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
                else:
                    self.assertFalse(path.exists())
                real_unlink(copies[0])

    def test_a_mode_its_owner_may_not_write_is_refused_before_a_copy_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.md"
            with (
                watched_descriptors() as (opened, _closes, _reused),
                self.assertRaisesRegex(ValueError, "^create_bytes needs a mode its owner may write, not 444$"),
            ):
                create_bytes(path, b"next\n", mode=0o444)
            self.assertEqual([], opened, "no copy was made")
            self.assertEqual([], names_in(temporary))


class ReadOnlyTargetTests(unittest.TestCase):
    """A read-only target is refused before a copy exists, the same way on every platform: Windows cannot replace
    one, and Linux would replace it silently. TemporaryDirectory removes a read-only file it holds."""

    def read_only_file(self, folder: str, name: str) -> Path:
        path = Path(folder) / name
        path.write_bytes(b"previous\n")
        path.chmod(stat.S_IREAD)
        return path

    def test_a_read_only_target_is_refused_before_a_copy_exists_and_left_as_it_was(self) -> None:
        for mode_of in ("its own mode", "the default mode"):
            with self.subTest(mode=mode_of), tempfile.TemporaryDirectory() as temporary:
                path = self.read_only_file(temporary, "Foo.cs")
                mode = stat.S_IMODE(path.stat().st_mode) if mode_of == "its own mode" else 0o600
                with (
                    mock.patch.object(tempfile, "mkstemp", wraps=tempfile.mkstemp) as mkstemp,
                    self.assertRaises(ReadOnlyTargetError) as raised,
                ):
                    replace_bytes(path, b"next\n", mode=mode)
                mkstemp.assert_not_called()
                self.assertIsInstance(raised.exception, PermissionError)
                self.assertEqual(path, raised.exception.target)
                self.assertEqual("the file is read-only; make it writable to replace it", raised.exception.strerror)
                self.assertEqual(str(path), raised.exception.filename)
                self.assertEqual(b"previous\n", path.read_bytes())
                self.assertEqual(["Foo.cs"], names_in(temporary))
                self.assertFalse(path.stat().st_mode & stat.S_IWUSR, "the target keeps its read-only mode")

    def test_a_target_read_only_only_through_a_link_is_read_only(self) -> None:
        writable = os.stat_result((stat.S_IFLNK | 0o777, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        read_only = os.stat_result((stat.S_IFREG | 0o444, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        for name, link, target, expected in (
            ("writable link to a read-only file", writable, read_only, True),
            ("writable link to a writable file", writable, writable, False),
            ("read-only entry", read_only, writable, True),
            ("link that leads nowhere", writable, FileNotFoundError(), False),
        ):

            def fake_stat(path: Any, *, follow_symlinks: bool = True, link: Any = link, target: Any = target) -> Any:
                result = target if follow_symlinks else link
                if isinstance(result, BaseException):
                    raise result
                return result

            with self.subTest(case=name), mock.patch.object(Path, "stat", fake_stat):
                self.assertIs(expected, is_read_only(Path("entry")))
        with tempfile.TemporaryDirectory() as temporary:
            self.assertFalse(is_read_only(Path(temporary) / "missing"))

    def test_a_copy_given_a_read_only_mode_is_made_writable_and_removed_when_the_replace_fails(self) -> None:
        # Linux removes a read-only file as readily as any other and Windows refuses to, so the mode the copy has as
        # it is removed is read on every platform rather than left to Windows alone to show.
        modes: list[int] = []
        real_unlink = Path.unlink

        def unlink(self: Path, missing_ok: bool = False) -> None:
            modes.append(stat.S_IMODE(self.stat().st_mode))
            real_unlink(self, missing_ok=missing_ok)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            path.write_bytes(b"previous\n")
            with (
                mock.patch.object(Path, "replace", side_effect=OSError("synthetic replace")),
                mock.patch.object(Path, "unlink", unlink),
                self.assertRaisesRegex(OSError, "^synthetic replace$") as raised,
            ):
                replace_bytes(path, b"next\n", mode=0o444)
            self.assertIs(OSError, type(raised.exception))
            self.assertEqual(1, len(modes))
            self.assertTrue(modes[0] & stat.S_IWUSR, f"the copy is writable as it is removed: {modes[0]:o}")
            self.assertEqual(b"previous\n", path.read_bytes())
            self.assertEqual(["state.json"], names_in(temporary), "the read-only copy is removed")

    def test_a_new_file_may_be_given_a_read_only_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.json"
            replace_bytes(path, b"next\n", mode=0o444)
            self.assertEqual(b"next\n", path.read_bytes())
            self.assertFalse(path.stat().st_mode & stat.S_IWUSR)
            self.assertEqual(["state.json"], names_in(temporary))
