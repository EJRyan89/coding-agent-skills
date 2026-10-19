"""The review's closed vocabularies, each pinned to its members as the record contract names them, and the tables and
readers built from them."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import get_args

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_vocabularies as vocabularies
from review_config import VerdictPolicy, verdict_policy
from review_operation import LEGACY_COUNT, LEGACY_SECTIONS
from review_specialists import SEVERITY


def test_severities_run_most_severe_first_with_their_ranks_and_section_titles() -> None:
    assert get_args(vocabularies.Severity) == ("MUST_FIX", "SHOULD_FIX", "SUGGESTION")
    assert vocabularies.SEVERITIES == ("MUST_FIX", "SHOULD_FIX", "SUGGESTION")
    assert vocabularies.SEVERITY_RANK == {"SUGGESTION": 1, "SHOULD_FIX": 2, "MUST_FIX": 3}
    assert vocabularies.SEVERITY_SECTIONS == (
        ("MUST_FIX", "MUST FIX"),
        ("SHOULD_FIX", "SHOULD FIX"),
        ("SUGGESTION", "SUGGESTIONS"),
    )


def test_a_role_may_write_a_severity_in_words_and_the_record_spells_it_with_underscores() -> None:
    assert list(SEVERITY.items()) == [
        ("MUST_FIX", "MUST_FIX"),
        ("MUST FIX", "MUST_FIX"),
        ("SHOULD_FIX", "SHOULD_FIX"),
        ("SHOULD FIX", "SHOULD_FIX"),
        ("SUGGESTION", "SUGGESTION"),
    ]


def test_a_legacy_report_counts_each_severity_under_its_section_title() -> None:
    assert LEGACY_SECTIONS == {"MUST FIX": "MUST_FIX", "SHOULD FIX": "SHOULD_FIX", "SUGGESTIONS": "SUGGESTION"}
    headings = "".join(
        f"<summary><strong>{title} ({count})</strong></summary>"
        for title, count in (("MUST FIX", 1), ("SHOULD FIX", 2), ("SUGGESTIONS", 3))
    )
    assert LEGACY_COUNT.findall(headings) == [("MUST FIX", "1"), ("SHOULD FIX", "2"), ("SUGGESTIONS", "3")]
    assert LEGACY_COUNT.findall("<summary><strong>SUGGESTION (3)</strong></summary>") == []


def test_verdicts() -> None:
    assert get_args(vocabularies.Verdict) == ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
    assert vocabularies.VERDICTS == ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")


def test_dispositions_and_the_ledger_state_each_leaves() -> None:
    dispositions = ("addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify")
    assert get_args(vocabularies.Disposition) == dispositions
    assert dispositions == vocabularies.DISPOSITIONS
    assert {"still_present", "partially_addressed"} == vocabularies.OPEN_DISPOSITIONS
    assert get_args(vocabularies.LedgerState) == ("open", "closed", "unverified")
    assert vocabularies.LEDGER_STATE_NAMES == ("open", "closed", "unverified")
    assert vocabularies.LEDGER_STATES == {
        "still_present": "open",
        "partially_addressed": "open",
        "addressed": "closed",
        "superseded": "closed",
        "unable_to_verify": "unverified",
    }


def test_dispatch_modes() -> None:
    assert get_args(vocabularies.DispatchMode) == ("subagents", "copilot-host", "inline")
    assert vocabularies.DISPATCH_MODES == ("subagents", "copilot-host", "inline")


def test_a_verdict_policy_fills_its_defaults_once_and_keeps_what_it_is_given() -> None:
    assert verdict_policy({}) == VerdictPolicy(frozenset({"MUST_FIX"}), 3)
    given = {"request_changes_for": ["MUST_FIX", "SHOULD_FIX"], "should_fix_threshold": 2}
    assert verdict_policy(given) == VerdictPolicy(frozenset({"MUST_FIX", "SHOULD_FIX"}), 2)
