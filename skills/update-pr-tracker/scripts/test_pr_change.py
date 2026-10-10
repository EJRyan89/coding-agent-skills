from __future__ import annotations

import json
import sys
import threading
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from github_client import CommandResult
from pr_change import (
    CHANGED,
    COMPARE_FILE_LIMIT,
    UNCHANGED,
    UNKNOWN,
    ChangeDetector,
    ComparisonError,
    at_or_before,
    changed_files,
    tree_modes,
    with_modes,
)
from review_github import GitHubClient, GitHubError

REVIEWED = "a" * 40
HEAD = "d" * 40


def changed(filename: str, content: str, status: str = "modified", **extra: str) -> dict:
    return {"filename": filename, "status": status, "sha": content, "patch": "@@ -1 +1 @@", **extra}


def comparison(*files: object) -> dict:
    """A comparison listing the files; a malformed one may list anything."""
    return {"status": "ahead", "files": list(files)}


def tree(comparison_value: object, modes: dict[str, str] | None = None, truncated: bool = False) -> dict:
    """A complete recursive tree containing every non-removed file of a comparison."""
    files = comparison_value.get("files", []) if isinstance(comparison_value, dict) else []
    paths = [f["filename"] for f in files if isinstance(f, dict) and f.get("status") != "removed" and "filename" in f]
    entries = [{"path": path, "mode": (modes or {}).get(path, "100644"), "type": "blob"} for path in paths]
    return {"truncated": truncated, "tree": [{"path": "unrelated.txt", "mode": "100644", "type": "blob"}, *entries]}


class FakeClient(GitHubClient):
    """Answers the detector's REST calls from canned comparisons and trees, without running gh."""

    def __init__(self, comparisons: Mapping[str, object], trees: Mapping[str, object] | None = None) -> None:
        self.comparisons = comparisons
        self.trees = trees or {sha: tree(value) for sha, value in comparisons.items()}
        self.calls: list[str] = []

    def api_json(self, endpoint: str, *, paginate: bool = False, allow_absent: bool = False) -> Any:
        self.calls.append(endpoint)
        if "/git/trees/" in endpoint:
            value = self.trees[endpoint.split("/git/trees/")[1].split("?")[0]]
        else:
            value = self.comparisons[endpoint.rsplit("...", 1)[1]]
        if isinstance(value, GitHubError):
            if allow_absent and value.kind == "not_found":
                return None
            raise value
        return value


# Failures that say nothing about one pull request's commits, so they stop the run.
RUN_LEVEL_KINDS = ("prerequisite", "execution", "authentication", "rate_limit", "network", "timeout")
# Failures of one call that are not a missing commit, so its pull request's comparison fails.
CALL_LEVEL_KINDS = ("forbidden", "sso_partial", "api", "malformed")


