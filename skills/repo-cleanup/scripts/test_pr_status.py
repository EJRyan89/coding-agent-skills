from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pr_status import COMMIT_LIMIT, LIMIT, QueryError, base_contains, branch_tips, classify

TIP = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
MAIN_NOW = "d" * 40
UPDATE = "e" * 40
UPDATE_AGAIN = "1" * 40
WORK = "2" * 40
FOREIGN = "3" * 40


def responder(stdout: str = "[]", returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(arguments: list[str]) -> subprocess.CompletedProcess:
        calls.append(arguments)
        return subprocess.CompletedProcess(["gh", *arguments], returncode, stdout, stderr)

    run.calls = calls
    return run


def pull(state: str, sha: str = TIP, owner: str = "owner", name: str = "repo") -> dict:
    return {
        "number": 1,
        "state": state,
        "headRefOid": sha,
        "headRepository": {"name": name},
        "headRepositoryOwner": {"login": owner},
    }


def listing(*pulls: dict) -> str:
    return json.dumps(list(pulls))


def state_of(*pulls: dict) -> str:
    return classify("owner/repo", "topic", TIP, runner=responder(listing(*pulls)))


class ClassifyTests(unittest.TestCase):
    def test_any_open_pull_request_makes_the_branch_active(self) -> None:
        cases = (
            (pull("OPEN"),),
            (pull("MERGED"), pull("OPEN", OLD)),
            (pull("CLOSED"), pull("OPEN", owner="fork")),
        )
        for pulls in cases:
            with self.subTest(pulls=pulls):
                self.assertEqual("OPEN", state_of(*pulls))

    def test_closed_pull_request_at_the_current_tip_is_stale(self) -> None:
        self.assertEqual("MERGED", state_of(pull("CLOSED"), pull("MERGED")))
        self.assertEqual("CLOSED", state_of(pull("CLOSED"), pull("MERGED", OLD)))
        self.assertEqual("MERGED", state_of(pull("MERGED", owner="OWNER", name="Repo")))

    def test_pull_request_closed_at_an_older_commit_does_not_describe_current_work(self) -> None:
        self.assertEqual("UNMATCHED", state_of(pull("MERGED", OLD)))
        self.assertEqual("UNMATCHED", state_of(pull("CLOSED", OLD), pull("MERGED", TIP, owner="fork")))

    def test_newer_upstream_commits_mean_the_pull_request_does_not_describe_current_work(self) -> None:
        run = responder(listing(pull("MERGED")))
        self.assertEqual("UNMATCHED", classify("owner/repo", "topic", TIP, OLD, runner=run))
        self.assertEqual("MERGED", classify("owner/repo", "topic", TIP, TIP, runner=run))

    def test_pull_requests_from_other_head_repositories_are_ignored(self) -> None:
        self.assertEqual("NONE", state_of(pull("MERGED", owner="fork")))
        self.assertEqual("NONE", state_of(pull("CLOSED", name="other")))
        deleted_fork = dict(pull("MERGED"), headRepository=None)
        self.assertEqual("NONE", state_of(deleted_fork))

    def test_no_matching_pull_request_is_none(self) -> None:
        self.assertEqual("NONE", state_of())

    def test_queries_every_state_with_head_identity_and_a_bounded_limit(self) -> None:
        run = responder("[]")
        classify("owner/repo", "topic", TIP, runner=run)
        arguments = run.calls[0]
        self.assertEqual(["pr", "list", "--repo", "owner/repo", "--head", "topic", "--state", "all"], arguments[:8])
        fields = arguments[arguments.index("--json") + 1].split(",")
        for field in ("state", "headRefOid", "headRepository", "headRepositoryOwner"):
            self.assertIn(field, fields)
        self.assertEqual(str(LIMIT), arguments[arguments.index("--limit") + 1])

    def test_query_problems_fail_closed(self) -> None:
        cases = {
            "gh pr list failed": responder("", returncode=1, stderr="HTTP 502"),
            "invalid JSON": responder("not json"),
            "did not return a list": responder("{}"),
            "unexpected pull request state": responder(listing(pull("OPEN"), pull("DRAFT"))),
            "may be incomplete": responder(listing(*([pull("CLOSED")] * LIMIT))),
        }
        for message, run in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(QueryError, message):
                classify("owner/repo", "topic", TIP, runner=run)

    def test_invalid_arguments_fail_before_querying(self) -> None:
        for repository, sha, upstream, message in (
            ("owner/repo", "abc", None, "head SHA must be a full lowercase commit SHA"),
            ("owner/repo", TIP.upper(), None, "head SHA must be a full lowercase commit SHA"),
            ("owner/repo", TIP, "", "upstream SHA must be a full lowercase commit SHA"),
            ("repo", TIP, None, "owner/name"),
        ):
            run = responder("[]")
            with self.subTest(repository=repository, sha=sha, upstream=upstream), self.assertRaisesRegex(QueryError, message):
                classify(repository, "topic", sha, upstream, runner=run)
            self.assertEqual([], run.calls)

    def test_missing_gh_fails_closed(self) -> None:
        def missing(arguments: list[str]) -> subprocess.CompletedProcess:
            raise FileNotFoundError("gh")

        with self.assertRaisesRegex(QueryError, "could not run gh"):
            classify("owner/repo", "topic", TIP, runner=missing)


def router(pulls: str, commits: str = "", returncode: int = 0, stderr: str = ""):
    """Answers `pr list` with pulls and the pull request commits query with commits, recording every call."""
    calls: list[list[str]] = []

    def run(arguments: list[str]) -> subprocess.CompletedProcess:
        calls.append(arguments)
        if arguments[0] == "api":
            return subprocess.CompletedProcess(["gh", *arguments], returncode, commits, stderr)
        return subprocess.CompletedProcess(["gh", *arguments], 0, pulls, "")

    run.calls = calls
    return run


def history(*commits: tuple[str, ...], page_size: int = 100) -> str:
    """The commits as `gh api --paginate --slurp` prints them: a JSON array of pages, each an array of commits."""
    objects = [{"sha": sha, "parents": [{"sha": parent} for parent in parents]} for sha, *parents in commits]
    return json.dumps([objects[start:start + page_size] for start in range(0, len(objects), page_size)] or [[]])


def in_main(sha: str) -> bool:
    return sha in (BASE, MAIN_NOW)


# The tip's pull request gained a merge of main ("Update branch") before it was merged.
UPDATED = history((TIP, BASE), (UPDATE, TIP, MAIN_NOW))


class UpdatedPullRequestTests(unittest.TestCase):
    def state(self, head: str, commits: str, upstream: str | None = None, state: str = "MERGED", **pull_fields) -> str:
        run = router(listing(dict(pull(state, head, **pull_fields), number=7)), commits)
        self.run = run
        return classify("owner/repo", "topic", TIP, upstream, runner=run, in_base=in_main)

    def api_calls(self) -> list[list[str]]:
        return [call for call in self.run.calls if call[0] == "api"]

    def test_merges_of_the_base_after_the_tip_still_prove_the_branch_merged(self) -> None:
        self.assertEqual("MERGED", self.state(UPDATE, UPDATED))
        twice = history((TIP, BASE), (UPDATE, TIP, BASE), (UPDATE_AGAIN, UPDATE, MAIN_NOW))
        self.assertEqual("MERGED", self.state(UPDATE_AGAIN, twice))
        self.assertEqual("MERGED", self.state(UPDATE, UPDATED, upstream=TIP))

    def test_queries_the_pull_requests_commits_with_parents_across_every_page(self) -> None:
        self.state(UPDATE, UPDATED)
        self.assertEqual(
            [["api", "--paginate", "--slurp", "repos/owner/repo/pulls/7/commits?per_page=100"]],
            self.api_calls(),
        )

    def test_commits_split_across_pages_are_read_as_one_list(self) -> None:
        paged = history((TIP, BASE), (UPDATE, TIP, BASE), (UPDATE_AGAIN, UPDATE, MAIN_NOW), page_size=2)
        self.assertEqual(2, len(json.loads(paged)))
        self.assertEqual("MERGED", self.state(UPDATE_AGAIN, paged))

    def test_a_later_commit_with_real_changes_leaves_the_branch_unmatched(self) -> None:
        extended = history((TIP, BASE), (UPDATE, TIP, MAIN_NOW), (WORK, UPDATE))
        self.assertEqual("UNMATCHED", self.state(WORK, extended))
        self.assertEqual("UNMATCHED", self.state(WORK, history((TIP, BASE), (WORK, TIP))))

    def test_a_merge_that_brings_in_commits_outside_the_base_leaves_the_branch_unmatched(self) -> None:
        self.assertEqual("UNMATCHED", self.state(UPDATE, history((TIP, BASE), (UPDATE, TIP, FOREIGN))))
        self.assertEqual("UNMATCHED", self.state(UPDATE, history((TIP, BASE), (UPDATE, TIP, MAIN_NOW, FOREIGN))))

    def test_the_tip_must_be_one_of_the_pull_requests_commits(self) -> None:
        self.assertEqual("UNMATCHED", self.state(UPDATE, history((UPDATE, TIP, MAIN_NOW))))
        self.assertEqual("UNMATCHED", self.state(UPDATE, history((OLD, BASE), (UPDATE, OLD, MAIN_NOW))))
        self.assertEqual("UNMATCHED", self.state(UPDATE, history((TIP, BASE), (UPDATE, MAIN_NOW, TIP))))

    def test_only_a_merged_same_repository_pull_request_without_newer_upstream_work_is_checked(self) -> None:
        for expected, arguments in (
            ("UNMATCHED", {"upstream": OLD}),
            ("UNMATCHED", {"state": "CLOSED"}),
            ("NONE", {"owner": "fork"}),
        ):
            with self.subTest(arguments=arguments):
                self.assertEqual(expected, self.state(UPDATE, UPDATED, **arguments))
                self.assertEqual([], self.api_calls())

    def test_an_exact_match_or_no_base_test_needs_no_commit_query(self) -> None:
        self.assertEqual("MERGED", self.state(TIP, UPDATED))
        self.assertEqual([], self.api_calls())
        run = router(listing(pull("MERGED", UPDATE)), UPDATED)
        self.assertEqual("UNMATCHED", classify("owner/repo", "topic", TIP, runner=run))
        self.assertEqual([], [call for call in run.calls if call[0] == "api"])

    def test_commit_query_problems_fail_closed(self) -> None:
        merged = listing(dict(pull("MERGED", UPDATE), number=7))
        many = history(*((f"{index:040x}", BASE) for index in range(COMMIT_LIMIT)))
        commit = {"sha": UPDATE, "parents": [{"sha": TIP}]}
        cases = {
            r"commits of pull request 7 failed \(1\): HTTP 502": router(merged, "", 1, "HTTP 502"),
            "may be incomplete": router(merged, many),
        }
        malformed = (
            "",
            "not json",
            json.dumps({"message": "Not Found"}),
            json.dumps([commit]),  # one page, not an array of pages
            json.dumps([[commit], {"sha": TIP}]),
        )
        unexpected = (
            [{"parents": []}],
            [{"sha": "not-a-sha", "parents": []}],
            [{"sha": UPDATE, "parents": [TIP]}],
            [{"sha": UPDATE, "parents": [{"sha": "short"}]}],
            [{"sha": UPDATE, "parents": None}],
            [{"sha": UPDATE.upper(), "parents": []}],
            ["a commit"],
        )
        for output in malformed:
            cases[f"malformed commits for pull request 7: {re.escape(output[:20])}"] = router(merged, output)
        for page in unexpected:
            cases[f"unexpected commit for pull request 7: {re.escape(repr(page[0])[:20])}"] = router(
                merged, json.dumps([page]))
        for message, run in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(QueryError, message):
                classify("owner/repo", "topic", TIP, runner=run, in_base=in_main)


class GitFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="pr-status.")
        root = Path(self._temporary.name)
        self.remote, self.clone, self.other = root / "remote.git", root / "clone", root / "other"
        self.git("init", "--bare", "-b", "main", str(self.remote))
        self.git("clone", str(self.remote), str(self.clone))
        self.commit(self.clone, "initial")
        self.git("-C", str(self.clone), "push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def git(self, *arguments: str) -> str:
        result = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *arguments],
            capture_output=True, text=True, encoding="utf-8", check=True,
        )
        return result.stdout.strip()

    def commit(self, checkout: Path, message: str) -> str:
        self.git("-C", str(checkout), "commit", "--allow-empty", "-m", message)
        return self.git("-C", str(checkout), "rev-parse", "HEAD")

    def tracking_branch(self) -> str:
        self.git("-C", str(self.clone), "checkout", "-b", "topic")
        tip = self.commit(self.clone, "work")
        self.git("-C", str(self.clone), "push", "-u", "origin", "topic")
        return tip

    def push_newer_remote_work(self) -> str:
        self.git("clone", "-b", "topic", str(self.remote), str(self.other))
        newer = self.commit(self.other, "newer work")
        self.git("-C", str(self.other), "push", "origin", "topic")
        self.git("-C", str(self.clone), "fetch", "origin")
        return newer


