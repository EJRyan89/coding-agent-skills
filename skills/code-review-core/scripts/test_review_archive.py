"""The review archive: version allocation under a lock, and archive paths."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_archive import ArchiveError, commit_record, latest_record, list_versions, pull_directory, record_files
from review_records import ReviewRecord, build_record, validate_adapter_result
from test_review_records import valid_adapter_result, valid_request


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
            for version in range(1, 12):
                json_path, _, _ = commit_record(
                    root,
                    "example/one",
                    12,
                    self._record("example/one", 12, version),
                    expected_latest_version=version - 1 if version > 1 else None,
                )
            self.assertEqual("review-v11.json", json_path.name)
            latest = latest_record(root, "example/one", 12)
            if latest is None:
                self.fail("eleven versions are archived")
            self.assertEqual(11, latest["review"]["version"])
