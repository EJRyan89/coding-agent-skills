from __future__ import annotations

import contextlib
import copy
import itertools
import json
import re
import stat
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, ClassVar, override
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

from github_client import CommandResult, GitHubError
from pr_change import CHANGED, UNCHANGED, UNKNOWN, ChangeDetector, ComparisonError, Query
from review_config import ConfigurationError, dashboard_status_heading, dashboard_status_key
from review_github import GitHubClient
from typed_fields import FieldError
from update_pr_dashboard import (
    SECTION_AWAITING,
    SECTION_SUMMARIES,
    SECTION_TO_REVIEW,
    ComparisonFailures,
    Row,
    TrackerError,
    TrackerFieldError,
    _ai_review,
    evaluate,
    normalize_overrides,
    normalize_pull_keys,
    replacement,
    review_candidates,
    stale_since,
    tracked_pulls,
    update_dashboard_rows,
    user_review_bases,
    validate_items,
    validate_tracker_record,
)

# The default markers, as literals: users' dashboards hold them and docs/code-review-operations-contract.md states
# them, so a change to review_config's DASHBOARD_START_MARKER or DASHBOARD_END_MARKER fails here.
START_MARKER = "<!-- code-review-pr-tracker:start -->"
END_MARKER = "<!-- code-review-pr-tracker:end -->"
HEAD = "b" * 40
AI_REVIEWED = "a" * 40
USER_REVIEWED = "c" * 40
# The base tip a tracker record holds for the user's review, and the base tip a later collection reads.
RECORDED_BASE = "d" * 40
BASE_TIP = "e" * 40


class FakeDetector:
    """Answers each comparison from `results`, by pull request and earlier commit, and records the base each earlier
    commit was compared with, by pull request and earlier commit, None for the base branch as it is now."""

    def __init__(self, results: dict[tuple[int, str], str] | None = None) -> None:
        self.results = results or {}
        self.bases: dict[tuple[int, str], str | None] = {}

    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        self.bases[(number, since_sha)] = since_base
        # Only one comparison, the same head on the same base, is unchanged without a call, as the real detector
        # reads it; the same head on another base, as after a retarget, is compared like any other.
        if Query(repository, base_ref, since_sha, head_sha, since_base).one_comparison:
            return UNCHANGED
        return self.results.get((number, since_sha), CHANGED)

    def prefetch(self, queries: Iterable[Query]) -> None:
        pass


class FailingDetector(FakeDetector):
    """Fails each named comparison, as the change detector does when a GitHub call it needs fails."""

    def __init__(self, failing: dict[tuple[int, str], str]) -> None:
        super().__init__()
        self.failing = failing

    @override
    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        if (number, since_sha) in self.failing:
            raise ComparisonError(f"{repository}#{number}", GitHubError(self.failing[(number, since_sha)]))
        return super().detect(repository, number, base_ref, since_sha, head_sha, since_base)


def item(repository: str = "owner/repo", number: int = 1) -> dict[str, Any]:
    return {
        "repository": repository,
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": "Improve | behavior",
        "author": "someone",
        "requested_reviewers": ["reviewer"],
        "draft": False,
        "base_ref": "main",
        "head_sha": HEAD,
        "updated_at": "2026-01-02T00:00:00Z",
        "reviewed_head_sha": AI_REVIEWED,
        "user_review_state": None,
        "user_review_sha": None,
    }


