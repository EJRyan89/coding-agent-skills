"""Regression tests for the one atomic replace of a file the skills' scripts share."""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_file import (
    NO_HARD_LINK_ERRNOS,
    ReadOnlyTargetError,
    UnfinishedCopyError,
    UnfinishedReservationError,
    create_bytes,
    is_read_only,
    replace_bytes,
    replace_text,
)
from file_presence import is_occupied
from path_containment import without_extended_prefix

SKIP_ALLOWED = {
    "LinkAtTheTargetTests.setUp": "the races it holds are a symbolic link put at the target, which Windows makes only "
    "with a privilege or developer mode a machine may lack",
}


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

    def test_a_file_system_that_refuses_hard_links_leaves_no_copy_and_never_reads_as_already_there(self) -> None:
        for code, kind in (
            (errno.EPERM, PermissionError),
            (errno.ENOTSUP, OSError),
            (errno.EOPNOTSUPP, OSError),
            (errno.EINVAL, OSError),
            (errno.EXDEV, OSError),
        ):
            with self.subTest(errno=errno.errorcode[code]), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                refusal = OSError(code, os.strerror(code), "copy", "target")
                with (
                    mock.patch.object(os, "link", side_effect=refusal),
                    self.assertRaises(OSError) as raised,
                ):
                    create_bytes(path, b"the whole report\n")
                error = raised.exception
                self.assertIs(kind, type(error))
                self.assertNotIsInstance(error, FileExistsError)
                self.assertEqual(code, error.errno)
                self.assertEqual(str(path), error.filename)
                self.assertEqual(
                    f"[Errno {code}] the file system refused to link the finished copy into place: "
                    f"{os.strerror(code)}: {str(path)!r}",
                    str(error),
                )
                self.assertIs(refusal, error.__cause__)
                self.assertEqual([], names_in(temporary), "nothing at the target and no copy beside it")

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
                self.assertEqual((path, copies[0], linked), (error.target, error.copy, error.made))
                self.assertEqual(1, chmod.call_count, "the copy's mode is set once and never changed after")
                if linked:
                    self.assertEqual(
                        f"{path} is made, but {copies[0]}, its copy beside it, cannot be removed: synthetic unlink",
                        str(error),
                    )
                    self.assertEqual(b"the whole report\n", path.read_bytes(), "the file made keeps its bytes")
                    if os.name != "nt":  # Windows keeps no permission bits but read-only
                        self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
                else:
                    self.assertEqual(
                        f"Cannot remove {copies[0]}, the unfinished copy of {path}: synthetic unlink", str(error)
                    )
                    self.assertFalse(path.exists())
                real_unlink(copies[0])

    def test_once_the_link_is_made_the_file_is_made_whatever_ends_the_copys_removal(self) -> None:
        real_unlink = Path.unlink
        for name, first, raised in (
            ("refused once", OSError(16, "Device or resource busy"), None),
            ("interrupted", KeyboardInterrupt(), KeyboardInterrupt),
        ):
            removals: list[Path] = []

            def unlink(
                self: Path, missing_ok: bool = False, first: BaseException = first, removals: list[Path] = removals
            ) -> None:
                removals.append(self)
                if len(removals) == 1:
                    raise first
                real_unlink(self, missing_ok=missing_ok)

            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with mock.patch.object(Path, "unlink", unlink):
                    if raised is None:
                        create_bytes(path, b"the whole report\n")
                    else:
                        with self.assertRaises(raised):
                            create_bytes(path, b"the whole report\n")
                self.assertEqual(2, len(removals), "the copy's name is removed on the second try")
                self.assertEqual(b"the whole report\n", path.read_bytes())
                self.assertEqual(1, path.stat().st_nlink, "the copy's own name is gone")
                self.assertEqual(["report.md"], names_in(temporary))

    def test_a_copy_name_refused_for_a_while_is_removed_after_short_waits_and_one_refused_throughout_is_named(
        self,
    ) -> None:
        # Holds on every platform, by injected refusals of the copy's removal: Windows refuses to remove the copy's
        # name while another reader holds the new file open, and such a reader lets go after a moment.
        real_unlink = Path.unlink
        cases: tuple[tuple[str, int | None, BaseException | None], ...] = (
            ("refused for a while", 5, None),
            ("refused throughout", None, None),
            ("interrupted in a wait", 1, KeyboardInterrupt()),
        )
        for name, refusals, interrupt in cases:
            removals: list[Path] = []
            waits: list[float] = []

            def unlink(
                self: Path,
                missing_ok: bool = False,
                refusals: int | None = refusals,
                removals: list[Path] = removals,
            ) -> None:
                removals.append(self)
                if refusals is None or len(removals) <= refusals:
                    raise PermissionError(errno.EACCES, "the file is being used by another process", str(self))
                real_unlink(self, missing_ok=missing_ok)

            def wait(seconds: float, waits: list[float] = waits, interrupt: BaseException | None = interrupt) -> None:
                waits.append(seconds)
                if interrupt is not None:
                    raise interrupt

            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                unfinished: list[UnfinishedCopyError] = []
                with mock.patch.object(Path, "unlink", unlink), mock.patch.object(time, "sleep", wait):
                    if refusals is None:
                        with self.assertRaises(UnfinishedCopyError) as raised:
                            create_bytes(path, REPORT)
                        unfinished.append(raised.exception)
                    elif interrupt is not None:
                        with self.assertRaises(KeyboardInterrupt):
                            create_bytes(path, REPORT)
                    else:
                        create_bytes(path, REPORT)
                self.assertEqual(REPORT, path.read_bytes(), "the file made keeps its bytes")
                self.assertLess(sum(waits), 0.5, "the waits are short and few, well inside a hook's time limit")
                for error in unfinished:
                    self.assertTrue(error.made)
                    self.assertEqual(5, len(waits))
                    self.assertEqual(7, len(removals), "a try after each wait, and the clean-up's")
                    self.assertEqual([error.copy] * 7, removals)
                    real_unlink(error.copy)
                if refusals is None:
                    self.assertEqual(1, len(unfinished), "a name no try removes raises UnfinishedCopyError")
                elif interrupt is not None:
                    self.assertEqual(1, len(waits))
                    self.assertEqual(2, len(removals), "the clean-up removes the copy once the wait is interrupted")
                else:
                    self.assertEqual(refusals, len(waits))
                    self.assertEqual(refusals + 1, len(removals), "the try after the last wait removes the name")
                self.assertEqual(1, path.stat().st_nlink, "the copy's own name is gone")
                self.assertEqual(["report.md"], names_in(temporary))

    def test_a_reader_holding_the_new_file_open_for_a_moment_keeps_no_create_from_finishing(self) -> None:
        # The race of a reader that opens the new file as soon as the link puts it in place, with a real handle, which
        # lets go at the create's first wait. Holds on every platform, but only Windows refuses to remove the copy's
        # name while the reader holds the file, since Python opens a file without delete sharing, so only there does
        # the create wait at all, and only there does it fail without its waits, the reader still holding the file.
        real_link, real_sleep = os.link, time.sleep
        reader = contextlib.ExitStack()
        read: list[Path] = []
        waits: list[float] = []

        def link_then_read(source: Any, target: Any, *args: Any, **kwargs: Any) -> None:
            real_link(source, target, *args, **kwargs)
            reader.callback(os.close, os.open(target, os.O_RDONLY))
            read.append(Path(target))

        def let_go_then_wait(seconds: float) -> None:
            waits.append(seconds)
            reader.close()
            real_sleep(seconds)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.md"
            with (
                reader,
                mock.patch.object(os, "link", link_then_read),
                mock.patch.object(time, "sleep", let_go_then_wait),
            ):
                create_bytes(path, REPORT)
            self.assertEqual([path], read, "the reader opened the new file once it was in place")
            self.assertEqual(REPORT, path.read_bytes())
            self.assertEqual(1, path.stat().st_nlink, "the copy's own name is gone")
            self.assertEqual(["report.md"], names_in(temporary))
            self.assertEqual(os.name == "nt", bool(waits), "only Windows refuses the removal while the file is held")

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


