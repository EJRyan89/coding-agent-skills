from __future__ import annotations

import copy
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_change import CHANGED, UNCHANGED, UNKNOWN
from update_pr_tracker import (
    END_MARKER,
    START_MARKER,
    TrackerError,
    evaluate,
    review_candidates,
    update_dashboard,
    validate_items,
)

HEAD = "b" * 40
AI_REVIEWED = "a" * 40
USER_REVIEWED = "c" * 40


class FakeDetector:
    def __init__(self, results: dict[tuple[int, str], str] | None = None) -> None:
        self.results = results or {}

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        if since_sha == head_sha:
            return UNCHANGED
        return self.results.get((number, since_sha), CHANGED)


def item(repository: str = "owner/repo", number: int = 1) -> dict:
    return {
        "repository": repository,
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": "Improve | behavior",
        "author": "someone",
        "requested_reviewers": ["reviewer"],
        "participants": [],
        "draft": False,
        "base_ref": "main",
        "head_sha": HEAD,
        "updated_at": "2026-01-02T00:00:00Z",
        "reviewed_head_sha": AI_REVIEWED,
        "user_review_state": None,
        "user_review_sha": None,
    }


def reviewed(number: int, state: str) -> dict:
    value = item(number=number)
    value["user_review_state"] = state
    value["user_review_sha"] = USER_REVIEWED
    return value


