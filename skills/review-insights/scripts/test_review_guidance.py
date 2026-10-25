from __future__ import annotations

import errno
import hashlib
import json
import re
import shutil
import sys
import tempfile
import unittest
from collections import Counter
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import review_guidance as rg
import review_synthesis as rs
from git_client import GitClient
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
# The conventions as a later reviewer was given them: the async rule lowered to SUGGESTION.
CONVENTIONS_LOWERED = CONVENTIONS.replace("CancellationToken as MUST_FIX", "CancellationToken as SUGGESTION", 1)


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


class RuleTests(unittest.TestCase):
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


def generated(tag: str, planted: str) -> str:
    """A guidance file of list items near READ_BYTES, each rule's words its own but two every rule shares, and one
    planted rule."""
    lines = [f"- {planted}"]
    while sum(map(len, lines)) < rg.READ_BYTES - 200:
        index = len(lines)
        lines.append(f"- Reviewers check {' '.join(f'{tag}{index}{letter}' for letter in 'abcde')} handling.")
    return "\n".join(lines) + "\n"


class OverlapBoundTests(unittest.TestCase):
    """The overlap search stays bounded however large the guidance files it reads."""

    def counted(self, limit: int) -> mock.Mock:
        """A stand-in for `compare` that fails once it has been called more than `limit` times."""
        real = rg.compare

        def compare(first: rg.Rule, second: rg.Rule) -> dict[str, Any] | None:
            if stand_in.call_count > limit:
                raise AssertionError(f"compared more than {limit} rule pairs")
            return real(first, second)

        stand_in = mock.Mock(side_effect=compare)
        return stand_in

    def test_two_files_near_the_read_limit_compare_only_rules_that_share_their_rarer_words(self) -> None:
        texts = {
            ("docs/a.md", "1"): generated("a", "Reviewers must flag unchecked inputs in request handlers."),
            ("docs/b.md", "2"): generated("b", "Reviewers should flag unchecked inputs in request handlers."),
        }
        compare = self.counted(100)
        with mock.patch.object(rg, "compare", compare):
            found, stopped = rg.overlaps("owner/repo", texts, {})
        self.assertEqual(
            [("conflict", [1, 1], ["must", "should"])], [(i["relation"], i["lines"], i["differs"]) for i in found]
        )
        self.assertFalse(stopped)
        self.assertGreater(len(rg.rules("docs/a.md", texts["docs/a.md", "1"])), 10_000, "the files hold many rules")

    def test_files_of_near_identical_rules_compare_at_most_the_budget(self) -> None:
        rule = "- Reviewers flag unchecked inputs in request handlers.\n"
        texts = {("docs/a.md", "1"): rule * 300, ("docs/b.md", "2"): rule * 300}
        compare = self.counted(1_000)
        with mock.patch.object(rg, "CANDIDATE_BUDGET", 1_000), mock.patch.object(rg, "compare", compare):
            found, stopped = rg.overlaps("owner/repo", texts, {})
        self.assertLessEqual(compare.call_count, 1_000)
        self.assertTrue(found and {"duplicate"} == {item["relation"] for item in found})
        self.assertTrue(stopped, "pairs were left to compare")

    def test_a_search_that_visits_exactly_the_budget_has_not_stopped(self) -> None:
        # One rule in each file, visited once for each rare word they share. A budget of every visit leaves none
        # unvisited; one fewer stops with one left.
        texts = {
            ("docs/a.md", "1"): "- Reviewers flag unchecked inputs in request handlers.\n",
            ("docs/b.md", "2"): "- Reviewers flag unchecked inputs in request handlers.\n",
        }
        visits = len(
            list(rg._visits([(key, rg.rules(key[0], text)) for key, text in texts.items()], self.order(texts)))
        )
        for budget, expected in ((visits, False), (visits - 1, True)):
            with self.subTest(budget=budget), mock.patch.object(rg, "CANDIDATE_BUDGET", budget):
                self.assertEqual(expected, rg.overlaps("owner/repo", texts, {})[1])

    def order(self, texts: dict[tuple[str, str], str]) -> dict[str, tuple[int, str]]:
        """The rarest-first order overlaps gives the words of these texts' rules."""
        frequency = Counter(
            word for key, text in texts.items() for rule in rg.rules(key[0], text) for word in rule.words
        )
        return {word: (count, word) for word, count in frequency.items()}