def reviewed(number: int, state: str) -> dict[str, Any]:
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
    items: list[dict[str, Any]],
    detector: FakeDetector | None = None,
    **options: Any,
) -> tuple[str, list[dict[str, Any]]]:
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
) -> dict[str, Any]:
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
        content, _ = run([first, second, third, mine_draft, mine_open, draft])
        self.assertIn("### To Review (3)\n\n<details open>\n<summary>", content)
        header = "| Requestor | Repository | PR | Status | AI Result | Findings | AI Review |\n" + "| :--- " * 7 + "|"
        self.assertEqual(2, content.count(header), "the To Review and Drafts tables")
        self.assertIn(
            "| Ada Lovelace | owner/repo | [#3 Improve &#124; "
            "behavior](https://github.com/owner/repo/pull/3) | Open | Changes Requested | 1M 2H open | "
            "[AI Review](vscode://file/C:/Reviews/a%20b/review.md) (stale) |",
            content,
        )
        self.assertIn(
            "|  | owner/repo | [#7 Improve &#124; behavior](https://github.com/owner/repo/pull/7) | Open | Approved | "
            "2S open | [AI Review](vscode://file/C:/Reviews/a%20b/review.md) |",
            content,
        )
        self.assertIn(
            "| bob | owner/other | [#5 Improve &#124; behavior](https://github.com/owner/other/pull/5) | Open | - | - "
            "| - |",
            content,
        )
        self.assertIn("### Drafts (1)\n\n<details>\n", content)
        self.assertIn(
            "| carol | owner/repo | [#6 Improve &#124; behavior](https://github.com/owner/repo/pull/6) | Draft | - | - "
            "| (stale) |",
            content,
        )
        mine = content[content.index("### My PRs (2)") :]
        self.assertIn("| Repository | PR | Status | AI Result | Findings | AI Review |\n" + "| :--- " * 6 + "|", mine)
        self.assertLess(mine.index("#9 "), mine.index("#8 "))
        self.assertIn(
            "| owner/repo | [#9 Improve &#124; behavior](https://github.com/owner/repo/pull/9) | Draft | Incomplete | "
            "none open | - |",
            mine,
        )
        self.assertIn(
            "| owner/repo | [#8 Improve &#124; behavior](https://github.com/owner/repo/pull/8) | Approved | - | - | "
            "(stale) |",
            mine,
        )
        self.assertNotIn("Awaiting Response", content)
        self.assertLess(content.index("### Drafts"), content.index("### My PRs"))

    def test_pinned_sections_link_the_configuration_and_are_collapsed(self) -> None:
        pinned = item(number=4)
        content, _ = run([pinned], overrides={"owner/repo#4": "on hold"}, config_path="D:/AgentData/config.json")
        # A line starting with <summary> opens a CommonMark HTML block, so the summary is HTML, not Markdown.
        self.assertIn(
            "### On Hold (1)\n\n<details>\n<summary>Pinned by a status override in "
            '<a href="vscode://file/D:/AgentData/config.json">the code-review configuration</a>. Set or clear one '
            "through update-pr-dashboard, which runs <code>dashboard_pipeline.py override</code>; do not edit the file "
            "by hand.</summary>\n",
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
        summary = next(line for line in content.splitlines() if line.startswith("<summary>Pinned"))
        self.assertNotIn("&", summary.replace("%26", ""))
        self.assertNotIn("`", summary)

    def test_without_a_configuration_path_the_pinned_summary_names_it_without_a_link(self) -> None:
        content, _ = run([item(number=4)], overrides={"owner/repo#4": "on hold"})
        self.assertIn(
            "<summary>Pinned by a status override in the code-review configuration. Set or clear one through "
            "update-pr-dashboard, which runs <code>dashboard_pipeline.py override</code>; do not edit the file by "
            "hand.</summary>",
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
        ) -> dict[str, Any]:
            return {
                "open": {"MUST_FIX": must, "SHOULD_FIX": should, "SUGGESTION": suggestion},
                "addressed": addressed,
                "since": since,
                "version": version,
            }

        def since(version: int, new: int = 0, addressed: int = 0) -> dict[str, Any]:
            return {"version": version, "new": new, "addressed": addressed}

        def unreviewed(summary: dict[str, Any] | None, flagged: int = 0) -> dict[str, Any]:
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
            # A repeat is linked to the entry it repeats, so the severity counts leave it out and the head names it.
            (
                "repeats",
                unreviewed(ledger(0, 0, 2, addressed=0, since=1) | {"repeats": 3}, flagged=0),
                "2S open (3 repeats)",
            ),
            (
                "one repeat, flagged",
                open_since_v1
                | {"ledger": open_since_v1["ledger"] | {"repeats": 1}, "since_review": since(3)}
                | {"flagged": 1},
                "1M 1S open (1 repeat, 1 flagged) · unchanged since your review",
            ),
            (
                "collected before repeats were counted",
                unreviewed(ledger(0, 0, 2, addressed=0, since=1) | {"repeats": 0}, flagged=0),
                "2S open",
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
        validate_items([item() | {"ai_review": ai("CHANGES_REQUESTED") | {"ledger": good | {"repeats": 3}}}])
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
            ("ai_review", ai("APPROVED") | {"ledger": good | {"repeats": -1}}),
            ("ai_review", ai("APPROVED") | {"ledger": good | {"repeats": "3"}}),
            # A repeat is of an open entry, so a ledger with none open has none.
            (
                "ai_review",
                ai("APPROVED")
                | {
                    "ledger": good
                    | {"open": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}, "since": None, "repeats": 1}
                },
            ),
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
        none_open = {"open": {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}, "since": None, "repeats": 1}
        bad = item() | {"ai_review": ai("APPROVED") | {"ledger": good | none_open}}
        with self.assertRaises(TrackerError) as raised:
            validate_items([bad])
        self.assertEqual(
            f"Tracker item {bad['repository']}#{bad['number']}.ai_review.ledger is invalid", str(raised.exception)
        )


class TrackerTests(unittest.TestCase):
    def test_preserves_outside_content_and_escapes_titles(self) -> None:
        content, _ = run([item()])
        self.assertTrue(content.startswith("Before\n"))
        self.assertTrue(content.endswith("\nAfter\n"))
        self.assertIn("Improve &#124; behavior", content)
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
        self.assertRegex(content, r"owner/repo/pull/1\) \| Changes Requested \| - \| - \| \(stale\) \|")
        self.assertRegex(content, r"owner/repo/pull/2\) \| Open \| - \| - \| - \|")
        self.assertRegex(content, r"owner/repo/pull/3\) \| Open \| - \| - \| - \|")

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

    def test_every_ai_review_state_the_tracker_computes_is_refused_as_an_override(self) -> None:
        # One item per branch of the computation: never reviewed, reviewed at an earlier head that changed since,
        # reviewed at this head in full, and reviewed at this head without reading every changed file.
        cases = {
            "missing": item() | {"reviewed_head_sha": None},
            "stale": item(),
            "current": item() | {"reviewed_head_sha": HEAD},
            "incomplete": item() | {"reviewed_head_sha": HEAD, "reviewed_incomplete": True},
        }
        self.assertEqual(
            {state: state for state in cases},
            {state: _ai_review(validate_items([value])[0], FakeDetector()) for state, value in cases.items()},
        )
        for state in cases:
            for value in (state, state.upper(), f" {state.title()} "):
                with self.subTest(value=value), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                    run([item()], overrides={"owner/repo#1": value})

    def test_the_reviewed_sections_say_status_is_the_latest_deciding_review(self) -> None:
        # _status shows the user's latest deciding review, so an approval followed by a comment still shows Approved;
        # the summaries once said "your own latest review", which a reader took to mean the comment.
        stated = "Status reflects your own latest deciding review, which later comments leave standing."
        for section in (SECTION_TO_REVIEW, SECTION_AWAITING):
            with self.subTest(section=section):
                self.assertTrue(SECTION_SUMMARIES[section].endswith(stated))

    def test_an_override_naming_a_rendered_section_is_refused(self) -> None:
        # A pinned section named like a computed one rendered twice, and the computed one lost its own rows.
        rendered = ["To Review", "Awaiting Response", "My PRs", "Drafts"]
        self.assertEqual(sorted(rendered), sorted(SECTION_SUMMARIES))
        for section in rendered:
            for value in (section, section.upper(), f" {section.lower()} "):
                with self.subTest(value=value), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                    run([item()], overrides={"owner/repo#1": value})

    def test_statuses_differing_in_case_or_spacing_share_one_section_headed_as_the_first_row_spells_it(self) -> None:
        # Spelled differently, delegated and Delegated made two sections, which the headings could not tell apart.
        items = [item(number=number) for number in (1, 2, 3, 4)]
        overrides = {
            "owner/repo#1": "on  HOLD",
            "owner/repo#2": "delegated",
            "owner/repo#3": "Delegated",
            "owner/repo#4": "On hold",
        }
        content, _ = run(items, overrides=overrides)
        self.assertEqual(
            {"Delegated": ["owner/repo#2", "owner/repo#3"], "On HOLD": ["owner/repo#1", "owner/repo#4"]},
            sections(content),
        )
        reordered, _ = run(items, overrides=overrides | {"owner/repo#2": "DELEGATED", "owner/repo#3": "delegated"})
        self.assertEqual(
            {"DELEGATED": ["owner/repo#2", "owner/repo#3"], "On HOLD": ["owner/repo#1", "owner/repo#4"]},
            sections(reordered),
        )

    def test_statuses_whose_headings_are_the_same_share_one_section(self) -> None:
        # Capitalized, the dotless i (U+0131) and i are both I, so the two spellings of ix were two sections headed Ix.
        items = [item(number=number) for number in (1, 2, 3, 4)]
        overrides = {
            "owner/repo#1": "\u0131x",
            "owner/repo#2": "ix",
            "owner/repo#3": "wait  on\tci",
            "owner/repo#4": "Wait On ci",
        }
        content, _ = run(items, overrides=overrides)
        self.assertEqual(
            {"Ix": ["owner/repo#1", "owner/repo#2"], "Wait On Ci": ["owner/repo#3", "owner/repo#4"]},
            sections(content),
        )

    def test_two_statuses_are_one_exactly_when_their_headings_are_the_same_ignoring_case(self) -> None:
        # Letters a word's capital first letter folds together, and ones it leaves apart because they are not first.
        statuses = (
            "\u0131x",
            "ix",
            "Ix",
            "x\u0131",
            "xi",
            "\u00dfa",
            "ssa",
            "SSA",
            "\u01c6x",
            "\u01c4x",
            "a  b",
            "A B",
        )
        for first, second in itertools.product(statuses, repeat=2):
            with self.subTest(first=first, second=second):
                self.assertEqual(
                    dashboard_status_heading(first).casefold() == dashboard_status_heading(second).casefold(),
                    dashboard_status_key(first) == dashboard_status_key(second),
                )
        self.assertEqual("Ix", dashboard_status_heading("\u0131x"))
        self.assertNotEqual(dashboard_status_key("x\u0131"), dashboard_status_key("xi"))

    def test_a_computed_state_spaced_differently_is_refused(self) -> None:
        # The heading collapses the spaces, so "to  review" rendered a second To Review section.
        for value in ("to  review", "Awaiting\tResponse", " my  PULL requests "):
            with self.subTest(value=value), self.assertRaisesRegex(TrackerError, "computed tracker state"):
                normalize_overrides({"owner/repo#1": value})

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

    def test_a_pull_request_is_tracked_when_the_user_authored_it_is_asked_to_review_it_or_has_reviewed_it(self) -> None:
        authored = item(number=1) | {"author": "Reviewer", "requested_reviewers": []}
        requested = item(number=2)
        # The user has a review in each state GitHub keeps; a review comment alone leaves a COMMENTED review.
        states = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED")
        reviewing = [reviewed(3 + index, state) | {"requested_reviewers": []} for index, state in enumerate(states)]
        # Pushing commits or commenting on the conversation leaves the user no review and asks for none.
        unreviewed = item(number=7) | {"requested_reviewers": []}
        detector = FakeDetector({(number, USER_REVIEWED): CHANGED for number in range(3, 7)})
        items = validate_items([authored, requested, *reviewing, unreviewed])
        self.assertEqual([f"owner/repo#{number}" for number in range(1, 7)], tracked_pulls(items, "reviewer"))
        rows = evaluate(items, "reviewer", detector)
        self.assertEqual(
            [
                (1, "authored"),
                (2, "review requested"),
                (3, "reviewing"),
                (4, "reviewing"),
                (5, "reviewing"),
                (6, "reviewing"),
            ],
            [(row.item["number"], row.relationship) for row in rows],
        )
        self.assertNotIn(7, [candidate["number"] for candidate in review_candidates(rows)])

    def test_the_ai_review_is_compared_from_the_base_it_was_reviewed_on_when_the_record_names_it(self) -> None:
        rebased = item(number=1) | {"reviewed_base_sha": "e" * 40}
        legacy = item(number=2)
        detector = ReadAheadDetector({(1, AI_REVIEWED): UNCHANGED})
        rows = evaluate(validate_items([rebased, legacy]), "reviewer", detector)
        self.assertEqual({(1, AI_REVIEWED): "e" * 40, (2, AI_REVIEWED): None}, detector.bases)
        self.assertIn(("prefetch", Query("owner/repo", "main", AI_REVIEWED, HEAD, "e" * 40)), detector.events)
        self.assertEqual([2], [candidate["number"] for candidate in review_candidates(rows)])

    def test_the_user_s_review_is_compared_from_the_base_recorded_for_it(self) -> None:
        # GitHub compares the review commit as unchanged from the base it was recorded on, and as changed from the base
        # branch as it is now, which a rewrite of a lower branch moved.
        results = {(USER_REVIEWED, RECORDED_BASE): UNCHANGED, (USER_REVIEWED, None): CHANGED}
        for state, recorded, shown in (
            ("CHANGES_REQUESTED", True, "Awaiting Response"),
            ("COMMENTED", True, "Awaiting Response"),
            ("APPROVED", True, None),
            ("CHANGES_REQUESTED", False, SECTION_TO_REVIEW),
            ("APPROVED", False, SECTION_TO_REVIEW),
        ):
            with self.subTest(state=state, recorded=recorded):
                detector = BaseDetector(results)
                bases = {"owner/repo#2": RECORDED_BASE} if recorded else None
                rows = evaluate(validate_items([reviewed(2, state)]), "reviewer", detector, review_bases=bases)
                self.assertEqual([shown] if shown else [], [row.section for row in rows])
                expected = RECORDED_BASE if recorded else None
                self.assertEqual([(USER_REVIEWED, expected)], detector.compared[:1], "the recorded base is asked first")
                self.assertIn(Query("owner/repo", "main", USER_REVIEWED, HEAD, expected), detector.read_ahead)

    def test_a_recorded_base_github_cannot_compare_reads_as_the_base_branch_does(self) -> None:
        for now, shown in (
            (CHANGED, SECTION_TO_REVIEW),
            (UNCHANGED, "Awaiting Response"),
            (UNKNOWN, "Awaiting Response"),
        ):
            with self.subTest(now=now):
                detector = BaseDetector({(USER_REVIEWED, RECORDED_BASE): UNKNOWN, (USER_REVIEWED, None): now})
                rows = evaluate(
                    validate_items([reviewed(2, "CHANGES_REQUESTED")]),
                    "reviewer",
                    detector,
                    review_bases={"owner/repo#2": RECORDED_BASE},
                )
                self.assertEqual([shown], [row.section for row in rows])
                self.assertEqual([(USER_REVIEWED, RECORDED_BASE), (USER_REVIEWED, None)], detector.compared[:2])

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


class DashboardReadRefusedForAMomentTests(unittest.TestCase):
    """A dashboard whose read another process refuses for a moment, as Windows refuses one that meets a concurrent
    run's replace of it through atomic_file."""

    DASHBOARD = f"Before\n{START_MARKER}\nold\n{END_MARKER}\nAfter\n".encode()

    def update(self, refusals: int | None) -> tuple[Any, list[float], list[OSError], bytes]:
        """update_dashboard_rows over a dashboard whose reads are refused with PermissionError `refusals` times, or
        every time for None. Returns its rows or what it raised, the waits, each refusal, and the dashboard's bytes."""
        real_read_bytes = Path.read_bytes
        raised: list[OSError] = []
        waits: list[float] = []
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(Path(temporary), self.DASHBOARD)

            def read_bytes(self: Path) -> bytes:
                if self == dashboard and (refusals is None or len(raised) < refusals):
                    raised.append(PermissionError(13, "Permission denied", str(self)))
                    raise raised[-1]
                return real_read_bytes(self)

            with mock.patch.object(Path, "read_bytes", read_bytes), mock.patch.object(time, "sleep", waits.append):
                try:
                    result: Any = update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())
                except TrackerError as error:
                    result = error
            return result, waits, raised, dashboard.read_bytes()

    def test_a_dashboard_read_refused_for_a_while_is_read_after_short_waits(self) -> None:
        # Holds on every platform, by injected refusals.
        rows, waits, _raised, written = self.update(2)
        self.assertIsInstance(rows, list)
        self.assertEqual([0.01, 0.02], waits)
        self.assertNotEqual(self.DASHBOARD, written, "the update wrote the dashboard")

    def test_a_dashboard_read_refused_throughout_fails_from_the_last_refusal_before_github_is_asked(self) -> None:
        # Holds on every platform. The back-off is pinned against literals: five waits of 10, 20, 40, 80, and 160 ms.
        error, waits, raised, written = self.update(None)
        self.assertIsInstance(error, TrackerError)
        self.assertEqual([0.01, 0.02, 0.04, 0.08, 0.16], waits)
        self.assertEqual(6, len(raised))
        self.assertIs(raised[-1], error.__cause__, "the failure names the last try's refusal")
        self.assertEqual(self.DASHBOARD, written)