def sections(content: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    current = None
    for line in content.splitlines():
        heading = re.fullmatch(r"### (.+) \((\d+)\)", line)
        if heading:
            current = heading.group(1)
            result[current] = []
            continue
        row = re.search(r"\]\(https://github\.com/([^/]+/[^/]+)/pull/(\d+)\)", line)
        if row and current is not None:
            result[current].append(f"{row.group(1)}#{row.group(2)}")
    return result


def run(
    items: list[dict],
    detector: FakeDetector | None = None,
    **options,
) -> tuple[str, list[dict]]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        source = root / "input.json"
        dashboard = root / "dashboard.md"
        source.write_text(json.dumps(items), encoding="utf-8")
        dashboard.write_text(f"Before\n{START_MARKER}\nold\n{END_MARKER}\nAfter\n", encoding="utf-8")
        candidates = update_dashboard(source, dashboard, "reviewer", detector=detector or FakeDetector(), **options)
        return dashboard.read_text(encoding="utf-8"), candidates


def ai(
    verdict: str, must: int = 0, should: int = 0, suggestion: int = 0, report: str | None = "C:/Reviews/a b/review.md"
) -> dict:
    """A first review's collected result: its findings are its open ledger entries."""
    counts = {"MUST_FIX": must, "SHOULD_FIX": should, "SUGGESTION": suggestion}
    return {
        "verdict": verdict,
        "counts": counts,
        "ledger": {"open": counts, "addressed": 0, "since": 1 if any(counts.values()) else None, "version": 1},
        "report": report,
    }


LEGEND = "Findings: M must fix, H should fix, S suggestion; flagged means you flagged it with flag-review-finding."


class LayoutTests(unittest.TestCase):
    def test_legacy_layout_groups_requestors_and_shows_ai_results(self) -> None:
        first = item(number=7)
        first.update(
            author="ada", author_name="Ada Lovelace", reviewed_head_sha=HEAD, ai_review=ai("APPROVED", suggestion=2)
        )
        second = item(number=3)
        second.update(author="ada", author_name="Ada Lovelace", ai_review=ai("CHANGES_REQUESTED", must=1, should=2))
        third = item(repository="owner/other", number=5)
        third.update(author="bob", reviewed_head_sha=None)
        mine_draft = item(number=9)
        mine_draft.update(
            author="reviewer", draft=True, ai_review=ai("INCOMPLETE", report=None), reviewed_head_sha=HEAD
        )
        mine_open = item(number=8)
        mine_open.update(author="reviewer", review_decision="APPROVED")
        draft = item(number=6)
        draft.update(author="carol", draft=True)
        content, _ = run([first, second, third, mine_draft, mine_open, draft], home_repositories={"owner/repo"})
        self.assertIn("### To Review (3)\n\n<details open>\n<summary>", content)
        self.assertIn(
            "| Requestor | PR | AI Result | Findings | AI Review |\n| :--- | :--- | :--- | :--- | :--- |", content
        )
        self.assertIn(
            "| Ada Lovelace | [#3 Improve \\| "
            "behavior](https://github.com/owner/repo/pull/3) | Changes Requested | 1M 2H open | "
            "[AI Review](vscode://file/C:/Reviews/a%20b/review.md) (stale) |",
            content,
        )
        self.assertIn(
            "|  | [#7 Improve \\| behavior](https://github.com/owner/repo/pull/7) | Approved | 2S open | "
            "[AI Review](vscode://file/C:/Reviews/a%20b/review.md) |",
            content,
        )
        self.assertIn(
            "| bob | [other#5 Improve \\| behavior](https://github.com/owner/other/pull/5) | - | - | - |", content
        )
        self.assertIn("### Drafts (1)\n\n<details>\n", content)
        mine = content[content.index("### My PRs (2)") :]
        self.assertIn("| PR | Status | AI Result | Findings | AI Review |", mine)
        self.assertLess(mine.index("#9 "), mine.index("#8 "))
        self.assertIn("| Draft | Incomplete | none open | - |", mine)
        self.assertIn("| Approved | - | - | (stale) |", mine)
        self.assertNotIn("Awaiting Response", content)
        self.assertLess(content.index("### Drafts"), content.index("### My PRs"))

    def test_pinned_sections_link_the_configuration_and_are_collapsed(self) -> None:
        pinned = item(number=4)
        content, _ = run([pinned], overrides={"owner/repo#4": "on hold"}, config_path="D:/AgentData/config.json")
        self.assertIn(
            "### On Hold (1)\n\n<details>\n<summary>Manually managed — edit `dashboard.status_overrides` in "
            "[the code-review configuration](vscode://file/D:/AgentData/config.json)",
            content,
        )

    def test_configured_author_names_replace_profile_names_and_fall_back_without_one(self) -> None:
        profiled = item(number=1)
        profiled.update(author="Ada", author_name="ada-nickname")
        unnamed = item(number=2)
        unnamed.update(author="zed")
        kept = item(number=3)
        kept.update(author="bob", author_name="Bob Builder")
        bare = item(number=4)
        bare.update(author="carol")
        content, _ = run(
            [profiled, unnamed, kept, bare],
            home_repositories={"owner/repo"},
            author_names={"ada": "Ada Lovelace", "ZED": "Aaron Zed"},
        )
        requestors = [
            line.split(" | ")[0].removeprefix("| ")
            for line in content.splitlines()
            if "](https://github.com/owner/repo/pull/" in line
        ]
        # Rows are sorted by the display name, so the override for zed moves it ahead of Ada.
        self.assertEqual(["Aaron Zed", "Ada Lovelace", "Bob Builder", "carol"], requestors)
        self.assertNotIn("ada-nickname", content)

    def test_author_names_do_not_change_relationships_or_the_input(self) -> None:
        mine = item(number=1)
        mine.update(author="reviewer")
        content, _ = run([mine], author_names={"reviewer": "Me Myself"})
        self.assertEqual(["owner/repo#1"], sections(content)["My PRs"])
        self.assertNotIn("Me Myself", content)

    def test_invalid_author_names_are_rejected(self) -> None:
        for names in ({"ada": " "}, {"bad login": "Ada"}, {"ada": "A", "ADA": "B"}, ["ada"]):
            with self.subTest(names=names), self.assertRaisesRegex(TrackerError, "author.name"):
                run([item()], author_names=names)

    def test_the_findings_cell_has_one_grammar_for_every_row(self) -> None:
        def ledger(
            must: int = 0,
            should: int = 0,
            suggestion: int = 0,
            *,
            addressed: int | None = 0,
            since: int | None = None,
            version: int | None = 3,
        ) -> dict:
            return {
                "open": {"MUST_FIX": must, "SHOULD_FIX": should, "SUGGESTION": suggestion},
                "addressed": addressed,
                "since": since,
                "version": version,
            }

        def since(version: int, new: int = 0, addressed: int = 0) -> dict:
            return {"version": version, "new": new, "addressed": addressed}

        def unreviewed(summary: dict | None, flagged: int = 0) -> dict:
            return ai("CHANGES_REQUESTED") | {"ledger": summary, "since_review": None, "flagged": flagged}

        open_since_v1 = ai("CHANGES_REQUESTED", suggestion=1) | {"ledger": ledger(1, 0, 1, addressed=3, since=1)}
        for situation, review, expected in (
            # The issue's table, row by row.
            ("first review", unreviewed(ledger(1, 0, 1, since=1, version=1)), "1M 1S open"),
            ("re-review", unreviewed(ledger(0, 1, 3, addressed=2, since=1, version=2)), "1H 3S open · 2 addressed"),
            (
                "re-review, every earlier finding fixed",
                unreviewed(ledger(7, 0, 1, addressed=3, since=2, version=2)),
                "7M 1S open · 3 addressed",
            ),
            ("re-review, nothing addressed yet", unreviewed(ledger(2, since=1, version=2)), "2M open"),
            ("re-review, everything fixed", unreviewed(ledger(addressed=5)), "none open · 5 addressed"),
            (
                "after the user's review",
                ai("CHANGES_REQUESTED")
                | {"ledger": ledger(4, 4, 2, addressed=1, since=1), "since_review": since(1, 7, 1), "flagged": 0},
                "4M 4H 2S open · 7 new, 1 addressed since your review",
            ),
            (
                "after the user's review, no movement",
                open_since_v1 | {"since_review": since(3), "flagged": 0},
                "1M 1S open · unchanged since your review",
            ),
            # A record written before ledgers: collect summarizes the ledger its records compute, so its counts
            # (one suggestion, the only finding it raised) are not what the cell shows.
            (
                "record before ledgers",
                ai("CHANGES_REQUESTED", suggestion=1)
                | {"ledger": ledger(0, 1, 1, addressed=1, since=1, version=2), "since_review": None, "flagged": 0},
                "1H 1S open · 1 addressed",
            ),
            # A migrated legacy review cannot say what was addressed, so its counts are the head alone.
            (
                "legacy review",
                ai("APPROVED", 1, 0, 3) | {"ledger": ledger(1, 0, 3, addressed=None, version=None)},
                "1M 3S open",
            ),
            ("legacy review with an unreadable report", ai("APPROVED", 1) | {"ledger": None}, "-"),
            (
                "flagged head",
                unreviewed(ledger(1, 0, 1, addressed=3, since=1), flagged=1),
                "1M 1S open (1 flagged) · 3 addressed",
            ),
            (
                "flagged head after the user's review",
                open_since_v1 | {"since_review": since(2, addressed=2), "flagged": 1},
                "1M 1S open (1 flagged) · 2 addressed since your review",
            ),
            (
                "new only",
                open_since_v1 | {"since_review": since(0, 2), "flagged": 0},
                "1M 1S open · 2 new since your review",
            ),
            (
                "everything fixed since the user's review",
                ai("APPROVED") | {"ledger": ledger(addressed=2), "since_review": since(1, addressed=2), "flagged": 0},
                "none open · 2 addressed since your review",
            ),
            ("commit not placed", open_since_v1 | {"since_review": None, "flagged": 0}, "1M 1S open · 3 addressed"),
            ("collected before since_review existed", open_since_v1, "1M 1S open · 3 addressed"),
        ):
            row = item(number=4)
            row.update(author="ada", reviewed_head_sha=HEAD, ai_review=review)
            content, _ = run([row])
            with self.subTest(situation):
                self.assertIn(f"| {expected} | [AI Review]", content)
                self.assertNotRegex(content, r"\| [^|]*v\d[^|]* \| \[AI Review\]")

    def test_a_tracker_with_rows_has_one_legend_above_its_first_section(self) -> None:
        content, _ = run([item(number=1), reviewed(2, "COMMENTED")])
        block = content[content.index(START_MARKER) : content.index(END_MARKER)]
        self.assertEqual(1, content.count(LEGEND))
        self.assertEqual(1, sum(line.startswith("Findings:") for line in content.splitlines()))
        self.assertLess(block.index(LEGEND), block.index("### "))
        self.assertIn(f"{START_MARKER}\n\n{LEGEND}\n\n### ", content)
        empty, _ = run([])
        self.assertNotIn("Findings:", empty)

    def test_presentation_fields_are_validated(self) -> None:
        good = {"open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 0}, "addressed": 0, "since": 1, "version": 2}
        validate_items([item() | {"ai_review": ai("CHANGES_REQUESTED") | {"ledger": good}}])
        legacy = {"open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 0}, "addressed": None, "since": None}
        validate_items([item() | {"ai_review": ai("CHANGES_REQUESTED") | {"ledger": legacy | {"version": None}}}])
        validate_items(
            [
                item()
                | {
                    "ai_review": ai("CHANGES_REQUESTED")
                    | {"ledger": good, "flagged": 1, "since_review": {"version": 0, "new": 1, "addressed": 0}}
                }
            ]
        )
        for field, value in (
            ("author_name", ""),
            ("review_decision", "MERGED"),
            ("ai_review", {"verdict": "OK"}),
            ("ai_review", ai("APPROVED") | {"counts": {"MUST_FIX": -1, "SHOULD_FIX": 0, "SUGGESTION": 0}}),
            ("ai_review", ai("APPROVED") | {"ledger": good | {"addressed": -1}}),
            # Only a legacy review, which has no version, may leave what was addressed unknown.
            ("ai_review", ai("APPROVED") | {"ledger": good | {"addressed": None}}),
            ("ai_review", ai("APPROVED") | {"ledger": legacy | {"version": None, "addressed": 0}}),
            ("ai_review", ai("APPROVED") | {"ledger": legacy | {"version": None, "since": 1}}),
            ("ai_review", ai("APPROVED") | {"ledger": legacy | {"version": None}, "since_review": None, "flagged": 0}),
            ("ai_review", ai("APPROVED") | {"ledger": good | {"since": 3}}),
            ("ai_review", ai("APPROVED") | {"ledger": good | {"since": None}}),
            ("ai_review", ai("APPROVED") | {"ledger": good | {"open": {"MUST_FIX": 1}}}),
            ("ai_review", ai("APPROVED") | {"ledger": "1M"}),
            ("ai_review", ai("APPROVED") | {"ledger": None, "since_review": None, "flagged": 0}),
            ("ai_review", ai("APPROVED") | {"ledger": good, "flagged": 0}),
            ("ai_review", ai("APPROVED") | {"ledger": good, "since_review": None, "flagged": 2}),
            ("ai_review", ai("APPROVED") | {"ledger": good, "since_review": None, "flagged": True}),
            (
                "ai_review",
                ai("APPROVED")
                | {"ledger": good, "flagged": 0, "since_review": {"version": 3, "new": 0, "addressed": 0}},
            ),
            (
                "ai_review",
                ai("APPROVED")
                | {"ledger": good, "flagged": 0, "since_review": {"version": 1, "new": -1, "addressed": 0}},
            ),
            ("ai_review", ai("APPROVED") | {"ledger": good, "flagged": 0, "since_review": {"version": 1, "new": 0}}),
        ):
            bad = item()
            bad[field] = value
            with self.subTest(field=field), self.assertRaises(TrackerError):
                validate_items([bad])


class TrackerTests(unittest.TestCase):
    def test_preserves_outside_content_and_escapes_titles(self) -> None:
        content, _ = run([item()])
        self.assertTrue(content.startswith("Before\n"))
        self.assertTrue(content.endswith("\nAfter\n"))
        self.assertIn("Improve \\| behavior", content)
        self.assertEqual({"To Review": ["owner/repo#1"]}, sections(content))
        self.assertIn("### To Review (1)", content)

    def test_sections_follow_the_users_own_review_state(self) -> None:
        authored = item(number=10)
        authored["author"] = "Reviewer"
        draft = item(number=11)
        draft["draft"] = True
        detector = FakeDetector(
            {
                (2, USER_REVIEWED): CHANGED,
                (3, USER_REVIEWED): UNCHANGED,
                (4, USER_REVIEWED): UNKNOWN,
                (5, USER_REVIEWED): UNCHANGED,
                (6, USER_REVIEWED): UNCHANGED,
                (7, USER_REVIEWED): CHANGED,
                (8, USER_REVIEWED): UNKNOWN,
            }
        )
        content, _ = run(
            [
                item(number=1),
                reviewed(2, "CHANGES_REQUESTED"),
                reviewed(3, "CHANGES_REQUESTED"),
                reviewed(4, "CHANGES_REQUESTED"),
                reviewed(5, "COMMENTED"),
                reviewed(6, "APPROVED"),
                reviewed(7, "APPROVED"),
                reviewed(8, "APPROVED"),
                reviewed(9, "DISMISSED"),
                authored,
                draft,
            ],
            detector,
        )
        self.assertEqual(
            {
                "To Review": [
                    "owner/repo#1",
                    "owner/repo#2",
                    "owner/repo#7",
                    "owner/repo#8",
                    "owner/repo#9",
                ],
                "Awaiting Response": ["owner/repo#3", "owner/repo#4", "owner/repo#5"],
                "My PRs": ["owner/repo#10"],
                "Drafts": ["owner/repo#11"],
            },
            sections(content),
        )
        self.assertNotIn("owner/repo#6", content)

    def test_ai_review_status_is_independent_of_section(self) -> None:
        awaiting_but_stale = reviewed(1, "CHANGES_REQUESTED")
        merged_only = item(number=2)
        missing = item(number=3)
        missing["reviewed_head_sha"] = None
        detector = FakeDetector(
            {
                (1, USER_REVIEWED): UNCHANGED,
                (1, AI_REVIEWED): CHANGED,
                (2, AI_REVIEWED): UNCHANGED,
            }
        )
        content, _ = run([awaiting_but_stale, merged_only, missing], detector)
        self.assertEqual(["owner/repo#1"], sections(content)["Awaiting Response"])
        self.assertRegex(content, r"owner/repo/pull/1\) \| - \| - \| \(stale\) \|")
        self.assertRegex(content, r"owner/repo/pull/2\) \| - \| - \| - \|")
        self.assertRegex(content, r"owner/repo/pull/3\) \| - \| - \| - \|")

    def test_explicit_row_removal_is_one_run_and_does_not_create_override(self) -> None:
        content, candidates = run([item()], removals=["OWNER/REPO#1"])
        self.assertNotIn("owner/repo#1", content)
        self.assertIn("No matching open pull requests", content)
        self.assertEqual([], candidates)

    def test_overrides_pin_a_section_and_reject_computed_states(self) -> None:
        content, candidates = run([item()], overrides={"owner/repo#1": "On hold"})
        self.assertEqual({"On Hold": ["owner/repo#1"]}, sections(content))
        self.assertEqual([], candidates)
        for computed in ("stale", "Drafts", "awaiting response", "To Review"):
            with self.subTest(computed=computed), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                run([item()], overrides={"owner/repo#1": computed})

    def test_review_candidates_list_relevant_missing_and_stale_reviews_including_drafts(self) -> None:
        missing = item(number=1)
        missing["reviewed_head_sha"] = None
        stale = item(number=2)
        current = item(number=3)
        current["reviewed_head_sha"] = HEAD
        draft = item(number=4)
        draft["draft"] = True
        unrelated = item(number=5)
        unrelated["requested_reviewers"] = []
        approved = reviewed(6, "APPROVED")
        approved["reviewed_head_sha"] = None
        detector = FakeDetector({(6, USER_REVIEWED): UNCHANGED})
        rows = evaluate(
            validate_items([missing, stale, current, draft, unrelated, approved]),
            "reviewer",
            detector,
        )
        self.assertEqual(
            [
                {
                    "repository": "owner/repo",
                    "number": 1,
                    "url": "https://github.com/owner/repo/pull/1",
                    "status": "missing",
                },
                {
                    "repository": "owner/repo",
                    "number": 2,
                    "url": "https://github.com/owner/repo/pull/2",
                    "status": "stale",
                },
                {
                    "repository": "owner/repo",
                    "number": 4,
                    "url": "https://github.com/owner/repo/pull/4",
                    "status": "stale",
                },
            ],
            review_candidates(rows),
        )

    def test_review_commit_is_required_exactly_when_a_review_is_active(self) -> None:
        missing_sha = item()
        missing_sha["user_review_state"] = "CHANGES_REQUESTED"
        with self.assertRaisesRegex(TrackerError, "user_review_sha is required"):
            validate_items([missing_sha])
        orphan_sha = item()
        orphan_sha["user_review_sha"] = USER_REVIEWED
        with self.assertRaisesRegex(TrackerError, "requires a review state"):
            validate_items([orphan_sha])
        dismissed = item()
        dismissed["user_review_state"] = "DISMISSED"
        self.assertEqual(1, len(validate_items([dismissed])))

    def test_duplicate_identity_and_bad_markers_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.json"
            dashboard = root / "dashboard.md"
            source.write_text(json.dumps([item(), copy.deepcopy(item())]), encoding="utf-8")
            dashboard.write_text("No markers\n", encoding="utf-8")
            with self.assertRaisesRegex(TrackerError, "Duplicate"):
                update_dashboard(source, dashboard, "reviewer", detector=FakeDetector())
            source.write_text(json.dumps([item()]), encoding="utf-8")
            with self.assertRaisesRegex(TrackerError, "marker pair"):
                update_dashboard(source, dashboard, "reviewer", detector=FakeDetector())


if __name__ == "__main__":
    unittest.main()