REPORT = b"the whole report\n"

# The errnos a hard link is refused with where the file system has none, written here apart from the module's set:
# EPERM on Linux's FAT and exFAT, ENOTSUP on macOS's, EINVAL on Windows', EOPNOTSUPP and EXDEV from a network share.
NO_HARD_LINKS = (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EINVAL, errno.EXDEV)


def refused_link(code: int) -> OSError:
    return OSError(code, os.strerror(code), "copy", "target")


class ReservationTests(unittest.TestCase):
    """create_bytes with allow_empty_reservation, where the file system refuses hard links: an exclusive create
    reserves the target empty and the copy replaces it, so the file is whole or not there, or the reservation empty.
    These mock the link's refusal, so they hold on every platform."""

    def test_the_errnos_that_fall_back_are_those_that_say_the_file_system_has_no_hard_links(self) -> None:
        self.assertEqual(frozenset(NO_HARD_LINKS), NO_HARD_LINK_ERRNOS)

    def test_each_refusal_reserves_the_target_empty_and_replaces_it_whole_leaving_no_copy(self) -> None:
        real_open, real_replace = os.open, os.replace
        for code in NO_HARD_LINKS:
            reservations: list[tuple[int, int]] = []
            replaced: list[bytes] = []

            def reserve(
                opened: Any, flags: int, mode: int = 0o777, reservations: list[tuple[int, int]] = reservations
            ) -> int:
                descriptor = real_open(opened, flags, mode)
                if Path(opened).name == "report.md":  # tempfile.mkstemp opens the copy through os.open too
                    reservations.append((flags, descriptor))
                return descriptor

            def replace(source: Any, target: Any, replaced: list[bytes] = replaced) -> None:
                replaced.append(Path(target).read_bytes())
                real_replace(source, target)

            with self.subTest(errno=errno.errorcode[code]), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "inner" / "report.md"
                with (
                    watched_descriptors() as (opened, closes, reused),
                    mock.patch.object(os, "link", side_effect=refused_link(code)),
                    mock.patch.object(os, "open", reserve),
                    mock.patch.object(os, "replace", replace),
                    mock.patch.object(Path, "unlink", autospec=True) as unlink,
                ):
                    create_bytes(path, REPORT, mode=0o644, allow_empty_reservation=True)
                self.assertEqual([os.O_CREAT | os.O_EXCL | os.O_WRONLY], [flags for flags, _ in reservations])
                self.assertEqual([b""], replaced, "the copy replaced an empty reservation")
                self.assertEqual([], unlink.call_args_list, "the replace left no name of the copy's own to remove")
                self.assertEqual(1, len(opened))
                # The reservation's descriptor may take the number the copy's freed, which the watcher counts too.
                self.assertEqual(opened + [number for _, number in reservations if number in opened], closes)
                self.assertEqual([], reused)
                self.assertEqual(REPORT, path.read_bytes())
                self.assertEqual(1, path.stat().st_nlink)
                if os.name != "nt":  # Windows keeps no permission bits but read-only
                    self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
                self.assertEqual(["report.md"], names_in(str(path.parent)))

    def test_any_other_refusal_of_the_link_is_raised_without_a_reservation(self) -> None:
        for code in (errno.EACCES, errno.ENOSPC, errno.EMLINK):
            with self.subTest(errno=errno.errorcode[code]), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(code)),
                    mock.patch.object(os, "open", wraps=os.open) as opened,
                    self.assertRaises(OSError) as raised,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertEqual(code, raised.exception.errno)
                self.assertRegex(str(raised.exception), "the file system refused to link the finished copy into place")
                self.assertEqual([], [call for call in opened.call_args_list if Path(call.args[0]) == path])
                self.assertEqual([], names_in(temporary), "nothing at the target and no copy beside it")

    def test_an_entry_that_appears_at_the_target_during_the_fallback_is_never_replaced(self) -> None:
        def file(path: Path) -> None:
            path.write_bytes(b"theirs\n")

        def folder(path: Path) -> None:
            path.mkdir()

        def dangling(path: Path) -> None:
            path.symlink_to(path.parent / "missing")

        entries: list[tuple[str, Callable[[Path], None]]] = [("file", file), ("folder", folder)]
        with tempfile.TemporaryDirectory() as temporary, contextlib.suppress(OSError, NotImplementedError):
            dangling(Path(temporary) / "probe")  # a link only where the platform lets one be made
            entries.append(("link that leads nowhere", dangling))
        for name, appear in entries:
            with self.subTest(entry=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"

                def appear_then_refuse(
                    _copy: Any, _target: Any, path: Path = path, appear: Callable[[Path], None] = appear
                ) -> None:
                    # The entry appears after the link is refused and before the reservation is made.
                    appear(path)
                    raise refused_link(errno.EPERM)

                with (
                    mock.patch.object(os, "link", appear_then_refuse),
                    mock.patch.object(os, "replace", wraps=os.replace) as replace,
                    mock.patch.object(os, "open", wraps=os.open) as opened,
                    self.assertRaises(FileExistsError),
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertEqual([], replace.call_args_list)
                # Never asked of the exclusive create, which on Windows follows a link and refuses a folder otherwise.
                self.assertEqual([], [call for call in opened.call_args_list if Path(call.args[0]) == path])
                self.assertEqual(["report.md"], names_in(temporary), "no copy is left beside the target")
                if name == "file":
                    self.assertEqual(b"theirs\n", path.read_bytes())
                elif name == "folder":
                    self.assertTrue(path.is_dir())
                else:
                    self.assertTrue(path.is_symlink())
                    self.assertFalse(path.exists())

    def test_each_failure_in_the_fallback_removes_the_reservation_and_the_copy_and_is_raised_as_it_was(self) -> None:
        real_close = os.close
        reserved: list[int] = []
        real_open = os.open

        def reserve(opened: Any, flags: int, mode: int = 0o777) -> int:
            descriptor = real_open(opened, flags, mode)
            if Path(opened).name == "report.md":  # tempfile.mkstemp opens the copy through os.open too
                reserved.append(descriptor)
            return descriptor

        for name, failure in (
            ("reservation", OSError(errno.EACCES, "synthetic reservation")),
            ("close", OSError(errno.EIO, "synthetic close")),
            ("replace", OSError(errno.EIO, "synthetic replace")),
            ("interrupted replace", KeyboardInterrupt()),
        ):
            reserved.clear()

            def fail_open(opened: Any, flags: int, mode: int = 0o777, failure: BaseException = failure) -> int:
                if Path(opened).name == "report.md":
                    raise failure
                return real_open(opened, flags, mode)

            def close(descriptor: int, failure: BaseException = failure) -> None:
                real_close(descriptor)
                if descriptor in reserved:
                    raise failure

            patches: dict[str, contextlib.AbstractContextManager[Any]] = {
                "reservation": mock.patch.object(os, "open", fail_open),
                "close": mock.patch.object(os, "close", close),
                "replace": mock.patch.object(os, "replace", side_effect=failure),
                "interrupted replace": mock.patch.object(os, "replace", side_effect=failure),
            }
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(errno.ENOTSUP)),
                    mock.patch.object(os, "open", reserve),
                    patches[name],
                    self.assertRaises(type(failure)) as raised,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertIs(failure, raised.exception)
                self.assertEqual([], names_in(temporary), "no reservation at the target and no copy beside it")
                create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertEqual(REPORT, path.read_bytes(), "the next create makes it")

    def test_a_reservation_that_cannot_be_removed_is_named_and_the_copy_is_still_removed(self) -> None:
        real_unlink = os.unlink
        for name, copy_removable in (("copy removed", True), ("copy kept too", False)):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                replace_failure = OSError(errno.EIO, "synthetic replace")

                def unlink(target: Any, *args: Any, path: Path = path, copy_removable: bool = copy_removable) -> None:
                    if Path(target) == path or not copy_removable:
                        raise OSError(errno.EBUSY, "synthetic unlink")
                    real_unlink(target, *args)

                expected = UnfinishedReservationError if copy_removable else UnfinishedCopyError
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(errno.EINVAL)),
                    mock.patch.object(os, "replace", side_effect=replace_failure),
                    mock.patch.object(os, "unlink", unlink),
                    self.assertRaises(expected) as raised,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                error = raised.exception
                reservation = error.failure if isinstance(error, UnfinishedCopyError) else error
                self.assertIsInstance(reservation, UnfinishedReservationError)
                assert isinstance(reservation, UnfinishedReservationError)
                self.assertIsInstance(reservation, OSError)
                self.assertEqual(path, reservation.target)
                self.assertIs(replace_failure, reservation.failure)
                self.assertIs(reservation.removal, reservation.__cause__)
                self.assertEqual(
                    f"{path} was reserved by a create that then failed, and what is there now, its empty reservation "
                    f"or another entry, cannot be removed: [Errno {errno.EBUSY}] synthetic unlink",
                    str(reservation),
                )
                self.assertEqual(b"", path.read_bytes(), "the reservation is left empty where the error says")
                copies = [item for item in Path(temporary).iterdir() if item != path]
                if copy_removable:
                    self.assertEqual([], copies)
                else:
                    assert isinstance(error, UnfinishedCopyError)
                    self.assertEqual([error.copy], copies)
                    self.assertFalse(error.made)
                for item in Path(temporary).iterdir():
                    real_unlink(item)

    def test_a_check_of_the_reservation_that_fails_closes_and_removes_it_and_is_raised_as_it_was(self) -> None:
        real_open, real_fstat, real_close = os.open, os.fstat, os.close
        real_lstat = Path.lstat
        for name, raised in (
            ("fstat", OSError(errno.EIO, "synthetic fstat")),
            ("lstat", OSError(errno.EIO, "synthetic lstat")),
            ("interrupted fstat", KeyboardInterrupt()),
            ("fstat, then a close that fails", OSError(errno.EIO, "synthetic fstat")),
        ):
            reserved: list[int] = []
            closed: list[int] = []

            def reserve(opened: Any, flags: int, mode: int = 0o777, reserved: list[int] = reserved) -> int:
                descriptor = real_open(opened, flags, mode)
                if Path(opened).name == "report.md":  # tempfile.mkstemp opens the copy through os.open too
                    reserved.append(descriptor)
                return descriptor

            def fstat(descriptor: int, reserved: list[int] = reserved, raised: BaseException = raised) -> Any:
                if descriptor in reserved:
                    raise raised
                return real_fstat(descriptor)

            def lstat(target: Path, raised: BaseException = raised) -> os.stat_result:
                if target.name == "report.md":
                    raise raised
                return real_lstat(target)

            def close(
                descriptor: int, reserved: list[int] = reserved, closed: list[int] = closed, case: str = name
            ) -> None:
                if descriptor in reserved:
                    closed.append(descriptor)
                real_close(descriptor)
                if descriptor in reserved and case == "fstat, then a close that fails":
                    # The descriptor is released, as a close that fails releases it, and the close then fails.
                    raise OSError(errno.EIO, "synthetic close")

            check = (
                mock.patch.object(Path, "lstat", lstat) if name == "lstat" else mock.patch.object(os, "fstat", fstat)
            )
            with self.subTest(failure=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(errno.ENOTSUP)),
                    mock.patch.object(os, "open", reserve),
                    mock.patch.object(os, "close", close),
                    check,
                    self.assertRaises(type(raised)) as caught,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertIs(raised, caught.exception)
                self.assertEqual(1, len(reserved))
                self.assertEqual(reserved, closed, "the reservation's descriptor is closed once")
                self.assertEqual([], names_in(temporary), "no reservation at the target and no copy beside it")

    def test_an_exclusive_create_refused_while_an_entry_is_at_the_target_reads_as_already_there(self) -> None:
        # Windows refuses an exclusive create over a folder as PermissionError, not FileExistsError.
        real_open = os.open
        for name, appears in (("a folder appears", True), ("nothing there", False)):
            refusal = PermissionError(errno.EACCES, "synthetic reservation")

            def refused(
                opened: Any, flags: int, mode: int = 0o777, appears: bool = appears, refusal: OSError = refusal
            ) -> int:
                if Path(opened).name != "report.md":  # tempfile.mkstemp opens the copy through os.open too
                    return real_open(opened, flags, mode)
                if appears:
                    Path(opened).mkdir()
                raise refusal

            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(errno.EINVAL)),
                    mock.patch.object(os, "open", refused),
                    self.assertRaises(OSError) as raised,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                if appears:
                    self.assertIs(FileExistsError, type(raised.exception))
                    self.assertEqual((errno.EEXIST, str(path)), (raised.exception.errno, raised.exception.filename))
                    self.assertIs(refusal, raised.exception.__cause__)
                    self.assertTrue(path.is_dir(), "the folder is left as it was")
                    self.assertEqual(["report.md"], names_in(temporary), "no copy beside it")
                else:
                    self.assertIs(refusal, raised.exception)
                    self.assertEqual([], names_in(temporary))

    def test_the_clean_up_removes_whatever_is_left_at_the_target_and_never_misreports_it(self) -> None:
        real_replace = os.replace

        def gone(_source: Any, target: Any) -> None:
            # Another process removed the reservation; the replace then fails, and the clean-up finds nothing.
            Path(target).unlink()
            raise OSError(errno.EIO, "synthetic replace")

        def made_then_interrupted(source: Any, target: Any) -> None:
            # The replace is made, and an interrupt lands as it returns.
            real_replace(source, target)
            raise KeyboardInterrupt

        for name, replace, raised in (
            ("reservation already gone", gone, OSError),
            ("interrupted once the copy is in place", made_then_interrupted, KeyboardInterrupt),
        ):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "report.md"
                with (
                    mock.patch.object(os, "link", side_effect=refused_link(errno.ENOTSUP)),
                    mock.patch.object(os, "replace", replace),
                    self.assertRaises(raised) as caught,
                ):
                    create_bytes(path, REPORT, allow_empty_reservation=True)
                self.assertNotIsInstance(caught.exception, UnfinishedReservationError)
                self.assertEqual([], names_in(temporary), "nothing at the target and no copy beside it")

    def test_an_entry_put_in_the_reservations_place_that_cannot_be_removed_is_reported_as_another_entry(self) -> None:
        failure = OSError(errno.EIO, "synthetic replace")

        def folder_instead(_source: Any, target: Any) -> None:
            # Another process put a folder where the reservation was; the replace then fails.
            Path(target).unlink()
            Path(target).mkdir()
            raise failure

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.md"
            with (
                mock.patch.object(os, "link", side_effect=refused_link(errno.EPERM)),
                mock.patch.object(os, "replace", folder_instead),
                self.assertRaises(UnfinishedReservationError) as raised,
            ):
                create_bytes(path, REPORT, allow_empty_reservation=True)
            error = raised.exception
            self.assertIs(failure, error.failure)
            self.assertIs(error.removal, error.__cause__)
            self.assertIn("its empty reservation or another entry, cannot be removed", str(error))
            self.assertTrue(path.is_dir(), "the folder is left as it was")
            self.assertEqual(["report.md"], names_in(temporary), "no copy beside it")


