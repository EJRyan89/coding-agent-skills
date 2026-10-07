from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

from github_client import CommandResult
from pr_status import COMMIT_LIMIT, LIMIT, QueryError, base_contains, branch_tips, classify

TIP = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
MAIN_NOW = "d" * 40
UPDATE = "e" * 40
UPDATE_AGAIN = "1" * 40
WORK = "2" * 40
FOREIGN = "3" * 40


class Recorder:
    """A gh runner that records every call, without the leading `gh`, and answers it with `answer`."""

    def __init__(self, answer: Callable[[list[str]], subprocess.CompletedProcess]) -> None:
        self.answer = answer
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> CommandResult:
        if command[0] != "gh":
            raise AssertionError(command)
        arguments = list(command[1:])
        self.calls.append(arguments)
        answer = self.answer(arguments)
        return CommandResult(answer.returncode, answer.stdout, answer.stderr)


def responder(stdout: str = "[]", returncode: int = 0, stderr: str = "") -> Recorder:
    return Recorder(lambda arguments: subprocess.CompletedProcess(["gh", *arguments], returncode, stdout, stderr))


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
            with (
                self.subTest(repository=repository, sha=sha, upstream=upstream),
                self.assertRaisesRegex(QueryError, message),
            ):
                classify(repository, "topic", sha, upstream, runner=run)
            self.assertEqual([], run.calls)

    def test_missing_gh_fails_closed(self) -> None:
        def missing(arguments: Sequence[str]) -> CommandResult:
            raise FileNotFoundError("gh")

        with self.assertRaisesRegex(QueryError, "could not run gh"):
            classify("owner/repo", "topic", TIP, runner=missing)

    def test_gh_missing_from_the_shared_runner_fails_closed(self) -> None:
        with (
            mock.patch("subprocess.run", side_effect=FileNotFoundError("gh")),
            self.assertRaisesRegex(QueryError, "could not run gh: GitHub CLI executable 'gh' was not found"),
        ):
            classify("owner/repo", "topic", TIP)

    def test_a_rate_limit_is_waited_out_before_classifying(self) -> None:
        answers = [(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)"), (0, listing(pull("MERGED")), "")]
        run = Recorder(lambda arguments: subprocess.CompletedProcess(arguments, *answers.pop(0)))
        with mock.patch("time.sleep") as sleep:
            self.assertEqual("MERGED", classify("owner/repo", "topic", TIP, runner=run))
        sleep.assert_called_once_with(5.0)
        self.assertEqual(2, len(run.calls))

    def test_a_rate_limit_that_persists_fails_closed_with_the_exit_code(self) -> None:
        run = responder("", returncode=1, stderr="gh: API rate limit exceeded (HTTP 403)")
        with (
            mock.patch("time.sleep"),
            self.assertRaisesRegex(QueryError, r"gh pr list failed \(1\): .*\(gave up after 5 retries\)"),
        ):
            classify("owner/repo", "topic", TIP, runner=run)
        self.assertEqual(6, len(run.calls))

    def test_output_that_is_not_utf8_is_replaced_instead_of_failing(self) -> None:
        # A pull request field gh prints can hold any bytes; strict decoding used to end the sweep with a traceback.
        listed = json.dumps([dict(pull("MERGED"), title="TITLE")]).replace("TITLE", "caf\xe9").encode("latin-1")
        with mock.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, listed, b"")):
            self.assertEqual("MERGED", classify("owner/repo", "topic", TIP))


def router(pulls: str, commits: str = "", returncode: int = 0, stderr: str = "") -> Recorder:
    """Answers `pr list` with pulls and the pull request commits query with commits, recording every call."""

    def answer(arguments: list[str]) -> subprocess.CompletedProcess:
        if arguments[0] == "api":
            return subprocess.CompletedProcess(["gh", *arguments], returncode, commits, stderr)
        return subprocess.CompletedProcess(["gh", *arguments], 0, pulls, "")

    return Recorder(answer)


def history(*commits: tuple[str, ...], page_size: int = 100) -> str:
    """The commits as `gh api --paginate --slurp` prints them: a JSON array of pages, each an array of commits."""
    objects = [{"sha": sha, "parents": [{"sha": parent} for parent in parents]} for sha, *parents in commits]
    return json.dumps([objects[start : start + page_size] for start in range(0, len(objects), page_size)] or [[]])


def in_main(sha: str) -> bool:
    return sha in (BASE, MAIN_NOW)


# The tip's pull request gained a merge of main ("Update branch") before it was merged.
UPDATED = history((TIP, BASE), (UPDATE, TIP, MAIN_NOW))


