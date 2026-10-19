from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))

import review_insights as ri
import review_signals
import review_synthesis as rs
from review_archive import commit_record, current_ledger
from review_records import build_record, validate_adapter_result
from test_review_insights import LINE_BREAKS, InsightFixture, reviewer

HEAD = "c" * 40
OTHER_HEAD = "d" * 40
POLICY = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
SIGNAL_PREFIXES = (
    "SEVERITY_DRIFT",
    "DRIFT_EXAMPLE",
    "VERDICT_FLIP",
    "FLIP_DIFFERENCE",
    "REVIEWER_DISAGREEMENT",
    "DISAGREEING",
    "GUIDANCE_OVERLAP",
    "GUIDANCE_UNSUPPLIED",
    "SIGNALS_OMITTED",
)


def finding(severity: str, path: str, line: int, title: str, **extra: Any) -> dict[str, Any]:
    """A Correctness finding from source `fixture` unless `extra` says otherwise."""
    return {
        "candidate_key": f"{path}:{line}:{severity}",
        "severity": severity,
        "category": "Correctness",
        "path": path,
        "line": line,
        "title": title,
        "body": f"{title}.",
        "evidence": "Evidence.",
        "source": "fixture",
        **extra,
    }


class SignalFixture(InsightFixture):
    def review(
        self,
        number: int,
        version: int,
        findings: list[dict[str, Any]],
        *,
        head: str = HEAD,
        mode: str = "initial",
        dispositions: tuple[tuple[str, str], ...] = (),
        reviewers: list[dict[str, Any]] | None = None,
    ) -> None:
        """Commit one review of owner/repo; a re-review carries the prior ledger, as `finalize` assembles it."""
        result: Any = {
            "protocol_version": 1,
            "repository": "owner/repo",
            "pull_number": number,
            "head_sha": head,
            "summary": "Fixture",
            "reviewer": "fixture",
            "status": "complete",
            "findings": findings,
            "prior_dispositions": [
                {"finding_id": identifier, "disposition": value, "rationale": "Checked."}
                for identifier, value in dispositions
            ],
            "usage": None,
        }
        if mode == "initial":
            result = validate_adapter_result(
                result, expected_repository="owner/repo", expected_number=number, expected_head_sha=head
            )
        request = {
            "repository": "owner/repo",
            "pull_number": number,
            "pull_url": f"https://github.com/owner/repo/pull/{number}",
            "title": "Fixture",
            "base_ref": "main",
            "base_sha": "a" * 40,
            "head_sha": head,
            "mode": mode,
            "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
            **({"reviewers": reviewers} if reviewers else {}),
        }
        built = build_record(
            request,
            result,
            version=version,
            policy=POLICY,
            reviewed_at=f"2026-01-{10 + version:02d}T12:00:00+00:00",
            prior_ledger=current_ledger(self.archive, "owner/repo", number) if mode == "re-review" else None,
        )
        commit_record(self.archive, "owner/repo", number, built, expected_latest_version=version - 1 or None)

    def signal_lines(self) -> tuple[Path, list[str]]:
        code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        return Path(lines[0].removeprefix("REPORT ")), [line for line in lines if line.startswith(SIGNAL_PREFIXES)]


