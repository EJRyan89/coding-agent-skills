"""Building, rendering, and writing review records, and the verdict they carry."""

from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from review_config import verdict_policy
from review_records import (
    READ,
    TITLE_MAXIMUM_LENGTH,
    AdapterResult,
    RecordError,
    RecordFieldError,
    build_record,
    calculate_verdict,
    read_title,
    record_artifacts,
    render_markdown,
    valid_analyzer,
    validate_adapter_result,
    validate_record,
    validate_record_pair,
    write_record_pair,
)

SCRIPT_DIRECTORY = Path(__file__).resolve().parent


# What a typed-field reader expects of a git object id, and what it finds in one with a character outside 0-9a-f.
OBJECT_ID = "a lowercase hexadecimal digest of 40 or 64 characters"


NOT_HEX = "a string with a character that is not a lowercase hexadecimal digit"


REVIEWER_MODEL = "a non-empty trimmed single-line string of at most 200 characters"


SEVERITIES = 'one of "MUST_FIX", "SHOULD_FIX", "SUGGESTION"'


DISPOSITIONS = 'one of "addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify"'


def field_message(path: str, expected: str, found: str) -> str:
    """The one message a field of the wrong kind raises, built from literal kinds rather than the reader's own."""
    return f"{path} must be {expected}, found {found}"


def field_pattern(path: str, expected: str, found: str) -> str:
    """`field_message` as a regular expression that matches the whole message."""
    return f"^{re.escape(field_message(path, expected, found))}$"


def valid_request(repository: str = "example/one", number: int = 12) -> dict:
    return {
        "repository": repository,
        "pull_number": number,
        "pull_url": f"https://github.com/{repository}/pull/{number}",
        "title": "Improve behavior",
        "base_ref": "main",
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "mode": "initial",
        "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
    }


def valid_adapter_result(repository: str = "example/one", number: int = 12) -> Any:
    return {
        "protocol_version": 1,
        "repository": repository,
        "pull_number": number,
        "head_sha": "b" * 40,
        "summary": "One actionable issue was found.",
        "reviewer": "fixture-reviewer",
        "status": "complete",
        "findings": [
            {
                "candidate_key": "candidate-a",
                "severity": "SHOULD_FIX",
                "category": "Correctness",
                "path": "src/file.cs",
                "line": 42,
                "body": "Handle the boundary condition.",
                "evidence": "The added branch excludes zero.",
                "source": "generic",
            }
        ],
        "prior_dispositions": [],
        "usage": None,
    }


