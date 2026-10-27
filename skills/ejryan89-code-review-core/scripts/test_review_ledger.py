"""The finding ledger: how findings are counted across review versions and within one review."""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_review_fixture as review_fixture
from review_archive import archive_head, commit_record, current_ledger, pull_directory, pull_records, record_paths
from review_config import verdict_policy
from review_operation import archive_base, commit_adapter_result, reviewed_head
from review_records import (
    AdapterResult,
    LedgerEntry,
    PriorDisposition,
    RecordError,
    RecordFieldError,
    ReviewRecord,
    build_record,
    calculate_verdict,
    carried_findings,
    display_id,
    finding_reference,
    flagged_entries,
    joined_source,
    ledger_display_id,
    ledger_history,
    ledger_id,
    ledger_summary,
    render_markdown,
    source_parts,
    validate_adapter_result,
    validate_record,
)
from review_vocabularies import Disposition, LedgerState, Severity

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

POLICY = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
VERDICT_POLICY = verdict_policy(POLICY)
ADAPTER: dict[str, Any] = {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}
NO_OPEN = {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}


def field_pattern(path: str, expected: str, found: str) -> str:
    """The whole message a field of the wrong kind raises, built from literal kinds rather than the reader's own."""
    return f"^{re.escape(f'{path} must be {expected}, found {found}')}$"


def finding(key: str, severity: str = "SHOULD_FIX", line: int = 10, **extra: object) -> dict[str, Any]:
    return {
        "candidate_key": key,
        "severity": severity,
        "category": "Correctness",
        "path": "src/file.cs",
        "line": line,
        "title": f"Problem {key}",
        "body": f"Problem {key} breaks the rule.",
        "evidence": "The added line shows it.",
        "source": "generic",
        **extra,
    }


def reviewed_ledger(archive: Path) -> dict[str, Any]:
    """The ledger summary of the pull request's reviewed head, which the test expects to exist."""
    head = reviewed_head(archive, "example/one", 12)
    if head is None:
        raise AssertionError("example/one#12 has no reviewed head")
    ledger: dict[str, Any] = head["ledger"]
    return ledger


def disposition(identifier: str, value: Disposition) -> PriorDisposition:
    return {"finding_id": identifier, "disposition": value, "rationale": "Checked against the current code."}


def adapter_result(findings: Sequence[Any] = (), dispositions: Sequence[PriorDisposition] = ()) -> Any:
    return {
        "protocol_version": 1,
        "repository": "example/one",
        "pull_number": 12,
        "head_sha": "b" * 40,
        "summary": "Reviewed.",
        "reviewer": "fixture-reviewer",
        "status": "complete",
        "findings": list(findings),
        "prior_dispositions": list(dispositions),
        "usage": None,
    }


def record_request(mode: str = "initial") -> dict[str, Any]:
    return {
        "repository": "example/one",
        "pull_number": 12,
        "pull_url": "https://github.com/example/one/pull/12",
        "title": "Improve behavior",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "mode": mode,
        "adapter": ADAPTER,
    }


def validate(result: object, prior: Sequence[dict[str, Any]] = ()) -> AdapterResult:
    return validate_adapter_result(
        result,
        expected_repository="example/one",
        expected_number=12,
        expected_head_sha="b" * 40,
        prior_ids=[item["id"] for item in prior],
        prior_severities={item["id"]: item["severity"] for item in prior},
    )


def entry(
    version: int,
    identifier: str,
    severity: Severity,
    state: LedgerState,
    judged_in: int,
    dispositions: Sequence[tuple[int, Disposition]] = (),
    repeats: Sequence[tuple[int, str]] = (),
) -> LedgerEntry:
    return {
        "version": version,
        "id": identifier,
        "severity": severity,
        "category": "Correctness",
        "state": state,
        "judged_in": judged_in,
        "dispositions": [{"version": v, "disposition": d} for v, d in dispositions],
        "repeats": [{"version": v, "id": i} for v, i in repeats],
    }


