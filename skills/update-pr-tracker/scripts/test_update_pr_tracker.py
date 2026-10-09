from __future__ import annotations

import contextlib
import copy
import json
import re
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_client import CommandResult
from pr_change import CHANGED, UNCHANGED, UNKNOWN, ChangeDetector, ComparisonError
from review_github import GitHubClient, GitHubError
from update_pr_tracker import (
    END_MARKER,
    SECTION_SUMMARIES,
    SECTION_TO_REVIEW,
    START_MARKER,
    ComparisonFailures,
    ConfigurationError,
    TrackerError,
    evaluate,
    review_candidates,
    update_dashboard_rows,
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

    def prefetch(self, queries: Iterable[tuple[str, str, str, str]]) -> None:
        pass


class FailingDetector(FakeDetector):
    """Fails each named comparison, as the change detector does when a GitHub call it needs fails."""

    def __init__(self, failing: dict[tuple[int, str], str]) -> None:
        super().__init__()
        self.failing = failing

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        if (number, since_sha) in self.failing:
            raise ComparisonError(f"{repository}#{number}", GitHubError(self.failing[(number, since_sha)]))
        return super().detect(repository, number, base_ref, since_sha, head_sha)


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
        rows = update_dashboard_rows(source, dashboard, "reviewer", detector=detector or FakeDetector(), **options)
        candidates = review_candidates(rows)
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
        # A line starting with <summary> opens a CommonMark HTML block, so the summary is HTML, not Markdown.
        self.assertIn(
            "### On Hold (1)\n\n<details>\n<summary>Manually managed — edit <code>dashboard.status_overrides</code> "
            'in <a href="vscode://file/D:/AgentData/config.json">the code-review configuration</a> to add or remove '
            "entries.</summary>\n",
            content,
        )

    def test_the_configuration_link_is_url_quoted_and_html_escaped(self) -> None:
        pinned = item(number=4)
        content, _ = run(
            [pinned], overrides={"owner/repo#4": "on hold"}, config_path="D:\\Agent Data\\R&D <x>\\config.json"
        )
        self.assertIn(
            '<a href="vscode://file/D:/Agent%20Data/R%26D%20%3Cx%3E/config.json">the code-review configuration</a>',
            content,
        )
        summary = next(line for line in content.splitlines() if "status_overrides" in line)
        self.assertNotIn("&", summary.replace("%26", ""))
        self.assertNotIn("`", summary)

    def test_without_a_configuration_path_the_pinned_summary_names_it_without_a_link(self) -> None:
        content, _ = run([item(number=4)], overrides={"owner/repo#4": "on hold"})
        self.assertIn(
            "<summary>Manually managed — edit <code>dashboard.status_overrides</code> in the code-review "
            "configuration to add or remove entries.</summary>",
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
        for computed in ("stale", "Drafts", "awaiting response", "To Review", "my pull requests"):
            with self.subTest(computed=computed), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                run([item()], overrides={"owner/repo#1": computed})

    def test_an_override_naming_a_rendered_section_is_refused(self) -> None:
        # A pinned section named like a computed one rendered twice, and the computed one lost its own rows.
        rendered = ["To Review", "Awaiting Response", "My PRs", "Drafts"]
        self.assertEqual(sorted(rendered), sorted(SECTION_SUMMARIES))
        for section in rendered:
            for value in (section, section.upper(), f" {section.lower()} "):
                with self.subTest(value=value), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                    run([item()], overrides={"owner/repo#1": value})

    def test_an_authored_row_survives_an_override_naming_another_status(self) -> None:
        mine = item(number=1)
        mine["author"] = "reviewer"
        pinned = item(number=2)
        pinned["author"] = "reviewer"
        content, _ = run([mine, pinned], overrides={"owner/repo#2": "My PR"})
        self.assertEqual({"My PR": ["owner/repo#2"], "My PRs": ["owner/repo#1"]}, sections(content))
        self.assertEqual(1, content.count("### My PRs ("))

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

    def test_a_failed_comparison_fails_the_update_and_leaves_the_dashboard_as_it_was(self) -> None:
        failing = {(1, USER_REVIEWED): "HTTP 502: Bad Gateway", (2, AI_REVIEWED): "HTTP 403: Forbidden"}
        before = f"Before\n{START_MARKER}\nold\n{END_MARKER}\nAfter\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            # The section's comparison fails for pull 1 and the AI review's for pull 2; pull 3 compares.
            source, dashboard = dashboard_files(Path(temporary), before)
            source.write_text(json.dumps([item(number=2), reviewed(1, "APPROVED"), item(number=3)]), encoding="utf-8")
            with self.assertRaises(ComparisonFailures) as raised:
                update_dashboard_rows(source, dashboard, "reviewer", detector=FailingDetector(failing))
            self.assertEqual(before, dashboard.read_bytes(), "no row is marked stale for a call that failed")
        self.assertEqual(
            [("owner/repo#1", "HTTP 502: Bad Gateway"), ("owner/repo#2", "HTTP 403: Forbidden")],
            [(failure.pull, failure.reason) for failure in raised.exception.failures],
        )
        self.assertEqual(
            "2 of 3 pull requests could not be compared; the dashboard keeps its previous rows",
            str(raised.exception),
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
                update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())
            source.write_text(json.dumps([item()]), encoding="utf-8")
            with self.assertRaisesRegex(TrackerError, "marker pair"):
                update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())

    def test_markers_are_checked_before_github_is_asked(self) -> None:
        detector = RecordingDetector()
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(Path(temporary), b"No markers\n")
            with self.assertRaisesRegex(TrackerError, "marker pair"):
                update_dashboard_rows(source, dashboard, "reviewer", detector=detector)
            self.assertEqual([], detector.calls)
            self.assertEqual(b"No markers\n", dashboard.read_bytes())


class RecordingDetector(FakeDetector):
    """Records each comparison and runs an action during the first, as a user saving the dashboard mid-run would."""

    def __init__(self, during: Callable[[], object] | None = None) -> None:
        super().__init__()
        self.calls: list[int] = []
        self.during = during

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        if not self.calls and self.during is not None:
            self.during()
        self.calls.append(number)
        return super().detect(repository, number, base_ref, since_sha, head_sha)


def dashboard_files(root: Path, dashboard_bytes: bytes) -> tuple[Path, Path]:
    source = root / "input.json"
    dashboard = root / "dashboard.md"
    source.write_text(json.dumps([item()]), encoding="utf-8")
    dashboard.write_bytes(dashboard_bytes)
    return source, dashboard


def outside(document: bytes) -> tuple[bytes, bytes]:
    """The bytes before the start marker and after the end marker."""
    start = document.index(START_MARKER.encode())
    end = document.index(END_MARKER.encode()) + len(END_MARKER)
    return document[:start], document[end:]


class ReadAheadDetector(FakeDetector):
    """Records the order of read-aheads and detections, so a test can see each detection was read ahead first."""

    def __init__(self, results: dict[tuple[int, str], str] | None = None) -> None:
        super().__init__(results)
        self.events: list[tuple[str, tuple[str, str, str, str]]] = []

    def prefetch(self, queries: Iterable[tuple[str, str, str, str]]) -> None:
        self.events.extend(("prefetch", query) for query in queries)
        self.events.append(("round", ("", "", "", "")))

    def detect(self, repository: str, number: int, base_ref: str, since_sha: str, head_sha: str) -> str:
        self.events.append(("detect", (repository, base_ref, since_sha, head_sha)))
        return super().detect(repository, number, base_ref, since_sha, head_sha)


def commit(number: int) -> str:
    return f"{number:040x}"


class GitHubRunner:
    """Answers the change detector's gh api calls: every commit's comparison lists a.py at its own blob, so each
    comparison decides without a tree. Each call waits until four are running at once, the endpoints in
    `rate_limited` fail once with a rate limit first, and those in `failing` always fail with their error output."""

    def __init__(self, rate_limited: set[str] | None = None, failing: dict[str, str] | None = None) -> None:
        self.barrier = threading.Barrier(4, timeout=10)
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.running = 0
        self.peak = 0
        self.rate_limited = set(rate_limited or ())
        self.failing = failing or {}

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        endpoint = arguments[-1]
        with self.lock:
            self.calls.append(endpoint)
            self.running += 1
            self.peak = max(self.peak, self.running)
            limited = endpoint in self.rate_limited
            self.rate_limited.discard(endpoint)
        try:
            # The last calls of a round are fewer than four; they go on once the barrier gives up on the rest.
            with contextlib.suppress(threading.BrokenBarrierError):
                self.barrier.wait(timeout=0.5)
            if limited:
                return CommandResult(1, "", "HTTP 429: API rate limit exceeded")
            if endpoint in self.failing:
                return CommandResult(1, "", self.failing[endpoint])
            files = [{"filename": "a.py", "status": "modified", "sha": endpoint.rsplit("...", 1)[1]}]
            return CommandResult(0, json.dumps({"status": "ahead", "files": files}), "")
        finally:
            with self.lock:
                self.running -= 1


class ReadAheadTests(unittest.TestCase):
    def items(self, count: int) -> list[dict]:
        """Pull requests reviewed by the user and by the AI at earlier commits, each pair of commits its own."""
        items = []
        for number in range(1, count + 1):
            value = reviewed(number, "COMMENTED")
            value["head_sha"] = commit(3 * number)
            value["user_review_sha"] = commit(3 * number + 1)
            value["reviewed_head_sha"] = commit(3 * number + 2)
            items.append(value)
        return validate_items(items)

    def test_every_detection_is_read_ahead_in_two_rounds(self) -> None:
        approved = reviewed(1, "APPROVED")
        commented = reviewed(2, "COMMENTED")
        pinned = reviewed(3, "COMMENTED")
        mine = reviewed(4, "COMMENTED")
        mine["author"] = "reviewer"
        detector = ReadAheadDetector({(1, USER_REVIEWED): UNCHANGED})
        evaluate(
            validate_items([approved, commented, pinned, mine]),
            "reviewer",
            detector,
            overrides={"owner/repo#3": "on hold"},
        )
        user, ai_review = (("owner/repo", "main", sha, HEAD) for sha in (USER_REVIEWED, AI_REVIEWED))
        self.assertEqual(
            [
                ("prefetch", user),  # approved#1
                ("prefetch", user),  # commented#2; pinned#3 and mine#4 need no user comparison
                ("round", ("", "", "", "")),
                ("detect", user),
                ("detect", user),
                ("prefetch", ai_review),  # commented#2, pinned#3, mine#4; approved#1 is unchanged and left out
                ("prefetch", ai_review),
                ("prefetch", ai_review),
                ("round", ("", "", "", "")),
                ("detect", ai_review),
                ("detect", ai_review),
                ("detect", ai_review),
            ],
            detector.events,
        )

    def test_the_change_detector_reads_four_comparisons_at_a_time_and_each_once(self) -> None:
        runner = GitHubRunner()
        rows = evaluate(self.items(6), "reviewer", ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None)))
        self.assertEqual(4, runner.peak)
        self.assertEqual(18, len(runner.calls), "three commits per pull request, each compared once, no trees")
        self.assertEqual(18, len(set(runner.calls)))
        self.assertEqual({(SECTION_TO_REVIEW, "stale")}, {(row.section, row.ai_review) for row in rows})

    def test_the_shared_backoff_retries_a_rate_limit_among_concurrent_comparisons(self) -> None:
        limited = {f"repos/owner/repo/compare/main...{commit(sha)}" for sha in (4, 8)}
        runner = GitHubRunner(limited)
        waits: list[float] = []
        rows = evaluate(self.items(6), "reviewer", ChangeDetector(GitHubClient(runner, sleeper=waits.append)))
        self.assertEqual([5.0, 5.0], waits)
        self.assertEqual(20, len(runner.calls), "eighteen comparisons and two retries")
        self.assertEqual(6, len(rows))

    def test_a_failed_comparison_is_reported_and_a_transport_failure_stops_the_update(self) -> None:
        endpoint = f"repos/owner/repo/compare/main...{commit(4)}"  # pull 1's commit the user reviewed
        for stderr in ("HTTP 403: Forbidden", "HTTP 502: Bad Gateway", "HTTP 404: Not Found"):
            with self.subTest(stderr=stderr):
                runner = GitHubRunner(failing={endpoint: stderr})
                detector = ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None))
                if stderr.startswith("HTTP 404"):
                    rows = evaluate(self.items(6), "reviewer", detector)  # a missing commit is unknown evidence
                    self.assertEqual(6, len(rows))
                    continue
                with self.assertRaises(ComparisonFailures) as raised:
                    evaluate(self.items(6), "reviewer", detector)
                self.assertEqual([("owner/repo#1", stderr)], [(f.pull, f.reason) for f in raised.exception.failures])
                self.assertEqual(17, len(runner.calls), "every other pull request is still compared")
        runner = GitHubRunner(failing={endpoint: "error connecting to api.github.com"})
        with self.assertRaises(GitHubError) as stopped:
            evaluate(self.items(6), "reviewer", ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None)))
        self.assertEqual("network", stopped.exception.kind)


