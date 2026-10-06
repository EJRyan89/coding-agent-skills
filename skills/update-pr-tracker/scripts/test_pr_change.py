from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIRECTORY))

from pr_change import (
    CHANGED,
    COMPARE_FILE_LIMIT,
    UNCHANGED,
    UNKNOWN,
    ChangeDetector,
    at_or_before,
    contribution_fingerprint,
    tree_modes,
)
from review_github import GitHubError

REVIEWED = "a" * 40
HEAD = "d" * 40


def changed(filename: str, content: str, status: str = "modified", **extra: str) -> dict:
    return {"filename": filename, "status": status, "sha": content, "patch": "@@ -1 +1 @@", **extra}


def comparison(*files: dict) -> dict:
    return {"status": "ahead", "files": list(files)}


def tree(comparison_value: object, modes: dict[str, str] | None = None, truncated: bool = False) -> dict:
    """A complete recursive tree containing every non-removed file of a comparison."""
    files = comparison_value.get("files", []) if isinstance(comparison_value, dict) else []
    paths = [f["filename"] for f in files if isinstance(f, dict) and f.get("status") != "removed" and "filename" in f]
    entries = [{"path": path, "mode": (modes or {}).get(path, "100644"), "type": "blob"} for path in paths]
    return {"truncated": truncated, "tree": [{"path": "unrelated.txt", "mode": "100644", "type": "blob"}, *entries]}


class FakeClient:
    def __init__(self, comparisons: dict[str, object], trees: dict[str, object] | None = None) -> None:
        self.comparisons = comparisons
        self.trees = trees or {sha: tree(value) for sha, value in comparisons.items()}
        self.calls: list[str] = []

    def api_json(self, endpoint: str) -> object:
        self.calls.append(endpoint)
        if "/git/trees/" in endpoint:
            value = self.trees[endpoint.split("/git/trees/")[1].split("?")[0]]
        else:
            value = self.comparisons[endpoint.rsplit("...", 1)[1]]
        if isinstance(value, GitHubError):
            raise value
        return value


def detect(before: object, after: object, trees: dict[str, object] | None = None) -> str:
    client = FakeClient({REVIEWED: before, HEAD: after}, trees)
    return ChangeDetector(client).detect("owner/repo", 7, "main", REVIEWED, HEAD)