class UpdatedPullRequestTests(unittest.TestCase):
    def state(self, head: str, commits: str, upstream: str | None = None, state: str = "MERGED", **pull_fields) -> str:
        run = router(listing(dict(pull(state, head, **pull_fields), number=7)), commits)
        self.runner = run
        return classify("owner/repo", "topic", TIP, upstream, runner=run, in_base=in_main)

    def api_calls(self) -> list[list[str]]:
        return [call for call in self.runner.calls if call[0] == "api"]

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
        # Each page holds one commit the query must refuse, a commit that is not even an object among them.
        unexpected: tuple[list[object], ...] = (
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
                merged, json.dumps([page])
            )
        for message, run in cases.items():
            with self.subTest(message=message), self.assertRaisesRegex(QueryError, message):
                classify("owner/repo", "topic", TIP, runner=run, in_base=in_main)


LIST_CALL = [
    "pr",
    "list",
    "--repo",
    "owner/repo",
    "--head",
    "topic",
    "--state",
    "all",
    "--json",
    "number,state,headRefOid,headRepository,headRepositoryOwner",
    "--limit",
    "1000",
]


def commits_call(number: int) -> list[str]:
    return ["api", "--paginate", "--slurp", f"repos/owner/repo/pulls/{number}/commits?per_page=100"]