class ArchiveFixture:
    """Commits review versions of example/one#12 through the same path `finalize` uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.archive = root / "archive"
        self.count = 0

    def prior(self) -> list[dict[str, Any]]:
        return carried_findings(pull_records(self.archive, "example/one", 12))

    def commit(
        self,
        findings: Sequence[dict[str, Any]] = (),
        dispositions: Sequence[PriorDisposition] = (),
        *,
        mode: str = "re-review",
        used: str = "incremental",
    ) -> ReviewRecord:
        self.count += 1
        prior = self.prior() if mode == "re-review" else []
        request = {
            "protocol_version": 1,
            "mode": mode,
            "repository": "example/one",
            "pull_number": 12,
            "pull_request": {
                "title": "Improve behavior",
                "url": "https://github.com/example/one/pull/12",
                "base_ref": "main",
                "base_sha": "a" * 40,
                "head_sha": "b" * 40,
            },
            "diff_path": str(self.root / "diff.patch"),
            "prior_findings": prior,
            "github_comments": [],
        }
        request_path = self.root / f"request-{self.count}.json"
        result_path = self.root / f"result-{self.count}.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        result_path.write_text(json.dumps(adapter_result(findings, dispositions)), encoding="utf-8")
        versions = pull_records(self.archive, "example/one", 12)
        scope: Any = None
        if mode == "re-review":
            scope = {
                "requested": used,
                "used": used,
                "reason": "requested",
                "since_version": len(versions),
                "files_changed": 0,
                "files_total": 1,
                "lines_changed": 0,
                "lines_total": 3,
            }
        _, _, record = commit_adapter_result(
            request_path=request_path,
            result_path=result_path,
            archive_root=self.archive,
            policy=POLICY,
            adapter=ADAPTER,
            base=archive_base(*archive_head(self.archive, "example/one", 12)),
            scope=scope,
        )
        return record


class CarriedFindingTests(unittest.TestCase):
    def test_a_still_present_must_fix_requests_changes_in_a_later_version_that_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            first = reviews.commit([finding("leak", "MUST_FIX")], mode="initial")
            self.assertEqual("CHANGES_REQUESTED", first["review"]["verdict"])
            carried = reviews.prior()
            self.assertEqual(
                [
                    {
                        "id": "v1:F001",
                        "severity": "MUST_FIX",
                        "category": "Correctness",
                        "path": "src/file.cs",
                        "line": 10,
                        "title": "Problem leak",
                        "body": "Problem leak breaks the rule.",
                    }
                ],
                carried,
            )
            second = reviews.commit([], [disposition("v1:F001", "still_present")])
            self.assertEqual("CHANGES_REQUESTED", second["review"]["verdict"])
            # Version 2 reported no findings, so only the ledger can still offer the problem to version 3.
            self.assertEqual(["v1:F001"], [item["id"] for item in reviews.prior()])
            third = reviews.commit([], [disposition("v1:F001", "still_present")])
            self.assertEqual(3, third["review"]["version"])
            self.assertEqual([], third["findings"])
            self.assertEqual("CHANGES_REQUESTED", third["review"]["verdict"])
            self.assertEqual(
                [entry(1, "F001", "MUST_FIX", "open", 3, [(2, "still_present"), (3, "still_present")])], third["ledger"]
            )
            self.assertEqual(
                {"open": {**NO_OPEN, "MUST_FIX": 1}, "repeats": 0, "addressed": 0, "since": 1, "version": 3},
                ledger_summary(third["ledger"], 3),
            )
            self.assertEqual(ledger_summary(third["ledger"], 3), reviewed_ledger(reviews.archive))
            fourth = reviews.commit([], [disposition("v1:F001", "addressed")])
            self.assertEqual("APPROVED", fourth["review"]["verdict"])
            self.assertEqual("closed", fourth["ledger"][0]["state"])
            self.assertEqual(
                {"open": NO_OPEN, "repeats": 0, "addressed": 1, "since": None, "version": 4},
                ledger_summary(fourth["ledger"], 4),
            )
            self.assertEqual([], reviews.prior())

    def test_an_unverified_entry_is_offered_again_but_does_not_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            reviews.commit([finding("leak", "MUST_FIX")], mode="initial")
            second = reviews.commit([], [disposition("v1:F001", "unable_to_verify")])
            self.assertEqual("APPROVED", second["review"]["verdict"])
            self.assertEqual("unverified", second["ledger"][0]["state"])
            self.assertEqual(["v1:F001"], [item["id"] for item in reviews.prior()])

    def test_a_full_re_review_links_a_re_reported_problem_to_its_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            reviews.commit([finding("boundary", "SHOULD_FIX")], mode="initial")
            second = reviews.commit(
                [finding("again", "SHOULD_FIX", line=12, repeats="v1:F001"), finding("other", "SUGGESTION", 30)],
                [disposition("v1:F001", "still_present")],
                used="full",
            )
            self.assertEqual(
                [
                    entry(1, "F001", "SHOULD_FIX", "open", 2, [(2, "still_present")], [(2, "F001")]),
                    entry(2, "F002", "SUGGESTION", "open", 2),
                ],
                second["ledger"],
            )
            self.assertEqual({"version": 1, "id": "F001"}, second["findings"][0]["repeats"])
            # The entry is offered again where it was last reported.
            self.assertEqual(
                [("v1:F001", 12), ("v2:F002", 30)], [(item["id"], item["line"]) for item in reviews.prior()]
            )
            markdown = render_markdown(
                second, record_payload_hash="0" * 64, prior_records=pull_records(reviews.archive, "example/one", 12)[:1]
            )
            # The re-reported problem is shown inside the entry it repeats, not as a finding of its own.
            self.assertIn(
                "<summary>v1 F001. [Correctness] Problem boundary</summary>\n\n> **Open since v1.** Still "
                "present in v2: Checked against the current code&#46;\n",
                markdown,
            )
            self.assertIn("<summary>v2 F001. Repeats v1 F001: [Correctness] Problem again</summary>", markdown)
            self.assertIn("<summary><strong>SHOULD FIX (1 issue, 1 repeat)</strong></summary>", markdown)
            self.assertIn("<summary>v2 F002. [Correctness] Problem other</summary>\n\n> **New in v2.**\n", markdown)

    def test_a_repeat_of_a_prior_finding_of_another_category_opens_its_own_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            cleanup = finding("cleanup", category="Error handling", title="Cleanup stops when one call throws")
            reviews.commit([cleanup], mode="initial")
            # A new defect in the same method, which the reviewer linked to the earlier one.
            race = finding("race", line=11, category="Concurrency", title="Method is not safe against concurrent calls")
            second = reviews.commit(
                [{**race, "repeats": "v1:F001"}],
                [disposition("v1:F001", "still_present")],
            )
            self.assertNotIn("repeats", second["findings"][0])
            self.assertEqual(
                [
                    {**entry(1, "F001", "SHOULD_FIX", "open", 2, [(2, "still_present")]), "category": "Error handling"},
                    {**entry(2, "F001", "SHOULD_FIX", "open", 2), "category": "Concurrency"},
                ],
                second["ledger"],
            )
            self.assertEqual(
                {"open": {**NO_OPEN, "SHOULD_FIX": 2}, "repeats": 0, "addressed": 0, "since": 1, "version": 2},
                ledger_summary(second["ledger"], 2),
            )
            markdown = render_markdown(
                second, record_payload_hash="0" * 64, prior_records=pull_records(reviews.archive, "example/one", 12)[:1]
            )
            self.assertIn("<summary><strong>SHOULD FIX (2)</strong></summary>", markdown)
            self.assertIn(
                "<summary>v2 F001. [Concurrency] Method is not safe against concurrent calls</summary>", markdown
            )
            # Resolving the earlier defect leaves the new one open.
            third = reviews.commit([], [disposition("v1:F001", "addressed"), disposition("v2:F001", "still_present")])
            self.assertEqual(["v2:F001"], [item["id"] for item in reviews.prior()])
            self.assertEqual({**NO_OPEN, "SHOULD_FIX": 1}, ledger_summary(third["ledger"], 3)["open"])

    def test_a_repeat_of_a_prior_finding_of_the_same_category_links_in_another_file_and_letter_case(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            reviews.commit([finding("boundary", "SHOULD_FIX")], mode="initial")
            second = reviews.commit(
                [
                    finding(
                        "again", "SHOULD_FIX", line=12, path="src/other.cs", category="correctness", repeats="v1:F001"
                    )
                ],
                [disposition("v1:F001", "still_present")],
            )
            self.assertEqual({"version": 1, "id": "F001"}, second["findings"][0]["repeats"])
            self.assertEqual(
                [entry(1, "F001", "SHOULD_FIX", "open", 2, [(2, "still_present")], [(2, "F001")])], second["ledger"]
            )
            self.assertEqual(1, ledger_summary(second["ledger"], 2)["repeats"])

    def test_an_initial_review_starts_a_fresh_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            reviews.commit([finding("leak", "MUST_FIX")], mode="initial")
            second = reviews.commit([], mode="initial")
            self.assertEqual(("APPROVED", []), (second["review"]["verdict"], second["ledger"]))
            self.assertEqual([], current_ledger(reviews.archive, "example/one", 12))

    def test_a_ledger_is_computed_from_older_records_dispositions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            older = build_record(
                record_request(),
                validate(adapter_result([finding("a", "MUST_FIX"), finding("b", "SHOULD_FIX", 20)])),
                version=1,
                policy=POLICY,
            )
            del older["ledger"]
            commit_record(reviews.archive, "example/one", 12, older, expected_latest_version=None)
            # The reviewed head's summary is computed from the records, never read as missing.
            self.assertEqual(
                {
                    "open": {**NO_OPEN, "MUST_FIX": 1, "SHOULD_FIX": 1},
                    "repeats": 0,
                    "addressed": 0,
                    "since": 1,
                    "version": 1,
                },
                reviewed_ledger(reviews.archive),
            )
            # A record written before ledgers disposes bare IDs of the version it compared with.
            legacy = build_record(
                record_request("re-review"),
                validate(adapter_result([finding("c", "SUGGESTION", 30)])),
                version=2,
                policy=POLICY,
            )
            legacy["prior_dispositions"] = [disposition("F001", "addressed"), disposition("F002", "still_present")]
            del legacy["ledger"]
            validate_record(legacy)
            commit_record(reviews.archive, "example/one", 12, legacy, expected_latest_version=1)
            history = ledger_history(pull_records(reviews.archive, "example/one", 12))
            self.assertEqual(
                [
                    entry(1, "F001", "MUST_FIX", "closed", 2, [(2, "addressed")]),
                    entry(1, "F002", "SHOULD_FIX", "open", 2, [(2, "still_present")]),
                    entry(2, "F001", "SUGGESTION", "open", 2),
                ],
                history[2],
            )
            # A legacy re-review's counts cover only the findings it raised, not the one it judged still present,
            # so the summary counts the computed ledger's open entries and what it addressed.
            self.assertEqual({**NO_OPEN, "SUGGESTION": 1}, legacy["review"]["counts"])
            self.assertEqual(
                {
                    "open": {**NO_OPEN, "SHOULD_FIX": 1, "SUGGESTION": 1},
                    "repeats": 0,
                    "addressed": 1,
                    "since": 1,
                    "version": 2,
                },
                reviewed_ledger(reviews.archive),
            )
            self.assertEqual(["v1:F002", "v2:F001"], [item["id"] for item in reviews.prior()])
            third = reviews.commit([], [disposition("v1:F002", "still_present"), disposition("v2:F001", "addressed")])
            self.assertEqual(
                [("open", 3), ("closed", 3)], [(item["state"], item["judged_in"]) for item in third["ledger"][1:]]
            )
            self.assertEqual(
                {"open": {**NO_OPEN, "SHOULD_FIX": 1}, "repeats": 0, "addressed": 2, "since": 1, "version": 3},
                ledger_summary(third["ledger"], 3),
            )


class RepeatTests(unittest.TestCase):
    def test_a_linked_repeat_counts_once_toward_the_should_fix_threshold(self) -> None:
        linked = [finding("a", line=10), finding("b", line=20), finding("a-again", line=30, repeats="a")]
        record = build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY)
        self.assertEqual("APPROVED", record["review"]["verdict"])
        self.assertEqual({"version": 1, "id": "F001"}, record["findings"][2]["repeats"])
        self.assertEqual(
            [
                entry(1, "F001", "SHOULD_FIX", "open", 1, repeats=[(1, "F003")]),
                entry(1, "F002", "SHOULD_FIX", "open", 1),
            ],
            record["ledger"],
        )
        unlinked = [{key: value for key, value in item.items() if key != "repeats"} for item in linked]
        record = build_record(record_request(), validate(adapter_result(unlinked)), version=1, policy=POLICY)
        self.assertEqual("CHANGES_REQUESTED", record["review"]["verdict"])
        markdown = render_markdown(
            build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY),
            record_payload_hash="0" * 64,
        )
        self.assertIn("<summary><strong>SHOULD FIX (2 issues, 1 repeat)</strong></summary>", markdown)
        self.assertIn("<summary>v1 F003. Repeats v1 F001: [Correctness] Problem a&#45;again</summary>", markdown)
        self.assertLess(markdown.index("v1 F001. [Correctness]"), markdown.index("v1 F003. Repeats v1 F001"))
        self.assertLess(markdown.index("v1 F003. Repeats v1 F001"), markdown.index("v1 F002. [Correctness]"))

    def test_a_repeat_of_a_finding_of_another_category_in_the_same_review_opens_its_own_entry(self) -> None:
        linked = [finding("a", line=10), finding("b", line=20, category="Security", repeats="a")]
        record = build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY)
        self.assertEqual([None, None], [item.get("repeats") for item in record["findings"]])
        self.assertEqual(
            [
                entry(1, "F001", "SHOULD_FIX", "open", 1),
                {**entry(1, "F002", "SHOULD_FIX", "open", 1), "category": "Security"},
            ],
            record["ledger"],
        )
        self.assertEqual(
            {"open": {**NO_OPEN, "SHOULD_FIX": 2}, "repeats": 0, "addressed": 0, "since": 1, "version": 1},
            ledger_summary(record["ledger"], 1),
        )

    def test_a_group_heading_counts_entries_and_names_their_repeats_beside_them(self) -> None:
        linked = [
            finding("a", "SUGGESTION", line=10),
            finding("b", "SUGGESTION", line=20),
            *(finding(f"a-{line}", "SUGGESTION", line=line, repeats="a") for line in (30, 40, 50)),
            finding("c", line=60),
        ]
        record = build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY)
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("<summary><strong>SUGGESTIONS (2 issues, 3 repeats)</strong></summary>", markdown)
        self.assertEqual(6, markdown.count("<summary>v1 F00"), "every entry and every repeat is shown")
        # A group without repeats keeps its bare count.
        self.assertIn("<summary><strong>SHOULD FIX (1)</strong></summary>", markdown)
        self.assertEqual(
            {"open": {**NO_OPEN, "SHOULD_FIX": 1, "SUGGESTION": 2}, "repeats": 3, "addressed": 0, "since": 1},
            {key: value for key, value in ledger_summary(record["ledger"], 1).items() if key != "version"},
        )
        # Only the summarized version's repeats are counted: a later review that reports none has none.
        self.assertEqual(0, ledger_summary(record["ledger"], 2)["repeats"])

    def test_a_repeat_of_a_prior_version_past_the_digit_limit_is_an_unknown_finding(self) -> None:
        target = "v1" + "0" * 5000 + ":F001"
        with self.assertRaises(RecordError) as raised:
            build_record(record_request(), adapter_result([finding("a", repeats=target)]), version=2, policy=POLICY)
        self.assertEqual(f"Finding a repeats an unknown finding: {target}", str(raised.exception))

    def test_the_verdict_counts_open_ledger_entries(self) -> None:
        should = [entry(1, f"F00{n}", "SHOULD_FIX", "open", 1) for n in (1, 2)]
        self.assertEqual("APPROVED", calculate_verdict(should, VERDICT_POLICY))
        self.assertEqual(
            "APPROVED",
            calculate_verdict(
                [*should, entry(1, "F003", "SHOULD_FIX", "closed", 2, [(2, "addressed")])], VERDICT_POLICY
            ),
        )
        self.assertEqual(
            "CHANGES_REQUESTED", calculate_verdict([*should, entry(1, "F003", "SHOULD_FIX", "open", 1)], VERDICT_POLICY)
        )
        self.assertEqual(
            "APPROVED",
            calculate_verdict(
                [entry(1, "F001", "MUST_FIX", "unverified", 2, [(2, "unable_to_verify")])], VERDICT_POLICY
            ),
        )
        self.assertEqual(
            "CHANGES_REQUESTED",
            calculate_verdict([entry(1, "F001", "MUST_FIX", "open", 2, [(2, "partially_addressed")])], VERDICT_POLICY),
        )
        self.assertEqual("INCOMPLETE", calculate_verdict(should, VERDICT_POLICY, ["src/large.cs"]))

    def test_invalid_repeats_fail_result_validation(self) -> None:
        prior = [{"id": "v1:F001", "severity": "SHOULD_FIX"}, {"id": "v1:F002", "severity": "SUGGESTION"}]
        still = [disposition("v1:F001", "still_present"), disposition("v1:F002", "still_present")]
        for findings, dispositions, message in (
            ([finding("a", repeats="missing")], still, "repeats an unknown finding"),
            ([finding("a", repeats="a")], still, "cannot repeat itself"),
            ([finding("a"), finding("b", repeats="a"), finding("c", repeats="b")], still, "repeats a repeat"),
            ([finding("a", "SUGGESTION"), finding("b", "MUST_FIX", repeats="a")], still, "less severe"),
            ([finding("a", repeats="v1:F002")], still, "less severe"),
            (
                [finding("a", repeats="v1:F001")],
                [disposition("v1:F001", "addressed"), still[1]],
                "still_present or partially_addressed",
            ),
            ([finding("v1:F001"), finding("b", repeats="v1:F001")], still, "ambiguous"),
            (
                [finding("a", repeats=["a"])],
                still,
                field_pattern("adapter-result.findings[0].repeats", "a non-empty string", "an array"),
            ),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate(adapter_result(findings, dispositions), prior)

    def test_invalid_repeats_and_ledgers_fail_record_validation(self) -> None:
        prior_ledger = [entry(1, "F001", "SHOULD_FIX", "open", 1)]
        result = validate(
            adapter_result(
                [
                    finding("again", repeats="v1:F001"),
                    finding("a", line=20),
                    finding("b", "SUGGESTION", 30, repeats="a"),
                ],
                [disposition("v1:F001", "still_present")],
            ),
            [{"id": "v1:F001", "severity": "SHOULD_FIX"}],
        )
        record = build_record(record_request("re-review"), result, version=2, policy=POLICY, prior_ledger=prior_ledger)
        validate_record(record)
        self.assertEqual(
            [{"version": 1, "id": "F001"}, None, {"version": 2, "id": "F002"}],
            [item.get("repeats") for item in record["findings"]],
        )
        mutate: Callable[[ReviewRecord], object]
        for mutate, message in (
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F009"}), "repeats an unknown finding"),
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F003"}), "cannot repeat itself"),
            (lambda r: r["findings"][2].update(repeats={"version": 3, "id": "F001"}), "repeats is malformed"),
            (
                lambda r: r["findings"][2].update(repeats="F002"),
                field_pattern("record.findings[2].repeats", "an object", 'string "F002"'),
            ),
            (
                lambda r: (
                    r["findings"][1].update(severity="SUGGESTION")
                    or r["review"]["counts"].update(SHOULD_FIX=1, SUGGESTION=2)
                    or r["ledger"][1].update(severity="SUGGESTION")
                    or r["findings"][2].update(severity="SHOULD_FIX")
                    or r["review"]["counts"].update(SHOULD_FIX=2, SUGGESTION=1)
                ),
                "less severe",
            ),
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F001"}), "repeats a repeat"),
            (
                lambda r: (
                    r["prior_dispositions"][0].update(disposition="addressed")
                    or r["ledger"][0]["dispositions"][0].update(disposition="addressed")
                    or r["ledger"][0].update(state="open")
                ),
                "still_present or partially_addressed",
            ),
            (lambda r: r.pop("ledger"), "repeats need a ledger"),
            (lambda r: r["ledger"][0].update(state="closed"), "state"),
            (lambda r: r["ledger"][0].update(judged_in=1), "judged_in"),
            (lambda r: r["ledger"].pop(1), "ledger entry for F002"),
            (lambda r: r["ledger"][0].update(repeats=[]), "F001 is not in its target's ledger entry"),
            (
                lambda r: r["ledger"][0]["dispositions"].append({"version": 2, "disposition": "addressed"}),
                "dispositions",
            ),
            (lambda r: r["prior_dispositions"][0].update(finding_id="v1:F007"), "prior dispositions"),
            (lambda r: r["ledger"].append(entry(3, "F001", "MUST_FIX", "open", 3)), "after this review"),
            (lambda r: r["ledger"].append(r["ledger"][0]), "unique"),
            (lambda r: r["review"].update(mode="initial") or r["review"].pop("scope", None), "fresh ledger"),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)

    def test_repeats_and_ledger_fields_of_the_wrong_kind_are_field_errors(self) -> None:
        prior_ledger = [entry(1, "F001", "SHOULD_FIX", "open", 1)]
        result = validate(
            adapter_result(
                [
                    finding("again", repeats="v1:F001"),
                    finding("a", line=20),
                    finding("b", "SUGGESTION", 30, repeats="a"),
                ],
                [disposition("v1:F001", "still_present")],
            ),
            [{"id": "v1:F001", "severity": "SHOULD_FIX"}],
        )
        record = build_record(record_request("re-review"), result, version=2, policy=POLICY, prior_ledger=prior_ledger)
        severities = 'one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION"'
        dispositions = 'one of "addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify"'
        mutate: Callable[[ReviewRecord], object]
        for mutate, path, expected, found in (
            (
                lambda r: r["findings"][2].update(repeats=["F002"]),
                "record.findings[2].repeats",
                "an object",
                "an array",
            ),
            (
                lambda r: r["findings"][2].update(repeats={"version": "2", "id": "F002"}),
                "record.findings[2].repeats.version",
                "an integer",
                'string "2"',
            ),
            (
                lambda r: r["findings"][2].update(repeats={"version": 2, "id": 5}),
                "record.findings[2].repeats.id",
                "a non-empty string",
                "integer 5",
            ),
            (
                lambda r: r["findings"][2].update(repeats={"version": 2}),
                "record.findings[2].repeats",
                "an object with field id",
                "an object without it",
            ),
            (lambda r: r.update(ledger={}), "record.ledger", "an array", "an object"),
            (lambda r: r["ledger"].__setitem__(0, "F001"), "record.ledger[0]", "an object", 'string "F001"'),
            (
                lambda r: r["ledger"][0].update(extra=1),
                "record.ledger[0]",
                "an object of known fields",
                'unknown field "extra"',
            ),
            (lambda r: r["ledger"][0].update(version=True), "record.ledger[0].version", "an integer", "boolean true"),
            (lambda r: r["ledger"][0].update(id=1), "record.ledger[0].id", "a non-empty string", "integer 1"),
            (lambda r: r["ledger"][0].update(severity=[]), "record.ledger[0].severity", severities, "an array"),
            (
                lambda r: r["ledger"][0].update(state=5),
                "record.ledger[0].state",
                'one of "open", "closed", "unverified"',
                "integer 5",
            ),
            (lambda r: r["ledger"][0].update(judged_in="1"), "record.ledger[0].judged_in", "an integer", 'string "1"'),
            (
                lambda r: r["ledger"][0].update(dispositions="x"),
                "record.ledger[0].dispositions",
                "an array",
                'string "x"',
            ),
            (
                lambda r: r["ledger"][0]["dispositions"].__setitem__(0, {"version": 2, "disposition": 1}),
                "record.ledger[0].dispositions[0].disposition",
                dispositions,
                "integer 1",
            ),
            (lambda r: r["ledger"][0].update(repeats="F001"), "record.ledger[0].repeats", "an array", 'string "F001"'),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with (
                self.subTest(path=path, found=found),
                self.assertRaisesRegex(RecordFieldError, field_pattern(path, expected, found)),
            ):
                validate_record(broken)

    def test_the_adapter_schema_publishes_the_repeats_link(self) -> None:
        # The record's ledger and finding references are stated in docs/code-review-operations-contract.md and
        # checked against validate_record by tests/code-review/test_format_contract.py.
        adapter = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json").read_text(encoding="utf-8")
        )
        self.assertEqual("string", adapter["properties"]["findings"]["items"]["properties"]["repeats"]["type"])

    def test_a_record_without_a_ledger_validates_and_is_summarized_from_its_computed_ledger(self) -> None:
        record = build_record(
            record_request(), validate(adapter_result([finding("a", "MUST_FIX")])), version=1, policy=POLICY
        )
        del record["ledger"]
        validate_record(record)
        self.assertEqual({1: [entry(1, "F001", "MUST_FIX", "open", 1)]}, ledger_history([record]))
        self.assertEqual(
            {"open": {**NO_OPEN, "MUST_FIX": 1}, "repeats": 0, "addressed": 0, "since": 1, "version": 1},
            ledger_summary(ledger_history([record])[1], 1),
        )


# Today's v1 report of the fixture, with the finding IDs in the `v<version> F<nnn>` notation.
FIXTURE_V1_REPORT = """# Code Review — example/one#12

