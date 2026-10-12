"""Fixture tests for tests/validation/pull_request_title.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pull_request_title
from pull_request_title import title_problems, title_types_problems


class PullRequestTitlePolicy(unittest.TestCase):
    def test_the_types_are_the_agreed_ones(self) -> None:
        self.assertEqual(
            ("feat", "fix", "docs", "test", "refactor", "chore", "ci", "build"), pull_request_title.TITLE_TYPES
        )

    def test_a_typed_title_passes_with_or_without_a_scope(self) -> None:
        for title in (
            "fix(deployer): --at refuses a filtered archive member by name",
            "fix(review-insights, repo-cleanup): a schema version is a non-boolean integer",
            "docs: name the targeted local run",
            "build: raise the ruff pin\n",
        ):
            with self.subTest(title=title):
                self.assertEqual([], title_problems(title, None))

    def test_a_title_without_a_type_fails(self) -> None:
        for title in (
            "Reuse shared flat_text in skill formatters",
            "fix:no space after the colon",
            "fix: ",
            "(deployer): a scope without a type",
            "fix(): an empty scope",
            "",
        ):
            with self.subTest(title=title):
                problems = title_problems(title, None)
                self.assertEqual(1, len(problems))
                self.assertIn("does not open with a type", problems[0])

    def test_an_unknown_type_fails_and_names_the_types(self) -> None:
        for title in ("style: sort the imports", "Fix: capitalized", "feature(deployer): spelled out"):
            with self.subTest(title=title):
                problems = title_problems(title, None)
                self.assertEqual(1, len(problems))
                self.assertIn("is not a known type", problems[0])
                self.assertIn("`feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, or `build`", problems[0])

    def test_a_breaking_change_mark_fails(self) -> None:
        for title in ("feat!: drop the old flag", "feat(deployer)!: drop the old flag"):
            with self.subTest(title=title):
                problems = title_problems(title, None)
                self.assertEqual(1, len(problems))
                self.assertIn("`!`", problems[0])
                self.assertIn("## Upgrade note", problems[0])

    def test_the_type_agrees_with_a_branch_kind_that_is_a_type(self) -> None:
        self.assertEqual([], title_problems("fix(deployer): refuse a bad name", "fix/bad-name"))
        problems = title_problems("feat(deployer): refuse a bad name", "fix/bad-name")
        self.assertEqual(1, len(problems))
        self.assertIn("branch fix/bad-name", problems[0])
        # A branch whose first segment names no type, or that has none, constrains nothing.
        for branch in ("inv/flaky-suite", "claude/conventions-1a2b3c", "main", "fix", ""):
            with self.subTest(branch=branch):
                self.assertEqual([], title_problems("docs: anything", branch))

    def test_a_guide_must_name_every_type(self) -> None:
        listed = "Types: `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `ci`, and `build`.\n"
        self.assertEqual([], title_types_problems("GUIDE.md", listed))
        problems = title_types_problems("GUIDE.md", listed.replace(", and `build`", ""))
        self.assertEqual(["GUIDE.md does not name the pull request title type `build`"], problems)


if __name__ == "__main__":
    unittest.main()