class ClassifySequenceTests(unittest.TestCase):
    """classify called directly: each outcome with the exact gh calls it made, and each refusal's exact message."""

    def outcome(
        self,
        *pulls: dict,
        upstream: str | None = None,
        in_base: Callable[[str], bool] | None = None,
        commits: dict[int, str] | None = None,
    ) -> tuple[str, list[list[str]]]:
        answers = commits or {}

        def answer(arguments: list[str]) -> subprocess.CompletedProcess:
            if arguments[0] == "api":
                number = int(arguments[3].split("/")[4])
                return subprocess.CompletedProcess(["gh", *arguments], 0, answers[number], "")
            return subprocess.CompletedProcess(["gh", *arguments], 0, listing(*pulls), "")

        run = Recorder(answer)
        return classify("owner/repo", "topic", TIP, upstream, runner=run, in_base=in_base), run.calls

    def refusal(
        self, run: Recorder, repository: str = "owner/repo", head: str = TIP, upstream: str | None = None
    ) -> str:
        with self.assertRaises(QueryError) as caught:
            classify(repository, "topic", head, upstream, runner=run, in_base=in_main)
        return str(caught.exception)

    def test_identities_are_refused_in_order_before_any_query(self) -> None:
        cases = (
            ({"repository": "owner"}, "repository must be owner/name, got 'owner'"),
            ({"repository": "owner/repo/extra"}, "repository must be owner/name, got 'owner/repo/extra'"),
            ({"repository": "owner", "head": "x"}, "repository must be owner/name, got 'owner'"),
            ({"head": TIP.upper()}, f"head SHA must be a full lowercase commit SHA, got '{TIP.upper()}'"),
            ({"head": "a" * 39}, f"head SHA must be a full lowercase commit SHA, got '{'a' * 39}'"),
            ({"head": "x", "upstream": "y"}, "head SHA must be a full lowercase commit SHA, got 'x'"),
            ({"upstream": "abc"}, "upstream SHA must be a full lowercase commit SHA, got 'abc'"),
            ({"upstream": ""}, "upstream SHA must be a full lowercase commit SHA, got ''"),
        )
        for arguments, message in cases:
            run = responder(listing())
            with self.subTest(arguments=arguments):
                self.assertEqual(message, self.refusal(run, **arguments))
                self.assertEqual([], run.calls)

    def test_listing_problems_are_refused_with_their_exact_message(self) -> None:
        cases = (
            (responder("", 1, "HTTP 502: Bad"), "gh pr list failed (1): HTTP 502: Bad"),
            (responder("nope"), "gh pr list returned invalid JSON: Expecting value: line 1 column 1 (char 0)"),
            (responder("{}"), "gh pr list did not return a list"),
            (responder('"OPEN"'), "gh pr list did not return a list"),
            (
                responder(listing(*([pull("CLOSED", OLD)] * 1000))),
                "branch matches 1000 or more pull requests; the result may be incomplete",
            ),
            (responder(json.dumps(["a pull"])), "gh pr list returned an unexpected pull request state: None"),
            (responder(listing({"number": 1})), "gh pr list returned an unexpected pull request state: None"),
            (responder(listing(pull("DRAFT"))), "gh pr list returned an unexpected pull request state: 'DRAFT'"),
            (
                responder(listing(pull("OPEN"), pull("open"))),
                "gh pr list returned an unexpected pull request state: 'open'",
            ),
        )
        for run, message in cases:
            with self.subTest(message=message):
                self.assertEqual(message, self.refusal(run))
                self.assertEqual([LIST_CALL], run.calls)

    def test_a_gh_that_cannot_start_is_refused(self) -> None:
        def missing(command: Sequence[str]) -> CommandResult:
            raise OSError("no gh")

        with self.assertRaises(QueryError) as caught:
            classify("owner/repo", "topic", TIP, runner=missing)
        self.assertEqual("could not run gh: no gh", str(caught.exception))

    def test_one_fewer_than_the_limit_is_classified(self) -> None:
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(*([pull("CLOSED", OLD)] * 999)))

    def test_each_outcome_with_its_calls(self) -> None:
        cases: tuple[tuple[str, tuple[dict, ...], dict], ...] = (
            ("NONE", (), {}),
            ("NONE", (pull("MERGED", owner="fork"), pull("CLOSED", name="other")), {}),
            ("NONE", (dict(pull("MERGED"), headRepository=None), dict(pull("MERGED"), headRepositoryOwner={})), {}),
            ("OPEN", (pull("MERGED"), pull("OPEN", OLD, owner="fork")), {}),
            ("OPEN", (pull("OPEN"),), {"upstream": OLD}),
            ("MERGED", (pull("MERGED"),), {}),
            ("MERGED", (pull("MERGED", owner="OWNER", name="Repo"),), {}),
            ("MERGED", (pull("MERGED"),), {"upstream": TIP}),
            ("UNMATCHED", (pull("MERGED"),), {"upstream": OLD}),
            ("UNMATCHED", (pull("CLOSED"),), {"upstream": OLD}),
            ("MERGED", (pull("CLOSED"), pull("MERGED")), {}),
            ("MERGED", (pull("MERGED"), pull("CLOSED")), {}),
            ("CLOSED", (pull("CLOSED"), pull("MERGED", OLD)), {}),
            ("CLOSED", (pull("CLOSED"), pull("MERGED", owner="fork")), {}),
            ("UNMATCHED", (pull("MERGED", OLD), pull("MERGED", owner="fork")), {}),
            ("UNMATCHED", (dict(pull("MERGED"), headRefOid=None),), {}),
        )
        for expected, pulls, arguments in cases:
            with self.subTest(expected=expected, pulls=pulls, arguments=arguments):
                self.assertEqual((expected, [LIST_CALL]), self.outcome(*pulls, **arguments))

    def test_the_repository_matches_its_pull_requests_whatever_its_case(self) -> None:
        run = responder(listing(pull("MERGED")))
        self.assertEqual("MERGED", classify("Owner/Repo", "topic", TIP, runner=run))
        self.assertEqual("Owner/Repo", run.calls[0][3])

    def test_a_merged_pull_request_updated_after_the_tip_is_queried_only_when_it_qualifies(self) -> None:
        updated = dict(pull("MERGED", UPDATE), number=7)
        cases: tuple[tuple[str, tuple[dict, ...], dict, list[list[str]]], ...] = (
            ("MERGED", (updated,), {}, [LIST_CALL, commits_call(7)]),
            ("MERGED", (updated,), {"upstream": TIP}, [LIST_CALL, commits_call(7)]),
            ("UNMATCHED", (updated,), {"upstream": OLD}, [LIST_CALL]),
            ("UNMATCHED", (updated,), {"in_base": None}, [LIST_CALL]),
            ("UNMATCHED", (dict(updated, state="CLOSED"),), {}, [LIST_CALL]),
            ("NONE", (dict(pull("MERGED", UPDATE, owner="fork"), number=7),), {}, [LIST_CALL]),
            ("UNMATCHED", (dict(updated, number="7"),), {}, [LIST_CALL]),
            ("UNMATCHED", (dict(updated, number=None),), {}, [LIST_CALL]),
            # An exact match is MERGED before any pull request's commits are read.
            ("MERGED", (updated, pull("MERGED")), {}, [LIST_CALL]),
            # A proof from the commits comes before an exact CLOSED match.
            ("MERGED", (pull("CLOSED"), updated), {}, [LIST_CALL, commits_call(7)]),
        )
        for expected, pulls, arguments, calls in cases:
            options = {"in_base": in_main, "commits": {7: UPDATED}, **arguments}
            with self.subTest(expected=expected, pulls=pulls, arguments=arguments):
                self.assertEqual((expected, calls), self.outcome(*pulls, **options))

    def test_merged_pull_requests_are_queried_in_order_until_one_proves_the_merge(self) -> None:
        unrelated = history((WORK, BASE))
        first, second, third = (dict(pull("MERGED", UPDATE), number=number) for number in (4, 5, 6))
        self.assertEqual(
            ("MERGED", [LIST_CALL, commits_call(4), commits_call(5)]),
            self.outcome(first, second, third, in_base=in_main, commits={4: unrelated, 5: UPDATED, 6: UPDATED}),
        )
        self.assertEqual(
            ("CLOSED", [LIST_CALL, commits_call(4)]),
            self.outcome(first, pull("CLOSED"), in_base=in_main, commits={4: unrelated}),
        )
        self.assertEqual(
            ("UNMATCHED", [LIST_CALL, commits_call(4), commits_call(5)]),
            self.outcome(first, second, in_base=in_main, commits={4: unrelated, 5: unrelated}),
        )


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
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
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
    """The branch's tips, the base's history, and its pull requests together, as repo_cleanup's
    pull_state reads them."""

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