class BranchTipTests(GitFixture):
    def test_local_only_branch_has_no_upstream(self) -> None:
        self.git("-C", str(self.clone), "checkout", "-b", "topic")
        tip = self.commit(self.clone, "work")
        self.assertEqual((tip, None), branch_tips(str(self.clone), "topic"))

    def test_tracking_branch_reports_the_fetched_upstream_tip(self) -> None:
        tip = self.tracking_branch()
        self.assertEqual((tip, tip), branch_tips(str(self.clone), "topic"))
        newer = self.push_newer_remote_work()
        self.assertEqual((tip, newer), branch_tips(str(self.clone), "topic"))

    def test_gone_upstream_is_treated_as_absent(self) -> None:
        tip = self.tracking_branch()
        self.git("-C", str(self.clone), "push", "origin", "--delete", "topic")
        self.git("-C", str(self.clone), "fetch", "--prune", "origin")
        self.assertEqual((tip, None), branch_tips(str(self.clone), "topic"))

    def test_missing_branch_fails_closed(self) -> None:
        with self.assertRaisesRegex(QueryError, "cannot resolve local branch"):
            branch_tips(str(self.clone), "missing")


class BaseContainsTests(GitFixture):
    def test_reports_whether_a_commit_is_reachable_from_the_base(self) -> None:
        base = self.git("-C", str(self.clone), "rev-parse", "main")
        tip = self.tracking_branch()
        contains = base_contains(str(self.clone), "refs/remotes/origin/main")
        self.assertTrue(contains(base))
        self.assertFalse(contains(tip))
        self.assertFalse(contains("f" * 40), "a commit missing locally is not in the base")

    def test_an_unresolvable_base_fails_closed(self) -> None:
        base = self.git("-C", str(self.clone), "rev-parse", "main")
        with self.assertRaisesRegex(QueryError, "cannot test whether"):
            base_contains(str(self.clone), "refs/remotes/origin/missing")(base)