class RecordTests(unittest.TestCase):
    def test_adapter_schema_matches_runtime_finding_contract(self) -> None:
        schema = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json").read_text(encoding="utf-8")
        )
        finding = schema["properties"]["findings"]["items"]
        self.assertFalse(finding["additionalProperties"])
        self.assertEqual(
            {"candidate_key", "severity", "category", "path", "line", "body", "evidence", "source"},
            set(finding["required"]),
        )
        self.assertEqual({"MUST_FIX", "SHOULD_FIX", "SUGGESTION"}, set(finding["properties"]["severity"]["enum"]))
        title = finding["properties"]["title"]
        self.assertEqual((1, 120), (title["minLength"], title["maxLength"]))
        self.assertEqual(120, TITLE_MAXIMUM_LENGTH)
        for value, accepted in (("Headline", True), ("a b", True), (" padded", False), ("two\nlines", False)):
            self.assertEqual(accepted, re.fullmatch(title["pattern"], value) is not None, value)
            if accepted:
                self.assertEqual(value, read_title(READ, value, "record.findings[0].title"))
        expected = "a non-empty trimmed single-line string of at most 120 characters"
        refused: tuple[tuple[Any, str], ...] = (
            (" padded", "a string with whitespace around it"),
            ("two\nlines", "a string of more than one line"),
            ("x" * 121, "a string of 121 characters"),
            (None, "null"),
            (7, "integer 7"),
            ("", "an empty string"),
        )
        for value, found in refused:
            with self.subTest(value=value):
                with self.assertRaises(RecordFieldError) as raised:
                    read_title(READ, value, "record.findings[0].title")
                self.assertEqual(f"record.findings[0].title must be {expected}, found {found}", str(raised.exception))

    def test_finding_title_is_optional_but_validated_in_results_and_records(self) -> None:
        def validate(result: object) -> AdapterResult:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        untitled = validate(valid_adapter_result())
        self.assertNotIn("title", build_record(valid_request(), untitled, version=1, policy={})["findings"][0])
        titled = valid_adapter_result()
        titled["findings"][0]["title"] = "Zero is not handled"
        record = build_record(valid_request(), validate(titled), version=1, policy={})
        self.assertEqual("Zero is not handled", validate_record(record)["findings"][0]["title"])
        title = "a non-empty trimmed single-line string of at most 120 characters"
        for bad, found in (
            ("", "an empty string"),
            ("x" * 121, "a string of 121 characters"),
            ("two\nlines", "a string of more than one line"),
            (7, "integer 7"),
        ):
            result = valid_adapter_result()
            result["findings"][0]["title"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern("adapter-result.findings[0].title", title, found)
            ):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["title"] = bad
            with self.assertRaisesRegex(RecordFieldError, field_pattern("record.findings[0].title", title, found)):
                validate_record(tampered)

    def test_a_finding_location_comes_from_the_suite_never_a_reviewer_and_is_validated_in_records(self) -> None:
        def validate(result: object) -> AdapterResult:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        given = valid_adapter_result()
        given["findings"][0]["location"] = "paragraph 2"
        with self.assertRaisesRegex(
            RecordFieldError,
            field_pattern("adapter-result.findings[0]", "an object of known fields", 'unknown field "location"'),
        ):
            validate(given)
        result = validate(valid_adapter_result())
        result["findings"][0]["location"] = 'paragraph 2, under the heading "Scope"'
        record = build_record(valid_request(), result, version=1, policy={})
        self.assertEqual('paragraph 2, under the heading "Scope"', validate_record(record)["findings"][0]["location"])
        location = "a non-empty trimmed single-line string of at most 200 characters"
        for bad, found in (
            ("", "an empty string"),
            ("x" * 201, "a string of 201 characters"),
            ("two\nlines", "a string of more than one line"),
            (" padded", "a string with whitespace around it"),
            (7, "integer 7"),
        ):
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["location"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern("record.findings[0].location", location, found)
            ):
                validate_record(tampered)

    def test_finding_analyzer_is_optional_but_validated_in_results_records_and_schema(self) -> None:
        schema = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "review-adapter.schema.json").read_text(encoding="utf-8")
        )
        analyzer = schema["properties"]["findings"]["items"]["properties"]["analyzer"]
        self.assertEqual(["available", "known", "custom-candidate"], analyzer["properties"]["coverage"]["enum"])
        self.assertEqual({"coverage", "tool", "rule"}, set(analyzer["required"]))
        self.assertFalse(analyzer["additionalProperties"])
        name_pattern = analyzer["properties"]["tool"]["pattern"]
        self.assertEqual(name_pattern, analyzer["properties"]["rule"]["pattern"])
        custom_rule = analyzer["then"]["properties"]["rule"]
        self.assertEqual(60, custom_rule["maxLength"])
        for coverage, tool, rule, accepted in (
            ("available", "StyleCop.Analyzers", "SA1515", True),
            ("known", "@typescript-eslint/eslint-plugin", "no-floating-promises", True),
            ("custom-candidate", "Roslyn", "unbounded-retry-loop", True),
            ("custom-candidate", "Roslyn", "x" * 60, True),
            ("custom-candidate", "Roslyn", "x" * 61, False),
            ("custom-candidate", "Roslyn", "CA2000", False),
            ("custom-candidate", "Roslyn", "trailing-", False),
            ("known", "Roslynator Analyzers", "RCS1001", False),
            ("known", "Roslynator.Analyzers", "RCS|1001", False),
            ("known", "Roslynator.Analyzers", "x" * 101, False),
            ("known", "<b>", "RCS1001", False),
            ("known", "", "RCS1001", False),
            ("maybe", "Roslynator.Analyzers", "RCS1001", False),
        ):
            value = {"coverage": coverage, "tool": tool, "rule": rule}
            self.assertEqual(accepted, valid_analyzer(value), value)
            by_schema = (
                coverage in analyzer["properties"]["coverage"]["enum"]
                and all(re.fullmatch(name_pattern, text) for text in (tool, rule))
                and (
                    coverage != "custom-candidate"
                    or (len(rule) <= custom_rule["maxLength"] and re.fullmatch(custom_rule["pattern"], rule))
                )
            )
            self.assertEqual(accepted, bool(by_schema), value)
        self.assertFalse(valid_analyzer({"coverage": "known", "tool": "x", "rule": "y", "extra": 1}))
        self.assertFalse(valid_analyzer({"coverage": ["known"], "tool": "x", "rule": "y"}))
        self.assertFalse(valid_analyzer("known"))

        def validate(result: object) -> AdapterResult:
            return validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

        self.assertNotIn(
            "analyzer",
            build_record(valid_request(), validate(valid_adapter_result()), version=1, policy={})["findings"][0],
        )
        covered = valid_adapter_result()
        analyzer_coverage = {"coverage": "available", "tool": "Microsoft.CodeAnalysis.NetAnalyzers", "rule": "CA2000"}
        covered["findings"][0]["analyzer"] = analyzer_coverage
        record = build_record(valid_request(), validate(covered), version=1, policy={})
        self.assertEqual(analyzer_coverage, validate_record(record)["findings"][0]["analyzer"])
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn(
            "> **Analyzer:** `CA2000` in `Microsoft.CodeAnalysis.NetAnalyzers`, which the repository "
            "already has, would catch this if enforced.\n",
            markdown,
        )
        name = "a name of at most 100 characters without whitespace, backticks, pipes, or angle brackets"
        for bad, field, expected, found in (
            ({"coverage": "known", "tool": "x"}, "", "an object with field rule", "an object without it"),
            ({"coverage": "custom-candidate", "tool": "Roslyn", "rule": "A B"}, ".rule", name, 'string "A B"'),
        ):
            result = valid_adapter_result()
            result["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern(f"adapter-result.findings[0].analyzer{field}", expected, found)
            ):
                validate(result)
            tampered = json.loads(json.dumps(record))
            tampered["findings"][0]["analyzer"] = bad
            with self.assertRaisesRegex(
                RecordFieldError, field_pattern(f"record.findings[0].analyzer{field}", expected, found)
            ):
                validate_record(tampered)
        unknown = valid_adapter_result()
        unknown["findings"][0]["note"] = "x"
        with self.assertRaisesRegex(
            RecordFieldError,
            field_pattern("adapter-result.findings[0]", "an object of known fields", 'unknown field "note"'),
        ):
            validate(unknown)

    def test_published_adapter_fixture_satisfies_protocol(self) -> None:
        result = json.loads(
            (SCRIPT_DIRECTORY.parent / "references" / "fixtures" / "adapter-result.json").read_text(encoding="utf-8")
        )
        validate_adapter_result(
            result, expected_repository="example/example-repository", expected_number=17, expected_head_sha="b" * 40
        )

    def test_adapter_result_requires_every_prior_disposition(self) -> None:
        result = valid_adapter_result()
        with self.assertRaisesRegex(RecordError, "missing"):
            validate_adapter_result(
                result,
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
                prior_ids=["F001"],
            )

    def test_comment_dispositions_are_required_except_from_older_repository_reviewers(self) -> None:
        arguments: dict[str, Any] = {
            "expected_repository": "example/one",
            "expected_number": 12,
            "expected_head_sha": "b" * 40,
            "comment_ids": ["C1", "C2"],
        }
        with self.assertRaisesRegex(RecordError, r"Comment dispositions mismatch; missing=\['C1', 'C2'\]"):
            validate_adapter_result(valid_adapter_result(), **arguments)
        # A repository entrypoint reviewer that predates comment dispositions may omit them all...
        validate_adapter_result(valid_adapter_result(), require_comment_dispositions=False, **arguments)
        # ...but one that gives any must cover every comment exactly once.
        partial = {
            **valid_adapter_result(),
            "comment_dispositions": [{"comment_id": "C1", "disposition": "addressed", "rationale": "Done."}],
        }
        with self.assertRaisesRegex(RecordError, r"missing=\['C2'\]"):
            validate_adapter_result(partial, require_comment_dispositions=False, **arguments)

    def test_record_patches_and_re_review_scope_are_validated(self) -> None:
        patches = {"src/file.cs": {"sha256": "c" * 64, "lines": 3}}
        scope = {
            "requested": "auto",
            "used": "incremental",
            "reason": "a small change",
            "since_version": 1,
            "files_changed": 1,
            "files_total": 1,
            "lines_changed": 3,
            "lines_total": 3,
        }
        request = {**valid_request(), "mode": "re-review", "patches": patches, "scope": scope}
        record = build_record(request, valid_adapter_result(), version=2, policy={})
        validate_record(record)
        self.assertEqual((patches, scope), (record["review"]["patches"], record["review"]["scope"]))
        self.assertIn(
            "| **Scope** | incremental&#44; 1 of 1 files and 3 of 3 changed lines differ from v1 &#40;requested "
            "auto&#58; a small change&#41; |\n",
            render_markdown(record, record_payload_hash="0" * 64),
        )
        uncompared = copy.deepcopy(record)
        uncompared["review"]["scope"].update({"used": "full", "files_changed": None, "lines_changed": None})
        validate_record(uncompared)
        self.assertIn("full&#44; could not compare with v1", render_markdown(uncompared, record_payload_hash="0" * 64))
        for mutate, message in (
            (lambda r: r["review"].update(patches={}), "patches must be a non-empty object"),
            (lambda r: r["review"].update(patches={"../x.cs": patches["src/file.cs"]}), "patch path is unsafe"),
            (
                lambda r: r["review"]["patches"]["src/file.cs"].update(sha256="C" * 64),
                field_pattern(
                    "record.review.patches.src/file.cs.sha256",
                    "a lowercase hexadecimal digest of 64 characters",
                    NOT_HEX,
                ),
            ),
            (
                lambda r: r["review"]["patches"]["src/file.cs"].update(lines=-1),
                field_pattern("record.review.patches.src/file.cs.lines", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"].update(mode="initial"), "Only a re-review has a scope"),
            (
                lambda r: r["review"]["scope"].update(requested="most"),
                field_pattern("record.review.scope.requested", 'one of "auto", "full", "incremental"', 'string "most"'),
            ),
            (
                lambda r: r["review"]["scope"].update(used="auto"),
                field_pattern("record.review.scope.used", 'one of "full", "incremental"', 'string "auto"'),
            ),
            (
                lambda r: r["review"]["scope"].update(reason=" "),
                field_pattern("record.review.scope.reason", "a non-empty string", "a blank string"),
            ),
            (lambda r: r["review"]["scope"].update(since_version=2), "since_version must be an earlier version"),
            (lambda r: r["review"]["scope"].update(since_version=0), "since_version must be an earlier version"),
            (
                lambda r: r["review"]["scope"].update(files_total=-1),
                field_pattern("record.review.scope.files_total", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"]["scope"].update(files_changed=2), "within their totals"),
            (lambda r: r["review"]["scope"].update(lines_changed=None), "both be null"),
            (
                lambda r: r["review"]["scope"].update(files_changed=None, lines_changed=None),
                "incremental re-review must have compared",
            ),
            (
                lambda r: r["review"]["scope"].pop("reason"),
                field_pattern("record.review.scope", "an object with field reason", "an object without it"),
            ),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)
        without = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertFalse({"patches", "scope"} & set(without["review"]), "older records have neither")

    def test_record_comments_and_reviewers_are_validated(self) -> None:
        request = {
            **valid_request(),
            "reviewers": [
                {
                    "id": "generic-review",
                    "category": "General",
                    "files": 2,
                    "findings": 1,
                    "retries": 0,
                    "dispositions_only": False,
                }
            ],
            "github_comments": [
                {
                    "id": "C1",
                    "author": "dev",
                    "path": "src/file.cs",
                    "line": None,
                    "outdated": False,
                    "body": "Why?",
                    "url": "https://example.invalid/1",
                }
            ],
        }
        result: Any = {
            **valid_adapter_result(),
            "comment_dispositions": [{"comment_id": "C1", "disposition": "still_present", "rationale": "Unchanged."}],
        }
        record = build_record(request, result, version=1, policy={"request_changes_for": ["MUST_FIX"]})
        validate_record(record)
        for mutate, message in (
            (lambda r: r.pop("comment_dispositions"), field_pattern("record.comment_dispositions", "an array", "null")),
            (lambda r: r.pop("github_comments"), "need the comments"),
            (lambda r: r["comment_dispositions"].clear(), r"missing=\['C1'\]"),
            (lambda r: r["github_comments"][0].update(id="X1"), "C<n>"),
            (
                lambda r: r["review"]["reviewers"][0].update(retries=-1),
                field_pattern("record.review.reviewers[0].retries", "an integer of at least 0", "integer -1"),
            ),
            (lambda r: r["review"]["reviewers"].append(dict(r["review"]["reviewers"][0])), "unique"),
            (
                lambda r: r["review"].update(reviewers=[]),
                field_pattern("record.review.reviewers", "a non-empty array", "an empty array"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=-1),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "integer -1"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=1.5),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "number 1.5"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(seconds=True),
                field_pattern("record.review.reviewers[0].seconds", "an integer of at least 0", "boolean true"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(minutes=1),
                field_pattern("record.review.reviewers[0]", "an object of known fields", 'unknown field "minutes"'),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model=""),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "an empty string"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model="a\nb"),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "a string of more than one line"),
            ),
            (
                lambda r: r["review"]["reviewers"][0].update(model=5),
                field_pattern("record.review.reviewers[0].model", REVIEWER_MODEL, "integer 5"),
            ),
        ):
            broken = copy.deepcopy(record)
            mutate(broken)
            with self.subTest(message=message), self.assertRaisesRegex(RecordError, message):
                validate_record(broken)
        timed = copy.deepcopy(record)
        timed["review"]["reviewers"][0].update({"seconds": 0, "model": "claude-sonnet-5"})
        validate_record(timed)  # a reviewer's time is optional and may be zero
        without = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertNotIn("github_comments", without)
        self.assertNotIn("reviewers", without["review"])

        result["prior_dispositions"] = [{"finding_id": "v1:F001", "disposition": "addressed", "rationale": "Fixed."}]
        first = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        markdown = render_markdown(
            build_record({**request, "mode": "re-review"}, result, version=2, policy={}, prior_ledger=first["ledger"]),
            record_payload_hash="0" * 64,
            prior_records=[first],
        )
        # Prior findings are shown with the findings; review comments keep a section of their own.
        self.assertLess(markdown.index("Addressed in v2: Fixed&#46;"), markdown.index("## Review Comments"))
        self.assertIn(
            "| [C1](https://example.invalid/1) | &#64;dev on `src/file.cs`: Why&#63; "
            "| STILL PRESENT | Unchanged&#46; |",
            markdown,
        )
        self.assertIn("## Reviewers", markdown)
        self.assertLess(markdown.index("## Review Comments"), markdown.index("## Reviewers"))
        self.assertLess(markdown.index("## Reviewers"), markdown.index("Review Details</strong>"))
        self.assertIn("| STILL PRESENT | Unchanged&#46; |\n\n**0/1 addressed**\n", markdown)
        self.assertEqual(1, markdown.count(" addressed**"), "the prior findings' count is in the verdict row")

    def test_unhashable_values_are_validation_errors_not_crashes(self) -> None:
        arguments: dict[str, Any] = {
            "expected_repository": "example/one",
            "expected_number": 12,
            "expected_head_sha": "b" * 40,
        }
        for mutate, message in (
            (
                lambda r: r.update(status=["complete"]),
                field_pattern("adapter-result.status", 'one of "complete", "partial", "failed"', "an array"),
            ),
            (
                lambda r: r["findings"][0].update(severity=["MUST_FIX"]),
                field_pattern("adapter-result.findings[0].severity", SEVERITIES, "an array"),
            ),
            (
                lambda r: r.update(
                    prior_dispositions=[{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
                ),
                field_pattern("adapter-result.prior_dispositions[0].disposition", DISPOSITIONS, "an array"),
            ),
            (
                lambda r: r.update(
                    comment_dispositions=[{"comment_id": ["C1"], "disposition": "addressed", "rationale": "x"}]
                ),
                field_pattern("adapter-result.comment_dispositions[0].comment_id", "a non-empty string", "an array"),
            ),
        ):
            result = valid_adapter_result()
            result["prior_dispositions"] = [{"finding_id": "F001", "disposition": "addressed", "rationale": "x"}]
            mutate(result)
            with self.subTest(message=message), self.assertRaisesRegex(RecordFieldError, message):
                validate_adapter_result(result, prior_ids=["F001"], comment_ids=["C1"], **arguments)
        record = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        for field, value, expected, found in (
            ("verdict", ["APPROVED"], 'one of "APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"', "an array"),
            ("mode", {"initial": 1}, 'one of "initial", "re-review"', "an object"),
        ):
            broken: Any = copy.deepcopy(record)
            broken["review"][field] = value
            with (
                self.subTest(field=field),
                self.assertRaisesRegex(RecordFieldError, field_pattern(f"record.review.{field}", expected, found)),
            ):
                validate_record(broken)
        broken = copy.deepcopy(record)
        broken["prior_dispositions"] = [{"finding_id": "F001", "disposition": ["addressed"], "rationale": "x"}]
        with self.assertRaisesRegex(
            RecordFieldError, field_pattern("record.prior_dispositions[0].disposition", DISPOSITIONS, "an array")
        ):
            validate_record(broken)

    def test_adapter_result_rejects_unsafe_paths_and_mismatched_sha(self) -> None:
        result = valid_adapter_result()
        result["findings"][0]["path"] = "../outside.cs"
        with self.assertRaisesRegex(RecordError, "safe repository-relative"):
            validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

    def test_record_validation_rejects_count_tampering(self) -> None:
        result = validate_adapter_result(
            valid_adapter_result(), expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
        )
        record = build_record(
            valid_request(), result, version=1, policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
        )
        record["review"]["counts"]["SHOULD_FIX"] = 0
        with self.assertRaisesRegex(RecordError, "counts"):
            write_record_pair(Path("unused.json"), Path("unused.md"), record)
        result = valid_adapter_result()
        result["head_sha"] = "c" * 40
        with self.assertRaisesRegex(RecordError, "head SHA"):
            validate_adapter_result(
                result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
            )

    def test_verdict_policy_is_centralized(self) -> None:
        ledger: Any = [{"severity": "SHOULD_FIX", "state": "open"}] * 2
        policy = verdict_policy({"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3})
        self.assertEqual("APPROVED", calculate_verdict(ledger, policy))
        ledger.append({"severity": "SHOULD_FIX", "state": "open"})
        self.assertEqual("CHANGES_REQUESTED", calculate_verdict(ledger, policy))

    def test_record_pair_detects_markdown_tampering(self) -> None:
        request = valid_request()
        result = validate_adapter_result(
            valid_adapter_result(), expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
        )
        record = build_record(
            request,
            result,
            version=1,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            reviewed_at="2026-01-01T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            json_path = root / "review.json"
            markdown_path = root / "review.md"
            write_record_pair(json_path, markdown_path, record)
            validated = validate_record_pair(json_path, markdown_path)
            self.assertEqual("F001", validated["findings"][0]["id"])
            markdown_path.write_text(markdown_path.read_text(encoding="utf-8") + "tampered\n", encoding="utf-8")
            with self.assertRaisesRegex(RecordError, "Markdown hash mismatch"):
                validate_record_pair(json_path, markdown_path)

    def test_a_record_pair_without_artifact_hashes_is_refused(self) -> None:
        # The contract reads a null `artifacts` as absent, and an archived pair without them cannot be verified.
        record = build_record(
            valid_request(),
            validate_adapter_result(
                valid_adapter_result(),
                expected_repository="example/one",
                expected_number=12,
                expected_head_sha="b" * 40,
            ),
            version=1,
            policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
            reviewed_at="2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(record_artifacts(record))
        for case in ("null", "missing"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                json_path = Path(temporary) / "review.json"
                markdown_path = Path(temporary) / "review.md"
                persisted = write_record_pair(json_path, markdown_path, record)
                self.assertEqual(persisted["artifacts"], record_artifacts(persisted))
                stored = json.loads(json_path.read_text(encoding="utf-8"))
                if case == "null":
                    stored["artifacts"] = None
                else:
                    del stored["artifacts"]
                json_path.write_text(json.dumps(stored), encoding="utf-8")
                with self.assertRaisesRegex(RecordError, "^Review record has no artifact hashes$"):
                    validate_record_pair(json_path, markdown_path)

    def test_markdown_groups_findings_by_severity_in_the_legacy_layout(self) -> None:
        request = valid_request()
        request["mode"] = "re-review"
        request["title"] = "Fix | split"
        result = valid_adapter_result()
        result["findings"] = [
            {
                **result["findings"][0],
                "candidate_key": "late",
                "severity": "SUGGESTION",
                "path": "src/z.cs",
                "line": 9,
                "body": "Prefer a constant.",
                "evidence": "z.cs:9 adds: x",
            },
            {
                **result["findings"][0],
                "candidate_key": "early",
                "severity": "MUST_FIX",
                "category": "A<B>",
                "path": "src/a.cs",
                "line": 3,
                "title": "List<T> & friends leak",
                "body": "First paragraph.\n\nSecond paragraph.",
                "evidence": "a.cs:3 adds: f(`x`) | y",
            },
        ]
        result["prior_dispositions"] = [
            {"finding_id": "v2:F001", "disposition": "partially_addressed", "rationale": "One | of two."}
        ]
        prior = build_record(valid_request(), valid_adapter_result(), version=2, policy={})
        record = build_record(
            request, result, version=3, policy={}, reviewed_at="2026-10-01T23:26:42+02:00", prior_ledger=prior["ledger"]
        )
        markdown = render_markdown(record, record_payload_hash="0" * 64, prior_records=[prior])
        self.assertTrue(markdown.startswith("# Code Review — example/one#12 (re-review v3)\n\n| | |\n|---|---|\n"))
        for expected in (
            "| **Title** | Fix &#124; split |",
            "| **Reviewed** | 01-Oct-2026 21:26 UTC |",
            "| **Verdict** | CHANGES REQUESTED, 3 open since v2 |",
            "<summary><strong>MUST FIX (1)</strong></summary>",
            "<summary>v3 F001. [A&lt;B&gt;] List&lt;T&gt; &amp; friends leak</summary>",
            "<summary>v3 F002. [Correctness] <code>z&#46;cs:9</code></summary>",
            "> **File:** `src/a.cs`  \n> **Line:** 3 | **Source:** generic\n>\n"
            "> First paragraph&#46;\n>\n> Second paragraph&#46;\n>\n> **Evidence:** ``a.cs:3 adds: f(`x`) | y``\n",
            "<summary><strong>SHOULD FIX (1)</strong></summary>",
            "> **Open since v2.** Partially addressed in v3: One &#124; of two&#46;\n",
            "<summary><strong>SUGGESTIONS (1)</strong></summary>",
            "| **Record payload SHA-256** | `" + "0" * 64 + "` |",
        ):
            self.assertIn(expected, markdown)
        self.assertLess(markdown.index("MUST FIX (1)"), markdown.index("SHOULD FIX (1)"))
        self.assertLess(markdown.index("SHOULD FIX (1)"), markdown.index("SUGGESTIONS (1)"))
        self.assertTrue(markdown.endswith(f"<!-- reviewed_head_sha: {'b' * 40} -->\n"))
        self.assertEqual(len(re.findall(r"<details[ >]", markdown)), markdown.count("</details>"))
        self.assertNotIn("addressed**", markdown)
        self.assertIn("<details>\n<summary><strong>Review Details</strong></summary>", markdown)
        self.assertIn("| **Base** | `main` |", markdown)
        empty = valid_adapter_result()
        empty["findings"] = []
        markdown = render_markdown(
            build_record(valid_request(), empty, version=1, policy={}), record_payload_hash="0" * 64
        )
        self.assertIn("## Findings\n\nNo findings.\n", markdown)
        self.assertNotIn("<details open>", markdown)
        self.assertNotIn("Prior Findings Status", markdown)

    def test_a_review_of_a_change_over_the_diff_endpoint_s_limit_says_so_in_one_line(self) -> None:
        snapshot = {
            "source": "tarball",
            "files": 2,
            "bytes": 30,
            "seconds": {"fetch": 1, "materialize": 0, "prompts": 0},
        }
        for source, words in (("files-api", "GitHub's files API"), ("checkout", "git's diff in the checkout")):
            with self.subTest(source=source):
                over = {**snapshot, "diff": {"source": source, "limit": "300 files"}}
                record = build_record(
                    {**valid_request(), "snapshot": over}, valid_adapter_result(), version=1, policy={}
                )
                self.assertEqual(over, validate_record(record)["review"]["snapshot"])
                markdown = render_markdown(record, record_payload_hash="0" * 64)
                note = (
                    "> **Large change:** GitHub's diff endpoint refuses a diff over 300 files, so this review read the "
                    f"diff from {words}.\n"
                )
                self.assertEqual(1, markdown.count(note))
        within = build_record({**valid_request(), "snapshot": snapshot}, valid_adapter_result(), version=1, policy={})
        self.assertNotIn("**Large change:**", render_markdown(within, record_payload_hash="0" * 64))

    def test_specialist_evidence_joins_the_line_and_the_head_branch_is_shown(self) -> None:
        request = {**valid_request(), "head_ref": "feature/zero-total"}
        result = valid_adapter_result()
        result["findings"][0]["evidence"] = "src/file.cs:42 adds:     return items.Sum();"
        record = build_record(request, result, version=1, policy={})
        validate_record(record)
        self.assertEqual("feature/zero-total", record["pull_request"]["head_ref"])
        markdown = render_markdown(record, record_payload_hash="0" * 64)
        self.assertIn("| **Branch** | `feature/zero-total` → `main` |", markdown)
        self.assertIn("> **Line 42:** `return items.Sum();` | **Source:** generic\n", markdown)
        self.assertNotIn("**Evidence:**", markdown)
        broken = copy.deepcopy(record)
        broken["pull_request"]["head_ref"] = " "
        with self.assertRaisesRegex(
            RecordFieldError, field_pattern("record.pull_request.head_ref", "a non-empty string", "a blank string")
        ):
            validate_record(broken)

    def test_the_record_keeps_how_much_of_the_description_reviewers_were_given_and_the_report_says_so(self) -> None:
        rows = {
            (0, 0): "| **Description** | none |",
            (1234, 1234): "| **Description** | 1,234 characters, given to reviewers whole |",
            (70_000, 24_000): "| **Description** | 70,000 characters, of which reviewers were given the first 24,000 |",
        }
        for (characters, given), row in rows.items():
            with self.subTest(characters=characters):
                request = {**valid_request(), "body_characters": characters, "body_given": given}
                record = build_record(request, valid_adapter_result(), version=1, policy={})
                validate_record(record)
                self.assertEqual(
                    (characters, given),
                    (record["pull_request"]["body_characters"], record["pull_request"]["body_given"]),
                )
                markdown = render_markdown(record, record_payload_hash="0" * 64)
                self.assertEqual(
                    [row], [line for line in markdown.splitlines() if line.startswith("| **Description**")]
                )
        older = build_record(valid_request(), valid_adapter_result(), version=1, policy={})
        self.assertNotIn("body_characters", older["pull_request"])
        self.assertNotIn("**Description**", render_markdown(older, record_payload_hash="0" * 64))
        faults: list[tuple[Any, Any]] = [(5, 6), (-1, 0), (True, 0), (5, "5")]
        for characters, given in faults:
            with self.subTest(characters=characters, given=given):
                broken = copy.deepcopy(older)
                broken["pull_request"].update({"body_characters": characters, "body_given": given})
                with self.assertRaisesRegex(RecordError, "body_"):
                    validate_record(broken)