class ReadTests(unittest.TestCase):
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

    def test_each_repository_s_candidates_and_stopped_search_are_kept_beside_the_others(self) -> None:
        # Two repositories with the same guidance: the second's search adds to the first's, never replaces it.
        both = {
            **self.records,
            **{Path(f"other-{path}"): {**record, "repository": "owner/other"} for path, record in self.records.items()},
        }
        checkouts = {"owner/other": self.checkout, "owner/repo": self.checkout}
        alone = rg.read_guidance(["owner/repo"], self.records, {"owner/repo": self.checkout})
        self.assertTrue(alone.overlaps)
        guidance = rg.read_guidance(["owner/other", "owner/repo"], both, checkouts)
        self.assertEqual(
            {"owner/other": len(alone.overlaps), "owner/repo": len(alone.overlaps)},
            dict(Counter(item["repository"] for item in guidance.overlaps)),
        )
        self.assertEqual([], guidance.stopped)
        with mock.patch.object(rg, "CANDIDATE_BUDGET", 1):
            stopped = rg.read_guidance(["owner/other", "owner/repo"], both, checkouts)
        self.assertEqual(["owner/other", "owner/repo"], stopped.stopped)

    def read_at(self, commit: str) -> rg.Guidance:
        """The guidance of reviewers whose records were built at `commit`."""
        self.records = {
            path: reviewed(
                record["review"]["adapter"]["name"],
                record["review"]["reviewed_at"],
                {name: self.files[name] for name in record["review"]["adapter"]["source_hashes"]},
                commit,
            )
            for path, record in self.records.items()
        }
        return self.read()

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
        self.assertNotEqual(digest(CHECKLIST), rg.read_working_copy(self.checkout, "docs/checklist.md").digest)
        content = rg.read_committed(self.checkout, commit, "docs/checklist.md", digest(CHECKLIST))
        self.assertEqual(CHECKLIST.encode("utf-8"), content)
        self.assertIsNone(rg.read_committed(self.checkout, None, "docs/checklist.md", digest(CHECKLIST)))
        self.assertIsNone(rg.read_committed(self.checkout, commit, "docs/checklist.md", "0" * 64))
        self.assertEqual("included", self.statuses(self.read_at(commit))["docs/checklist.md"][1])

    def test_a_version_git_cannot_read_is_named_not_taken_for_one_not_at_its_commit(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        commit = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        self.assertIsNone(rg.read_committed(self.checkout, "f" * 40, "docs/checklist.md", digest(CHECKLIST)))
        self.assertIsNone(rg.read_committed(self.checkout, commit, "docs/missing.md", digest(CHECKLIST)))
        (self.checkout / ".git" / "config").write_text("[core\nbogus\n", encoding="utf-8")
        with self.assertRaises(rg.GuidanceError) as context:
            rg.read_committed(self.checkout, commit, "docs/checklist.md", digest(CHECKLIST))
        self.assertTrue(
            str(context.exception).startswith(
                f"git could not read docs/checklist.md at {commit} in {self.checkout.as_posix()}: "
            ),
            context.exception,
        )
        self.assertIn("bad config line 1", str(context.exception))

    def test_a_committed_copy_of_exactly_the_read_bound_is_read_and_one_byte_more_is_not(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        commit = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        size = len(CHECKLIST.encode("utf-8"))
        with mock.patch.object(rg, "READ_BYTES", size):
            self.assertEqual(
                CHECKLIST.encode("utf-8"),
                rg.read_committed(self.checkout, commit, "docs/checklist.md", digest(CHECKLIST)),
            )
        with mock.patch.object(rg, "READ_BYTES", size - 1):
            self.assertIsNone(rg.read_committed(self.checkout, commit, "docs/checklist.md", digest(CHECKLIST)))

    def test_a_path_that_leaves_the_checkout_is_never_read(self) -> None:
        outside = self.checkout.parent / "outside.md"
        outside.write_text(CHECKLIST, encoding="utf-8", newline="\n")
        for path in ("../outside.md", str(outside).replace("\\", "/")):
            with self.subTest(path):
                self.assertEqual(rg.WorkingCopy(None), rg.read_working_copy(self.checkout, path))
                self.assertIsNone(rg.read_committed(self.checkout, "f" * 40, path, digest(CHECKLIST)))

    def test_a_working_copy_of_exactly_the_read_bound_is_read_and_one_byte_more_is_not(self) -> None:
        path = self.checkout / "docs" / "checklist.md"
        with mock.patch.object(rg, "READ_BYTES", path.stat().st_size):
            self.assertEqual(digest(CHECKLIST), rg.read_working_copy(self.checkout, "docs/checklist.md").digest)
        with mock.patch.object(rg, "READ_BYTES", path.stat().st_size - 1):
            self.assertIsNone(rg.read_working_copy(self.checkout, "docs/checklist.md").digest)

    def test_a_path_reached_through_a_link_is_never_read_even_when_the_link_stays_inside(self) -> None:
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside)
        (outside / "checklist.md").write_text(CHECKLIST, encoding="utf-8", newline="\n")
        for name, target in (("leaving", outside), ("staying", self.checkout / "docs")):
            if sys.platform == "win32":  # a junction needs no privilege
                import _winapi

                _winapi.CreateJunction(str(target), str(self.checkout / name))
            else:
                (self.checkout / name).symlink_to(target, target_is_directory=True)
            with self.subTest(name):
                self.assertEqual(rg.WorkingCopy(None), rg.read_working_copy(self.checkout, f"{name}/checklist.md"))
        self.assertEqual(digest(CHECKLIST), rg.read_working_copy(self.checkout, "docs/checklist.md").digest)

    def test_an_unreadable_working_copy_falls_back_to_the_commit_and_says_why(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        commit = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        with unreadable("checklist.md"):
            copy = rg.read_working_copy(self.checkout, "docs/checklist.md")
            guidance = self.read_at(commit)
        self.assertEqual(rg.WorkingCopy(None, None, "PermissionError: Permission denied"), copy)
        self.assertEqual("included", self.statuses(guidance)["docs/checklist.md"][1])
        self.assertEqual(
            ["GUIDANCE_UNREADABLE owner/repo docs/checklist.md fallback=commit PermissionError: Permission denied"],
            guidance.unreadable,
        )

    def test_any_os_error_while_checking_the_working_copy_is_named_not_raised(self) -> None:
        cases: dict[str, tuple[dict[str, OSError | bool], str]] = {
            "is_file": (
                {"is_file": OSError(errno.EIO, "Input/output error")},
                "OSError: Input/output error",
            ),
            "stat": (
                {"is_file": True, "stat": FileNotFoundError(errno.ENOENT, "No such file or directory")},
                "FileNotFoundError: No such file or directory",
            ),
        }
        for name, (failures, reason) in cases.items():
            with self.subTest(name), failing("checklist.md", failures):
                self.assertEqual(
                    rg.WorkingCopy(None, None, reason), rg.read_working_copy(self.checkout, "docs/checklist.md")
                )
                self.assertEqual(
                    [f"GUIDANCE_UNREADABLE owner/repo docs/checklist.md fallback=none {reason}"],
                    self.read().unreadable,
                )

    def test_a_version_is_read_at_an_older_pinning_commit_when_the_newest_lacks_its_bytes(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        older = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        (self.checkout / "docs" / "checklist.md").write_text("Rewritten since.\n", encoding="utf-8")
        git.output(["commit", "--quiet", "-am", "Rewrite"], directory=self.checkout)
        newer = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        self.records = {
            Path("a.json"): reviewed("security", "2026-01-02T00:00:00+00:00", {"docs/checklist.md": CHECKLIST}, older),
            Path("b.json"): reviewed("style", "2026-01-03T00:00:00+00:00", {"docs/checklist.md": CHECKLIST}, newer),
        }
        guidance = self.read()
        self.assertEqual(
            [(["security", "style"], "included", CHECKLIST)],
            [(line["reviewers"], line["status"], line["content"]) for line in guidance.lines],
        )

    def test_a_reference_in_one_version_only_is_unsupplied_to_the_reviewer_given_that_version(self) -> None:
        linked = CONVENTIONS + "\nRead [the glossary](glossary.md) before the rules.\n"
        self.records = {
            Path("a.json"): reviewed(
                "security",
                "2026-01-02T00:00:00+00:00",
                {"docs/conventions.md": linked, "docs/checklist.md": CHECKLIST},
            ),
            Path("b.json"): reviewed(
                "style",
                "2026-01-03T00:00:00+00:00",
                {"docs/conventions.md": CONVENTIONS, "docs/checklist.md": CHECKLIST},
            ),
        }
        texts = {digest(text): text.encode("utf-8") for text in (linked, CONVENTIONS, CHECKLIST)}
        guidance = rg.read_guidance(
            ["owner/repo"],
            self.records,
            {"owner/repo": self.checkout},
            rg.Reader(
                working=lambda _checkout, _path: rg.WorkingCopy(None),
                committed=lambda _checkout, _commit, _path, pinned: texts.get(pinned),
            ),
        )
        self.assertEqual(
            [
                ("security", "docs/checklist.md", 11, "docs/severity.md"),
                ("security", "docs/conventions.md", 7, "style/guide.md"),
                ("security", "docs/conventions.md", 13, "docs/glossary.md"),
                ("style", "docs/checklist.md", 11, "docs/severity.md"),
                ("style", "docs/conventions.md", 7, "style/guide.md"),
            ],
            [(item["reviewer"], item["path"], item["line"], item["reference"]) for item in guidance.unsupplied],
        )

    def test_a_report_names_an_unreadable_file_and_reads_the_rest(self) -> None:
        with unreadable("checklist.md"):
            guidance = self.read()
        self.assertEqual(
            {
                "docs/checklist.md": ("prose", "unavailable", False),
                "docs/conventions.md": ("prose", "included", True),
                "scripts/check.py": ("code", "listed", False),
            },
            self.statuses(guidance),
        )
        self.assertEqual(
            ["GUIDANCE_UNREADABLE owner/repo docs/checklist.md fallback=none PermissionError: Permission denied"],
            guidance.unreadable,
        )
        self.assertEqual([], self.read().unreadable)

    def test_the_working_copy_is_read_once_however_many_commits_and_versions_pinned_it(self) -> None:
        self.records = {
            Path("a.json"): reviewed(
                "security", "2026-01-02T00:00:00+00:00", {"docs/checklist.md": CHECKLIST}, "1" * 40
            ),
            Path("b.json"): reviewed("style", "2026-01-03T00:00:00+00:00", {"docs/checklist.md": CHECKLIST}, "2" * 40),
            Path("c.json"): reviewed("docs", "2026-01-04T00:00:00+00:00", {"docs/checklist.md": CONVENTIONS}, "3" * 40),
        }
        GitClient().output(["init", "--quiet"], directory=self.checkout)
        (self.checkout / "docs" / "checklist.md").write_text("Rewritten since.\n", encoding="utf-8")
        with mock.patch.object(rg, "_working_copy", side_effect=rg._working_copy) as read:
            guidance = self.read()
        self.assertEqual(1, read.call_count)
        self.assertEqual(["unavailable", "unavailable"], [line["status"] for line in guidance.lines])

    def test_a_file_unreadable_at_every_version_is_named_once(self) -> None:
        self.two_versions()
        with unreadable("conventions.md"):
            guidance = self.read()
        self.assertEqual(
            ["GUIDANCE_UNREADABLE owner/repo docs/conventions.md fallback=none PermissionError: Permission denied"],
            guidance.unreadable,
        )

    def test_a_file_found_at_a_commit_for_some_versions_only_says_so_once(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        commit = git.output(["rev-parse", "HEAD"], directory=self.checkout).strip()
        self.records = {
            Path("a.json"): reviewed(
                "security", "2026-01-02T00:00:00+00:00", {"docs/conventions.md": CONVENTIONS}, commit
            ),
            Path("b.json"): reviewed(
                "style", "2026-01-03T00:00:00+00:00", {"docs/conventions.md": CONVENTIONS_LOWERED}, commit
            ),
        }
        with unreadable("conventions.md"):
            guidance = self.read()
        self.assertEqual(
            ["GUIDANCE_UNREADABLE owner/repo docs/conventions.md fallback=partial PermissionError: Permission denied"],
            guidance.unreadable,
        )
        self.assertEqual(
            {("security",): "included", ("style",): "unavailable"},
            {tuple(line["reviewers"]): line["status"] for line in guidance.lines},
        )

    def two_versions(self) -> None:
        """Two reviewers given the same checklist and different versions of the conventions."""
        self.records = {
            Path("a.json"): reviewed(
                "security",
                "2026-01-02T00:00:00+00:00",
                {"docs/conventions.md": CONVENTIONS, "docs/checklist.md": CHECKLIST},
            ),
            Path("b.json"): reviewed(
                "style",
                "2026-01-03T00:00:00+00:00",
                {"docs/conventions.md": CONVENTIONS_LOWERED, "docs/checklist.md": CHECKLIST},
            ),
        }

    def test_reviewers_given_different_versions_of_a_file_each_get_their_own(self) -> None:
        self.two_versions()
        texts = {digest(text): text.encode("utf-8") for text in (CONVENTIONS, CONVENTIONS_LOWERED, CHECKLIST)}
        guidance = rg.read_guidance(
            ["owner/repo"],
            self.records,
            {"owner/repo": self.checkout},
            rg.Reader(
                working=lambda _checkout, _path: rg.WorkingCopy(None),
                committed=lambda _checkout, _commit, _path, pinned: texts.get(pinned),
            ),
        )
        conventions = sorted(
            [
                ("docs/conventions.md", ["security"], CONVENTIONS),
                ("docs/conventions.md", ["style"], CONVENTIONS_LOWERED),
            ],
            key=lambda item: digest(item[2]),
        )
        self.assertEqual(
            [("docs/checklist.md", ["security", "style"], CHECKLIST), *conventions],
            [(line["path"], line["reviewers"], line["content"]) for line in guidance.lines],
        )
        self.assertEqual(
            [
                ("conflict", [7, 3], ["security"]),
                ("conflict", [10, 6], ["security", "style"]),
                ("duplicate", [7, 3], ["style"]),
                ("duplicate", [9, 5], ["security", "style"]),
            ],
            [(item["relation"], item["lines"], item["reviewers"]) for item in guidance.overlaps],
        )

    def test_a_version_whose_bytes_are_gone_is_unavailable_and_lends_no_reviewer_the_other(self) -> None:
        self.two_versions()
        (self.checkout / "docs" / "conventions.md").write_text(CONVENTIONS_LOWERED, encoding="utf-8", newline="\n")
        guidance = self.read()
        conventions = sorted(
            [(["security"], "unavailable", None, CONVENTIONS), (["style"], "included", CONVENTIONS_LOWERED, None)],
            key=lambda item: digest(item[3] or CONVENTIONS_LOWERED),
        )
        self.assertEqual(
            [item[:3] for item in conventions],
            [
                (line["reviewers"], line["status"], line["content"])
                for line in guidance.lines
                if line["path"] == "docs/conventions.md"
            ],
        )
        self.assertEqual(
            {("style",)}, {tuple(item["reviewers"]) for item in guidance.overlaps}, "security never read this text"
        )


@contextmanager
def unreadable(name: str) -> Iterator[None]:
    """Reading any file named `name` fails as a file locked against reading does."""
    real = Path.read_bytes

    def read_bytes(path: Path) -> bytes:
        if path.name == name:
            raise PermissionError(13, "Permission denied", str(path))
        return real(path)

    with mock.patch.object(Path, "read_bytes", read_bytes):
        yield


@contextmanager
def failing(name: str, failures: dict[str, OSError | bool]) -> Iterator[None]:
    """Each `Path` method named in `failures`, called on a file named `name`, raises its error or returns its value."""
    with ExitStack() as stack:
        for method, outcome in failures.items():
            real = getattr(Path, method)

            def replaced(path: Path, *args: Any, _real: Any = real, _outcome: Any = outcome, **kwargs: Any) -> Any:
                if path.name != name:
                    return _real(path, *args, **kwargs)
                if isinstance(_outcome, OSError):
                    raise _outcome
                return _outcome

            stack.enter_context(mock.patch.object(Path, method, replaced))
        yield


class ReportTests(InsightFixture):
    """The report reads each repository's configured checkout and gives the synthesis its guidance."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        self.files = {"docs/conventions.md": CONVENTIONS, "docs/checklist.md": CHECKLIST}
        for path, text in self.files.items():
            (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
            (self.checkout / path).write_text(text, encoding="utf-8", newline="\n")
        config = load_config(self.config_path)
        config["repositories"]["owner/repo"]["checkout_path"] = str(self.checkout)
        write_config(config, self.config_path)

    def reviewed_at(self, commit: str | None) -> None:
        """Commit a review by a repository-scoped reviewer built from the guidance files at `commit`."""
        adapter = {
            "name": "repository-reviewer",
            "scope": "repository",
            "source_commit": commit,
            "source_hashes": {path: digest(text) for path, text in self.files.items()},
        }
        self.commit("owner/repo", 7, ["Correctness"], adapter=adapter)

    def test_the_input_carries_the_text_and_the_candidates_and_the_report_prints_them(self) -> None:
        self.reviewed_at(None)
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

    def test_a_search_that_stops_at_its_budget_is_named_in_a_fact_line_and_the_input(self) -> None:
        # The skill names the line; with a budget of one visit the search stops with pairs left, and without the line
        # a report with no candidate past it would look like one whose guidance agrees.
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        [named] = re.findall(r"`(GUIDANCE_SEARCH_STOPPED) <repo> pairs=<n>`", skill)
        self.reviewed_at(None)
        with mock.patch.object(rg, "CANDIDATE_BUDGET", 1):
            code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        self.assertEqual(f"{named} owner/repo pairs=1", lines[2])
        directory = Path(lines[0].removeprefix("REPORT ")).parent
        items = [json.loads(line) for line in (directory / rs.INPUT_NAME).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [{"kind": "guidance-search-stopped", "repository": "owner/repo", "pairs_compared": 1}],
            [item for item in items if item["kind"] == "guidance-search-stopped"],
        )
        code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertNotIn(named, out, "a search that compared every pair names none")

    def test_an_unreadable_guidance_file_is_named_and_the_report_still_written(self) -> None:
        self.reviewed_at(None)
        with unreadable("checklist.md"):
            code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        self.assertIn(
            "GUIDANCE_UNREADABLE owner/repo docs/checklist.md fallback=none PermissionError: Permission denied", lines
        )
        directory = Path(lines[0].removeprefix("REPORT ")).parent
        items = [json.loads(line) for line in (directory / rs.INPUT_NAME).read_text(encoding="utf-8").splitlines()]
        files = {item["path"]: item for item in items if item["kind"] == "guidance-file"}
        self.assertEqual(
            [("unavailable", None), ("included", CONVENTIONS)],
            [(files[path]["status"], files[path]["content"]) for path in ("docs/checklist.md", "docs/conventions.md")],
        )

    def test_an_unreadable_file_found_at_its_commit_is_included_and_named_before_the_signals(self) -> None:
        git = GitClient()
        git.output(["init", "--quiet"], directory=self.checkout)
        git.output(["add", "."], directory=self.checkout)
        git.output(["commit", "--quiet", "-m", "Guidance"], directory=self.checkout)
        self.reviewed_at(git.output(["rev-parse", "HEAD"], directory=self.checkout).strip())
        with unreadable("checklist.md"):
            code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31")
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        self.assertEqual(
            [
                "MARKDOWN",
                "GUIDANCE_UNREADABLE owner/repo docs/checklist.md fallback=commit PermissionError: Permission denied",
                "GUIDANCE_OVERLAP",
            ],
            [lines[1].split()[0], lines[2], lines[3].split()[0]],
        )
        directory = Path(lines[0].removeprefix("REPORT ")).parent
        items = [json.loads(line) for line in (directory / rs.INPUT_NAME).read_text(encoding="utf-8").splitlines()]
        files = {item["path"]: item for item in items if item["kind"] == "guidance-file"}
        self.assertEqual(
            ("included", CHECKLIST), (files["docs/checklist.md"]["status"], files["docs/checklist.md"]["content"])
        )