| | |
|---|---|
| **Title** | Release the lock |
| **Base** | `main` |
| **URL** | https://github.com/example/one/pull/12 |
| **Reviewed** | 01-Oct-2026 09:30 UTC |
| **Verdict** | CHANGES REQUESTED |

---

## Summary

Version 1 of the fixture&#46;

## Findings

<details open>
<summary><strong>MUST FIX (1)</strong></summary>

<details open>
<summary>v1 F001. [Correctness] Lock is never released</summary>

> **File:** `src/lock.py`\x20\x20
> **Line 10:** `lock.acquire()` | **Source:** generic
>
> The lock taken here is not released when the read fails&#46;

</details>

</details>

<details open>
<summary><strong>SHOULD FIX (1)</strong></summary>

<details open>
<summary>v1 F002. [Correctness] Null result is not checked</summary>

> **File:** `src/parse.py`\x20\x20
> **Line 20:** `value = parse(text)` | **Source:** generic
>
> A null result from parse reaches the caller unchecked&#46;

</details>

</details>

---

<details>
<summary><strong>Review Details</strong></summary>

| | |
|---|---|
| **Mode** | initial v1 |
| **Adapter** | `generic` (generic) |
| **Reviewer** | fixture&#45;reviewer (complete) |
| **Base SHA** | `0000000000000000000000000000000000000000` |
| **Reviewed HEAD** | `1111111111111111111111111111111111111111` |
| **Record payload SHA-256** | `{payload}` |

