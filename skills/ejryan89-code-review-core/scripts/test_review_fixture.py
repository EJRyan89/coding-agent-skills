"""A three-version review archive for the finding-ledger tests of ejryan89-code-review-core, flag-review-finding, and
update-pr-dashboard, and the tests that hold it to what this says. Its name makes it a regression suite, so it runs as
one and the deployer leaves it out of what it deploys, as it leaves every suite.

Version 1 raises a must-fix that stays open and a should-fix that version 2 addresses. Version 2 reports nothing new.
Version 3 reports a new suggestion and repeats the open must-fix. Each version's head is its digit repeated.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from review_archive import commit_record, current_ledger, pull_records
from review_records import ReviewRecord, build_record, carried_findings, validate_adapter_result

REPOSITORY = "example/one"
NUMBER = 12
HEADS = {version: str(version) * 40 for version in (1, 2, 3)}
MODEL_ARN = "arn:aws:bedrock:us-east-1:111122223333:application-inference-profile/fixture"
POLICY = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
ADAPTER: dict[str, Any] = {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}


def _finding(
    key: str, severity: str, category: str, path: str, line: int, title: str, added: str, body: str, **extra: Any
) -> dict[str, Any]:
    return {
        "candidate_key": key,
        "severity": severity,
        "category": category,
        "path": path,
        "line": line,
        "title": title,
        "body": body,
        "evidence": f"{path}:{line} adds: {added}",
        "source": "generic",
        **extra,
    }


def _disposition(identifier: str, value: str, rationale: str) -> dict[str, str]:
    return {"finding_id": identifier, "disposition": value, "rationale": rationale}


VERSIONS: tuple[dict[str, Any], ...] = (
    {
        "mode": "initial",
        "findings": [
            _finding(
                "lock",
                "MUST_FIX",
                "Correctness",
                "src/lock.py",
                10,
                "Lock is never released",
                "lock.acquire()",
                "The lock taken here is not released when the read fails.",
            ),
            _finding(
                "null",
                "SHOULD_FIX",
                "Correctness",
                "src/parse.py",
                20,
                "Null result is not checked",
                "value = parse(text)",
                "A null result from parse reaches the caller unchecked.",
            ),
        ],
        "dispositions": [],
    },
    {
        "mode": "re-review",
        "used": "incremental",
        "findings": [],
        "dispositions": [
            _disposition("v1:F001", "still_present", "The read still runs outside a try block."),
            _disposition("v1:F002", "addressed", "The caller now returns early on null."),
        ],
    },
    {
        "mode": "re-review",
        "used": "full",
        "findings": [
            _finding(
                "timeout",
                "MUST_FIX",
                "Correctness",
                "src/lock.py",
                14,
                "Lock leaks on the timeout path",
                "return None",
                "The timeout branch returns before releasing the lock.",
                repeats="v1:F001",
            ),
            _finding(
                "retry",
                "SUGGESTION",
                "Maintainability",
                "src/retry.py",
                5,
                "Name the retry limit",
                "for attempt in range(3):",
                "Name the retry limit so its purpose is clear.",
            ),
        ],
        "dispositions": [
            _disposition("v1:F001", "still_present", "The timeout branch still returns while holding the lock."),
        ],
        "reviewers": [
            {
                "id": "generic",
                "category": "General",
                "files": 3,
                "findings": 1,
                "retries": 0,
                "dispositions_only": False,
                "seconds": 40,
                "model": MODEL_ARN,
            },
            {
                "id": "style",
                "category": "Style",
                "files": 1,
                "findings": 1,
                "retries": 0,
                "dispositions_only": False,
                "seconds": 12,
                "model": "claude-sonnet-5-5",
            },
        ],
    },
)


def commit_fixture(
    archive: Path, *, versions: int = 3, model_names: dict[str, str] | None = None, flags: Iterable[dict[str, Any]] = ()
) -> list[ReviewRecord]:
    """Commit the first `versions` reviews of example/one#12 to `archive` and return the persisted records."""
    flags = list(flags)
    persisted = []
    for version, spec in enumerate(VERSIONS[:versions], start=1):
        records = pull_records(archive, REPOSITORY, NUMBER)
        prior = carried_findings(records) if spec["mode"] == "re-review" else []
        head = HEADS[version]
        result = validate_adapter_result(
            {
                "protocol_version": 1,
                "repository": REPOSITORY,
                "pull_number": NUMBER,
                "head_sha": head,
                "summary": f"Version {version} of the fixture.",
                "reviewer": "fixture-reviewer",
                "status": "complete",
                "findings": spec["findings"],
                "prior_dispositions": spec["dispositions"],
                "usage": None,
            },
            expected_repository=REPOSITORY,
            expected_number=NUMBER,
            expected_head_sha=head,
            prior_ids=[item["id"] for item in prior],
            prior_severities={item["id"]: item["severity"] for item in prior},
        )
        request = {
            "repository": REPOSITORY,
            "pull_number": NUMBER,
            "pull_url": f"https://github.com/{REPOSITORY}/pull/12",
            "title": "Release the lock",
            "base_ref": "main",
            "base_sha": "0" * 40,
            "head_sha": head,
            "mode": spec["mode"],
            "adapter": ADAPTER,
            "reviewers": spec.get("reviewers", []),
        }
        if spec["mode"] == "re-review":
            request["scope"] = {
                "requested": spec["used"],
                "used": spec["used"],
                "reason": "requested",
                "since_version": version - 1,
                "files_changed": 1,
                "files_total": 3,
                "lines_changed": 4,
                "lines_total": 30,
            }
        prior_ledger = current_ledger(archive, REPOSITORY, NUMBER) if spec["mode"] == "re-review" else []
        record = build_record(
            request,
            result,
            version=version,
            policy=POLICY,
            reviewed_at=f"2026-10-0{version}T09:30:00+00:00",
            prior_ledger=prior_ledger,
        )
        _, _, saved = commit_record(
            archive,
            REPOSITORY,
            NUMBER,
            record,
            expected_latest_version=version - 1 if version > 1 else None,
            model_names=model_names,
            flags=flags,
        )
        persisted.append(saved)
    return persisted


class ReviewFixtureTests(unittest.TestCase):
    """The archive holds what the module docstring says, since every suite that commits it relies on that."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name)

    def test_the_three_versions_raise_address_and_repeat_the_documented_findings(self) -> None:
        records = commit_fixture(self.archive)
        self.assertEqual(
            [
                [("F001", "MUST_FIX", "open"), ("F002", "SHOULD_FIX", "open")],
                [("F001", "MUST_FIX", "open"), ("F002", "SHOULD_FIX", "closed")],
                [("F001", "MUST_FIX", "open"), ("F002", "SHOULD_FIX", "closed"), ("F002", "SUGGESTION", "open")],
            ],
            [[(entry["id"], entry["severity"], entry["state"]) for entry in record["ledger"]] for record in records],
        )
        self.assertEqual(["1" * 40, "2" * 40, "3" * 40], [record["pull_request"]["head_sha"] for record in records])
        self.assertEqual([REPOSITORY] * 3, [record["repository"] for record in records])

    def test_fewer_versions_commit_only_the_first_reviews(self) -> None:
        self.assertEqual(1, len(commit_fixture(self.archive, versions=1)))
        self.assertEqual(1, len(pull_records(self.archive, REPOSITORY, NUMBER)))
