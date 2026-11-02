"""The review archive: version allocation under a lock, and archive paths."""

from __future__ import annotations

import contextlib
import errno
import os
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from file_presence import NotADirectory
from review_archive import ArchiveError, commit_record, latest_record, list_versions, pull_directory, record_files
from review_records import ReviewRecord, build_record, validate_adapter_result
from test_review_records import valid_adapter_result, valid_request

# The predicates Python 3.14 and later answer False for a path whose stat fails with any OSError, on Path and in
# os.path.
SWALLOWING_PREDICATES = ("exists", "is_file", "is_dir")
SWALLOWING_OS_PATH_PREDICATES = ("exists", "lexists", "isfile", "isdir")


class Refusals:
    """Whether a call on `path` is refused: a call naming it, the first `times` of them when `times` is given."""

    def __init__(self, path: Path, times: int | None) -> None:
        self.path = path
        self.left = times

    def __call__(self, candidate: object = ".") -> bool:
        if not isinstance(candidate, str | os.PathLike) or Path(candidate) != self.path or self.left == 0:
            return False
        if self.left is not None:
            self.left -= 1
        return True


def refusing(original: Callable[..., Any], refuses: Refusals) -> Callable[..., Any]:
    """`original`, raising the permission error a refused stat or listing raises for a path `refuses` refuses."""

    def call(candidate: Any = ".", *args: Any, **kwargs: Any) -> Any:
        if refuses(candidate):
            raise PermissionError(errno.EACCES, "Permission denied", str(candidate))
        return original(candidate, *args, **kwargs)

    return call


def answering_false(original: Callable[..., Any], refuses: Refusals) -> Callable[..., bool]:
    """`original`, answering False for a path `refuses` refuses, as a predicate that swallows the stat's error does."""

    def predicate(candidate: Any, *args: Any, **kwargs: Any) -> bool:
        return False if refuses(candidate) else bool(original(candidate, *args, **kwargs))

    return predicate


@contextlib.contextmanager
def unstatable(path: Path, *, times: int | None = None) -> Iterator[None]:
    """`path` as one whose stat is refused looks to Python 3.14 and later: Path.stat, os.stat, and os.lstat raise a
    permission error naming it, and Path's exists, is_file, and is_dir and os.path's exists, lexists, isfile, and isdir
    answer False. With `times`, only that many of these calls are refused and the rest read the path as it is, as for
    a file another process holds for a moment. Every other path is read as it is."""
    refuses = Refusals(path, times)
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", refusing(Path.stat, refuses)))
        for name in ("stat", "lstat"):
            stack.enter_context(mock.patch.object(os, name, refusing(getattr(os, name), refuses)))
        for name in SWALLOWING_PREDICATES:
            stack.enter_context(mock.patch.object(Path, name, answering_false(getattr(Path, name), refuses)))
        for name in SWALLOWING_OS_PATH_PREDICATES:
            stack.enter_context(mock.patch.object(os.path, name, answering_false(getattr(os.path, name), refuses)))
        yield


@contextlib.contextmanager
def unlistable(folder: Path) -> Iterator[None]:
    """`folder` as one whose listing is refused: os.scandir and os.listdir raise a permission error naming it, which
    Path.iterdir and os.walk raise too, while Path.glob and Path.rglob match nothing in it, as they do on every
    supported Python. Its stat, and every other folder, are read as they are."""
    refuses = Refusals(folder, None)
    with contextlib.ExitStack() as stack:
        for name in ("scandir", "listdir"):
            stack.enter_context(mock.patch.object(os, name, refusing(getattr(os, name), refuses)))
        yield


