"""The finding ledger: how findings are counted across review versions and within one review."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from review_archive import commit_record, current_ledger, pull_records
from review_operation import commit_adapter_result, reviewed_head
from review_records import (
    RecordError,
    build_record,
    calculate_verdict,
    carried_findings,
    ledger_history,
    ledger_summary,
    render_markdown,
    validate_adapter_result,
    validate_record,
)

POLICY = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
ADAPTER = {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}
NO_OPEN = {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}


def finding(key: str, severity: str = "SHOULD_FIX", line: int = 10, **extra: object) -> dict:
    return {"candidate_key": key, "severity": severity, "category": "Correctness", "path": "src/file.cs",
            "line": line, "title": f"Problem {key}", "body": f"Problem {key} breaks the rule.",
            "evidence": "The added line shows it.", "source": "generic", **extra}


def disposition(identifier: str, value: str) -> dict:
    return {"finding_id": identifier, "disposition": value, "rationale": "Checked against the current code."}


def adapter_result(findings: list[dict] = (), dispositions: list[dict] = ()) -> dict:
    return {"protocol_version": 1, "repository": "example/one", "pull_number": 12, "head_sha": "b" * 40,
            "summary": "Reviewed.", "reviewer": "fixture-reviewer", "status": "complete",
            "findings": list(findings), "prior_dispositions": list(dispositions), "usage": None}


def record_request(mode: str = "initial") -> dict:
    return {"repository": "example/one", "pull_number": 12, "pull_url": "https://github.com/example/one/pull/12",
            "title": "Improve behavior", "base_ref": "main", "base_sha": "a" * 40, "head_sha": "b" * 40,
            "mode": mode, "adapter": ADAPTER}


def validate(result: dict, prior: list[dict] = ()) -> dict:
    return validate_adapter_result(
        result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40,
        prior_ids=[item["id"] for item in prior], prior_severities={item["id"]: item["severity"] for item in prior})


def entry(version: int, identifier: str, severity: str, state: str, judged_in: int,
          dispositions: list[tuple[int, str]] = (), repeats: list[tuple[int, str]] = ()) -> dict:
    return {"version": version, "id": identifier, "severity": severity, "category": "Correctness", "state": state,
            "judged_in": judged_in,
            "dispositions": [{"version": v, "disposition": d} for v, d in dispositions],
            "repeats": [{"version": v, "id": i} for v, i in repeats]}


class ArchiveFixture:
    """Commits review versions of example/one#12 through the same path `finalize` uses."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.archive = root / "archive"
        self.count = 0

    def prior(self) -> list[dict]:
        return carried_findings(pull_records(self.archive, "example/one", 12))

    def commit(self, findings: list[dict] = (), dispositions: list[dict] = (), *, mode: str = "re-review",
               used: str = "incremental") -> dict:
        self.count += 1
        prior = self.prior() if mode == "re-review" else []
        request = {
            "protocol_version": 1, "mode": mode, "repository": "example/one", "pull_number": 12,
            "pull_request": {"title": "Improve behavior", "url": "https://github.com/example/one/pull/12",
                             "base_ref": "main", "base_sha": "a" * 40, "head_sha": "b" * 40},
            "diff_path": str(self.root / "diff.patch"), "prior_findings": prior, "github_comments": [],
        }
        request_path = self.root / f"request-{self.count}.json"
        result_path = self.root / f"result-{self.count}.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        result_path.write_text(json.dumps(adapter_result(findings, dispositions)), encoding="utf-8")
        versions = pull_records(self.archive, "example/one", 12)
        scope = None
        if mode == "re-review":
            scope = {"requested": used, "used": used, "reason": "requested", "since_version": len(versions),
                     "files_changed": 0, "files_total": 1, "lines_changed": 0, "lines_total": 3}
        _, _, record = commit_adapter_result(request_path=request_path, result_path=result_path,
                                             archive_root=self.archive, policy=POLICY, adapter=ADAPTER,
                                             scope=scope)
        return record


