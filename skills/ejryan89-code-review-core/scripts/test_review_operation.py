"""A review operation's selection and archive: its selectors, the pull requests it selects, the watermarks it
advances over UTC merge dates, and archiving a reviewer's result as the next review record, with what the archive
reports."""

from __future__ import annotations

import contextlib
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import review_state
from review_archive import commit_record, latest_record, list_versions, pull_directory
from review_io import PersistenceError
from review_operation import (
    ReviewOperationError,
    ReviewOperationFieldError,
    archive_base,
    commit_adapter_result,
    latest_reviewed_heads,
    merged_date,
    parse_pull_selector,
    safe_watermark,
    select_eligible_pulls,
    validate_canary_pull,
    validate_pull,
)
from review_records import build_record, validate_adapter_result
from test_review_records import field_message, field_pattern, valid_adapter_result, valid_request


def frozen_clock(seconds: int) -> contextlib.AbstractContextManager[Any]:
    """The clock `utc_today` reads, stopped at `seconds` since the epoch."""
    return mock.patch.object(review_state, "time", mock.Mock(time=mock.Mock(return_value=seconds)))


class ReviewOperationTests(unittest.TestCase):
    @staticmethod
    def _pull(number: int, state: str = "OPEN", merged_at: str | None = None) -> dict[str, Any]:
        return {
            "number": number,
            "title": f"Pull {number}",
            "url": f"https://github.com/example/one/pull/{number}",
            "state": state,
            "isDraft": False,
            "baseRefName": "main",
            "baseRefOid": "a" * 40,
            "headRefOid": chr(98 + number) * 40,
            "headRefName": f"feature-{number}",
            "mergedAt": merged_at,
        }

    def test_single_pull_canary_selector_is_explicit_and_bounded(self) -> None:
        self.assertEqual(("example/one", 42), parse_pull_selector("Example/One#42"))
        for value in ("example/one", "example/one#0", "#42", "example/one#42#43"):
            with self.subTest(value=value), self.assertRaises(ReviewOperationError):
                parse_pull_selector(value)
        selected = validate_canary_pull(self._pull(42), repository="example/one", number=42)
        self.assertEqual(42, selected["number"])
        draft = self._pull(42)
        draft["isDraft"] = True
        self.assertTrue(validate_canary_pull(draft, repository="example/one", number=42)["isDraft"])
        with self.assertRaisesRegex(ReviewOperationError, "does not match"):
            validate_canary_pull(self._pull(41), repository="example/one", number=42)

    def test_pull_metadata_and_selectors_of_the_wrong_kind_are_field_errors(self) -> None:
        def changed(**fields: Any) -> dict[str, Any]:
            return {**self._pull(42), **fields}

        merged = self._pull(42, "MERGED", "2026-01-05T00:00:00Z")
        cases: tuple[tuple[Any, str, str, str], ...] = (
            ([], "pull", "an object", "an array"),
            (changed(extra=1), "pull", "an object of known fields", 'unknown field "extra"'),
            (
                {key: value for key, value in self._pull(42).items() if key != "title"},
                "pull",
                "an object with field title",
                "an object without it",
            ),
            (changed(number="42"), "pull.number", "an integer of at least 1", 'string "42"'),
            (changed(number=True), "pull.number", "an integer of at least 1", "boolean true"),
            (changed(number=42.0), "pull.number", "an integer of at least 1", "number 42.0"),
            (changed(number=0), "pull.number", "an integer of at least 1", "integer 0"),
            (changed(state="open"), "pull.state", 'one of "OPEN", "MERGED"', 'string "open"'),
            (changed(state=["OPEN"]), "pull.state", 'one of "OPEN", "MERGED"', "an array"),
            (changed(isDraft="false"), "pull.isDraft", "a boolean", 'string "false"'),
            (changed(isDraft=0), "pull.isDraft", "a boolean", "integer 0"),
            (changed(title=7), "pull.title", "a non-empty string", "integer 7"),
            (changed(url=""), "pull.url", "a non-empty string", "an empty string"),
            (changed(headRefOid={}), "pull.headRefOid", "a non-empty string", "an object"),
            (changed(baseRefName=None), "pull.baseRefName", "a non-empty string", "null"),
            (changed(body=["text"]), "pull.body", "a string", "an array"),
            ({**merged, "mergedAt": None}, "pull.mergedAt", "a non-empty string", "null"),
            ({**merged, "mergedAt": 20260105}, "pull.mergedAt", "a non-empty string", "integer 20260105"),
        )
        for value, path, expected, found in cases:
            with self.subTest(path=path, found=found):
                with self.assertRaises(ReviewOperationFieldError) as context:
                    validate_pull(value)
                self.assertEqual(field_message(path, expected, found), str(context.exception))
        selectors: tuple[tuple[Any, str], ...] = (
            (42, "integer 42"),
            (None, "null"),
            ("", "an empty string"),
            (["a/b#1"], "an array"),
        )
        for selector, found in selectors:
            with self.subTest(selector=selector), self.assertRaises(ReviewOperationFieldError) as context:
                parse_pull_selector(selector)
            self.assertEqual(field_message("pull selector", "a non-empty string", found), str(context.exception))

    def test_selection_skips_current_heads_and_old_merges(self) -> None:
        pulls = [
            self._pull(1),
            self._pull(2, "MERGED", "2025-12-01T00:00:00Z"),
            self._pull(3, "MERGED", "2026-01-03T00:00:00Z"),
        ]
        selected = select_eligible_pulls(
            pulls, merged_since=date(2026, 1, 1), reviewed_heads=lambda numbers: {1: "c" * 40}
        )
        self.assertEqual([3], [item["number"] for item in selected])

    def test_selection_reads_the_archive_only_for_pulls_past_the_state_and_watermark_filters(self) -> None:
        draft = self._pull(4)
        draft["isDraft"] = True
        pulls = [
            self._pull(5, "MERGED", "2026-01-01T00:00:00Z"),  # merged on the watermark day
            self._pull(1),
            draft,
            # Merged before the watermark: a later comment or label lists it again, but it stays out.
            self._pull(2, "MERGED", "2025-12-31T23:59:59Z"),
            self._pull(3, "MERGED", "2026-01-03T00:00:00Z"),
        ]
        looked_up: list[list[int]] = []

        def heads(numbers: list[int]) -> dict[int, str]:
            looked_up.append(list(numbers))
            return {5: pulls[0]["headRefOid"]}

        selected = select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads)
        self.assertEqual([[1, 3, 5]], looked_up)
        self.assertEqual([1, 3], [item["number"] for item in selected])

        looked_up.clear()
        forced = select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads, force=True)
        self.assertEqual([], looked_up)
        self.assertEqual([1, 3, 5], [item["number"] for item in forced])

    def test_selection_refuses_duplicate_and_malformed_pulls_before_reading_the_archive(self) -> None:
        def heads(numbers: list[int]) -> dict[int, str]:
            raise AssertionError("the archive is read only after every pull is validated")

        for pulls in ([self._pull(1), self._pull(1)], [self._pull(1), {"number": 2}]):
            with self.subTest(pulls=pulls), self.assertRaises(ReviewOperationError):
                select_eligible_pulls(pulls, merged_since=date(2026, 1, 1), reviewed_heads=heads)

    def test_watermark_retains_unreviewed_merges(self) -> None:
        merged = [self._pull(2, "MERGED", "2026-01-05T00:00:00Z"), self._pull(3, "MERGED", "2026-01-08T00:00:00Z")]
        previous = date(2026, 1, 1)
        self.assertEqual(
            date(2026, 1, 7),
            safe_watermark(previous=previous, today=date(2026, 1, 10), eligible_merged=merged, completed_numbers={2}),
        )

    def test_watermark_uses_the_earliest_pending_merge_date(self) -> None:
        self.assertEqual(
            date(2026, 1, 4),
            safe_watermark(
                previous=date(2026, 1, 1),
                today=date(2026, 1, 10),
                eligible_merged=[
                    self._pull(3, "MERGED", "2026-01-08T00:00:00Z"),
                    self._pull(2, "MERGED", "2026-01-05T23:59:59Z"),
                ],
                completed_numbers=set(),
            ),
        )

    def test_watermark_advances_to_today_without_pending_merges(self) -> None:
        for merged in ([], [self._pull(2, "MERGED", "2026-01-05T00:00:00Z")]):
            with self.subTest(merged=merged):
                self.assertEqual(
                    date(2026, 1, 10),
                    safe_watermark(
                        previous=date(2026, 1, 1),
                        today=date(2026, 1, 10),
                        eligible_merged=merged,
                        completed_numbers={2},
                    ),
                )

    def test_watermark_never_moves_backwards(self) -> None:
        for previous, merged in (
            (date(2026, 1, 11), []),
            (date(2026, 1, 1), [self._pull(2, "MERGED", "2026-01-01T00:00:00Z")]),
        ):
            with self.subTest(previous=previous, merged=merged):
                self.assertEqual(
                    previous,
                    safe_watermark(
                        previous=previous, today=date(2026, 1, 10), eligible_merged=merged, completed_numbers=set()
                    ),
                )

    def test_watermark_refuses_malformed_pending_merge_dates(self) -> None:
        for merged_at in ("not-a-date", "2026-02-30T00:00:00Z", "", None):
            with self.subTest(merged_at=merged_at), self.assertRaises(ReviewOperationError):
                safe_watermark(
                    previous=date(2026, 1, 1),
                    today=date(2026, 1, 10),
                    eligible_merged=[self._pull(2, "MERGED", merged_at)],
                    completed_numbers=set(),
                )

    def test_watermark_validates_completed_merge_metadata(self) -> None:
        pull = self._pull(2, "MERGED", "2026-01-05T00:00:00Z")
        del pull["headRefOid"]
        with self.assertRaisesRegex(
            ReviewOperationFieldError, field_pattern("pull", "an object with field headRefOid", "an object without it")
        ):
            safe_watermark(
                previous=date(2026, 1, 1), today=date(2026, 1, 10), eligible_merged=[pull], completed_numbers={2}
            )

    def test_today_is_the_utc_date_on_either_side_of_utc_midnight(self) -> None:
        # Seconds since the epoch, written out so the expected dates do not come from the code under test. East of
        # UTC the first is already the next local day; west of UTC the second is still the previous one.
        for seconds, expected in ((1773187199, date(2026, 3, 10)), (1773187200, date(2026, 3, 11))):
            with self.subTest(seconds=seconds), frozen_clock(seconds):
                self.assertEqual(expected, review_state.utc_today())

    def test_merge_dates_are_utc_dates_whatever_offset_the_time_carries(self) -> None:
        for merged_at, expected in (
            ("2026-03-10T23:59:59Z", date(2026, 3, 10)),
            ("2026-03-11T00:00:00Z", date(2026, 3, 11)),
            ("2026-03-11T01:30:00+02:00", date(2026, 3, 10)),  # the next local day east of UTC
            ("2026-03-10T20:30:00-05:00", date(2026, 3, 11)),  # the previous local day west of UTC
            ("2026-03-10T23:30:00", date(2026, 3, 10)),  # no offset is read as UTC
        ):
            with self.subTest(merged_at=merged_at):
                self.assertEqual(expected, merged_date(self._pull(2, "MERGED", merged_at)))

    def test_generic_and_specialized_results_share_record_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {
                "protocol_version": 1,
                "mode": "initial",
                "repository": "example/one",
                "pull_number": 12,
                "pull_request": {
                    "title": "Improve behavior",
                    "url": "https://github.com/example/one/pull/12",
                    "base_ref": "main",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                "diff_path": str(root / "diff.patch"),
                "prior_findings": [],
                "github_comments": [],
            }
            request_path = root / "request.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path = root / "result.json"
            result_path.write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
            archive = root / "archive"
            json_path, markdown_path, persisted = commit_adapter_result(
                request_path=request_path,
                result_path=result_path,
                archive_root=archive,
                policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                adapter={
                    "name": "repository-specialist",
                    "scope": "repository",
                    "source_commit": "c" * 40,
                    "source_hashes": {"SKILL.md": "d" * 64},
                },
                base=archive_base(None, []),
            )
            self.assertTrue(json_path.is_file())
            self.assertTrue(markdown_path.is_file())
            self.assertEqual("F001", persisted["findings"][0]["id"])

    def test_archive_retry_reuses_validated_pending_local_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            request = {
                "protocol_version": 1,
                "mode": "initial",
                "repository": "example/one",
                "pull_number": 12,
                "pull_request": {
                    "title": "Improve behavior",
                    "url": "https://github.com/example/one/pull/12",
                    "base_ref": "main",
                    "base_sha": "a" * 40,
                    "head_sha": "b" * 40,
                },
                "diff_path": str(root / "diff.patch"),
                "prior_findings": [],
                "github_comments": [],
            }
            request_path = root / "request.json"
            result_path = root / "result.json"
            request_path.write_text(json.dumps(request), encoding="utf-8")
            result_path.write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
            archive = root / "archive"
            archive.write_text("blocks directory creation", encoding="utf-8")
            local = root / "local"
            arguments: dict[str, Any] = {
                "request_path": request_path,
                "result_path": result_path,
                "archive_root": archive,
                "local_mirror_root": local,
                "policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
                "base": archive_base(None, []),
            }
            with self.assertRaises(PersistenceError):
                commit_adapter_result(**arguments)
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            archive.unlink()
            json_path, _, persisted = commit_adapter_result(**arguments)
            self.assertTrue(json_path.is_file())
            latest = latest_record(local, "example/one", 12)
            if latest is None:
                self.fail("the retried commit is in the local mirror")
            self.assertEqual(latest["artifacts"], persisted["artifacts"])

    @staticmethod
    def _pending_mirror_commit(root: Path) -> dict[str, Any]:
        request = {
            "protocol_version": 1,
            "mode": "initial",
            "repository": "example/one",
            "pull_number": 12,
            "pull_request": {
                "title": "Improve behavior",
                "url": "https://github.com/example/one/pull/12",
                "base_ref": "main",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "diff_path": str(root / "diff.patch"),
            "prior_findings": [],
            "github_comments": [],
        }
        arguments: dict[str, Any] = {
            "request_path": root / "request.json",
            "result_path": root / "result.json",
            "archive_root": root / "archive",
            "local_mirror_root": root / "local",
            "policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
            "base": archive_base(None, []),
        }
        arguments["request_path"].write_text(json.dumps(request), encoding="utf-8")
        arguments["result_path"].write_text(json.dumps(valid_adapter_result()), encoding="utf-8")
        unrelated = build_record(
            valid_request(number=13), valid_adapter_result(number=13), version=1, policy=arguments["policy"]
        )
        for archive in (arguments["archive_root"], arguments["local_mirror_root"]):
            commit_record(archive, "example/one", 13, unrelated, expected_latest_version=None)
        commit_adapter_result(
            **{**arguments, "archive_root": arguments["local_mirror_root"], "local_mirror_root": None}
        )
        return arguments

    def _assert_mirror_refusal(self, arguments: dict[str, Any], message: str) -> None:
        roots = [arguments["archive_root"], arguments["local_mirror_root"]]
        before = [
            {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} for root in roots
        ]
        for files in before:
            self.assertIn(Path("example") / "one" / "pulls" / "13" / "review.json", files)
            self.assertIn(Path("example") / "one" / "pulls" / "13" / "review.md", files)
        with self.assertRaises(ReviewOperationError) as raised:
            commit_adapter_result(**arguments)
        self.assertIs(type(raised.exception), ReviewOperationError)
        self.assertEqual(message, str(raised.exception))
        for root, expected in zip(roots, before, strict=True):
            with self.subTest(archive=root.name):
                self.assertEqual(
                    expected, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
                )

    def test_mirror_refuses_a_reported_version_without_a_valid_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            local = arguments["local_mirror_root"]
            directory = pull_directory(local, "example/one", 12)
            observed = list_versions(directory)
            self.assertEqual([1], observed)
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            (directory / "review.json").unlink()
            self.assertIsNone(latest_record(local, "example/one", 12))
            # The record disappeared after the version scan; only that stale observation is simulated.
            with mock.patch("review_operation.list_versions", return_value=observed) as versions:
                self._assert_mirror_refusal(arguments, "Local mirror reports a version without a valid record")
            versions.assert_called_once_with(directory)

    def test_mirror_refuses_a_different_pending_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            result = valid_adapter_result()
            result["summary"] = "A different review of the same head."
            arguments["result_path"].write_text(json.dumps(result), encoding="utf-8")
            self._assert_mirror_refusal(
                arguments, "Local mirror contains a different pending review; reconcile before retrying"
            )

    def test_mirror_refuses_versions_that_differ_from_the_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            arguments = self._pending_mirror_commit(Path(temporary))
            local = arguments["local_mirror_root"]
            record = build_record(valid_request(), valid_adapter_result(), version=2, policy=arguments["policy"])
            commit_record(local, "example/one", 12, record, expected_latest_version=1)
            self.assertEqual([1, 2], list_versions(pull_directory(local, "example/one", 12)))
            self.assertIsNotNone(latest_record(local, "example/one", 12))
            self.assertEqual([], list_versions(pull_directory(arguments["archive_root"], "example/one", 12)))
            self._assert_mirror_refusal(
                arguments, "Local mirror and archive versions differ; reconcile before committing"
            )

    def test_latest_reviewed_heads_ignores_missing_pull_records(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = validate_adapter_result(
                valid_adapter_result(),
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            )
            record = build_record(
                valid_request(),
                result,
                version=1,
                policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            )
            commit_record(root, "example/one", 12, record, expected_latest_version=None)
            self.assertEqual({12: "b" * 40}, latest_reviewed_heads(root, "example/one", [12, 13]))