# How a link reaches the target, each a case of LinkAtTheTargetTests.
OWN_CREATE = "the platform's own create"
FOLLOWING_CREATE = "a create that follows the link"
RETARGETED = "the link retargeted"
FOLLOWED_KEPT = "the followed file cannot be removed"
LINK_REPLACES = "a link replaces the reservation before a failed replace"
CHECK_SWAP = "a link replaces the reservation as its check fails"
# The cases in which the link is put at the target only after the reservation is made.
LATE_LINKS = (LINK_REPLACES, CHECK_SWAP)


class LinkAtTheTargetTests(unittest.TestCase):
    """A symbolic link put at the target while the fallback runs is refused or left alone, never written through or
    removed. The cases with a create that follows the link emulate Windows' exclusive create, so they hold on every
    platform; the platform's own create follows the link only on Windows."""

    def setUp(self) -> None:
        with tempfile.TemporaryDirectory() as probe:
            try:
                (Path(probe) / "link").symlink_to(Path(probe) / "missing")
            except (OSError, NotImplementedError) as unsupported:
                self.skipTest(f"this platform makes no symbolic link here: {unsupported}")

    def prepare(self) -> None:
        """Fresh folders for one case, so what a case that fails leaves behind never reaches the next."""
        folders = [tempfile.TemporaryDirectory() for _ in range(2)]
        for folder in folders:
            self.addCleanup(folder.cleanup)
        # Resolved, so a folder Windows names by its short 8.3 form reads as the long form readlink gives back.
        self.folder = Path(folders[0].name).resolve()
        self.path = self.folder / "report.md"
        self.leads_to = Path(folders[1].name).resolve() / "missing.md"
        self.theirs = Path(folders[1].name).resolve() / "theirs.md"
        self.theirs.write_bytes(b"theirs\n")
        self.kept = OSError(errno.EBUSY, "synthetic unlink", str(self.leads_to))
        self.failure = OSError(errno.EIO, "synthetic replace")
        self.check_failure = OSError(errno.EIO, "synthetic fstat")

    def patches(self, case: str) -> list[contextlib.AbstractContextManager[Any]]:
        real_open, real_occupied, real_unlink = os.open, is_occupied, Path.unlink
        looks: list[Path] = []

        def appear_then_refuse(_copy: Any, _target: Any) -> None:
            if case not in LATE_LINKS:
                self.path.symlink_to(self.leads_to)
            raise refused_link(errno.EPERM)

        def look(target: Path) -> bool:
            # The first look finds nothing, as when the link is put there just after it.
            looks.append(target)
            return False if len(looks) == 1 else real_occupied(target)

        def create(opened: Any, flags: int, mode: int = 0o777) -> int:
            if Path(opened) != self.path or case in (OWN_CREATE, *LATE_LINKS):
                return real_open(opened, flags, mode)
            # Create through the link, as Windows' exclusive create does.
            descriptor = real_open(self.path.resolve(), flags, mode)
            if case == RETARGETED:
                self.path.unlink()
                self.path.symlink_to(self.theirs)
            return descriptor

        def unlink(target: Path, missing_ok: bool = False) -> None:
            if without_extended_prefix(target) == without_extended_prefix(self.leads_to.resolve()):
                raise self.kept
            real_unlink(target, missing_ok=missing_ok)

        def replace(_source: Any, _target: Any) -> None:
            # Another process puts a link where the reservation was; the replace then fails.
            self.path.unlink()
            self.path.symlink_to(self.theirs)
            raise self.failure

        patches: list[contextlib.AbstractContextManager[Any]] = [
            mock.patch.object(os, "link", appear_then_refuse),
            mock.patch("atomic_file.is_occupied", look),
            mock.patch.object(os, "open", create),
        ]
        if case == FOLLOWED_KEPT:
            patches.append(mock.patch.object(Path, "unlink", unlink))
        if case == LINK_REPLACES:
            patches.append(mock.patch.object(os, "replace", replace))
        if case == CHECK_SWAP:
            patches += self.check_swap_patches()
        return patches

    def check_swap_patches(self) -> list[contextlib.AbstractContextManager[Any]]:
        real_fstat, real_close = os.fstat, os.close
        swapped: list[int] = []

        def fstat(descriptor: int) -> os.stat_result:
            if (
                self.path.exists()
                and not self.path.is_symlink()
                and os.path.samestat(real_fstat(descriptor), self.path.lstat())
            ):
                # The check fails, and another process puts a link where the reservation was once it is closed,
                # since Windows removes no file a process holds open.
                swapped.append(descriptor)
                raise self.check_failure
            return real_fstat(descriptor)

        def close(descriptor: int) -> None:
            real_close(descriptor)
            if descriptor in swapped:
                swapped.remove(descriptor)
                self.path.unlink()
                self.path.symlink_to(self.theirs)

        return [mock.patch.object(os, "fstat", fstat), mock.patch.object(os, "close", close)]

    def test_a_link_put_at_the_target_is_refused_or_left_and_never_written_through_or_removed(self) -> None:
        for case in (OWN_CREATE, FOLLOWING_CREATE, RETARGETED, FOLLOWED_KEPT, LINK_REPLACES, CHECK_SWAP):
            with self.subTest(case=case):
                self.prepare()
                error = {FOLLOWED_KEPT: self.kept, LINK_REPLACES: self.failure, CHECK_SWAP: self.check_failure}.get(
                    case
                )
                with contextlib.ExitStack() as stack:
                    for patch in self.patches(case):
                        stack.enter_context(patch)
                    with self.assertRaises(OSError) as raised:
                        create_bytes(self.path, REPORT, allow_empty_reservation=True)
                if error is None:
                    self.assertIs(FileExistsError, type(raised.exception))
                else:
                    self.assertIs(error, raised.exception)
                self.assertTrue(self.path.is_symlink(), "the link is left at the target")
                self.assertEqual(["report.md"], names_in(str(self.folder)), "no copy beside it")
                self.assertEqual(b"theirs\n", self.theirs.read_bytes(), "a file the create did not make is left alone")
                if case in (RETARGETED, FOLLOWED_KEPT):
                    # The file made through the link is left where the error, or the link it lost, says.
                    self.assertEqual(b"", self.leads_to.read_bytes())
                else:
                    self.assertFalse(self.leads_to.exists(), "nothing is left where the link leads")
                # Windows may give a link's target back in its extended-length form, which names the same place.
                self.assertEqual(
                    self.theirs if case in (RETARGETED, *LATE_LINKS) else self.leads_to,
                    without_extended_prefix(self.path.readlink()),
                )