</details>

<!-- reviewed_head_sha: 1111111111111111111111111111111111111111 -->
"""

FIXTURE_V3_OPEN_ENTRY = """<details open>
<summary>v1 F001. [Correctness] Lock is never released</summary>

> **Open since v1.** Still present in v3: The timeout branch still returns while holding the lock&#46;
>
> **File:** `src/lock.py`\x20\x20
> **Line 10:** `lock.acquire()` | **Source:** generic
>
> The lock taken here is not released when the read fails&#46;

<details open>
<summary>v3 F001. Repeats v1 F001: [Correctness] Lock leaks on the timeout path</summary>

> **File:** `src/lock.py`\x20\x20
> **Line 14:** `return None` | **Source:** generic
>
> The timeout branch returns before releasing the lock&#46;

</details>

</details>
"""

FIXTURE_ADDRESSED = """<details>
<summary><strong>Addressed since v1</strong></summary>

- **v1 F002.** SHOULD FIX [Correctness] Null result is not checked, `src/parse.py:20`. Addressed in v2: The caller \
now returns early on null&#46;

</details>
"""


def fixture_report(archive: Path, version: int) -> str:
    return record_paths(pull_directory(archive, review_fixture.REPOSITORY, review_fixture.NUMBER), version)[
        1
    ].read_text(encoding="utf-8")


def flag(
    identifier: int, version: int | None, finding_id: str | None, *, number: int = 12, status: str = "open"
) -> dict[str, Any]:
    resolved = status == "resolved"
    return {
        "id": f"RF-{identifier:06d}",
        "status": status,
        "created_at": "2026-10-04T10:00:00+00:00",
        "resolved_at": "2026-10-05T10:00:00+00:00" if resolved else None,
        "repository": "example/one",
        "pull_number": number,
        "review_version": version,
        "finding_id": finding_id,
        "category": "noise",
        "body": "The context manager\nreleases the lock.",
        "resolution": "Prompt adjusted." if resolved else None,
    }


class FindingNameTests(unittest.TestCase):
    def test_a_finding_is_shown_and_referenced_in_one_form(self) -> None:
        self.assertEqual("v3 F012", display_id(3, "F012"))
        self.assertEqual("v3 F012", ledger_display_id(ledger_id(3, "F012")))
        self.assertEqual("v3:F012", ledger_id(3, "F012"))
        self.assertEqual("owner/repo#41 v3 F012", finding_reference("Owner/Repo", 41, 3, "F012"))

    def test_a_merged_source_splits_back_into_its_reviewers(self) -> None:
        self.assertEqual("correctness + security", joined_source(["correctness", "security"]))
        self.assertEqual(["correctness", "security"], source_parts(joined_source(["correctness", "security"])))
        self.assertEqual(["a", "b"], source_parts(" a + b + a +  + b"))
        self.assertEqual(["free text a+b"], source_parts("free text a+b"))


class LedgerReportTests(unittest.TestCase):
    """The report renders a pull request's whole ledger, from the three-version fixture in review_fixture.py."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name) / "archive"

    def test_a_re_review_report_shows_every_open_entry_in_full_and_what_was_addressed(self) -> None:
        review_fixture.commit_fixture(self.archive)
        report = fixture_report(self.archive, 3)
        self.assertIn("| **Verdict** | CHANGES REQUESTED, 2 open since v1, 1 addressed |\n", report)
        self.assertIn(
            "## Findings\n\n<details open>\n<summary><strong>MUST FIX (1 issue, 1 repeat)</strong></summary>\n\n"
            + FIXTURE_V3_OPEN_ENTRY
            + "\n</details>\n",
            report,
        )
        self.assertIn(
            "<details open>\n<summary><strong>SUGGESTIONS (1)</strong></summary>\n\n<details open>\n"
            "<summary>v3 F002. [Maintainability] Name the retry limit</summary>\n\n"
            "> **New in v3.**\n>\n> **File:** `src/retry.py`  \n",
            report,
        )
        self.assertIn(FIXTURE_ADDRESSED, report)
        self.assertLess(report.index("SUGGESTIONS (1)"), report.index("Addressed since v1"))
        self.assertNotIn("SHOULD FIX (", report, "an addressed entry leaves the open set")
        for removed in ("## Open Findings", "## Prior Findings Status", "v1:F00", "**Repeats:**"):
            self.assertNotIn(removed, report)

    def test_an_initial_report_changes_only_its_finding_ids(self) -> None:
        first = review_fixture.commit_fixture(self.archive, versions=1)[0]
        artifacts = first.get("artifacts")
        if artifacts is None:
            raise AssertionError("a committed record has its artifact hashes")
        self.assertEqual(
            FIXTURE_V1_REPORT.replace("{payload}", artifacts["payload_sha256"]),
            fixture_report(self.archive, 1),
        )

    def test_a_report_that_reports_nothing_new_still_shows_the_open_entry(self) -> None:
        review_fixture.commit_fixture(self.archive, versions=2)
        report = fixture_report(self.archive, 2)
        self.assertIn("| **Verdict** | CHANGES REQUESTED, 1 open since v1, 1 addressed |\n", report)
        self.assertIn(
            "<summary>v1 F001. [Correctness] Lock is never released</summary>\n\n"
            "> **Open since v1.** Still present in v2: The read still runs outside a try block&#46;\n>\n",
            report,
        )
        self.assertNotIn("No findings.", report)
        self.assertIn(FIXTURE_ADDRESSED, report)

    def test_the_report_names_unverified_partially_addressed_and_superseded_entries(self) -> None:
        reviews = ArchiveFixture(self.archive.parent)
        reviews.commit(
            [finding("leak", "MUST_FIX"), finding("bound", "SHOULD_FIX", 20), finding("style", "SUGGESTION", 30)],
            mode="initial",
        )
        reviews.commit(
            [],
            [
                disposition("v1:F001", "unable_to_verify"),
                disposition("v1:F002", "partially_addressed"),
                disposition("v1:F003", "superseded"),
            ],
        )
        report = fixture_report(self.archive, 2)
        self.assertIn("| **Verdict** | APPROVED, 1 open since v1, 1 unverified |\n", report)
        self.assertIn(
            "<summary>v1 F001. [Correctness] Problem leak</summary>\n\n> **Unverified, raised in v1.** "
            "Unable to verify in v2: Checked against the current code&#46;\n",
            report,
        )
        self.assertIn("> **Open since v1.** Partially addressed in v2: Checked against the current code&#46;\n", report)
        self.assertIn(
            "- **v1 F003.** SUGGESTION [Correctness] Problem style, `src/file.cs:30`. Superseded in v2: "
            "Checked against the current code&#46;\n",
            report,
        )
        reviews.commit([], [disposition("v1:F001", "addressed"), disposition("v1:F002", "addressed")])
        report = fixture_report(self.archive, 3)
        self.assertIn("| **Verdict** | APPROVED, none open, 2 addressed |\n", report)
        self.assertIn("## Findings\n\nNo open findings.\n", report)

    def test_rendering_needs_the_record_that_raised_each_entry(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        render_markdown(third, record_payload_hash="0" * 64, prior_records=[first, second])
        with self.assertRaisesRegex(RecordError, "v1 with finding F001 is missing"):
            render_markdown(third, record_payload_hash="0" * 64, prior_records=[second])

    def test_configured_model_names_replace_identifiers_in_the_reviewers_table(self) -> None:
        review_fixture.commit_fixture(self.archive, model_names={review_fixture.MODEL_ARN: "Fixture Opus"})
        report = fixture_report(self.archive, 3)
        self.assertIn("| `generic` | General | Fixture Opus | 3 | 1 | 0 | 40s | - | - |\n", report)
        self.assertIn(
            "| `style` | Style | claude&#45;sonnet&#45;5&#45;5 | 1 | 1 | 0 | 12s | - | - |\n",
            report,
            "unmapped stays literal",
        )
        self.assertIn(
            "| **Reviewer models** | Fixture Opus: `arn:aws:bedrock:us-east-1:111122223333:"
            "application-inference-profile/fixture` |\n",
            report,
        )

    def test_the_snapshot_line_and_the_files_read_column(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        review = copy.deepcopy(third)
        review["review"]["snapshot"] = {
            "source": "tarball",
            "files": 12_700,
            "bytes": 213_909_504,
            "seconds": {"fetch": 6.0, "materialize": 33.2, "prompts": 0.4},
        }
        generic, style = review["review"]["reviewers"]
        generic.update({"files_read": 14, "bytes_read": 52_000})
        style.update({"files_read": None, "bytes_read": None})
        report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertIn(
            "| **Snapshot** | tarball: 12,700 files, 204.0 MiB; fetch 6.0s, materialize 33.2s, prompts 0.4s |\n",
            report,
        )
        self.assertIn("| Retries | Time | Files read | Searches |\n", report)
        self.assertIn(" | 0 | 40s | 14 (50.8 KiB) | - |\n", report)
        self.assertIn(" | 0 | 12s | unknown | - |\n", report)
        # A record that counted its exclusions names them by reason; one that excluded nothing names none.
        review["review"]["snapshot"]["excluded"] = {"agent-instruction": 3, "configured": 1_200}
        self.assertIn(
            "| **Snapshot** | tarball: 12,700 files, 204.0 MiB (excluded: agent-instruction 3, configured 1,200); "
            "fetch 6.0s, materialize 33.2s, prompts 0.4s |\n",
            render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second]),
        )
        review["review"]["snapshot"]["excluded"] = {}
        self.assertIn(
            "| **Snapshot** | tarball: 12,700 files, 204.0 MiB; fetch 6.0s, materialize 33.2s, prompts 0.4s |\n",
            render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second]),
        )
        del review["review"]["snapshot"]["excluded"]
        generic.update({"files_read": 0, "bytes_read": 0})
        report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertIn(" | 0 | 40s | 0 (0 B) | - |\n", report)

    def test_the_searches_column_and_what_the_read_columns_measure(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        review = copy.deepcopy(third)
        generic, style = review["review"]["reviewers"]
        generic.update({"files_read": 14, "bytes_read": 52_000, "searches": 6})
        style.update({"files_read": None, "bytes_read": None, "searches": None})
        self.assertIs(review, validate_record(review))
        report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertIn(
            "| Retries | Time | Files read | Searches |\n"
            "|----------|-------|-------|-------|----------|---------|------|------------|----------|\n",
            report,
        )
        self.assertIn(" | 0 | 40s | 14 (50.8 KiB) | 6 |\n", report)
        self.assertIn(" | 0 | 12s | unknown | unknown |\n", report)
        self.assertIn(
            "\n*Files read* counts the distinct files of the source snapshot a reviewer read or fetched, with their "
            "size, beyond its prompt and the diff; *Searches* counts its searches of the snapshot: a Grep of a folder, "
            "a Glob, or a `source-search`. `unknown` is a count no guard measured, never zero, and `-` one the record "
            "predates.\n\n---\n",
            report,
        )
        generic.update({"searches": 0})
        report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertIn(" | 0 | 40s | 14 (50.8 KiB) | 0 |\n", report)
        del generic["searches"]
        report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertIn(" | 0 | 40s | 14 (50.8 KiB) | - |\n", report)

    def test_a_reviewer_that_read_and_searched_no_source_of_a_large_change_is_noted(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        review: Any = copy.deepcopy(third)
        review["review"]["patches"] = {
            "src/file.cs": {"sha256": "a" * 64, "lines": 150},
            "src/other.cs": {"sha256": "b" * 64, "lines": 51},
        }
        generic, style = review["review"]["reviewers"]
        unread = {"files_read": 0, "bytes_read": 0, "searches": 0}
        generic.update(unread)
        style.update(unread)

        def report(threshold: int | None = 200) -> str:
            return render_markdown(
                review, record_payload_hash="0" * 64, prior_records=[first, second], unread_source_lines=threshold
            )

        self.assertIn(
            "> **No source read:** reviewers `generic`, `style` read and searched no file of the source beyond the "
            "diff of this 201-line change.\n\n---\n",
            report(),
        )
        self.assertNotIn("No source read", report(201), "a change of exactly the threshold is not above it")
        self.assertNotIn("No source read", report(None), "a report given no threshold notes nothing")
        style.update({"searches": 1})
        self.assertIn(
            "> **No source read:** reviewer `generic` read and searched no file of the source beyond the diff of this "
            "201-line change.\n",
            report(),
        )
        for counts in (
            {"files_read": 1, "bytes_read": 9, "searches": 0},  # read a file
            {"files_read": None, "bytes_read": None, "searches": None},  # unmeasured
            {"files_read": 0, "bytes_read": 0, "searches": None},  # searches unmeasured
            {"files_read": 0, "bytes_read": 0},  # a record written before searches were counted
            {"dispositions_only": True, **unread},  # it reviewed nothing new
        ):
            with self.subTest(counts=counts):
                generic.pop("searches", None)
                generic.update(counts)
                self.assertNotIn("No source read", report())
                generic.update({"dispositions_only": False, **unread})
        self.assertIn("No source read", report())
        reviewers = review["review"].pop("reviewers")
        self.assertNotIn("No source read", report(), "a record without reviewers names none")
        review["review"]["reviewers"] = reviewers
        del review["review"]["patches"]
        self.assertNotIn("No source read", report(), "a record without patches does not know the change's size")

    def test_the_reviewer_source_line_names_where_the_reviewer_came_from(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        review = copy.deepcopy(third)
        adapter = review["review"]["adapter"]
        commit = "0123456789ab" + "c" * 28
        for scope, source, source_commit, line in (
            ("generic", "generic", None, "the suite's generic reviewer"),
            (
                "generic",
                "generic-fallback",
                None,
                "the suite's generic reviewer, in place of a review skill the base predates",
            ),
            ("repository", "trusted-ref", commit, "the configured trusted ref, at `0123456789ab`"),
            ("repository", "base", commit, "the pull request's base, `0123456789ab`"),
            (
                "repository",
                "default-branch",
                commit,
                "the default branch's tip, `0123456789ab`, because the base predates the review skill",
            ),
        ):
            with self.subTest(source):
                adapter.update({"scope": scope, "source": source, "source_commit": source_commit})
                self.assertIs(review, validate_record(review))
                report = render_markdown(review, record_payload_hash="0" * 64, prior_records=[first, second])
                self.assertIn(f"| **Adapter** | `generic` ({scope}) |\n| **Reviewer source** | {line} |\n", report)

    def test_an_earlier_record_without_snapshot_or_reads_reads_as_absent(self) -> None:
        first, second, third = review_fixture.commit_fixture(self.archive)
        self.assertNotIn("source", third["review"]["adapter"])
        self.assertNotIn("**Reviewer source**", fixture_report(self.archive, 3))
        self.assertNotIn("snapshot", third["review"])
        self.assertTrue(all("files_read" not in reviewer for reviewer in third["review"]["reviewers"]))
        self.assertIs(third, validate_record(third))
        report = render_markdown(third, record_payload_hash="0" * 64, prior_records=[first, second])
        self.assertNotIn("**Snapshot**", report)
        self.assertIn(" | 0 | 40s | - | - |\n", report)
        self.assertNotIn("*Files read* counts", report, "a record without read counts has nothing to explain")

    def test_without_model_names_the_reviewers_table_shows_the_identifier(self) -> None:
        review_fixture.commit_fixture(self.archive)
        report = fixture_report(self.archive, 3)
        self.assertIn(
            "| `generic` | General | arn&#58;aws&#58;bedrock&#58;us&#45;east&#45;1&#58;111122223333&#58;"
            "application&#45;inference&#45;profile&#47;fixture | 3 |",
            report,
        )
        self.assertNotIn("Reviewer models", report)

    def test_a_flagged_finding_is_marked_on_its_entry_and_in_the_verdict(self) -> None:
        # The flag names version 3's repeat, which belongs to the entry raised in version 1. Resolving a flag
        # records that the improvement was handled, so it still marks the finding; other pull requests and flags
        # without a review version mark nothing.
        flags = [flag(1, 3, "F001", status="resolved"), flag(2, 3, "F002", number=13), flag(3, None, None)]
        review_fixture.commit_fixture(self.archive, flags=flags)
        report = fixture_report(self.archive, 3)
        self.assertIn("| **Verdict** | CHANGES REQUESTED, 2 open since v1 (1 flagged), 1 addressed |\n", report)
        self.assertIn(
            "> **Open since v1.** Still present in v3: The timeout branch still returns while holding the "
            "lock&#46;  \n> **Flagged:** RF&#45;000001 (noise): The context manager<br>\n"
            "> releases the lock&#46;\n>\n"
            "> **File:** `src/lock.py`",
            report,
        )
        self.assertEqual(1, report.count("**Flagged:**"))

    def test_flags_attach_to_the_entry_holding_the_flagged_finding(self) -> None:
        ledger = review_fixture.commit_fixture(self.archive)[2]["ledger"]
        flags = [
            flag(1, 1, "F001"),
            flag(2, 3, "F001"),
            flag(3, 3, "F002"),
            flag(4, 2, "F001"),
            flag(5, 1, "F002", number=13),
        ]
        self.assertEqual(
            {"v1:F001": [flags[0], flags[1]], "v3:F002": [flags[2]]}, flagged_entries(ledger, flags, "Example/One", 12)
        )

    def test_a_carried_finding_brings_the_flags_on_its_entry_to_the_next_review(self) -> None:
        # The resolved flag names version 3's repeat of the entry raised in version 1, so the entry carries it;
        # a flag on another pull request or on no finding reaches no reviewer, and an unflagged entry has no key.
        records = review_fixture.commit_fixture(self.archive)
        flags = [flag(1, 3, "F001", status="resolved"), flag(2, 3, "F002", number=13), flag(3, None, None)]
        carried = {item["id"]: item for item in carried_findings(records, flags)}
        self.assertEqual(["v1:F001", "v3:F002"], sorted(carried))
        self.assertEqual(
            [{"id": "RF-000001", "category": "noise", "rationale": "The context manager\nreleases the lock."}],
            carried["v1:F001"]["flags"],
        )
        self.assertNotIn("flags", carried["v3:F002"])
        self.assertEqual(
            carried_findings(records),
            [
                {key: value for key, value in item.items() if key != "flags"}
                for item in carried_findings(records, flags)
            ],
        )