class RecordingDetector(FakeDetector):
    """Records each comparison and runs an action during the first, as a user saving the dashboard mid-run would."""

    def __init__(self, during: Callable[[], object] | None = None) -> None:
        super().__init__()
        self.calls: list[int] = []
        self.during = during

    @override
    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        if not self.calls and self.during is not None:
            self.during()
        self.calls.append(number)
        return super().detect(repository, number, base_ref, since_sha, head_sha, since_base)


def dashboard_files(root: Path, dashboard_bytes: bytes, items: list[dict[str, Any]] | None = None) -> tuple[Path, Path]:
    source = root / "input.json"
    dashboard = root / "dashboard.md"
    source.write_text(json.dumps([item()] if items is None else items), encoding="utf-8")
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
        self.events: list[tuple[str, Query]] = []

    @override
    def prefetch(self, queries: Iterable[Query]) -> None:
        self.events.extend(("prefetch", query) for query in queries)
        self.events.append(("round", Query("", "", "", "")))

    @override
    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        self.events.append(("detect", Query(repository, base_ref, since_sha, head_sha, since_base)))
        return super().detect(repository, number, base_ref, since_sha, head_sha, since_base)


class BaseDetector(ReadAheadDetector):
    """Answers each comparison by its earlier commit and the base that commit is compared from, None for the base
    branch as it is now, CHANGED for one not given, and records each such pair it compares and each query it reads
    ahead."""

    def __init__(self, results: dict[tuple[str, str | None], str]) -> None:
        super().__init__()
        self.by_base = results
        self.compared: list[tuple[str, str | None]] = []

    @property
    def read_ahead(self) -> list[Query]:
        return [query for event, query in self.events if event == "prefetch"]

    @override
    def detect(
        self,
        repository: str,
        number: int,
        base_ref: str,
        since_sha: str,
        head_sha: str,
        since_base: str | None = None,
    ) -> str:
        super().detect(repository, number, base_ref, since_sha, head_sha, since_base)
        self.compared.append((since_sha, since_base))
        return self.by_base.get((since_sha, since_base), CHANGED)


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


class RetargetedHeadTests(unittest.TestCase):
    """A pull request GitHub retargets with no new head once the lower one of its stack is squash-merged, through the
    change detector itself: the reviewed head is the current head, on another base, so the head's merge base with
    each base decides."""

    RECORDED = "d" * 40  # the base commit the review was made on
    TIP = "e" * 40  # the base branch's tip the collection read
    MERGE_BASE = "2" * 40  # the head's merge base with the recorded base
    # The head's merge base with the base branch: another once the squash left the lower branch's commits out of it.
    MOVED = "1" * 40

    def evaluate(self, value: dict[str, Any], now: str, **options: Any) -> tuple[list[Any], list[str]]:
        """The rows `evaluate` gives the one item, and every endpoint the change detector read, with the head's merge
        base `MERGE_BASE` on `RECORDED` and `now` on the base branch, or a failure for `RECORDED` when `now` is an
        error's output."""
        endpoints: list[str] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            endpoints.append(arguments[-1])
            base = arguments[-1].split("/compare/", 1)[1].split("...", 1)[0]
            if base == self.RECORDED and now.startswith("HTTP"):
                return CommandResult(1, "", now)
            merge_base = self.MERGE_BASE if base == self.RECORDED or now.startswith("HTTP") else now
            return CommandResult(0, json.dumps({"status": "ahead", "merge_base_commit": {"sha": merge_base}}), "")

        detector = ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None))
        return evaluate(validate_items([value]), "reviewer", detector, **options), endpoints

    def ai_reviewed(self, base: str) -> dict[str, Any]:
        return item() | {"reviewed_head_sha": HEAD, "reviewed_base_sha": base, "base_sha": self.TIP}

    def test_the_ai_review_of_a_retargeted_head_is_stale_unless_its_merge_base_is_the_same(self) -> None:
        for now, expected in ((self.MOVED, ["stale"]), (self.MERGE_BASE, []), ("HTTP 404: Not Found", ["stale"])):
            with self.subTest(now=now):
                rows, endpoints = self.evaluate(self.ai_reviewed(self.RECORDED), now)
                self.assertEqual(expected, [candidate["status"] for candidate in review_candidates(rows)])
                self.assertEqual(
                    [
                        f"repos/owner/repo/compare/{self.RECORDED}...{HEAD}?per_page=1",
                        f"repos/owner/repo/compare/main...{HEAD}?per_page=1",
                    ],
                    sorted(endpoints),
                )

    def test_an_ai_review_on_the_base_tip_the_collection_read_compares_nothing(self) -> None:
        rows, endpoints = self.evaluate(self.ai_reviewed(self.TIP), self.MOVED)
        self.assertEqual(([], []), (review_candidates(rows), endpoints))

    def test_the_user_s_approval_of_a_retargeted_head_is_kept_only_while_its_merge_base_is_the_same(self) -> None:
        value = reviewed(1, "APPROVED") | {"user_review_sha": HEAD, "reviewed_head_sha": None, "base_sha": self.TIP}
        # A recorded base GitHub cannot compare is never read as the base branch: the same head there is one
        # comparison, which would keep an approval whose diff may have changed.
        for now, expected in (
            (self.MOVED, [SECTION_TO_REVIEW]),
            (self.MERGE_BASE, []),
            ("HTTP 404: Not Found", [SECTION_TO_REVIEW]),
        ):
            with self.subTest(now=now):
                rows, _ = self.evaluate(value, now, review_bases={"owner/repo#1": self.RECORDED})
                self.assertEqual(expected, [row.section for row in rows])