class CarriedFindingTests(unittest.TestCase):
    def test_a_still_present_must_fix_requests_changes_in_a_later_version_that_reports_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            reviews = ArchiveFixture(Path(temporary))
            first = reviews.commit([finding("leak", "MUST_FIX")], mode="initial")
            self.assertEqual("CHANGES_REQUESTED", first["review"]["verdict"])
            carried = reviews.prior()
            self.assertEqual(
                [{"id": "v1:F001", "severity": "MUST_FIX", "category": "Correctness", "path": "src/file.cs",
                  "line": 10, "title": "Problem leak", "body": "Problem leak breaks the rule."}], carried)
            second = reviews.commit([], [disposition("v1:F001", "still_present")])
            self.assertEqual("CHANGES_REQUESTED", second["review"]["verdict"])
            # Version 2 reported no findings, so only the ledger can still offer the problem to version 3.
            self.assertEqual(["v1:F001"], [item["id"] for item in reviews.prior()])
            third = reviews.commit([], [disposition("v1:F001", "still_present")])
            self.assertEqual(3, third["review"]["version"])
            self.assertEqual([], third["findings"])
            self.assertEqual("CHANGES_REQUESTED", third["review"]["verdict"])
            self.assertEqual(
                [entry(1, "F001", "MUST_FIX", "open", 3, [(2, "still_present"), (3, "still_present")])],
                third["ledger"])
            self.assertEqual({"open": {**NO_OPEN, "MUST_FIX": 1}, "addressed": 0, "since": 1, "version": 3},
                             ledger_summary(third))
            self.assertEqual(ledger_summary(third), reviewed_head(reviews.archive, "example/one", 12)["ledger"])
            fourth = reviews.commit([], [disposition("v1:F001", "addressed")])
            self.assertEqual("APPROVED", fourth["review"]["verdict"])
            self.assertEqual("closed", fourth["ledger"][0]["state"])
            self.assertEqual({"open": NO_OPEN, "addressed": 1, "since": None, "version": 4}, ledger_summary(fourth))
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
                [disposition("v1:F001", "still_present")], used="full")
            self.assertEqual(
                [entry(1, "F001", "SHOULD_FIX", "open", 2, [(2, "still_present")], [(2, "F001")]),
                 entry(2, "F002", "SUGGESTION", "open", 2)],
                second["ledger"])
            self.assertEqual({"version": 1, "id": "F001"}, second["findings"][0]["repeats"])
            # The entry is offered again where it was last reported.
            self.assertEqual([("v1:F001", 12), ("v2:F002", 30)], [(item["id"], item["line"]) for item in reviews.prior()])
            markdown = render_markdown(second, record_payload_hash="0" * 64)
            self.assertIn("> **Repeats:** v1 F001", markdown)
            self.assertIn("## Open Findings", markdown)
            self.assertIn("| v1 F001 | SHOULD FIX | Correctness | v2 | OPEN |", markdown)
            self.assertNotIn("| v2 F002 |", markdown, "only entries carried from earlier versions are listed")

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
            older = build_record(record_request(), validate(adapter_result(
                [finding("a", "MUST_FIX"), finding("b", "SHOULD_FIX", 20)])), version=1, policy=POLICY)
            del older["ledger"]
            commit_record(reviews.archive, "example/one", 12, older, expected_latest_version=None)
            self.assertIsNone(ledger_summary(older))
            self.assertIsNone(reviewed_head(reviews.archive, "example/one", 12)["ledger"])
            # A record written before ledgers disposes bare IDs of the version it compared with.
            legacy = build_record(record_request("re-review"), validate(adapter_result(
                [finding("c", "SUGGESTION", 30)])), version=2, policy=POLICY)
            legacy["prior_dispositions"] = [disposition("F001", "addressed"), disposition("F002", "still_present")]
            del legacy["ledger"]
            validate_record(legacy)
            commit_record(reviews.archive, "example/one", 12, legacy, expected_latest_version=1)
            history = ledger_history(pull_records(reviews.archive, "example/one", 12))
            self.assertEqual(
                [entry(1, "F001", "MUST_FIX", "closed", 2, [(2, "addressed")]),
                 entry(1, "F002", "SHOULD_FIX", "open", 2, [(2, "still_present")]),
                 entry(2, "F001", "SUGGESTION", "open", 2)],
                history[2])
            self.assertEqual(["v1:F002", "v2:F001"], [item["id"] for item in reviews.prior()])
            third = reviews.commit([], [disposition("v1:F002", "still_present"), disposition("v2:F001", "addressed")])
            self.assertEqual([("open", 3), ("closed", 3)],
                             [(item["state"], item["judged_in"]) for item in third["ledger"][1:]])
            self.assertEqual({"open": {**NO_OPEN, "SHOULD_FIX": 1}, "addressed": 2, "since": 1, "version": 3},
                             ledger_summary(third))