class DashboardWriteTests(unittest.TestCase):
    """The dashboard is the user's file: only the owned section changes, as it is on disk when it is written."""

    def test_an_edit_saved_while_github_is_asked_survives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(
                Path(temporary), f"Before\n{START_MARKER}\nold\n{END_MARKER}\nAfter\n".encode()
            )
            edited = f"Before, edited\n{START_MARKER}\nold\n{END_MARKER}\nAfter\nA new note\n".encode()
            detector = RecordingDetector(lambda: dashboard.write_bytes(edited))
            update_dashboard_rows(source, dashboard, "reviewer", detector=detector)
            self.assertNotEqual([], detector.calls)
            written = dashboard.read_bytes()
            self.assertEqual(outside(edited), outside(written))
            self.assertIn(b"### To Review (1)", written)
            self.assertNotIn(b"\nold\n", written)

    def test_markers_removed_while_github_is_asked_fail_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(Path(temporary), f"{START_MARKER}\n{END_MARKER}\n".encode())
            detector = RecordingDetector(lambda: dashboard.write_bytes(b"Rewritten\n"))
            with self.assertRaisesRegex(TrackerError, "marker pair"):
                update_dashboard_rows(source, dashboard, "reviewer", detector=detector)
            self.assertEqual(b"Rewritten\n", dashboard.read_bytes())

    def test_a_crlf_dashboard_keeps_crlf_and_every_byte_outside_the_owned_section(self) -> None:
        original = f"﻿Before\r\n\r\n{START_MARKER}\r\nold\r\n{END_MARKER}\r\nAfter — café\r\n".encode()
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(Path(temporary), original)
            update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())
            written = dashboard.read_bytes()
        self.assertEqual(outside(original), outside(written))
        start, end = written.index(START_MARKER.encode()), written.index(END_MARKER.encode())
        owned = written[start:end]
        self.assertIn(b"### To Review (1)\r\n\r\n<details open>\r\n", owned)
        self.assertEqual(owned.count(b"\n"), owned.count(b"\r\n"))

    def test_an_lf_dashboard_stays_lf_and_mixed_endings_follow_the_dominant_one(self) -> None:
        for name, before, expected in (
            ("lf", "One\nTwo\n", b"\n"),
            ("mostly lf", "One\r\nTwo\nThree\n", b"\n"),
            ("mostly crlf", "One\r\nTwo\r\nThree\r\nFour\n", b"\r\n"),
        ):
            original = f"{before}{START_MARKER}\n{END_MARKER}".encode()
            with self.subTest(name), tempfile.TemporaryDirectory() as temporary:
                source, dashboard = dashboard_files(Path(temporary), original)
                update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())
                written = dashboard.read_bytes()
                self.assertEqual(outside(original), outside(written))
                owned = written[written.index(START_MARKER.encode()) : written.index(END_MARKER.encode())]
                self.assertIn(START_MARKER.encode() + expected + expected, owned)
                if expected == b"\n":
                    self.assertNotIn(b"\r", owned)
                else:
                    self.assertEqual(owned.count(b"\n"), owned.count(b"\r\n"))


