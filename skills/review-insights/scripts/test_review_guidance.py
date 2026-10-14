from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))

import review_guidance as rg
import review_synthesis as rs
from git_client import GitClient
from git_test_base import GitTestCase
from review_config import load_config, write_config
from test_review_insights import InsightFixture

CONVENTIONS = """# Conventions

Flag every public async method that takes no CancellationToken as MUST_FIX.

- Each public API change needs a changelog entry under the Unreleased heading.
- Request changes when 3 or more SHOULD_FIX findings remain open on the pull request.
- See [the checklist](checklist.md) and [the style guide](../style/guide.md).

```csharp
// Flag every public async method that takes no CancellationToken as SUGGESTION.
```
"""
CHECKLIST = """# Checklist

| Rule | Severity |
| --- | --- |
| Flag every public async method that takes no CancellationToken | SUGGESTION |

1. Flag every public async method that takes no CancellationToken as a
   SUGGESTION.
2. Each public API change needs a changelog entry under the Unreleased heading.
3. Request changes when 5 or more SHOULD_FIX findings remain open on the pull request.
4. Prefer `docs/severity.md` for the severity model.
"""
CODE = "print('check')\n"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def reviewed(name: str, stamp: str, files: dict[str, str], commit: str | None = None) -> dict[str, Any]:
    """The parts of a record the guidance reader uses: a repository-scoped reviewer built from `files`."""
    adapter = {
        "name": name,
        "scope": "repository",
        "source_commit": commit,
        "source_hashes": {path: digest(text) for path, text in files.items()},
    }
    return {"repository": "owner/repo", "review": {"reviewed_at": stamp, "adapter": adapter}}


class RuleTests(GitTestCase):
    def test_rules_come_from_list_items_and_paragraphs_never_headings_tables_or_code(self) -> None:
        found = [(rule.line, rule.text) for rule in rg.rules("checklist.md", CHECKLIST)]
        self.assertEqual(
            [
                (7, "Flag every public async method that takes no CancellationToken as a SUGGESTION."),
                (9, "Each public API change needs a changelog entry under the Unreleased heading."),
                (10, "Request changes when 5 or more SHOULD_FIX findings remain open on the pull request."),
                (11, "Prefer `docs/severity.md` for the severity model."),
            ],
            found,
        )
        conventions = [rule.text for rule in rg.rules("conventions.md", CONVENTIONS)]
        self.assertFalse(any("SUGGESTION" in text for text in conventions), "a fenced line is no rule")

    def test_a_planted_contradiction_is_a_conflict_naming_what_differs(self) -> None:
        first, second = rg.rules("a.md", CONVENTIONS)[0], rg.rules("b.md", CHECKLIST)[0]
        candidate = rg.compare(first, second) or {}
        self.assertEqual(
            ("conflict", ["must_fix", "suggestion"], ["a.md", "b.md"], [3, 7]),
            (candidate.get("relation"), candidate.get("differs"), candidate.get("paths"), candidate.get("lines")),
        )

    def test_a_planted_duplicate_is_a_duplicate_and_a_changed_number_a_conflict(self) -> None:
        conventions, checklist = rg.rules("a.md", CONVENTIONS), rg.rules("b.md", CHECKLIST)
        duplicate = rg.compare(conventions[1], checklist[1])
        threshold = rg.compare(conventions[2], checklist[2])
        self.assertEqual(
            [("duplicate", 1.0, []), ("conflict", 1.0, ["3", "5"])],
            [(item["relation"], item["similarity"], item["differs"]) for item in (duplicate or {}, threshold or {})],
        )
        self.assertIsNone(rg.compare(conventions[0], checklist[1]), "unrelated rules make no candidate")