# The exit status hdiutil gives a detach while the volume is busy, EBUSY's number on macOS, as a volume macOS is still
# releasing is for a moment after a test's last file on it closes.
HDIUTIL_BUSY = 16
# The waits, in seconds, before each new try to detach a test volume hdiutil reports busy: a few, doubling, so a volume
# that stays busy is given up on within about ten seconds.
DETACH_BUSY_WAITS = (0.5, 1.0, 2.0, 4.0)


def detach_volume(mount: Path) -> str | None:
    """Detach the test volume at `mount`, trying again after each wait of DETACH_BUSY_WAITS while hdiutil reports it
    busy, and return None once it is detached, or why it is not. It raises nothing hdiutil answers, so a teardown that
    calls it neither fails a check that passed nor hides one that failed."""
    detach = ["hdiutil", "detach", "-quiet", "-force", str(mount)]
    for wait in (*DETACH_BUSY_WAITS, None):
        try:
            result = subprocess.run(detach, check=False, capture_output=True, timeout=120)
        except subprocess.TimeoutExpired:
            return "hdiutil detach did not finish within 120 seconds"
        if result.returncode == 0:
            return None
        if result.returncode != HDIUTIL_BUSY or wait is None:
            stderr = result.stderr.decode("utf-8", "replace").strip()
            return f"hdiutil detach exited with status {result.returncode}: {stderr}"
        time.sleep(wait)
    raise AssertionError("unreachable: the last try returns")