def refusal(value: object) -> tuple[str, str]:
    """The class and message validate_items refuses a value with."""
    try:
        validate_items(value)
    except (TrackerError, ConfigurationError) as exc:
        return type(exc).__name__, str(exc)
    raise AssertionError(f"validate_items accepted {value!r}")


class ValidateItemsSequenceTests(unittest.TestCase):
    """validate_items called directly: what it returns, and each refusal's class and exact message, in check order."""

    def test_an_input_that_is_not_an_array_is_refused(self) -> None:
        for value in ({"repository": "owner/repo"}, "[]", None):
            with self.subTest(value=value):
                self.assertEqual(("TrackerError", "Tracker input must be an array"), refusal(value))

    def test_an_empty_array_is_accepted(self) -> None:
        self.assertEqual([], validate_items([]))

    def test_an_item_whose_fields_do_not_match_the_contract_is_refused(self) -> None:
        missing = item()
        del missing["user_review_sha"]
        for value in ("owner/repo#1", ["owner/repo", 1], missing, item() | {"labels": []}):
            with self.subTest(value=value):
                self.assertEqual(
                    ("TrackerError", "Tracker item fields do not match the contract"), refusal([item(number=2), value])
                )

    def test_an_accepted_item_gains_its_defaults_first_and_a_lowercase_repository(self) -> None:
        source = item("Owner/Repo", 7)
        original = copy.deepcopy(source)
        [result] = validate_items([source])
        self.assertEqual(original, source)
        self.assertEqual(
            [
                "reviewed_incomplete",
                "author_name",
                "review_decision",
                "ai_review",
                "repository",
                "number",
                "url",
                "title",
                "author",
                "requested_reviewers",
                "participants",
                "draft",
                "base_ref",
                "head_sha",
                "updated_at",
                "reviewed_head_sha",
                "user_review_state",
                "user_review_sha",
            ],
            list(result),
        )
        self.assertEqual(
            {
                "reviewed_incomplete": False,
                "author_name": None,
                "review_decision": None,
                "ai_review": None,
                **original,
                "repository": "owner/repo",
            },
            result,
        )

    def test_given_optional_fields_keep_their_values_and_their_place(self) -> None:
        source = {
            "ai_review": ai("APPROVED"),
            **item(number=3),
            "author_name": "Someone Else",
            "reviewed_incomplete": True,
            "review_decision": "APPROVED",
        }
        [result] = validate_items([source])
        self.assertEqual(source, result)
        self.assertEqual(
            ["reviewed_incomplete", "author_name", "review_decision", "ai_review", *list(item())], list(result)
        )

    def test_items_keep_their_order(self) -> None:
        values = [item(number=3), item("other/repo", 1), item(number=1)]
        self.assertEqual(
            ["owner/repo#3", "other/repo#1", "owner/repo#1"],
            [f"{value['repository']}#{value['number']}" for value in validate_items(values)],
        )

    def test_each_field_refusal_has_its_exact_message(self) -> None:
        key = "Tracker item owner/repo#1"
        cases: list[tuple[str, object, tuple[str, str]]] = [
            ("author_name", " ", ("TrackerError", f"{key}.author_name must be null or a non-empty string")),
            ("reviewed_incomplete", 1, ("TrackerError", "Tracker item reviewed_incomplete must be Boolean")),
            ("repository", "owner", ("ConfigurationError", "Invalid repository identity: 'owner'")),
            ("repository", None, ("ConfigurationError", "Invalid repository identity: None")),
            ("number", "1", ("TrackerError", "Tracker pull number must be positive")),
            ("number", True, ("TrackerError", "Tracker pull number must be positive")),
            ("number", 0, ("TrackerError", "Tracker pull number must be positive")),
            ("number", 1.0, ("TrackerError", "Tracker pull number must be positive")),
            *(
                (field, value, ("TrackerError", f"{key}.{field} is required"))
                for field in ("url", "title", "author", "base_ref", "head_sha", "updated_at")
                for value in ("", None, 5)
            ),
            *(
                (field, value, ("TrackerError", f"{key}.{field} must be a string array"))
                for field in ("requested_reviewers", "participants")
                for value in ("reviewer", None, [""], ["reviewer", 5], ("reviewer",))
            ),
            ("draft", "false", ("TrackerError", f"{key}.draft must be Boolean")),
            ("draft", 0, ("TrackerError", f"{key}.draft must be Boolean")),
            ("reviewed_head_sha", "", ("TrackerError", f"{key}.reviewed_head_sha is invalid")),
            ("reviewed_head_sha", 5, ("TrackerError", f"{key}.reviewed_head_sha is invalid")),
            ("user_review_sha", "", ("TrackerError", f"{key}.user_review_sha is invalid")),
            ("user_review_sha", 5, ("TrackerError", f"{key}.user_review_sha is invalid")),
            ("user_review_state", "PENDING", ("TrackerError", f"{key}.user_review_state is invalid")),
            ("user_review_state", "approved", ("TrackerError", f"{key}.user_review_state is invalid")),
            ("user_review_sha", USER_REVIEWED, ("TrackerError", f"{key}.user_review_sha requires a review state")),
            *(
                ("user_review_state", state, ("TrackerError", f"{key}.user_review_sha is required for {state}"))
                for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")
            ),
        ]
        for field, value, expected in cases:
            bad = item()
            bad[field] = value
            with self.subTest(field=field, value=value):
                self.assertEqual(expected, refusal([item(number=2), bad]))

    def test_reviewed_incomplete_needs_a_reviewed_commit(self) -> None:
        self.assertEqual(
            ("TrackerError", "Tracker item reviewed_incomplete requires reviewed_head_sha"),
            refusal([item() | {"reviewed_incomplete": True, "reviewed_head_sha": None}]),
        )
        [result] = validate_items([item() | {"reviewed_incomplete": False, "reviewed_head_sha": None}])
        self.assertIsNone(result["reviewed_head_sha"])

    def test_review_states_that_need_no_commit_are_accepted(self) -> None:
        for state, sha in ((None, None), ("DISMISSED", None), ("DISMISSED", USER_REVIEWED)):
            with self.subTest(state=state, sha=sha):
                [result] = validate_items([item() | {"user_review_state": state, "user_review_sha": sha}])
                self.assertEqual((state, sha), (result["user_review_state"], result["user_review_sha"]))
        for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
            with self.subTest(state=state):
                [result] = validate_items([item() | {"user_review_state": state, "user_review_sha": USER_REVIEWED}])
                self.assertEqual(state, result["user_review_state"])

    def test_a_duplicate_is_named_by_its_normalized_identity(self) -> None:
        self.assertEqual(
            ("TrackerError", "Duplicate tracker item: owner/repo#4"),
            refusal([item(number=4), item("other/repo", 4), item("OWNER/Repo", 4)]),
        )

    def test_a_refused_item_is_refused_before_a_later_duplicate_is_seen(self) -> None:
        self.assertEqual(
            ("TrackerError", "Tracker item owner/repo#5.title is required"),
            refusal([item(number=4), item(number=5) | {"title": ""}, item(number=4)]),
        )

    def test_refusals_come_in_check_order(self) -> None:
        """An item wrong in every way is refused for one fault at a time, each repaired before the next is reported."""
        stages: list[tuple[str, object, object]] = [
            ("author_name", "", None),
            ("reviewed_incomplete", "yes", False),
            ("repository", "Owner", "Owner/Repo"),
            ("number", -1, 1),
            ("url", "", "https://github.com/owner/repo/pull/1"),
            ("title", None, "Title"),
            ("author", 1, "someone"),
            ("base_ref", "", "main"),
            ("head_sha", "", HEAD),
            ("updated_at", "", "2026-01-02T00:00:00Z"),
            ("requested_reviewers", "x", []),
            ("participants", [1], []),
            ("draft", None, True),
            ("reviewed_head_sha", "", AI_REVIEWED),
            ("user_review_sha", "", None),
            ("user_review_state", "PENDING", None),
        ]
        bad = item() | {field: wrong for field, wrong, _ in stages}
        seen: list[str] = []
        for field, _, repaired in stages:
            seen.append(refusal([bad])[1])
            bad[field] = repaired
        self.assertEqual(1, len(validate_items([bad])))
        self.assertEqual(
            [
                "Tracker item Owner#-1.author_name must be null or a non-empty string",
                "Tracker item reviewed_incomplete must be Boolean",
                "Invalid repository identity: 'Owner'",
                "Tracker pull number must be positive",
                "Tracker item owner/repo#1.url is required",
                "Tracker item owner/repo#1.title is required",
                "Tracker item owner/repo#1.author is required",
                "Tracker item owner/repo#1.base_ref is required",
                "Tracker item owner/repo#1.head_sha is required",
                "Tracker item owner/repo#1.updated_at is required",
                "Tracker item owner/repo#1.requested_reviewers must be a string array",
                "Tracker item owner/repo#1.participants must be a string array",
                "Tracker item owner/repo#1.draft must be Boolean",
                "Tracker item owner/repo#1.reviewed_head_sha is invalid",
                "Tracker item owner/repo#1.user_review_sha is invalid",
                "Tracker item owner/repo#1.user_review_state is invalid",
            ],
            seen,
        )


if __name__ == "__main__":
    unittest.main()