class ArchiveTests(unittest.TestCase):
    def _record(self, repository: str, number: int, version: int) -> ReviewRecord:
        request = valid_request(repository, number)
        result = validate_adapter_result(
            valid_adapter_result(repository, number),
            expected_repository=repository,
            expected_number=number,
            expected_head_sha="b" * 40,
        )
        return build_record(
            request, result, version=version, policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
        )

    def test_owner_prevents_same_short_name_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = pull_directory(root, "owner-a/shared", 1)
            second = pull_directory(root, "owner-b/shared", 1)
            self.assertNotEqual(first, second)

    def test_versions_are_allocated_under_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit_record(root, "example/one", 12, self._record("example/one", 12, 1), expected_latest_version=None)
            with self.assertRaisesRegex(ArchiveError, "changed concurrently"):
                commit_record(root, "example/one", 12, self._record("example/one", 12, 2), expected_latest_version=None)
            commit_record(root, "example/one", 12, self._record("example/one", 12, 2), expected_latest_version=1)
            latest = latest_record(root, "example/one", 12)
            if latest is None:
                self.fail("the second version is archived")
            self.assertEqual(2, latest["review"]["version"])

    def test_versions_list_every_number_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            names = ["review.json", *(f"review-v{n}.json" for n in range(2, 12)), "review-v100.json"]
            # Not record names: version 1 is review.json, and versions carry no leading zeros.
            names += ["review-v1.json", "review-v0.json", "review-v01.json", "review-v010.json", "review-vx.json"]
            for name in names:
                (directory / name).write_text("{}", encoding="utf-8")
            self.assertEqual([*range(1, 12), 100], list_versions(directory))

    def test_record_files_are_every_pair_of_a_repository_beside_its_version_chains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual([], record_files(root, "example/one"), "a repository with no reviews has none")
            pulls = pull_directory(root, "Example/One", 7).parent
            for name in ("7/review.json", "7/review-v2.json", "7/review-imported.json", "12/review.json"):
                (pulls / name).parent.mkdir(parents=True, exist_ok=True)
                (pulls / name).write_text("{}", encoding="utf-8")
            (pulls / "7" / "review.md").write_text("", encoding="utf-8")
            (pulls / "7" / "notes.json").write_text("{}", encoding="utf-8")
            other = pull_directory(root, "example/other", 7)
            other.mkdir(parents=True)
            (other / "review.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                [
                    pulls / "12/review.json",
                    pulls / "7/review-imported.json",
                    pulls / "7/review-v2.json",
                    pulls / "7/review.json",
                ],
                record_files(root, "EXAMPLE/one"),
            )
            self.assertEqual([1, 2], list_versions(pulls / "7"), "the chain leaves the imported pair out")

    def test_a_stray_file_in_the_pulls_folder_is_passed_over_not_refused(self) -> None:
        # Only a pull request's folder holds records, so a file beside them is not one and not a reason to fail.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = pull_directory(root, "example/one", 7)
            folder.mkdir(parents=True)
            (folder / "review.json").write_text("{}", encoding="utf-8")
            (folder.parent / "notes.txt").write_text("", encoding="utf-8")
            self.assertEqual([folder / "review.json"], record_files(root, "example/one"))

    def test_eleventh_review_follows_the_tenth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [
                commit_record(
                    root,
                    "example/one",
                    12,
                    self._record("example/one", 12, version),
                    expected_latest_version=version - 1 if version > 1 else None,
                )[0]
                for version in range(1, 12)
            ]
            self.assertEqual("review-v11.json", paths[-1].name)
            latest = latest_record(root, "example/one", 12)
            if latest is None:
                self.fail("eleven versions are archived")
            self.assertEqual(11, latest["review"]["version"])

    def test_a_pulls_folder_it_cannot_stat_or_a_file_in_its_place_is_refused_not_read_as_no_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = pull_directory(root, "example/one", 7)
            folder.mkdir(parents=True)
            (folder / "review.json").write_text("{}", encoding="utf-8")
            readers: dict[str, tuple[Path, Callable[[], object]]] = {
                "record_files": (folder.parent, lambda: record_files(root, "example/one")),
                "list_versions": (folder, lambda: list_versions(folder)),
            }
            for name, (path, read) in readers.items():
                with (
                    self.subTest(name, unstatable=True),
                    unstatable(path),
                    self.assertRaises(PermissionError) as raised,
                ):
                    read()
                self.assertEqual(str(path), raised.exception.filename)
            other = Path(temporary) / "other"
            pulls = pull_directory(other, "example/one", 7).parent
            pulls.parent.mkdir(parents=True)
            pulls.write_text("", encoding="utf-8")
            stand_ins: tuple[tuple[str, Callable[[], object]], ...] = (
                ("record_files", lambda: record_files(other, "example/one")),
                ("list_versions", lambda: list_versions(pulls)),
            )
            for name, read in stand_ins:
                with self.subTest(name, file_in_place=True), self.assertRaises(NotADirectory) as refused:
                    read()
                self.assertEqual(str(pulls), refused.exception.filename)

    def test_a_folder_it_cannot_list_is_refused_not_read_as_no_reviews(self) -> None:
        # A glob matches nothing in a folder it cannot list, so the archive lists each folder with iterdir.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = pull_directory(root, "example/one", 7)
            folder.mkdir(parents=True)
            (folder / "review.json").write_text("{}", encoding="utf-8")
            readers: dict[str, tuple[Path, Callable[[], object]]] = {
                "record_files, the pulls folder": (folder.parent, lambda: record_files(root, "example/one")),
                "record_files, a pull request's folder": (folder, lambda: record_files(root, "example/one")),
                "list_versions": (folder, lambda: list_versions(folder)),
            }
            for name, (path, read) in readers.items():
                with self.subTest(name), unlistable(path), self.assertRaises(PermissionError) as raised:
                    read()
                self.assertEqual(str(path), raised.exception.filename)

    def test_a_destination_it_cannot_stat_is_refused_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            folder = pull_directory(root, "example/one", 12)
            folder.mkdir(parents=True)
            markdown = folder / "review.md"
            record = self._record("example/one", 12, 1)
            with unstatable(markdown), self.assertRaises(PermissionError) as raised:
                commit_record(root, "example/one", 12, record, expected_latest_version=None)
            self.assertEqual(str(markdown), raised.exception.filename)
            self.assertEqual([], sorted(path.name for path in folder.iterdir()), "no half of the pair is written")