class DetachVolumeTests(unittest.TestCase):
    """The teardown of RealFileSystemWithoutHardLinksTests, held on every platform by a stand-in for hdiutil."""

    def detach(self, statuses: list[int | None]) -> tuple[str | None, list[float], int]:
        """detach_volume where hdiutil answers each try with the next of `statuses`, None for a try that runs out
        its time limit, and the waits it took and the tries it made."""
        answers = iter(statuses)
        tries: list[list[str]] = []
        waits: list[float] = []

        def run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            tries.append(command)
            status = next(answers)
            if status is None:
                raise subprocess.TimeoutExpired(command, 120)
            return subprocess.CompletedProcess(command, status, b"", b"hdiutil: couldn't eject - Resource busy\n")

        with mock.patch.object(subprocess, "run", run), mock.patch.object(time, "sleep", waits.append):
            reason = detach_volume(Path("mount"))
        self.assertEqual([["hdiutil", "detach", "-quiet", "-force", "mount"]] * len(tries), tries)
        return reason, waits, len(tries)

    def test_a_volume_busy_for_a_while_is_detached_once_it_is_free(self) -> None:
        self.assertEqual((None, [0.5, 1.0], 3), self.detach([HDIUTIL_BUSY, HDIUTIL_BUSY, 0]))
        self.assertEqual((None, [], 1), self.detach([0]))

    def test_a_detach_that_never_succeeds_is_returned_as_a_reason_and_never_raised(self) -> None:
        reason, waits, tries = self.detach([HDIUTIL_BUSY] * 5)
        self.assertEqual("hdiutil detach exited with status 16: hdiutil: couldn't eject - Resource busy", reason)
        self.assertEqual(([0.5, 1.0, 2.0, 4.0], 5), (waits, tries))
        self.assertLessEqual(sum(waits), 10, "a volume that stays busy is given up on within about ten seconds")
        reason, waits, tries = self.detach([1])
        self.assertEqual("hdiutil detach exited with status 1: hdiutil: couldn't eject - Resource busy", reason)
        self.assertEqual(([], 1), (waits, tries), "only a busy volume is tried again")
        self.assertEqual(
            ("hdiutil detach did not finish within 120 seconds", [0.5], 2), self.detach([HDIUTIL_BUSY, None])
        )