class ChangeDetectorTests(unittest.TestCase):
    def test_same_head_is_unchanged_without_api_calls(self) -> None:
        client = FakeClient({})
        self.assertEqual(UNCHANGED, ChangeDetector(client).detect("owner/repo", 7, "main", HEAD, HEAD))
        self.assertEqual([], client.calls)

    def test_identical_resulting_files_are_unchanged_despite_different_patches(self) -> None:
        before = comparison(changed("src/app.py", "blob1"))
        after = comparison({**changed("src/app.py", "blob1"), "patch": "@@ -40 +40 @@ moved"})
        self.assertEqual(UNCHANGED, detect(before, after))

    def test_same_edit_in_a_different_method_is_changed(self) -> None:
        self.assertEqual(
            CHANGED, detect(comparison(changed("src/app.py", "blob1")), comparison(changed("src/app.py", "blob2")))
        )

    def test_mode_only_change_is_changed(self) -> None:
        files = comparison(changed("tool.sh", "blob1"))
        trees = {REVIEWED: tree(files, {"tool.sh": "100644"}), HEAD: tree(files, {"tool.sh": "100755"})}
        self.assertEqual(CHANGED, detect(files, files, trees))

    def test_symlink_or_submodule_type_change_is_changed(self) -> None:
        files = comparison(changed("link", "blob1"))
        for mode in ("120000", "160000"):
            with self.subTest(mode=mode):
                trees = {REVIEWED: tree(files), HEAD: tree(files, {"link": mode})}
                self.assertEqual(CHANGED, detect(files, files, trees))

    def test_merge_resolution_or_merged_feature_work_is_changed(self) -> None:
        before = comparison(changed("src/app.py", "blob1"))
        self.assertEqual(CHANGED, detect(before, comparison(changed("src/app.py", "resolved"))))
        self.assertEqual(
            CHANGED, detect(before, comparison(changed("src/app.py", "blob1"), changed("src/other.py", "x", "added")))
        )

    def test_renames_and_status_changes_are_changed(self) -> None:
        before = comparison(changed("src/new.py", "blob1", "renamed", previous_filename="src/old.py"))
        after = comparison(changed("src/new.py", "blob1", "added"))
        self.assertEqual(CHANGED, detect(before, after))

    def test_removed_files_need_no_tree_entry(self) -> None:
        files = comparison(changed("old.py", "blob1", "removed"))
        self.assertEqual(UNCHANGED, detect(files, files))

    def test_comparisons_at_the_file_limit_are_unknown(self) -> None:
        full = comparison(*(changed(f"f{n}.py", "blob") for n in range(COMPARE_FILE_LIMIT)))
        self.assertIsNone(contribution_fingerprint(full, tree_modes(tree(full))))
        self.assertEqual(UNKNOWN, detect(full, full))

    def test_truncated_or_incomplete_trees_are_unknown(self) -> None:
        files = comparison(changed("a.py", "b"))
        cases = {
            "truncated": {REVIEWED: tree(files), HEAD: tree(files, truncated=True)},
            "missing entry": {REVIEWED: tree(files), HEAD: {"truncated": False, "tree": []}},
            "malformed": {REVIEWED: tree(files), HEAD: {"tree": "x"}},
            "unavailable": {REVIEWED: tree(files), HEAD: GitHubError("gone", kind="not_found")},
        }
        for name, trees in cases.items():
            with self.subTest(case=name):
                self.assertEqual(UNKNOWN, detect(files, files, trees))

    def test_malformed_comparisons_are_unknown(self) -> None:
        for malformed in ({}, {"files": "x"}, comparison({"filename": "a.py", "status": "modified"}), comparison("x")):
            with self.subTest(malformed=malformed):
                self.assertEqual(UNKNOWN, detect(malformed, comparison(changed("a.py", "b"))))

    def test_empty_contributions_on_both_sides_are_unknown(self) -> None:
        self.assertEqual(UNKNOWN, detect(comparison(), comparison()))
        self.assertEqual(CHANGED, detect(comparison(changed("a.py", "b")), comparison()))

    def test_unavailable_evidence_is_unknown(self) -> None:
        after = comparison(changed("a.py", "b"))
        trees = {REVIEWED: tree(after), HEAD: tree(after)}
        self.assertEqual(UNKNOWN, detect(GitHubError("gone", kind="not_found"), after, trees))

    def test_run_level_failures_propagate(self) -> None:
        for kind in ("prerequisite", "authentication", "rate_limit"):
            with self.subTest(kind=kind):
                with self.assertRaises(GitHubError):
                    detect(
                        GitHubError("stop", kind=kind),
                        comparison(),
                        {REVIEWED: tree(comparison()), HEAD: tree(comparison())},
                    )

    def test_requests_are_url_quoted_and_cached(self) -> None:
        client = FakeClient({REVIEWED: comparison(changed("a.py", "1")), HEAD: comparison(changed("a.py", "2"))})
        detector = ChangeDetector(client)
        for _ in range(2):
            detector.detect("owner/repo", 7, "release/1.0 rc#", REVIEWED, HEAD)
        self.assertEqual(
            [
                f"repos/owner/repo/compare/release/1.0%20rc%23...{REVIEWED}",
                f"repos/owner/repo/git/trees/{REVIEWED}?recursive=1",
                f"repos/owner/repo/compare/release/1.0%20rc%23...{HEAD}",
                f"repos/owner/repo/git/trees/{HEAD}?recursive=1",
            ],
            client.calls,
        )


class AncestryTests(unittest.TestCase):
    class Client:
        def __init__(self, answer: object) -> None:
            self.answer = answer
            self.calls: list[str] = []

        def api_json(self, endpoint: str) -> object:
            self.calls.append(endpoint)
            if isinstance(self.answer, GitHubError):
                raise self.answer
            return self.answer

    def test_a_commit_is_at_or_before_another_when_the_other_is_identical_or_ahead(self) -> None:
        for answer, expected in (
            ({"status": "identical"}, True),
            ({"status": "ahead"}, True),
            ({"status": "behind"}, False),
            ({"status": "diverged"}, False),
            ({"status": "unexpected"}, None),
            ([], None),
            (GitHubError("HTTP 404: Not Found", kind="not_found"), None),
        ):
            client = self.Client(answer)
            with self.subTest(answer=answer):
                self.assertIs(expected, at_or_before(client, "owner/repo", REVIEWED, HEAD))
                self.assertEqual([f"repos/owner/repo/compare/{REVIEWED}...{HEAD}?per_page=1"], client.calls)

    def test_a_fatal_failure_stops_the_run(self) -> None:
        with self.assertRaises(GitHubError):
            at_or_before(self.Client(GitHubError("rate limit", kind="rate_limit")), "owner/repo", REVIEWED, HEAD)


if __name__ == "__main__":
    unittest.main()
