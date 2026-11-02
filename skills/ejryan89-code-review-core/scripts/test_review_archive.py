"""The review archive: version allocation under a lock, and archive paths."""

from __future__ import annotations

import contextlib
import errno
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

# The predicates Python 3.14 and later answer False for a path whose stat fails with any OSError.
SWALLOWING_PREDICATES = ("exists", "is_file", "is_dir")


@contextlib.contextmanager
def unstatable(path: Path, *, times: int | None = None) -> Iterator[None]:
    """`path` as one whose stat is refused looks to Python 3.14 and later: Path.stat raises a permission error naming
    it, and exists, is_file, and is_dir answer False. With `times`, only that many of these calls are refused and
    the rest read the path as it is, as for a file another process holds for a moment. Every other path is read as it
    is."""
    stat = Path.stat
    predicates = {name: getattr(Path, name) for name in SWALLOWING_PREDICATES}
    refusals = [0]

    def refusing(candidate: Path) -> bool:
        if candidate != path or (times is not None and refusals[0] >= times):
            return False
        refusals[0] += 1
        return True

    def refused(self: Path, *args: Any, **kwargs: Any) -> Any:
        if refusing(self):
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return stat(self, *args, **kwargs)

    def answering(name: str) -> Any:
        def predicate(self: Path, *args: Any, **kwargs: Any) -> bool:
            return False if refusing(self) else bool(predicates[name](self, *args, **kwargs))

        return predicate

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(Path, "stat", refused))
        for name in SWALLOWING_PREDICATES:
            stack.enter_context(mock.patch.object(Path, name, answering(name)))
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