@unittest.skipUnless(sys.platform == "darwin", "only macOS makes and mounts a FAT image without privileges")
class RealFileSystemWithoutHardLinksTests(unittest.TestCase):
    """create_bytes on a real FAT and exFAT volume, which refuse hard links, attached from a disk image."""

    def test_a_volume_without_hard_links_refuses_by_default_and_reserves_when_allowed(self) -> None:
        # A volume that stays attached keeps its mount folder from being removed, so the folder's clean-up leaves what
        # it cannot remove, and the warning names the volume.
        for file_system in ("MS-DOS", "ExFAT"):
            with (
                self.subTest(file_system=file_system),
                tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary,
            ):
                image = Path(temporary) / "volume.dmg"
                mount = Path(temporary) / "mount"
                mount.mkdir()
                hdiutil = ["hdiutil", "create", "-quiet", "-size", "8m", "-fs", file_system, "-volname", "PROBE"]
                subprocess.run([*hdiutil, str(image)], check=True, capture_output=True, timeout=120)
                attach = ["hdiutil", "attach", "-quiet", "-nobrowse", "-mountpoint", str(mount), str(image)]
                subprocess.run(attach, check=True, capture_output=True, timeout=120)
                try:
                    self._check_volume(mount)
                finally:
                    # Reported apart from the checks, so a detach that fails neither fails the subtest nor hides
                    # the failure of a check.
                    reason = detach_volume(mount)
                    if reason is not None:
                        warnings.warn(
                            f"the {file_system} test volume at {mount} stays attached: {reason}", stacklevel=1
                        )

    def _check_volume(self, volume: Path) -> None:
        def names() -> set[str]:
            # macOS keeps a file's metadata in a "._" file beside it on a volume that has no place for it.
            return {name for name in names_in(str(volume)) if not name.startswith("._")}

        before = names()
        path = volume / "report.md"
        with self.assertRaises(OSError) as raised:
            create_bytes(path, REPORT)
        self.assertNotIsInstance(raised.exception, FileExistsError)
        self.assertIn(raised.exception.errno, NO_HARD_LINKS)
        self.assertEqual(before, names(), "nothing at the target and no copy beside it")
        create_bytes(path, REPORT, allow_empty_reservation=True)
        self.assertEqual(REPORT, path.read_bytes())
        self.assertEqual(before | {"report.md"}, names(), "no copy beside it")
        with self.assertRaises(FileExistsError):
            create_bytes(path, b"next\n", allow_empty_reservation=True)
        self.assertEqual(REPORT, path.read_bytes())
        self.assertEqual(before | {"report.md"}, names(), "no copy beside it")


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