class RepeatTests(unittest.TestCase):
    def test_a_linked_repeat_counts_once_toward_the_should_fix_threshold(self) -> None:
        linked = [finding("a", line=10), finding("b", line=20), finding("a-again", line=30, repeats="a")]
        record = build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY)
        self.assertEqual("APPROVED", record["review"]["verdict"])
        self.assertEqual({"version": 1, "id": "F001"}, record["findings"][2]["repeats"])
        self.assertEqual([entry(1, "F001", "SHOULD_FIX", "open", 1, repeats=[(1, "F003")]),
                          entry(1, "F002", "SHOULD_FIX", "open", 1)], record["ledger"])
        unlinked = [{key: value for key, value in item.items() if key != "repeats"} for item in linked]
        record = build_record(record_request(), validate(adapter_result(unlinked)), version=1, policy=POLICY)
        self.assertEqual("CHANGES_REQUESTED", record["review"]["verdict"])
        markdown = render_markdown(
            build_record(record_request(), validate(adapter_result(linked)), version=1, policy=POLICY),
            record_payload_hash="0" * 64)
        self.assertIn("<summary><strong>SHOULD FIX (2)</strong></summary>", markdown)
        self.assertIn("<summary>F003. Repeats F001: [Correctness] Problem a-again</summary>", markdown)
        self.assertLess(markdown.index("F001. [Correctness]"), markdown.index("F003. Repeats F001"))
        self.assertLess(markdown.index("F003. Repeats F001"), markdown.index("F002. [Correctness]"))

    def test_the_verdict_counts_open_ledger_entries(self) -> None:
        should = [entry(1, f"F00{n}", "SHOULD_FIX", "open", 1) for n in (1, 2)]
        self.assertEqual("APPROVED", calculate_verdict(should, POLICY))
        self.assertEqual("APPROVED", calculate_verdict(
            [*should, entry(1, "F003", "SHOULD_FIX", "closed", 2, [(2, "addressed")])], POLICY))
        self.assertEqual("CHANGES_REQUESTED", calculate_verdict(
            [*should, entry(1, "F003", "SHOULD_FIX", "open", 1)], POLICY))
        self.assertEqual("APPROVED", calculate_verdict(
            [entry(1, "F001", "MUST_FIX", "unverified", 2, [(2, "unable_to_verify")])], POLICY))
        self.assertEqual("CHANGES_REQUESTED", calculate_verdict(
            [entry(1, "F001", "MUST_FIX", "open", 2, [(2, "partially_addressed")])], POLICY))
        self.assertEqual("INCOMPLETE", calculate_verdict(should, POLICY, ["src/large.cs"]))

    def test_invalid_repeats_fail_result_validation(self) -> None:
        prior = [{"id": "v1:F001", "severity": "SHOULD_FIX"}, {"id": "v1:F002", "severity": "SUGGESTION"}]
        still = [disposition("v1:F001", "still_present"), disposition("v1:F002", "still_present")]
        for findings, dispositions, message in (
            ([finding("a", repeats="missing")], still, "repeats an unknown finding"),
            ([finding("a", repeats="a")], still, "cannot repeat itself"),
            ([finding("a"), finding("b", repeats="a"), finding("c", repeats="b")], still, "repeats a repeat"),
            ([finding("a", "SUGGESTION"), finding("b", "MUST_FIX", repeats="a")], still, "less severe"),
            ([finding("a", repeats="v1:F002")], still, "less severe"),
            ([finding("a", repeats="v1:F001")],
             [disposition("v1:F001", "addressed"), still[1]], "still_present or partially_addressed"),
            ([finding("v1:F001"), finding("b", repeats="v1:F001")], still, "ambiguous"),
            ([finding("a", repeats=["a"])], still, "repeats must be"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate(adapter_result(findings, dispositions), prior)

    def test_invalid_repeats_and_ledgers_fail_record_validation(self) -> None:
        prior_ledger = [entry(1, "F001", "SHOULD_FIX", "open", 1)]
        result = validate(adapter_result(
            [finding("again", repeats="v1:F001"), finding("a", line=20), finding("b", "SUGGESTION", 30, repeats="a")],
            [disposition("v1:F001", "still_present")]), [{"id": "v1:F001", "severity": "SHOULD_FIX"}])
        record = build_record(record_request("re-review"), result, version=2, policy=POLICY,
                              prior_ledger=prior_ledger)
        validate_record(record)
        self.assertEqual([{"version": 1, "id": "F001"}, None, {"version": 2, "id": "F002"}],
                         [item.get("repeats") for item in record["findings"]])
        for mutate, message in (
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F009"}), "repeats an unknown finding"),
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F003"}), "cannot repeat itself"),
            (lambda r: r["findings"][2].update(repeats={"version": 3, "id": "F001"}), "repeats is malformed"),
            (lambda r: r["findings"][2].update(repeats="F002"), "repeats is malformed"),
            (lambda r: r["findings"][1].update(severity="SUGGESTION") or r["review"]["counts"].update(
                SHOULD_FIX=1, SUGGESTION=2) or r["ledger"][1].update(severity="SUGGESTION")
                or r["findings"][2].update(severity="SHOULD_FIX") or r["review"]["counts"].update(
                SHOULD_FIX=2, SUGGESTION=1), "less severe"),
            (lambda r: r["findings"][2].update(repeats={"version": 2, "id": "F001"}), "repeats a repeat"),
            (lambda r: r["prior_dispositions"][0].update(disposition="addressed")
             or r["ledger"][0]["dispositions"][0].update(disposition="addressed")
             or r["ledger"][0].update(state="open"), "still_present or partially_addressed"),
            (lambda r: r.pop("ledger"), "repeats need a ledger"),
            (lambda r: r["ledger"][0].update(state="closed"), "state"),
            (lambda r: r["ledger"][0].update(judged_in=1), "judged_in"),
            (lambda r: r["ledger"].pop(1), "ledger entry for F002"),
            (lambda r: r["ledger"][0].update(repeats=[]), "F001 is not in its target's ledger entry"),
            (lambda r: r["ledger"][0]["dispositions"].append({"version": 2, "disposition": "addressed"}),
             "dispositions"),
            (lambda r: r["prior_dispositions"][0].update(finding_id="v1:F007"), "prior dispositions"),
            (lambda r: r["ledger"].append(entry(3, "F001", "MUST_FIX", "open", 3)), "after this review"),
            (lambda r: r["ledger"].append(r["ledger"][0]), "unique"),
            (lambda r: r["review"].update(mode="initial") or r["review"].pop("scope", None), "fresh ledger"),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)

    def test_the_adapter_schema_publishes_the_repeats_link(self) -> None:
        # The record's ledger and finding references are stated in docs/code-review-operations-contract.md and
        # checked against validate_record by tests/code-review/test_format_contract.py.
        adapter = json.loads((SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual("string", adapter["properties"]["findings"]["items"]["properties"]["repeats"]["type"])

    def test_a_record_without_a_ledger_validates_and_reads_as_no_history(self) -> None:
        record = build_record(record_request(), validate(adapter_result([finding("a", "MUST_FIX")])), version=1,
                              policy=POLICY)
        del record["ledger"]
        validate_record(record)
        self.assertIsNone(ledger_summary(record))
        self.assertEqual({1: [entry(1, "F001", "MUST_FIX", "open", 1)]}, ledger_history([record]))


if __name__ == "__main__":
    unittest.main()