class SeverityDriftTests(SignalFixture):
    def test_a_group_with_mixed_severities_prints_its_distribution_and_an_example_of_each(self) -> None:
        self.review(
            7,
            1,
            [
                finding("SHOULD_FIX", "src/a.cs", 3, "Missing timezone"),
                finding("MUST_FIX", "src/b.cs", 9, "Missing timezone"),
                finding("SHOULD_FIX", "src/c.cs", 1, "Missing timezone"),
            ],
        )
        json_path, lines = self.signal_lines()
        self.assertEqual(
            [
                "SEVERITY_DRIFT D1 basis=group repository=owner/repo severities=MUST_FIX:1,SHOULD_FIX:2 "
                "Correctness: Missing timezone",
                "DRIFT_EXAMPLE D1 MUST_FIX owner/repo#7 v1 F002",
                "DRIFT_EXAMPLE D1 SHOULD_FIX owner/repo#7 v1 F001",
                "DRIFT_EXAMPLE D1 SHOULD_FIX owner/repo#7 v1 F003",
            ],
            lines,
        )
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(
            "## Consistency signals\n\n### Severity drift\n\n- D1 (group) owner/repo Correctness: Missing timezone — "
            "MUST_FIX:1,SHOULD_FIX:2; e.g. MUST_FIX owner/repo#7 v1 F002; SHOULD_FIX owner/repo#7 v1 F001; "
            "SHOULD_FIX owner/repo#7 v1 F003\n",
            markdown,
        )
        self.assertLess(markdown.index("## Consistency signals"), markdown.index("## Synthesis"))

    def test_a_finding_repeated_at_a_lower_severity_drifts_along_its_ledger(self) -> None:
        self.review(7, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        repeat = finding("SHOULD_FIX", "src/a.cs", 3, "Unchecked input", repeats="v1:F001")
        self.review(7, 2, [repeat], head=OTHER_HEAD, mode="re-review", dispositions=(("v1:F001", "still_present"),))
        _, lines = self.signal_lines()
        self.assertEqual(
            [
                "SEVERITY_DRIFT D1 basis=ledger repository=owner/repo severities=MUST_FIX:1,SHOULD_FIX:1 "
                "Correctness: Unchecked input",
                "DRIFT_EXAMPLE D1 MUST_FIX owner/repo#7 v1 F001",
                "DRIFT_EXAMPLE D1 SHOULD_FIX owner/repo#7 v2 F001",
            ],
            lines,
        )

    def test_a_period_without_drift_prints_no_signal_and_renders_no_section(self) -> None:
        self.review(
            7,
            1,
            [finding("SHOULD_FIX", "src/a.cs", 3, "Missing timezone"), finding("SHOULD_FIX", "src/b.cs", 3, "Other")],
        )
        self.review(7, 2, [finding("SHOULD_FIX", "src/a.cs", 3, "Missing timezone")], head=OTHER_HEAD)
        json_path, lines = self.signal_lines()
        self.assertEqual([], lines)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(review_signals.empty(), report["signals"])
        self.assertNotIn("## Consistency signals", json_path.with_suffix(".md").read_text(encoding="utf-8"))
        kinds = {item["kind"] for item in _input(json_path)}
        self.assertFalse(kinds & {"severity-drift", "verdict-flip", "reviewer-disagreement"}, kinds)


class VerdictFlipTests(SignalFixture):
    def test_two_reviews_of_the_same_head_with_different_verdicts_print_the_findings_that_differ(self) -> None:
        self.review(7, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        self.review(
            7,
            2,
            [finding("SHOULD_FIX", "src/a.cs", 4, "Input is not checked"), finding("SUGGESTION", "src/b.cs", 1, "Nit")],
        )
        json_path, lines = self.signal_lines()
        self.assertEqual(
            [
                f"VERDICT_FLIP V1 owner/repo#7 head={HEAD[:12]} v1=CHANGES_REQUESTED v2=APPROVED",
                "FLIP_DIFFERENCE V1 src/a.cs:3 MUST_FIX -> SHOULD_FIX owner/repo#7 v1 F001, owner/repo#7 v2 F001",
                "FLIP_DIFFERENCE V1 src/b.cs:1 absent -> SUGGESTION owner/repo#7 v2 F002",
            ],
            [line for line in lines if line.startswith(("VERDICT_FLIP", "FLIP_DIFFERENCE"))],
        )
        flips = [item for item in _input(json_path) if item["kind"] == "verdict-flip"]
        self.assertEqual([[1, 2], ["CHANGES_REQUESTED", "APPROVED"]], [flips[0]["versions"], flips[0]["verdicts"]])

    def test_a_re_review_that_closes_a_finding_at_the_same_head_shows_it_absent_after(self) -> None:
        self.review(7, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        self.review(7, 2, [], mode="re-review", dispositions=(("v1:F001", "addressed"),))
        _, lines = self.signal_lines()
        self.assertEqual(
            [
                f"VERDICT_FLIP V1 owner/repo#7 head={HEAD[:12]} v1=CHANGES_REQUESTED v2=APPROVED",
                "FLIP_DIFFERENCE V1 src/a.cs:3 MUST_FIX -> absent owner/repo#7 v1 F001",
            ],
            lines,
        )

    def test_a_verdict_that_changes_with_the_code_is_no_flip(self) -> None:
        self.review(7, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        self.review(7, 2, [], head=OTHER_HEAD, mode="re-review", dispositions=(("v1:F001", "addressed"),))
        _, lines = self.signal_lines()
        self.assertEqual([], lines)


class DisagreementTests(SignalFixture):
    REVIEWERS: ClassVar = [reviewer("security", "model-a"), reviewer("style", "model-b")]

    def test_reviewers_sharing_no_source_that_rate_one_place_differently_disagree(self) -> None:
        self.review(
            7,
            1,
            [
                finding("MUST_FIX", "src/a.cs", 10, "Unvalidated path", source="security"),
                finding("SUGGESTION", "src/a.cs", 12, "Name the path variable", source="style"),
                finding("SHOULD_FIX", "src/a.cs", 40, "Far away", source="style"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual(
            [
                f"REVIEWER_DISAGREEMENT R1 owner/repo#7 head={HEAD[:12]} src/a.cs:10",
                "DISAGREEING R1 MUST_FIX owner/repo#7 v1 F001 security",
                "DISAGREEING R1 SUGGESTION owner/repo#7 v1 F002 style",
            ],
            lines,
        )

    def test_a_finding_a_few_lines_earlier_never_hides_a_disagreement_one_line_away(self) -> None:
        self.review(
            7,
            1,
            [
                finding("SHOULD_FIX", "src/a.cs", 10, "Earlier", source="style"),
                finding("SHOULD_FIX", "src/a.cs", 13, "Nearby", source="style"),
                finding("MUST_FIX", "src/a.cs", 14, "Unvalidated path", source="security"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual(
            [
                f"REVIEWER_DISAGREEMENT R1 owner/repo#7 head={HEAD[:12]} src/a.cs:13",
                "DISAGREEING R1 SHOULD_FIX owner/repo#7 v1 F002 style",
                "DISAGREEING R1 MUST_FIX owner/repo#7 v1 F003 security",
            ],
            lines,
        )

    def test_a_finding_between_a_disagreeing_pair_in_place_order_never_hides_it(self) -> None:
        # The middle finding agrees with each end: it shares the first's reviewer and the last's severity.
        self.review(
            7,
            1,
            [
                finding("MUST_FIX", "src/a.cs", 10, "Unvalidated path", source="security"),
                finding("SUGGESTION", "src/a.cs", 11, "Log the path", source="security"),
                finding("SUGGESTION", "src/a.cs", 12, "Name the path variable", source="style"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual(
            [
                f"REVIEWER_DISAGREEMENT R1 owner/repo#7 head={HEAD[:12]} src/a.cs:10",
                "DISAGREEING R1 MUST_FIX owner/repo#7 v1 F001 security",
                "DISAGREEING R1 SUGGESTION owner/repo#7 v1 F003 style",
            ],
            lines,
        )

    def test_findings_three_lines_apart_disagree_and_four_lines_apart_do_not(self) -> None:
        self.review(
            7,
            1,
            [
                finding("MUST_FIX", "src/a.cs", 10, "Unvalidated path", source="security"),
                finding("SUGGESTION", "src/a.cs", 13, "Name the path variable", source="style"),
                finding("MUST_FIX", "src/b.cs", 10, "Unvalidated path", source="security"),
                finding("SUGGESTION", "src/b.cs", 14, "Name the path variable", source="style"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual(
            [f"REVIEWER_DISAGREEMENT R1 owner/repo#7 head={HEAD[:12]} src/a.cs:10"],
            [line for line in lines if line.startswith("REVIEWER_DISAGREEMENT")],
        )

    def test_findings_joined_by_disagreeing_pairs_are_one_place_and_distant_ones_are_another(self) -> None:
        self.review(
            7,
            1,
            [
                finding("SUGGESTION", "src/a.cs", 10, "First", source="style"),
                finding("MUST_FIX", "src/a.cs", 13, "Second", source="security"),
                finding("SUGGESTION", "src/a.cs", 16, "Third", source="style"),
                finding("SHOULD_FIX", "src/a.cs", 30, "Fourth", source="style"),
                finding("MUST_FIX", "src/a.cs", 33, "Fifth", source="security"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual(
            [line for line in lines if line.startswith("REVIEWER_DISAGREEMENT")],
            [
                f"REVIEWER_DISAGREEMENT R1 owner/repo#7 head={HEAD[:12]} src/a.cs:10",
                f"REVIEWER_DISAGREEMENT R2 owner/repo#7 head={HEAD[:12]} src/a.cs:30",
            ],
        )
        self.assertEqual(3, sum(line.startswith("DISAGREEING R1 ") for line in lines))

    def test_a_cluster_cut_to_its_kept_reports_keeps_the_pair_that_disagrees(self) -> None:
        # Five findings of one severity from one reviewer and the one that disagrees with them, raised last.
        self.review(
            7,
            1,
            [
                *(
                    finding("SUGGESTION", "src/a.cs", line, f"Style {index}", source="style", candidate_key=f"s{index}")
                    for index, line in enumerate((11, 12, 13, 13, 14), start=1)
                ),
                finding("MUST_FIX", "src/a.cs", 14, "Unvalidated path", source="security"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        reports = [line for line in lines if line.startswith("DISAGREEING R1 ")]
        self.assertEqual(review_signals.REPORTS_KEPT, len(reports), lines)
        self.assertIn("DISAGREEING R1 MUST_FIX owner/repo#7 v1 F006 security", reports)
        self.assertEqual(
            sorted(reports, key=lambda line: line.split()[-2]), reports, "the kept reports stay in place order"
        )

    def test_a_path_holding_a_line_break_prints_and_renders_on_one_line(self) -> None:
        # A path is whatever the pull request named; the fact line and the Markdown bullet each stay one line.
        for number, character in enumerate(LINE_BREAKS, start=1):
            path = f"src/a{number}{character}FORGED.cs"
            self.review(
                number,
                1,
                [
                    finding("MUST_FIX", path, 10, "Unvalidated path", source="security"),
                    finding("SUGGESTION", path, 12, "Name the path variable", source="style"),
                ],
                reviewers=self.REVIEWERS,
            )
        json_path, lines = self.signal_lines()
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8").splitlines()
        self.assertEqual([], [line for line in markdown if line.startswith("FORGED")])
        for number, character in enumerate(LINE_BREAKS, start=1):
            with self.subTest(character=repr(character)):
                shown = f"owner/repo#{number} head={HEAD[:12]} src/a{number} FORGED.cs:10"
                self.assertEqual(1, sum(line.endswith(shown) for line in lines), lines)
                bullet = f" owner/repo#{number} src/a{number} FORGED.cs:10 — MUST_FIX owner/repo#{number} v1 F001"
                self.assertEqual(1, sum(line.startswith("- R") and bullet in line for line in markdown), markdown)

    def test_reviewers_that_share_a_source_or_agree_are_no_disagreement(self) -> None:
        self.review(
            7,
            1,
            [
                finding("MUST_FIX", "src/a.cs", 10, "Unvalidated path", source="security + style"),
                finding("SUGGESTION", "src/a.cs", 11, "Name the path variable", source="style"),
                finding("SHOULD_FIX", "src/b.cs", 5, "First", source="security"),
                finding("SHOULD_FIX", "src/b.cs", 6, "Second", source="style"),
            ],
            reviewers=self.REVIEWERS,
        )
        _, lines = self.signal_lines()
        self.assertEqual([], lines)


class CapTests(SignalFixture):
    def test_each_signal_keeps_its_strongest_items_and_counts_the_rest(self) -> None:
        self.review(
            7,
            1,
            [
                finding("SHOULD_FIX", "src/a.cs", 3, "Alpha"),
                finding("MUST_FIX", "src/b.cs", 3, "Alpha"),
                finding("SUGGESTION", "src/c.cs", 3, "Alpha"),
                finding("SHOULD_FIX", "src/d.cs", 3, "Bravo"),
                finding("SUGGESTION", "src/e.cs", 3, "Bravo"),
            ],
        )
        with mock.patch.object(review_signals, "SIGNALS_KEPT", 1):
            json_path, lines = self.signal_lines()
        self.assertEqual(
            [
                "SEVERITY_DRIFT D1 basis=group repository=owner/repo severities=MUST_FIX:1,SHOULD_FIX:1,SUGGESTION:1 "
                "Correctness: Alpha",
                "SIGNALS_OMITTED severity_drift 1",
            ],
            [line for line in lines if not line.startswith("DRIFT_EXAMPLE")],
        )
        signals = json.loads(json_path.read_text(encoding="utf-8"))["signals"]
        self.assertEqual((1, 2), (len(signals["severity_drift"]), signals["counts"]["severity_drift"]))
        self.assertEqual(["D1"], [item["id"] for item in _input(json_path) if item["kind"] == "severity-drift"])

    def test_verdict_flips_keep_the_flip_with_the_most_differences_not_the_first_pull(self) -> None:
        self.review(7, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        self.review(7, 2, [finding("SHOULD_FIX", "src/a.cs", 4, "Input is not checked")])
        self.review(8, 1, [finding("MUST_FIX", "src/a.cs", 3, "Unchecked input")])
        self.review(
            8,
            2,
            [finding("SHOULD_FIX", "src/a.cs", 4, "Input is not checked"), finding("SUGGESTION", "src/b.cs", 1, "Nit")],
        )
        with mock.patch.object(review_signals, "SIGNALS_KEPT", 1):
            _, lines = self.signal_lines()
        self.assertEqual(
            [f"VERDICT_FLIP V1 owner/repo#8 head={HEAD[:12]} v1=CHANGES_REQUESTED v2=APPROVED"],
            [line for line in lines if line.startswith("VERDICT_FLIP")],
        )
        self.assertIn("SIGNALS_OMITTED verdict_flips 1", lines)

    def test_a_flip_between_approval_and_requested_changes_outranks_one_from_an_incomplete_review(self) -> None:
        flip = {
            "verdicts": ["APPROVED", "CHANGES_REQUESTED"],
            "repository": "owner/repo",
            "pull_number": 9,
            "versions": [1, 2],
        }
        incomplete = {**flip, "verdicts": ["INCOMPLETE", "CHANGES_REQUESTED"], "pull_number": 1}
        self.assertLess(review_signals.flip_rank(flip, 1), review_signals.flip_rank(incomplete, 5))

    def test_disagreements_keep_the_widest_spread_then_the_most_findings_not_the_first_pull(self) -> None:
        security, style = [reviewer("security", "model-a")], [reviewer("style", "model-b")]
        places = {
            7: [("MUST_FIX", "security"), ("SHOULD_FIX", "style")],
            8: [("MUST_FIX", "security"), ("SHOULD_FIX", "style"), ("SHOULD_FIX", "style")],
            9: [("MUST_FIX", "security"), ("SUGGESTION", "style")],
        }
        for number, raised in places.items():
            self.review(
                number,
                1,
                [
                    finding(severity, "src/a.cs", 10 + index, f"Finding {index}", source=source)
                    for index, (severity, source) in enumerate(raised)
                ],
                reviewers=security + style,
            )
        with mock.patch.object(review_signals, "SIGNALS_KEPT", 2):
            _, lines = self.signal_lines()
        self.assertEqual(
            [
                f"REVIEWER_DISAGREEMENT R1 owner/repo#9 head={HEAD[:12]} src/a.cs:10",
                f"REVIEWER_DISAGREEMENT R2 owner/repo#8 head={HEAD[:12]} src/a.cs:10",
                "SIGNALS_OMITTED reviewer_disagreements 1",
            ],
            [line for line in lines if line.startswith(("REVIEWER_DISAGREEMENT", "SIGNALS_OMITTED"))],
        )


class UpgradeTests(SignalFixture):
    def test_a_version_eight_report_gains_empty_signals_and_a_malformed_one_fails_to_load(self) -> None:
        self.review(7, 1, [finding("SHOULD_FIX", "src/a.cs", 3, "Alpha")])
        json_path, _ = self.signal_lines()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        del report["signals"]
        json_path.write_text(json.dumps({**report, "schema_version": 8}), encoding="utf-8")
        upgraded = ri.load_report(json_path)
        self.assertEqual((9, review_signals.empty()), (upgraded["schema_version"], upgraded["signals"]))
        json_path.write_text(json.dumps({**report, "signals": {"severity_drift": []}}), encoding="utf-8")
        with self.assertRaisesRegex(ri.InsightError, "has invalid signals"):
            ri.load_report(json_path)


def _input(json_path: Path) -> list[dict[str, Any]]:
    text = (json_path.parent / rs.INPUT_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]