class ReadTests(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout with spaces"
        (self.checkout / "docs").mkdir(parents=True)
        self.files = {"docs/conventions.md": CONVENTIONS, "docs/checklist.md": CHECKLIST, "scripts/check.py": CODE}
        for path, text in self.files.items():
            (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / path).write_text(text, encoding="utf-8", newline="\n")
        self.records = {
            Path("a.json"): reviewed("security", "2026-01-02T00:00:00+00:00", self.files),
            Path("b.json"): reviewed("style", "2026-01-03T00:00:00+00:00", {"docs/conventions.md": CONVENTIONS}),
        }

    def read(self, checkouts: dict[str, Path] | None = None) -> rg.Guidance:
        return rg.read_guidance(
            ["owner/repo"], self.records, {"owner/repo": self.checkout} if checkouts is None else checkouts
        )

    def statuses(self, guidance: rg.Guidance) -> dict[str, tuple[str, str, bool]]:
        return {line["path"]: (line["role"], line["status"], line["content"] is not None) for line in guidance.lines}

    def test_prose_whose_hash_matches_is_included_and_code_is_listed_by_path(self) -> None:
        guidance = self.read()
        self.assertEqual(
            {
                "docs/checklist.md": ("prose", "included", True),
                "docs/conventions.md": ("prose", "included", True),
                "scripts/check.py": ("code", "listed", False),
            },
            self.statuses(guidance),
        )
        lines = {line["path"]: line for line in guidance.lines}
        self.assertEqual(["security", "style"], lines["docs/conventions.md"]["reviewers"])
        self.assertEqual(CONVENTIONS, lines["docs/conventions.md"]["content"])
        self.assertEqual({"owner/repo": sorted(self.files)}, guidance.files)

    def test_a_changed_file_or_a_missing_checkout_gives_no_text(self) -> None:
        (self.checkout / "docs" / "checklist.md").write_text(CHECKLIST + "\nA new rule.\n", encoding="utf-8")
        self.assertEqual(("prose", "unavailable", False), self.statuses(self.read())["docs/checklist.md"])
        nothing = self.read({})
        self.assertEqual({"unavailable", "listed"}, {status for _, status, _ in self.statuses(nothing).values()})
        self.assertEqual(([], []), (nothing.overlaps, nothing.unsupplied))

    def test_the_size_caps_list_a_file_by_path_but_still_compare_its_rules(self) -> None:
        with mock.patch.object(rg, "FILE_CHARS", len(CHECKLIST) - 1):
            self.assertEqual(("prose", "too-large", False), self.statuses(self.read())["docs/checklist.md"])
        with mock.patch.object(rg, "TOTAL_CHARS", len(CHECKLIST) + 1):
            guidance = self.read()
        self.assertEqual(
            [("prose", "included", True), ("prose", "over-budget", False)],
            [self.statuses(guidance)[path] for path in ("docs/checklist.md", "docs/conventions.md")],
        )
        self.assertEqual(3, len(guidance.overlaps), "a file past the budget is still compared")

    def test_overlaps_rank_conflicts_first_and_name_the_reviewers_given_both_files(self) -> None:
        guidance = self.read()
        self.assertEqual(
            [
                ("conflict", ["docs/checklist.md", "docs/conventions.md"], [7, 3], ["security"]),
                ("conflict", ["docs/checklist.md", "docs/conventions.md"], [10, 6], ["security"]),
                ("duplicate", ["docs/checklist.md", "docs/conventions.md"], [9, 5], ["security"]),
            ],
            [(item["relation"], item["paths"], item["lines"], item["reviewers"]) for item in guidance.overlaps],
        )

    def test_a_markdown_file_a_reviewer_is_not_given_is_unsupplied(self) -> None:
        self.assertEqual(
            [
                ("security", "docs/checklist.md", 11, "docs/severity.md"),
                ("security", "docs/conventions.md", 7, "style/guide.md"),
                ("style", "docs/conventions.md", 7, "docs/checklist.md"),
                ("style", "docs/conventions.md", 7, "style/guide.md"),
            ],
            [(item["reviewer"], item["path"], item["line"], item["reference"]) for item in self.read().unsupplied],
        )

    def test_a_file_changed_since_the_review_is_read_at_the_reviewer_commit(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        commit = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        path = self.checkout / "docs" / "checklist.md"
        path.write_text("Rewritten since.\n", encoding="utf-8")
        content = rg.read_pinned(self.checkout, commit, "docs/checklist.md", digest(CHECKLIST))
        self.assertEqual(CHECKLIST.encode("utf-8"), content)
        self.assertIsNone(rg.read_pinned(self.checkout, None, "docs/checklist.md", digest(CHECKLIST)))
        self.assertIsNone(rg.read_pinned(self.checkout, commit, "docs/checklist.md", "0" * 64))

    def test_a_path_that_leaves_the_checkout_is_never_read(self) -> None:
        outside = self.checkout.parent / "outside.md"
        outside.write_text(CHECKLIST, encoding="utf-8", newline="\n")
        for path in ("../outside.md", str(outside).replace("\\", "/")):
            with self.subTest(path):
                self.assertIsNone(rg.read_pinned(self.checkout, None, path, digest(CHECKLIST)))


class ReportTests(InsightFixture):
    """The report reads each repository's configured checkout and gives the synthesis its guidance."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        checkout = Path(temporary.name) / "checkout"
        files = {"docs/conventions.md": CONVENTIONS, "docs/checklist.md": CHECKLIST}
        for path, text in files.items():
            (checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (checkout / path).write_text(text, encoding="utf-8", newline="\n")
        config = load_config(self.config_path)
        config["repositories"]["owner/repo"]["checkout_path"] = str(checkout)
        write_config(config, self.config_path)
        adapter = {
            "name": "repository-reviewer",
            "scope": "repository",
            "source_commit": None,
            "source_hashes": {path: digest(text) for path, text in files.items()},
        }
        self.commit("owner/repo", 7, ["Correctness"], adapter=adapter)

    def test_the_input_carries_the_text_and_the_candidates_and_the_report_prints_them(self) -> None:
        code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        self.assertIn(
            "GUIDANCE_OVERLAP O1 relation=conflict similarity=1.0 repository=owner/repo docs/checklist.md:7 "
            "docs/conventions.md:3",
            lines,
        )
        self.assertIn(
            "GUIDANCE_UNSUPPLIED U1 repository=owner/repo reviewer=repository-reviewer docs/checklist.md:11 "
            "docs/severity.md",
            lines,
        )
        directory = Path(lines[0].removeprefix("REPORT ")).parent
        items = [json.loads(line) for line in (directory / rs.INPUT_NAME).read_text(encoding="utf-8").splitlines()]
        files = {item["path"]: item for item in items if item["kind"] == "guidance-file"}
        self.assertEqual(CHECKLIST, files["docs/checklist.md"]["content"])
        overlaps = [item for item in items if item["kind"] == "guidance-overlap"]
        self.assertEqual(["O1", "O2", "O3"], [item["id"] for item in overlaps])
        markdown = (directory / "insights.md").read_text(encoding="utf-8")
        self.assertIn(
            "### Guidance overlap candidates\n\n- O1 conflict (1.0) owner/repo: docs/checklist.md:7", markdown
        )


if __name__ == "__main__":
    unittest.main()