def detect(before: object, after: object, trees: Mapping[str, object] | None = None) -> str:
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
        self.assertIsNone(changed_files(full))
        self.assertEqual(UNKNOWN, detect(full, full))

    def test_truncated_or_incomplete_trees_are_unknown(self) -> None:
        files = comparison(changed("a.py", "b"))
        cases: dict[str, dict[str, object]] = {
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
        for kind in RUN_LEVEL_KINDS:
            with self.subTest(kind=kind), self.assertRaises(GitHubError) as raised:
                detect(
                    GitHubError("stop", kind=kind),
                    comparison(),
                    {REVIEWED: tree(comparison()), HEAD: tree(comparison())},
                )
            self.assertNotIsInstance(raised.exception, ComparisonError)

    def test_a_failed_call_fails_its_pull_requests_comparison_instead_of_being_unknown(self) -> None:
        files = comparison(changed("a.py", "b"))
        for kind in CALL_LEVEL_KINDS:
            for failed in ("comparison", "tree"):
                with self.subTest(kind=kind, failed=failed), self.assertRaises(ComparisonError) as raised:
                    error = GitHubError("HTTP 502: Bad Gateway", kind=kind)
                    if failed == "comparison":
                        detect(error, files)
                    else:
                        detect(files, files, {REVIEWED: tree(files), HEAD: error})
                self.assertEqual("owner/repo#7", raised.exception.pull)
                self.assertEqual("HTTP 502: Bad Gateway", raised.exception.reason)
                self.assertEqual("owner/repo#7 HTTP 502: Bad Gateway", str(raised.exception))

    def test_a_failed_read_ahead_is_reported_by_its_detection_alone_and_never_read_again(self) -> None:
        files = comparison(changed("a.py", "b"))
        other = "e" * 40
        client = FakeClient({REVIEWED: GitHubError("HTTP 403", kind="forbidden"), HEAD: files, other: files})
        detector = ChangeDetector(client)
        detector.prefetch([("owner/repo", "main", REVIEWED, HEAD), ("owner/repo", "main", other, HEAD)])
        calls = list(client.calls)
        self.assertEqual(UNCHANGED, detector.detect("owner/repo", 8, "main", other, HEAD))
        with self.assertRaisesRegex(ComparisonError, "^owner/repo#7 HTTP 403$"):
            detector.detect("owner/repo", 7, "main", REVIEWED, HEAD)
        self.assertEqual(calls, client.calls, "the failure is kept, not read again")

    def test_requests_are_url_quoted_and_cached(self) -> None:
        files = comparison(changed("a.py", "1"))
        client = FakeClient({REVIEWED: files, HEAD: files})
        detector = ChangeDetector(client)
        for _ in range(2):
            detector.detect("owner/repo", 7, "release/1.0 rc#", REVIEWED, HEAD)
        self.assertEqual(
            [
                f"repos/owner/repo/compare/release/1.0%20rc%23...{REVIEWED}",
                f"repos/owner/repo/compare/release/1.0%20rc%23...{HEAD}",
                f"repos/owner/repo/git/trees/{REVIEWED}?recursive=1",
                f"repos/owner/repo/git/trees/{HEAD}?recursive=1",
            ],
            client.calls,
        )


def full_detect(before: object, after: object, before_tree: object, after_tree: object) -> str:
    """The decision from both commits' complete fingerprints, comparison and tree alike, as detection made it before
    it learned to skip the trees."""
    fingerprints = [
        with_modes(changed_files(before), tree_modes(before_tree)),
        with_modes(changed_files(after), tree_modes(after_tree)),
    ]
    if None in fingerprints or not any(fingerprints):
        return UNKNOWN
    return UNCHANGED if fingerprints[0] == fingerprints[1] else CHANGED


class TreeShortcutTests(unittest.TestCase):
    """The trees are read only when the two comparisons list the same files, so the decision is the full one."""

    @staticmethod
    def trees_read(client: FakeClient) -> int:
        return sum("/git/trees/" in call for call in client.calls)

    def test_differing_files_decide_without_the_trees_and_match_the_full_fingerprints(self) -> None:
        base = changed("src/app.py", "blob1")
        cases = {
            "content": comparison(changed("src/app.py", "blob2")),
            "added file": comparison(base, changed("src/new.py", "x", "added")),
            "status": comparison(changed("src/app.py", "blob1", "added")),
            "rename": comparison(changed("src/app.py", "blob1", "renamed", previous_filename="src/old.py")),
            "removed": comparison(changed("src/app.py", "blob1", "removed")),
            "nothing left": comparison(),
        }
        before = comparison(base)
        for name, after in cases.items():
            for modes in ({}, {"src/app.py": "100755", "src/new.py": "120000"}):
                with self.subTest(case=name, modes=modes):
                    trees = {REVIEWED: tree(before), HEAD: tree(after, modes)}
                    client = FakeClient({REVIEWED: before, HEAD: after}, trees)
                    shortcut = ChangeDetector(client).detect("owner/repo", 7, "main", REVIEWED, HEAD)
                    self.assertEqual(full_detect(before, after, trees[REVIEWED], trees[HEAD]), shortcut)
                    self.assertEqual(CHANGED, shortcut)
                    self.assertEqual(0, self.trees_read(client), "differing files need no tree")

    def test_matching_files_read_both_trees_so_a_mode_change_is_still_seen(self) -> None:
        files = comparison(changed("tool.sh", "blob1"), changed("gone.py", "blob2", "removed"))
        for after_mode, expected in (("100644", UNCHANGED), ("100755", CHANGED), ("120000", CHANGED)):
            with self.subTest(mode=after_mode):
                trees = {REVIEWED: tree(files), HEAD: tree(files, {"tool.sh": after_mode})}
                client = FakeClient({REVIEWED: files, HEAD: files}, trees)
                result = ChangeDetector(client).detect("owner/repo", 7, "main", REVIEWED, HEAD)
                self.assertEqual(full_detect(files, files, trees[REVIEWED], trees[HEAD]), result)
                self.assertEqual(expected, result)
                self.assertEqual(2, self.trees_read(client))

    def test_files_that_were_all_removed_need_no_tree(self) -> None:
        files = comparison(changed("old.py", "blob1", "removed"))
        client = FakeClient({REVIEWED: files, HEAD: files})
        self.assertEqual(UNCHANGED, ChangeDetector(client).detect("owner/repo", 7, "main", REVIEWED, HEAD))
        self.assertEqual(0, self.trees_read(client))

    def test_differing_files_are_a_change_even_where_a_tree_was_unreadable(self) -> None:
        """The one decision the shortcut makes that the full fingerprints could not: the comparisons already prove
        the change, so a tree that is truncated or gone no longer makes it unknown."""
        before, after = comparison(changed("a.py", "1")), comparison(changed("a.py", "2"))
        trees = {REVIEWED: tree(before), HEAD: tree(after, truncated=True)}
        client = FakeClient({REVIEWED: before, HEAD: after}, trees)
        self.assertEqual(UNKNOWN, full_detect(before, after, trees[REVIEWED], trees[HEAD]))
        self.assertEqual(CHANGED, ChangeDetector(client).detect("owner/repo", 7, "main", REVIEWED, HEAD))
        self.assertEqual(0, self.trees_read(client))


def sha(number: int) -> str:
    return f"{number:040x}"


class ConcurrentRunner:
    """Answers gh api calls for commits sha(0), sha(1), and so on: each even commit changes a.py to its own blob,
    and each odd one matches the commit before it, so that pair needs its trees. A call waits until `parties` calls
    are running at once, so a detector that reads one call at a time fails on a broken barrier."""

    def __init__(self, parties: int, rate_limited: set[str] | None = None) -> None:
        self.barrier = threading.Barrier(parties, timeout=10)
        self.lock = threading.Lock()
        self.calls: list[str] = []
        self.running = 0
        self.peak = 0
        self.rate_limited = set(rate_limited or ())

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        endpoint = arguments[-1]
        with self.lock:
            self.calls.append(endpoint)
            self.running += 1
            self.peak = max(self.peak, self.running)
            limited = endpoint in self.rate_limited
            self.rate_limited.discard(endpoint)
        try:
            self.barrier.wait()
            if limited:
                return CommandResult(1, "", "HTTP 429: API rate limit exceeded")
            if "/git/trees/" in endpoint:
                return CommandResult(0, json.dumps(tree(comparison(changed("a.py", "blob")))), "")
            number = int(endpoint.rsplit("...", 1)[1], 16)
            return CommandResult(0, json.dumps(comparison(changed("a.py", f"blob{number - number % 2}"))), "")
        finally:
            with self.lock:
                self.running -= 1


# Four pairs whose files match, so each reads both comparisons and both trees.
QUERIES = [("owner/repo", "main", sha(2 * n), sha(2 * n + 1)) for n in range(4)]


class PrefetchTests(unittest.TestCase):
    def test_prefetch_reads_four_at_a_time_and_detect_needs_no_further_call(self) -> None:
        runner = ConcurrentRunner(4)
        detector = ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None))
        detector.prefetch([*QUERIES, *QUERIES, ("owner/repo", "main", sha(9), sha(9))])
        self.assertEqual(4, runner.peak, "four calls ran together, and never more")
        self.assertEqual(8, sum("/compare/" in call for call in runner.calls), "each commit is compared once")
        self.assertEqual(8, sum("/git/trees/" in call for call in runner.calls), "matching pairs read their trees")
        read = len(runner.calls)
        for repository, base, since, head in QUERIES:
            self.assertEqual(UNCHANGED, detector.detect(repository, 1, base, since, head))
        self.assertEqual(CHANGED, detector.detect("owner/repo", 1, "main", sha(0), sha(2)))
        self.assertEqual(read, len(runner.calls), "everything detect needed was prefetched")

    def test_differing_pairs_prefetch_no_tree(self) -> None:
        runner = ConcurrentRunner(1)
        detector = ChangeDetector(GitHubClient(runner, sleeper=lambda seconds: None))
        detector.prefetch([("owner/repo", "main", sha(2 * n), sha(2 * n + 2)) for n in range(4)])
        self.assertEqual(5, len(runner.calls))
        self.assertFalse(any("/git/trees/" in call for call in runner.calls))

    def test_the_shared_backoff_waits_out_a_rate_limit_hit_by_concurrent_calls(self) -> None:
        limited = {f"repos/owner/repo/compare/main...{sha(1)}", f"repos/owner/repo/git/trees/{sha(4)}?recursive=1"}
        runner = ConcurrentRunner(1, limited)
        waits: list[float] = []
        detector = ChangeDetector(GitHubClient(runner, sleeper=waits.append))
        detector.prefetch(QUERIES)
        self.assertEqual([5.0, 5.0], waits, "each limited call waits the policy's first backoff once")
        self.assertEqual(18, len(runner.calls), "sixteen reads and two retries")
        self.assertEqual(
            [UNCHANGED] * 4,
            [detector.detect(repository, 1, base, since, head) for repository, base, since, head in QUERIES],
        )

    def test_a_run_level_failure_in_a_concurrent_call_stops_the_prefetch(self) -> None:
        class Client(FakeClient):
            def api_json(self, endpoint: str, *, paginate: bool = False, allow_absent: bool = False) -> Any:
                if endpoint.endswith(sha(3)):
                    raise GitHubError("gave up", kind="rate_limit")
                return super().api_json(endpoint)

        client = Client({sha(n): comparison(changed("a.py", str(n))) for n in range(8)})
        with self.assertRaises(GitHubError):
            ChangeDetector(client).prefetch(QUERIES)


class AncestryTests(unittest.TestCase):
    class Client(GitHubClient):
        def __init__(self, answer: object) -> None:
            self.answer = answer
            self.calls: list[str] = []

        def api_json(self, endpoint: str, *, paginate: bool = False, allow_absent: bool = False) -> Any:
            self.calls.append(endpoint)
            if isinstance(self.answer, GitHubError):
                if allow_absent and self.answer.kind == "not_found":
                    return None
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

    def test_any_failure_but_a_missing_commit_is_raised(self) -> None:
        for kind in (*RUN_LEVEL_KINDS, *CALL_LEVEL_KINDS):
            with self.subTest(kind=kind), self.assertRaises(GitHubError):
                at_or_before(self.Client(GitHubError("stop", kind=kind)), "owner/repo", REVIEWED, HEAD)


if __name__ == "__main__":
    unittest.main()