class BranchClassificationTests(GitFixture):
    """The branch's tips, the base's history, and its pull requests together, as repo_cleanup's pull_state reads them."""

    def state(self, run, base_ref: str | None = None) -> str:
        head, upstream = branch_tips(str(self.clone), "topic")
        in_base = base_contains(str(self.clone), base_ref) if base_ref else None
        return classify("owner/repo", "topic", head, upstream, run, in_base)

    def test_a_merged_pull_request_at_the_tip_is_merged(self) -> None:
        tip = self.tracking_branch()
        self.assertEqual("MERGED", self.state(responder(listing(pull("MERGED", tip)))))

    def test_base_ref_lets_an_updated_pull_request_prove_the_branch_merged(self) -> None:
        base = self.git("-C", str(self.clone), "rev-parse", "main")
        tip = self.tracking_branch()
        self.git("-C", str(self.clone), "switch", "main")
        main_now = self.commit(self.clone, "main moves on")
        self.git("-C", str(self.clone), "push", "origin", "main")
        self.git("-C", str(self.clone), "switch", "--detach", "topic")
        self.git("-C", str(self.clone), "merge", "--no-ff", "-m", "Merge branch 'main' into topic", "main")
        update = self.git("-C", str(self.clone), "rev-parse", "HEAD")
        self.git("-C", str(self.clone), "switch", "main")
        run = router(listing(pull("MERGED", update)), history((tip, base), (update, tip, main_now)))
        self.assertEqual("MERGED", self.state(run, "refs/remotes/origin/main"))
        self.assertEqual("UNMATCHED", self.state(run))

    def test_reused_branch_with_newer_remote_work_is_not_stale(self) -> None:
        tip = self.tracking_branch()
        self.push_newer_remote_work()
        self.assertEqual("UNMATCHED", self.state(responder(listing(pull("MERGED", tip)))))

    def test_query_failure_fails_closed(self) -> None:
        self.tracking_branch()
        with self.assertRaisesRegex(QueryError, re.escape("gh pr list failed (1): HTTP 502")):
            self.state(responder("", returncode=1, stderr="HTTP 502"))


if __name__ == "__main__":
    unittest.main()