class ReadAheadTests(unittest.TestCase):
    def items(self, count: int) -> list[dict[str, Any]]:
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
        user, ai_review = (Query("owner/repo", "main", sha, HEAD) for sha in (USER_REVIEWED, AI_REVIEWED))
        self.assertEqual(
            [
                ("prefetch", user),  # approved#1
                ("prefetch", user),  # commented#2; pinned#3 and mine#4 need no user comparison
                ("round", Query("", "", "", "")),
                ("detect", user),
                ("detect", user),
                ("prefetch", ai_review),  # commented#2, pinned#3, mine#4; approved#1 is unchanged and left out
                ("prefetch", ai_review),
                ("prefetch", ai_review),
                ("round", Query("", "", "", "")),
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
        for stderr in ("HTTP 403: Forbidden", "HTTP 500: Internal Server Error", "HTTP 404: Not Found"):
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


class ReplacementTests(unittest.TestCase):
    def test_a_place_whose_own_stat_fails_after_its_target_is_not_found_is_refused_never_written(self) -> None:
        # The stat through the path answers "not found", as for a link to nothing, and the stat of the place itself
        # fails, so whether a link stands there cannot be told: the replacement is refused, naming the stat's reason.
        path = Path("tracker.json")

        def stat(self: Path, *, follow_symlinks: bool = True) -> object:
            if follow_symlinks:
                raise FileNotFoundError(2, "No such file or directory", str(self))
            raise PermissionError(13, "Permission denied", str(self))

        with mock.patch.object(Path, "stat", stat), self.assertRaises(TrackerError) as raised:
            replacement(path)
        self.assertEqual(f"Cannot examine {path}: Permission denied", str(raised.exception))

    @unittest.skipIf(sys.platform == "win32", "Windows gives a folder no permission that refuses a new file in it")
    def test_a_file_in_a_folder_that_takes_no_new_file_is_refused_and_nothing_is_left(self) -> None:
        # Linux and macOS: the replace writes its copy beside the file, which the folder's permission refuses, so update
        # asks it of the tracker record before the dashboard, in another folder, is replaced.
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "code-review"
            folder.mkdir()
            path = folder / "tracker.json"
            path.write_bytes(b"{}")
            folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
            try:
                with self.assertRaises(TrackerError) as raised:
                    replacement(path)
            finally:
                folder.chmod(stat.S_IRWXU)
            self.assertTrue(str(raised.exception).startswith(f"Cannot replace {path} for the update: "))
            self.assertIn("its folder takes no new file", str(raised.exception))
            self.assertEqual(["tracker.json"], sorted(entry.name for entry in folder.iterdir()))


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


class PullRequestTextTests(unittest.TestCase):
    """A pull request's title and its author's name are the author's text: written into the dashboard as literal text,
    they add no marker, comment, cell, or line, so the next update finds one marker pair and the same rows."""

    SHAPES: ClassVar[dict[str, str]] = {
        "end marker": f"Fix {END_MARKER} parsing",
        "start marker": f"Fix {START_MARKER} parsing",
        "comment opener": "Hide <!-- the rest",
        "comment closer": "Close --> early",
        "escaped pipe": "Split \\| cells",
        "pipe": "Split | cells",
        "carriage return": "One\rTwo",
        "line feed": "One\nTwo",
        "next line": "One\u0085Two",
        "line separator": "One\u2028Two",
        "paragraph separator": "One\u2029Two",
        "link": "[click](https://example.com) ![image](x.png)",
        "code and emphasis": "`code` *bold* _it_ <b>tag</b> &amp;",
        "leading spaces": "    indented",
    }

    def owned(self, dashboard: Path) -> str:
        content = dashboard.read_text(encoding="utf-8")
        self.assertEqual(1, content.count(START_MARKER))
        self.assertEqual(1, content.count(END_MARKER))
        return content[content.index(START_MARKER) + len(START_MARKER) : content.index(END_MARKER)]

    def assert_structure_holds(self, owned: str, text: str) -> None:
        rows = [line for line in owned.split("\n") if line.startswith("| ") and "/pull/" in line]
        self.assertEqual(2, len(rows), owned)
        for row in rows:
            self.assertEqual(8, row.count("|"), row)
        self.assertNotIn("<!--", owned)
        self.assertNotIn("-->", owned)
        self.assertNotIn("\\", owned)
        for separator in ("\r", "\u0085", "\u2028", "\u2029"):
            self.assertNotIn(separator, owned)
        for word in re.findall(r"[A-Za-z]{3,}", text):
            self.assertIn(word, owned)

    def update_twice(self, first: dict[str, Any], text: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source, dashboard = dashboard_files(
                Path(temporary), f"Before\n{START_MARKER}\nold\n{END_MARKER}\nAfter\n".encode(), [first, item(number=2)]
            )
            for _ in range(2):
                rows = update_dashboard_rows(source, dashboard, "reviewer", detector=FakeDetector())
                self.assertEqual(2, len(rows))
                self.assert_structure_holds(self.owned(dashboard), text)
            self.assertTrue(dashboard.read_text(encoding="utf-8").endswith(f"{END_MARKER}\nAfter\n"))

    def test_a_title_cannot_change_the_dashboard_s_structure(self) -> None:
        for name, title in self.SHAPES.items():
            first = item(number=1)
            first["title"] = title
            with self.subTest(name):
                self.update_twice(first, title)

    def test_an_author_s_profile_name_cannot_change_the_dashboard_s_structure(self) -> None:
        for name, author_name in self.SHAPES.items():
            first = item(number=1)
            first.update(author="writer", author_name=author_name)
            with self.subTest(name):
                self.update_twice(first, author_name)

    def test_a_title_is_shown_as_its_text(self) -> None:
        first = item(number=1)
        first["title"] = "Fix <!-- a | b \\| c\u2028d"
        content, _ = run([first])
        self.assertIn("| owner/repo | [#1 Fix &lt;&#33;&#45;&#45; a &#124; b &#92;&#124; c d](", content)

    def test_a_link_destination_that_is_not_absolute_https_leaves_the_title_unlinked(self) -> None:
        first = item(number=1)
        first["url"] = "javascript:alert(1) --> |"
        content, _ = run([first])
        self.assertIn("| someone | owner/repo | #1 Improve &#124; behavior | Open | - | - | (stale) |", content)
        self.assertNotIn("javascript", content)

    def test_a_link_destination_s_delimiters_are_percent_encoded(self) -> None:
        first = item(number=1)
        first["url"] = "https://github.com/owner/repo/pull/1?a=(b)|<c>"
        content, _ = run([first])
        self.assertIn("](https://github.com/owner/repo/pull/1?a=%28b%29%7C%3Cc%3E) |", content)

    def test_a_repository_name_markdown_reads_specially_is_literal_in_both_tables(self) -> None:
        repository = "owner/_hidden-repo_.v2"
        theirs = item(repository, 1)
        mine = item(repository, 2) | {"author": "reviewer"}
        content, _ = run([theirs, mine])
        literal = "owner/&#95;hidden&#45;repo&#95;&#46;v2"
        self.assertIn(
            f"| someone | {literal} | [#1 Improve &#124; behavior](https://github.com/{repository}/pull/1) |", content
        )
        self.assertIn(
            f"\n| {literal} | [#2 Improve &#124; behavior](https://github.com/{repository}/pull/2) |", content
        )
        self.assertEqual({"To Review": [f"{repository}#1"], "My PRs": [f"{repository}#2"]}, sections(content))


def statuses(content: str) -> dict[int, str]:
    """Each row's Status cell by its pull request's number: the cell after the one linking the pull request."""
    found: dict[int, str] = {}
    for line in content.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split(" | ")]
        for index, cell in enumerate(cells):
            number = re.search(r"/pull/(\d+)\)", cell)
            if line.startswith("| ") and number:
                found[int(number[1])] = cells[index + 1]
    return found


def decided(number: int, state: str, decision: str | None, **changes: Any) -> dict[str, Any]:
    """A pull request the user reviewed last with `state`, whose latest deciding review is `decision`."""
    return reviewed(number, state) | {"user_review_decision": decision, **changes}


class StatusTests(unittest.TestCase):
    """Every table has a Status column filled for every row: the user's own latest deciding review on a pull request
    someone else authored, and GitHub's review decision on one the user authored."""

    def test_each_status_of_a_pull_request_awaiting_the_user(self) -> None:
        rows = [
            (item(number=1), "Open"),
            (decided(2, "COMMENTED", None), "Reviewed with Comments"),
            (decided(3, "COMMENTED", "DISMISSED"), "Reviewed with Comments"),
            (decided(4, "DISMISSED", "DISMISSED"), "Reviewed with Comments"),
            (decided(5, "CHANGES_REQUESTED", "CHANGES_REQUESTED"), "Changes Requested"),
            (decided(6, "COMMENTED", "CHANGES_REQUESTED"), "Changes Requested"),
            (decided(7, "APPROVED", "APPROVED"), "Approved"),
            (decided(8, "COMMENTED", "APPROVED"), "Approved"),
            (decided(9, "APPROVED", "APPROVED", draft=True), "Draft"),
            (item(number=10) | {"draft": True}, "Draft"),
            # GitHub's decision on someone else's pull request is not the user's.
            (item(number=11) | {"review_decision": "APPROVED"}, "Open"),
        ]
        # Every review is followed by new commits, so even an approved pull request is shown again.
        content, _ = run([value for value, _ in rows])
        self.assertEqual({value["number"]: status for value, status in rows}, statuses(content))
        self.assertEqual(
            {
                "To Review": [f"owner/repo#{number}" for number in (1, 2, 3, 4, 5, 6, 7, 8, 11)],
                "Drafts": ["owner/repo#9", "owner/repo#10"],
            },
            sections(content),
        )

    def test_a_status_is_shown_in_the_awaiting_response_table_too(self) -> None:
        detector = FakeDetector({(1, USER_REVIEWED): UNCHANGED, (2, USER_REVIEWED): UNCHANGED})
        rows = [decided(1, "COMMENTED", None), decided(2, "COMMENTED", "CHANGES_REQUESTED")]
        content, _ = run(rows, detector)
        self.assertEqual({"Awaiting Response": ["owner/repo#1", "owner/repo#2"]}, sections(content))
        self.assertEqual({1: "Reviewed with Comments", 2: "Changes Requested"}, statuses(content))

    def test_each_status_of_a_pull_request_the_user_authored(self) -> None:
        mine = {"author": "reviewer"}
        rows = [
            (item(number=1) | mine, "Open"),
            (item(number=2) | mine | {"review_decision": "REVIEW_REQUIRED"}, "Open"),
            (item(number=3) | mine | {"review_decision": "APPROVED"}, "Approved"),
            (item(number=4) | mine | {"review_decision": "CHANGES_REQUESTED"}, "Changes Requested"),
            (item(number=5) | mine | {"review_decision": "APPROVED", "draft": True}, "Draft"),
            # The user's own reviews of their pull request decide nothing; GitHub's decision does.
            (decided(6, "COMMENTED", None) | mine, "Open"),
        ]
        content, _ = run([value for value, _ in rows])
        self.assertEqual({value["number"]: status for value, status in rows}, statuses(content))
        self.assertEqual({"My PRs": [f"owner/repo#{number}" for number in (5, 1, 2, 3, 4, 6)]}, sections(content))

    def test_a_pinned_row_shows_its_status_whoever_authored_it(self) -> None:
        rows = [
            decided(1, "COMMENTED", "APPROVED"),
            item(number=2) | {"author": "reviewer", "review_decision": "APPROVED"},
        ]
        content, _ = run(rows, overrides={"owner/repo#1": "on hold", "owner/repo#2": "on hold"})
        # A pinned section groups its rows by requestor, and the user's own login sorts ahead of someone.
        self.assertEqual({"On Hold": ["owner/repo#2", "owner/repo#1"]}, sections(content))
        self.assertEqual({1: "Approved", 2: "Approved"}, statuses(content))

    def test_an_input_collected_before_deciding_reviews_were_read_takes_its_latest_deciding_review(self) -> None:
        for state, decision in (
            (None, None),
            ("COMMENTED", None),
            ("APPROVED", "APPROVED"),
            ("CHANGES_REQUESTED", "CHANGES_REQUESTED"),
            ("DISMISSED", "DISMISSED"),
        ):
            with self.subTest(state=state):
                sha = None if state is None else USER_REVIEWED
                [result] = validate_items([item() | {"user_review_state": state, "user_review_sha": sha}])
                self.assertEqual(decision, result["user_review_decision"])

    def test_a_deciding_review_must_be_the_latest_review_when_that_decided(self) -> None:
        key = "Tracker item owner/repo#1"
        for state in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            for decision in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED", None):
                value = decided(1, state, decision)
                with self.subTest(state=state, decision=decision):
                    if decision == state:
                        self.assertEqual(decision, validate_items([value])[0]["user_review_decision"])
                    else:
                        self.assertEqual(
                            ("TrackerError", f"{key}.user_review_decision must be {state}, its latest review's"),
                            refusal([value]),
                        )
        for decision in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED", None):
            with self.subTest(state="COMMENTED", decision=decision):
                self.assertEqual(
                    decision, validate_items([decided(1, "COMMENTED", decision)])[0]["user_review_decision"]
                )


# The owned section as the version before the Repository and Status columns wrote it for PREVIOUS_ITEMS.
PREVIOUS_SECTION = f"""{START_MARKER}

{LEGEND}

### To Review (1)

<details open>
<summary>{SECTION_SUMMARIES[SECTION_TO_REVIEW]}</summary>

| Requestor | PR | AI Result | Findings | AI Review |
| :--- | :--- | :--- | :--- | :--- |
| someone | [#1 Improve &#124; behavior](https://github.com/owner/repo/pull/1) | Approved | none open | (stale) |

</details>

### Awaiting Response (1)

<details open>
<summary>PRs where you have left a review; waiting for the author to respond or push changes.</summary>

| Requestor | PR | AI Result | Findings | AI Review |
| :--- | :--- | :--- | :--- | :--- |
| someone | [other#2 Improve &#124; behavior](https://github.com/owner/other/pull/2) | - | - | - |

</details>

### My PRs (1)

<details open>
<summary>PRs you authored. Status reflects GitHub's review decision on the PR.</summary>

| PR | Status | AI Result | Findings | AI Review |
| :--- | :--- | :--- | :--- | :--- |
| [#3 Improve &#124; behavior](https://github.com/owner/repo/pull/3) | Approved | - | - | (stale) |

</details>

{END_MARKER}"""


class PreviousVersionTests(unittest.TestCase):
    """A dashboard, tracker record, and input the previous version wrote update to the new layout and nothing else."""

    def test_a_tracker_the_previous_version_wrote_changes_only_its_layout(self) -> None:
        # Collected before deciding reviews were read: no item has user_review_decision.
        previous_items = [
            reviewed(1, "COMMENTED") | {"ai_review": ai("APPROVED", report=None)},
            item("owner/other", 2)
            | {"reviewed_head_sha": None, "user_review_state": "CHANGES_REQUESTED", "user_review_sha": USER_REVIEWED},
            item(number=3) | {"author": "reviewer", "review_decision": "APPROVED"},
        ]
        detector = FakeDetector({(2, USER_REVIEWED): UNCHANGED})
        record_value: dict[str, Any] = {
            "schema_version": 2,
            "pulls": ["owner/other#2", "owner/repo#1", "owner/repo#3"],
            "reviews": {
                "owner/other#2": {"state": "missing", "reviewed_head_sha": None},
                "owner/repo#1": {"state": "stale", "reviewed_head_sha": AI_REVIEWED},
                "owner/repo#3": {"state": "stale", "reviewed_head_sha": AI_REVIEWED},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            document = f"# Notes\n{PREVIOUS_SECTION}\nKept.\n".encode()
            source, dashboard = dashboard_files(root, document, previous_items)
            record = root / "tracker.json"
            record.write_text(json.dumps(record_value), encoding="utf-8")
            update_dashboard_rows(source, dashboard, "reviewer", detector=detector, record=record)
            updated = dashboard.read_bytes()
            self.assertEqual(outside(document), outside(updated))
            content = updated.decode()
            self.assertEqual(sections(PREVIOUS_SECTION), sections(content), "every row keeps its section")
            for line in (
                "| someone | owner/repo | [#1 Improve &#124; behavior](https://github.com/owner/repo/pull/1) | "
                "Reviewed with Comments | Approved | none open | (stale) |",
                "| someone | owner/other | [#2 Improve &#124; behavior](https://github.com/owner/other/pull/2) | "
                "Changes Requested | - | - | - |",
                "| owner/repo | [#3 Improve &#124; behavior](https://github.com/owner/repo/pull/3) | Approved | "
                "- | - | (stale) |",
            ):
                self.assertIn(f"\n{line}\n", content)
            # The record takes the new version, and an input collected before base tips and review versions were
            # read gives no user review a base to record and no AI review a version.
            reviews = {key: review | {"reviewed_version": None} for key, review in record_value["reviews"].items()}
            self.assertEqual(
                {**record_value, "schema_version": 4, "reviews": reviews, "user_reviews": {}},
                json.loads(record.read_text(encoding="utf-8")),
            )
            update_dashboard_rows(source, dashboard, "reviewer", detector=detector, record=record)
            self.assertEqual(updated, dashboard.read_bytes(), "a second update changes nothing")


class UserReviewBaseTests(unittest.TestCase):
    """The base each of the user's own reviews is compared from: the tip of the base branch the first update that saw
    the review's commit collected, kept in the tracker record while that commit stays the user's latest review."""

    @staticmethod
    def record(user_reviews: dict[str, dict[str, str]]) -> dict[str, Any]:
        return validate_tracker_record(
            {"schema_version": 3, "pulls": sorted(user_reviews), "reviews": {}, "user_reviews": user_reviews}
        )

    def test_a_review_first_seen_takes_the_base_tip_collected_with_it(self) -> None:
        items = validate_items([reviewed(2, "CHANGES_REQUESTED") | {"base_sha": BASE_TIP}])
        expected = {"owner/repo#2": user_base(USER_REVIEWED, BASE_TIP)}
        self.assertEqual(expected, user_review_bases(items, "reviewer", None))
        self.assertEqual(expected, user_review_bases(items, "reviewer", self.record({})))

    def test_the_recorded_base_is_kept_while_the_review_commit_stays_the_same(self) -> None:
        items = validate_items([reviewed(2, "APPROVED") | {"base_sha": BASE_TIP}])
        kept = {"owner/repo#2": {"user_review_sha": USER_REVIEWED, "base_sha": RECORDED_BASE}}
        self.assertEqual(
            {"owner/repo#2": user_base(USER_REVIEWED, RECORDED_BASE)},
            user_review_bases(items, "Reviewer", self.record(kept)),
        )
        # A later review's commit is seen for the first time, so it takes the tip collected with it.
        earlier = {"owner/repo#2": {"user_review_sha": AI_REVIEWED, "base_sha": RECORDED_BASE}}
        self.assertEqual(
            {"owner/repo#2": user_base(USER_REVIEWED, BASE_TIP)},
            user_review_bases(items, "reviewer", self.record(earlier)),
        )

    def test_only_a_review_a_section_would_compare_has_a_base_a_draft_s_included(self) -> None:
        compared = [
            reviewed(1, "CHANGES_REQUESTED") | {"draft": True},
            reviewed(2, "COMMENTED") | {"requested_reviewers": []},
            reviewed(3, "APPROVED"),
        ]
        others = [
            reviewed(4, "COMMENTED") | {"author": "Reviewer"},
            item(number=5) | {"user_review_state": "DISMISSED", "user_review_sha": USER_REVIEWED},
            item(number=6),
        ]
        items = validate_items([value | {"base_sha": BASE_TIP} for value in compared + others])
        self.assertEqual(
            {f"owner/repo#{number}": user_base(USER_REVIEWED, BASE_TIP) for number in (1, 2, 3)},
            user_review_bases(items, "reviewer", None),
        )

    def test_two_user_reviews_of_one_pull_request_in_another_case_are_refused(self) -> None:
        base = {"user_review_sha": USER_REVIEWED, "base_sha": RECORDED_BASE}
        self.assertEqual(
            (
                "TrackerError",
                "Tracker user review owner/repo#2 names no pull request the record holds, or is named twice",
            ),
            raised(
                lambda: validate_tracker_record(
                    {
                        "schema_version": 3,
                        "pulls": ["owner/repo#2"],
                        "reviews": {},
                        "user_reviews": {"owner/repo#2": base, "Owner/Repo#2": base},
                    }
                )
            ),
        )

    def test_a_review_with_no_base_recorded_or_collected_has_none(self) -> None:
        items = validate_items([reviewed(2, "CHANGES_REQUESTED")])
        self.assertEqual({}, user_review_bases(items, "reviewer", None))

    def test_the_update_records_each_base_and_compares_the_review_from_it(self) -> None:
        # The first update records the tip collected with the review and compares the review from the base branch,
        # which is at that tip; a later one, after a rewrite moved the tip, compares it from the recorded tip and keeps
        # it.
        detector = BaseDetector({(USER_REVIEWED, RECORDED_BASE): UNCHANGED, (USER_REVIEWED, None): UNCHANGED})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "tracker.json"
            for tip in (RECORDED_BASE, BASE_TIP):
                source, dashboard = dashboard_files(
                    root,
                    f"{START_MARKER}\n{END_MARKER}\n".encode(),
                    [reviewed(2, "CHANGES_REQUESTED") | {"base_sha": tip}],
                )
                previous = (
                    validate_tracker_record(json.loads(record.read_text(encoding="utf-8"))) if tip == BASE_TIP else None
                )
                rows = update_dashboard_rows(
                    source, dashboard, "reviewer", detector=detector, record=record, previous=previous
                )
                self.assertEqual(["Awaiting Response"], [row.section for row in rows])
                self.assertEqual(
                    {"owner/repo#2": user_base(USER_REVIEWED, RECORDED_BASE)},
                    json.loads(record.read_text(encoding="utf-8"))["user_reviews"],
                )
        self.assertEqual(
            [(USER_REVIEWED, None), (USER_REVIEWED, RECORDED_BASE)],
            [pair for pair in detector.compared if pair[0] == USER_REVIEWED],
        )


class ReviewIdentityTests(unittest.TestCase):
    """What tells a review apart from the one the tracker record holds beside its commit: an AI review's version and a
    user review's GitHub ID, each compared only when the record and the collection both name it."""

    KEY = "owner/repo#1"
    FIRST_ID = "PRR_first"
    SECOND_ID = "PRR_second"

    def record(self, review: dict[str, Any], version: int = 4) -> dict[str, Any]:
        return validate_tracker_record(
            {"schema_version": version, "pulls": [self.KEY], "reviews": {self.KEY: review}, "user_reviews": {}}
        )

    def stale_row(self, head: str, version: int | None) -> Row:
        value = validate_items([item() | {"reviewed_head_sha": head, "reviewed_version": version}])[0]
        return Row(value, "review requested", SECTION_TO_REVIEW, "stale", False)

    def test_a_retargeted_head_reviewed_on_another_base_is_compared_through_the_fake_detector_too(self) -> None:
        # The fake detector reads the same head on another base as the real one does, as a comparison to make, so a
        # test through it shows a retargeted head compared; only the base tip the collection read is one comparison.
        recorded, tip = "d" * 40, "e" * 40
        retargeted = item() | {"reviewed_head_sha": HEAD, "reviewed_base_sha": recorded, "base_sha": tip}
        for value, detector, expected in (
            (retargeted, FakeDetector(), ["stale"]),
            (retargeted, FakeDetector({(1, HEAD): UNCHANGED}), []),
            (retargeted | {"reviewed_base_sha": tip}, FakeDetector({(1, HEAD): CHANGED}), []),
        ):
            with self.subTest(value=value["reviewed_base_sha"], results=detector.results):
                rows = evaluate(validate_items([value]), "reviewer", detector)
                self.assertEqual(expected, [candidate["status"] for candidate in review_candidates(rows)])

    def test_a_review_written_again_at_the_same_head_that_went_stale_is_named(self) -> None:
        # The record held version 1 stale at the head; a re-review on a new base wrote version 2 at the same head,
        # which went stale before this update.
        previous = self.record({"state": "stale", "reviewed_head_sha": AI_REVIEWED, "reviewed_version": 1})
        self.assertEqual([self.KEY], stale_since(previous, [self.stale_row(AI_REVIEWED, 2)]))
        self.assertEqual([], stale_since(previous, [self.stale_row(AI_REVIEWED, 1)]), "the same review stayed stale")

    def test_a_review_with_no_version_on_either_side_is_told_apart_by_its_head_alone(self) -> None:
        # A version 3 record names no review version, nor does a migrated legacy review or an input without one.
        held = {"state": "stale", "reviewed_head_sha": AI_REVIEWED}
        for previous, version in (
            (self.record(held, version=3), 2),
            (self.record(held | {"reviewed_version": None}), 2),
            (self.record(held | {"reviewed_version": 1}), None),
        ):
            with self.subTest(previous=previous, version=version):
                self.assertEqual([], stale_since(previous, [self.stale_row(AI_REVIEWED, version)]))
                self.assertEqual([self.KEY], stale_since(previous, [self.stale_row(HEAD, version)]))

    def test_a_version_3_record_reads_with_no_review_version_and_no_user_review_id(self) -> None:
        record = validate_tracker_record(
            {
                "schema_version": 3,
                "pulls": [self.KEY],
                "reviews": {self.KEY: {"state": "stale", "reviewed_head_sha": AI_REVIEWED}},
                "user_reviews": {self.KEY: {"user_review_sha": USER_REVIEWED, "base_sha": RECORDED_BASE}},
            }
        )
        self.assertEqual(
            {
                "schema_version": 4,
                "pulls": [self.KEY],
                "reviews": {self.KEY: {"state": "stale", "reviewed_head_sha": AI_REVIEWED, "reviewed_version": None}},
                "user_reviews": {self.KEY: user_base(USER_REVIEWED, RECORDED_BASE)},
            },
            record,
        )

    def test_each_record_version_takes_exactly_its_own_review_fields(self) -> None:
        unversioned = {"state": "stale", "reviewed_head_sha": AI_REVIEWED}
        review = unversioned | {"reviewed_version": 1}
        unidentified = {"user_review_sha": USER_REVIEWED, "base_sha": RECORDED_BASE}
        base = unidentified | {"user_review_id": self.FIRST_ID}
        path = f"tracker.reviews.{self.KEY}"
        user_path = f"tracker.user_reviews.{self.KEY}"
        cases: list[tuple[int, dict[str, Any], dict[str, Any], tuple[str, str]]] = [
            (4, unversioned, {}, wrong(path, "an object with field reviewed_version", "an object without it")),
            (4, review, unidentified, wrong(user_path, "an object with field user_review_id", "an object without it")),
            (3, review, {}, wrong(path, "an object of known fields", 'unknown field "reviewed_version"')),
            (3, {}, base, wrong(user_path, "an object of known fields", 'unknown field "user_review_id"')),
            (
                4,
                review | {"reviewed_version": 0},
                {},
                wrong(f"{path}.reviewed_version", "an integer of at least 1 or null", "integer 0"),
            ),
            (
                4,
                {},
                base | {"user_review_id": ""},
                wrong(f"{user_path}.user_review_id", TEXT_OR_NULL, "an empty string"),
            ),
            (
                4,
                {"state": "missing", "reviewed_head_sha": None, "reviewed_version": 1},
                {},
                ("TrackerError", f"Tracker review {self.KEY} names a review version without a reviewed head"),
            ),
        ]
        for version, given_review, given_base, expected in cases:
            with self.subTest(version=version, review=given_review, base=given_base):
                value = {
                    "schema_version": version,
                    "pulls": [self.KEY],
                    "reviews": {self.KEY: given_review} if given_review else {},
                    "user_reviews": {self.KEY: given_base} if given_base else {},
                }
                self.assertEqual(expected, raised(partial(validate_tracker_record, value)))

    def user_record(self, identity: str | None, version: int = 4) -> dict[str, Any]:
        base = user_base(USER_REVIEWED, RECORDED_BASE, identity)
        if version == 3:
            del base["user_review_id"]
        return validate_tracker_record(
            {"schema_version": version, "pulls": [self.KEY], "reviews": {}, "user_reviews": {self.KEY: base}}
        )

    def collected(self, identity: str | None) -> list[dict[str, Any]]:
        return validate_items([reviewed(1, "APPROVED") | {"base_sha": BASE_TIP, "user_review_id": identity}])

    def test_a_user_review_made_again_at_the_same_commit_takes_the_tip_collected_with_it(self) -> None:
        self.assertEqual(
            {self.KEY: user_base(USER_REVIEWED, BASE_TIP, self.SECOND_ID)},
            user_review_bases(self.collected(self.SECOND_ID), "reviewer", self.user_record(self.FIRST_ID)),
        )

    def test_the_same_user_review_keeps_its_base_and_gains_an_id_its_record_lacked(self) -> None:
        for record, identity, expected in (
            (self.user_record(self.FIRST_ID), self.FIRST_ID, self.FIRST_ID),
            # A version 3 record, or one written from an input that collected no ID, names none, so the review is
            # told apart by its commit alone and takes the ID collected now.
            (self.user_record(None, version=3), self.FIRST_ID, self.FIRST_ID),
            (self.user_record(None), self.FIRST_ID, self.FIRST_ID),
            # An input that collected no ID keeps the one recorded.
            (self.user_record(self.FIRST_ID), None, self.FIRST_ID),
        ):
            with self.subTest(record=record, identity=identity):
                self.assertEqual(
                    {self.KEY: user_base(USER_REVIEWED, RECORDED_BASE, expected)},
                    user_review_bases(self.collected(identity), "reviewer", record),
                )

    def test_an_approval_made_again_after_a_retarget_moved_its_merge_base_is_kept(self) -> None:
        # The user approved the head on the recorded base; a retarget moved its merge base, so the approval is offered
        # again. The user approves again at the same head, and from then on the approval is compared from the tip
        # collected with it, which the base branch is still at.
        detector = BaseDetector({(HEAD, None): UNCHANGED})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record = root / "tracker.json"
            approved = reviewed(1, "APPROVED") | {"user_review_sha": HEAD, "reviewed_head_sha": None}
            sections = []
            for tip, identity in (
                (RECORDED_BASE, self.FIRST_ID),
                (BASE_TIP, self.FIRST_ID),
                (BASE_TIP, self.SECOND_ID),
            ):
                source, dashboard = dashboard_files(
                    root,
                    f"{START_MARKER}\n{END_MARKER}\n".encode(),
                    [approved | {"base_sha": tip, "user_review_id": identity}],
                )
                previous = (
                    validate_tracker_record(json.loads(record.read_text(encoding="utf-8"))) if record.exists() else None
                )
                rows = update_dashboard_rows(
                    source, dashboard, "reviewer", detector=detector, record=record, previous=previous
                )
                sections.append([row.section for row in rows])
            self.assertEqual([[], [SECTION_TO_REVIEW], []], sections)
            self.assertEqual(
                {self.KEY: user_base(HEAD, BASE_TIP, self.SECOND_ID)},
                json.loads(record.read_text(encoding="utf-8"))["user_reviews"],
            )


def user_base(sha: str, base: str, identity: str | None = None) -> dict[str, str | None]:
    """A user review's base as the tracker record holds it: the review's commit, the base it is compared from, and its
    GitHub ID, null when none was collected."""
    return {"user_review_sha": sha, "base_sha": base, "user_review_id": identity}


def raised(call: Callable[[], object]) -> tuple[str, str]:
    """The class and message a tracker validator refuses its input with."""
    try:
        call()
    except (TrackerError, ConfigurationError) as exc:
        return type(exc).__name__, str(exc)
    raise AssertionError("the validator accepted its input")


def refusal(value: object) -> tuple[str, str]:
    """The class and message validate_items refuses a value with."""
    return raised(lambda: validate_items(value))


def wrong(path: str, expected: str, found: str) -> tuple[str, str]:
    """The class and message a field of the wrong kind is refused with, written out independently of the reader."""
    return "TrackerFieldError", f"{path} must be {expected}, found {found}"


TEXT = "a non-empty string"
TEXT_OR_NULL = "a non-empty string or null"
VERSION_OR_NULL = "an integer of at least 1 or null"
USER_REVIEW_STATES = 'one of "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED" or null'
REVIEW_DECISIONS = 'one of "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED" or null'
USER_REVIEW_DECISIONS = 'one of "APPROVED", "CHANGES_REQUESTED", "DISMISSED" or null'
VERDICTS = 'one of "APPROVED", "CHANGES_REQUESTED", "INCOMPLETE"'
SECOND = "tracker input[1]"
REVIEW = "tracker input[0].ai_review"


class ValidateItemsSequenceTests(unittest.TestCase):
    """validate_items called directly: what it returns, and each refusal's class and exact message, in check order."""

    def test_a_field_error_is_a_tracker_error(self) -> None:
        self.assertTrue(issubclass(TrackerFieldError, FieldError))
        self.assertTrue(issubclass(TrackerFieldError, TrackerError))
        with self.assertRaises(TrackerFieldError):
            validate_items(None)

    def test_an_input_that_is_not_an_array_is_refused(self) -> None:
        for value, found in (({"repository": "owner/repo"}, "an object"), ("[]", 'string "[]"'), (None, "null")):
            with self.subTest(value=value):
                self.assertEqual(wrong("tracker input", "an array", found), refusal(value))

    def test_an_empty_array_is_accepted(self) -> None:
        self.assertEqual([], validate_items([]))

    def test_an_item_whose_fields_do_not_match_the_contract_is_refused(self) -> None:
        missing = item()
        del missing["user_review_sha"]
        both_missing = copy.deepcopy(missing)
        del both_missing["draft"]
        for value, expected, found in (
            ("owner/repo#1", "an object", 'string "owner/repo#1"'),
            (["owner/repo", 1], "an object", "an array"),
            (missing, "an object with field user_review_sha", "an object without it"),
            (both_missing, "an object with fields draft, user_review_sha", "an object without them"),
            (item() | {"labels": []}, "an object of known fields", 'unknown field "labels"'),
            (item() | {"labels": [], "body": ""}, "an object of known fields", 'unknown fields "body", "labels"'),
        ):
            with self.subTest(value=value):
                self.assertEqual(wrong(SECOND, expected, found), refusal([item(number=2), value]))

    def test_an_accepted_item_gains_its_defaults_first_and_a_lowercase_repository(self) -> None:
        source = item("Owner/Repo", 7)
        original = copy.deepcopy(source)
        [result] = validate_items([source])
        self.assertEqual(original, source)
        self.assertEqual(
            [
                "reviewed_incomplete",
                "reviewed_base_sha",
                "reviewed_version",
                "base_sha",
                "author_name",
                "review_decision",
                "user_review_decision",
                "user_review_id",
                "ai_review",
                "repository",
                "number",
                "url",
                "title",
                "author",
                "requested_reviewers",
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
                "reviewed_base_sha": None,
                "reviewed_version": None,
                "base_sha": None,
                "author_name": None,
                "review_decision": None,
                "user_review_decision": None,
                "user_review_id": None,
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
            "reviewed_base_sha": "e" * 40,
            "reviewed_version": 2,
            "base_sha": "f" * 40,
            "review_decision": "APPROVED",
            "user_review_decision": "APPROVED",
            "user_review_id": "PRR_kwDOAbc",
            "user_review_state": "COMMENTED",
            "user_review_sha": USER_REVIEWED,
        }
        [result] = validate_items([source])
        self.assertEqual(source, result)
        self.assertEqual(
            [
                "reviewed_incomplete",
                "reviewed_base_sha",
                "reviewed_version",
                "base_sha",
                "author_name",
                "review_decision",
                "user_review_decision",
                "user_review_id",
                "ai_review",
                *item(),
            ],
            list(result),
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
            ("author_name", " ", wrong(f"{SECOND}.author_name", TEXT_OR_NULL, "a blank string")),
            ("author_name", 5, wrong(f"{SECOND}.author_name", TEXT_OR_NULL, "integer 5")),
            ("review_decision", "MERGED", wrong(f"{SECOND}.review_decision", REVIEW_DECISIONS, 'string "MERGED"')),
            ("review_decision", True, wrong(f"{SECOND}.review_decision", REVIEW_DECISIONS, "boolean true")),
            *(
                ("user_review_decision", value, wrong(f"{SECOND}.user_review_decision", USER_REVIEW_DECISIONS, found))
                for value, found in (
                    ("COMMENTED", 'string "COMMENTED"'),
                    ("approved", 'string "approved"'),
                    (1, "integer 1"),
                )
            ),
            (
                "user_review_decision",
                "APPROVED",
                ("TrackerError", f"{key}.user_review_decision requires a review state"),
            ),
            ("ai_review", [], wrong(f"{SECOND}.ai_review", "an object or null", "an array")),
            ("reviewed_incomplete", 1, wrong(f"{SECOND}.reviewed_incomplete", "a boolean", "integer 1")),
            ("repository", "owner", ("ConfigurationError", "Invalid repository identity: 'owner'")),
            ("repository", None, wrong(f"{SECOND}.repository", TEXT, "null")),
            ("number", "1", wrong(f"{SECOND}.number", "an integer of at least 1", 'string "1"')),
            ("number", True, wrong(f"{SECOND}.number", "an integer of at least 1", "boolean true")),
            ("number", 0, wrong(f"{SECOND}.number", "an integer of at least 1", "integer 0")),
            ("number", 1.0, wrong(f"{SECOND}.number", "an integer of at least 1", "number 1.0")),
            *(
                (field, value, wrong(f"{SECOND}.{field}", TEXT, found))
                for field in ("url", "title", "author", "base_ref", "head_sha", "updated_at")
                for value, found in (("", "an empty string"), (None, "null"), (5, "integer 5"))
            ),
            *(
                (field, value, wrong(f"{SECOND}.{field}{index}", expected, found))
                for field in ("requested_reviewers",)
                for value, index, expected, found in (
                    ("reviewer", "", "an array", 'string "reviewer"'),
                    (None, "", "an array", "null"),
                    ([""], "[0]", TEXT, "an empty string"),
                    (["reviewer", 5], "[1]", TEXT, "integer 5"),
                    (("reviewer",), "", "an array", "a tuple, not a JSON value"),
                )
            ),
            ("draft", "false", wrong(f"{SECOND}.draft", "a boolean", 'string "false"')),
            ("draft", 0, wrong(f"{SECOND}.draft", "a boolean", "integer 0")),
            ("reviewed_head_sha", "", wrong(f"{SECOND}.reviewed_head_sha", TEXT_OR_NULL, "an empty string")),
            ("reviewed_head_sha", 5, wrong(f"{SECOND}.reviewed_head_sha", TEXT_OR_NULL, "integer 5")),
            ("reviewed_base_sha", "", wrong(f"{SECOND}.reviewed_base_sha", TEXT_OR_NULL, "an empty string")),
            ("reviewed_base_sha", 5, wrong(f"{SECOND}.reviewed_base_sha", TEXT_OR_NULL, "integer 5")),
            ("base_sha", "", wrong(f"{SECOND}.base_sha", TEXT_OR_NULL, "an empty string")),
            ("base_sha", 5, wrong(f"{SECOND}.base_sha", TEXT_OR_NULL, "integer 5")),
            *(
                ("reviewed_version", value, wrong(f"{SECOND}.reviewed_version", VERSION_OR_NULL, found))
                for value, found in ((0, "integer 0"), ("2", 'string "2"'), (True, "boolean true"), (2.0, "number 2.0"))
            ),
            ("user_review_id", "", wrong(f"{SECOND}.user_review_id", TEXT_OR_NULL, "an empty string")),
            ("user_review_id", 5, wrong(f"{SECOND}.user_review_id", TEXT_OR_NULL, "integer 5")),
            ("user_review_id", "PRR_kwDOAbc", ("TrackerError", f"{key}.user_review_id requires a review state")),
            ("user_review_sha", "", wrong(f"{SECOND}.user_review_sha", TEXT_OR_NULL, "an empty string")),
            ("user_review_sha", 5, wrong(f"{SECOND}.user_review_sha", TEXT_OR_NULL, "integer 5")),
            (
                "user_review_state",
                "PENDING",
                wrong(f"{SECOND}.user_review_state", USER_REVIEW_STATES, 'string "PENDING"'),
            ),
            (
                "user_review_state",
                "approved",
                wrong(f"{SECOND}.user_review_state", USER_REVIEW_STATES, 'string "approved"'),
            ),
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

    def test_a_reviewed_base_needs_a_reviewed_commit(self) -> None:
        self.assertEqual(
            ("TrackerError", "Tracker item owner/repo#1.reviewed_base_sha requires reviewed_head_sha"),
            refusal([item() | {"reviewed_base_sha": "e" * 40, "reviewed_head_sha": None}]),
        )
        [result] = validate_items([item() | {"reviewed_base_sha": "e" * 40}])
        self.assertEqual("e" * 40, result["reviewed_base_sha"])

    def test_a_review_version_needs_a_reviewed_commit(self) -> None:
        self.assertEqual(
            ("TrackerError", "Tracker item owner/repo#1.reviewed_version requires reviewed_head_sha"),
            refusal([item() | {"reviewed_version": 1, "reviewed_head_sha": None}]),
        )
        [result] = validate_items([item() | {"reviewed_version": 1}])
        self.assertEqual(1, result["reviewed_version"])

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
            wrong(f"{SECOND}.title", TEXT, "an empty string"),
            refusal([item(number=4), item(number=5) | {"title": ""}, item(number=4)]),
        )

    def test_each_ai_review_field_of_the_wrong_kind_is_refused(self) -> None:
        severities = {"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 0}
        ledger = {"open": severities, "addressed": 0, "since": None, "version": 1}
        reading = {"flagged": 0, "since_review": {"version": 1, "new": 0, "addressed": 0}}
        valid = ai("APPROVED")
        cases: list[tuple[object, str, str, str]] = [
            ([], "", "an object or null", "an array"),
            ("Approved", "", "an object or null", 'string "Approved"'),
            ({"verdict": "APPROVED"}, "", "an object with fields counts, report", "an object without them"),
            (valid | {"extra": 1}, "", "an object of known fields", 'unknown field "extra"'),
            (valid | {"ledger": "1M"}, ".ledger", "an object or null", 'string "1M"'),
            (
                valid | {"ledger": {"open": severities, "addressed": 0, "since": None}},
                ".ledger",
                "an object with field version",
                "an object without it",
            ),
            (valid | {"ledger": ledger | {"open": []}}, ".ledger.open", "an object", "an array"),
            (
                valid | {"ledger": ledger | {"open": {"MUST_FIX": 0}}},
                ".ledger.open",
                "an object with fields SHOULD_FIX, SUGGESTION",
                "an object without them",
            ),
            (
                valid | {"ledger": ledger | {"open": severities | {"MUST_FIX": "1"}}},
                ".ledger.open.MUST_FIX",
                "an integer of at least 0",
                'string "1"',
            ),
            (
                valid | {"ledger": ledger | {"version": 0}},
                ".ledger.version",
                "an integer of at least 1 or null",
                "integer 0",
            ),
            (
                valid | {"ledger": ledger | {"version": 1.0}},
                ".ledger.version",
                "an integer of at least 1 or null",
                "number 1.0",
            ),
            (
                valid | {"ledger": ledger | {"addressed": True}},
                ".ledger.addressed",
                "an integer of at least 0 or null",
                "boolean true",
            ),
            (
                valid | {"ledger": ledger | {"since": "1"}},
                ".ledger.since",
                "an integer of at least 0 or null",
                'string "1"',
            ),
            (
                valid | {"ledger": ledger | {"repeats": "3"}},
                ".ledger.repeats",
                "an integer of at least 0",
                'string "3"',
            ),
            (valid | reading | {"flagged": True}, ".flagged", "an integer of at least 0", "boolean true"),
            (valid | reading | {"flagged": -1}, ".flagged", "an integer of at least 0", "integer -1"),
            (valid | reading | {"since_review": []}, ".since_review", "an object or null", "an array"),
            (
                valid | reading | {"since_review": {"version": 0, "new": 0}},
                ".since_review",
                "an object with field addressed",
                "an object without it",
            ),
            (
                valid | reading | {"since_review": {"version": 0, "new": 1.0, "addressed": 0}},
                ".since_review.new",
                "an integer of at least 0",
                "number 1.0",
            ),
            (valid | {"verdict": "OK"}, ".verdict", VERDICTS, 'string "OK"'),
            (valid | {"verdict": None}, ".verdict", VERDICTS, "null"),
            (valid | {"counts": []}, ".counts", "an object or null", "an array"),
            (
                valid | {"counts": {"MUST_FIX": 0}},
                ".counts",
                "an object with fields SHOULD_FIX, SUGGESTION",
                "an object without them",
            ),
            (
                valid | {"counts": severities | {"SUGGESTION": -1}},
                ".counts.SUGGESTION",
                "an integer of at least 0",
                "integer -1",
            ),
            (valid | {"report": ""}, ".report", TEXT_OR_NULL, "an empty string"),
            (valid | {"report": 5}, ".report", TEXT_OR_NULL, "integer 5"),
        ]
        validate_items([item() | {"ai_review": valid | reading}])
        for review, field, expected, found in cases:
            with self.subTest(field=field, review=review):
                self.assertEqual(wrong(f"{REVIEW}{field}", expected, found), refusal([item() | {"ai_review": review}]))

    def test_refusals_come_in_check_order(self) -> None:
        """An item wrong in every way is refused for one fault at a time, each repaired before the next is reported."""
        stages: list[tuple[str, object, object]] = [
            ("author_name", "", None),
            ("review_decision", "MERGED", None),
            ("user_review_decision", "PENDING", None),
            ("ai_review", [], None),
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
            ("draft", None, True),
            ("reviewed_head_sha", "", AI_REVIEWED),
            ("reviewed_base_sha", "", None),
            ("user_review_sha", "", None),
            ("user_review_state", "PENDING", None),
        ]
        bad = item() | {field: value for field, value, _ in stages}
        seen: list[str] = []
        for field, _, repaired in stages:
            seen.append(refusal([bad])[1])
            bad[field] = repaired
        self.assertEqual(1, len(validate_items([bad])))
        first = "tracker input[0]"
        self.assertEqual(
            [
                f"{first}.author_name must be a non-empty string or null, found an empty string",
                f'{first}.review_decision must be one of "APPROVED", "CHANGES_REQUESTED", "REVIEW_REQUIRED" or null, '
                'found string "MERGED"',
                f'{first}.user_review_decision must be one of "APPROVED", "CHANGES_REQUESTED", "DISMISSED" or null, '
                'found string "PENDING"',
                f"{first}.ai_review must be an object or null, found an array",
                f'{first}.reviewed_incomplete must be a boolean, found string "yes"',
                "Invalid repository identity: 'Owner'",
                f"{first}.number must be an integer of at least 1, found integer -1",
                f"{first}.url must be a non-empty string, found an empty string",
                f"{first}.title must be a non-empty string, found null",
                f"{first}.author must be a non-empty string, found integer 1",
                f"{first}.base_ref must be a non-empty string, found an empty string",
                f"{first}.head_sha must be a non-empty string, found an empty string",
                f"{first}.updated_at must be a non-empty string, found an empty string",
                f'{first}.requested_reviewers must be an array, found string "x"',
                f"{first}.draft must be a boolean, found null",
                f"{first}.reviewed_head_sha must be a non-empty string or null, found an empty string",
                f"{first}.reviewed_base_sha must be a non-empty string or null, found an empty string",
                f"{first}.user_review_sha must be a non-empty string or null, found an empty string",
                f'{first}.user_review_state must be one of "APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED" '
                'or null, found string "PENDING"',
            ],
            seen,
        )


class OverrideAndPullKeyTests(unittest.TestCase):
    """normalize_overrides and normalize_pull_keys: each kind they read refused in the one shape, and the value checks
    after it in their own words."""

    def test_each_status_override_of_the_wrong_kind_is_refused(self) -> None:
        key = "a key of status overrides"
        status = "status overrides.owner/repo#1"
        cases: list[tuple[Any, tuple[str, str]]] = [
            ([], wrong("status overrides", "an object", "an array")),
            ("owner/repo#1=On hold", wrong("status overrides", "an object", 'string "owner/repo#1=On hold"')),
            ({1: "On hold"}, wrong(key, TEXT, "integer 1")),
            ({"": "On hold"}, wrong(key, TEXT, "an empty string")),
            ({" ": "On hold"}, wrong(key, TEXT, "a blank string")),
            ({"owner/repo#1": 5}, wrong(status, "a string", "integer 5")),
            ({"owner/repo#1": None}, wrong(status, "a string", "null")),
            ({"owner/repo#1": ["On hold"]}, wrong(status, "a string", "an array")),
            ({"owner/repo#1": {"status": "On hold"}}, wrong(status, "a string", "an object")),
            ({"owner/repo#1": "  "}, ("TrackerError", "Status override owner/repo#1 must be non-empty")),
            ({"owner/repo": "On hold"}, ("TrackerError", "Invalid override pull-request identity: 'owner/repo'")),
        ]
        self.assertEqual({}, normalize_overrides(None))
        self.assertEqual({"owner/repo#1": "On hold"}, normalize_overrides({"Owner/Repo#1": " On hold "}))
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, raised(partial(normalize_overrides, value)))

    def test_each_pull_key_of_the_wrong_kind_is_refused(self) -> None:
        identity = "a removal pull-request identity"
        cases: list[tuple[Any, tuple[str, str]]] = [
            (1, wrong(identity, TEXT, "integer 1")),
            (None, wrong(identity, TEXT, "null")),
            (["owner/repo#1"], wrong(identity, TEXT, "an array")),
            ("", wrong(identity, TEXT, "an empty string")),
            (" ", wrong(identity, TEXT, "a blank string")),
            ("owner/repo", ("TrackerError", "Invalid removal pull-request identity: 'owner/repo'")),
            ("owner/repo#0", ("TrackerError", "Invalid removal pull-request identity: 'owner/repo#0'")),
            # A number GitHub's cannot reach, past ten digits, short of and past the interpreter's digit limit.
            *(
                (
                    f"owner/repo#{digits}",
                    ("TrackerError", f"Invalid removal pull-request identity: 'owner/repo#{digits}'"),
                )
                for digits in ("1" * 11, "9" * 5000)
            ),
        ]
        self.assertEqual({"owner/repo#1"}, normalize_pull_keys(["Owner/Repo#1"], "removal"))
        self.assertEqual({"owner/repo#9999999999"}, normalize_pull_keys(["owner/repo#9999999999"], "removal"))
        # A name may start with a dot, as an organization's `.github` does, but a name made only of dots is none.
        self.assertEqual({"owner/.github#1"}, normalize_pull_keys(["Owner/.github#1"], "removal"))
        self.assertEqual({"owner/.github#1": "On hold"}, normalize_overrides({"Owner/.github#1": "On hold"}))
        for name in (".", "..", "...", "...."):
            with self.subTest(name=name):
                self.assertEqual(
                    ("TrackerError", f"Invalid removal pull-request identity: 'owner/{name}#1'"),
                    raised(partial(normalize_pull_keys, [f"owner/{name}#1"], "removal")),
                )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(expected, raised(partial(normalize_pull_keys, [value], "removal")))
        self.assertEqual(
            ("TrackerError", "Duplicate removal pull-request identity: owner/repo#1"),
            raised(partial(normalize_pull_keys, ["owner/repo#1", "OWNER/repo#1"], "removal")),
        )
