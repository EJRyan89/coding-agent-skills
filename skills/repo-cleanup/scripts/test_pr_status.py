from __future__ import annotations

import json
import os
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

from bounded_process import Finished
from git_client import GitClient, GitResult, subprocess_runner
from git_test_base import GitTestCase
from github_client import CommandResult, GitHubError
from pr_status import (
    COMMIT_LIMIT,
    LIMIT,
    Bases,
    BaseTests,
    QueryError,
    base_contains,
    branch_tips,
    change_identity,
    change_match,
    classify,
    fetched_bases,
)

TIP = "a" * 40
OLD = "b" * 40
BASE = "c" * 40
MAIN_NOW = "d" * 40
UPDATE = "e" * 40
UPDATE_AGAIN = "1" * 40
WORK = "2" * 40
FOREIGN = "3" * 40
SQUASH = "4" * 40
OTHER_SQUASH = "beef" * 10
RELEASE_BASE = "5" * 40
RELEASE_SQUASH = "6" * 40


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


def pull(state: str, sha: str = TIP, owner: str = "owner", name: str = "repo", base: object = "main") -> dict:
    return {
        "number": 1,
        "state": state,
        "headRefOid": sha,
        "headRepository": {"name": name},
        "headRepositoryOwner": {"login": owner},
        "baseRefName": base,
    }


def never(tip: str, merge_commit: str) -> bool:
    return False


def in_main(sha: str) -> bool:
    return sha in (BASE, MAIN_NOW)


def in_release(sha: str) -> bool:
    return sha in (BASE, RELEASE_BASE)


def main_only(same_change: Callable[[str, str], bool] = never) -> Bases:
    """The tests of a fetched main and no other base."""
    return {"main": BaseTests(in_main, same_change)}.get


def listing(*pulls: dict) -> str:
    return json.dumps(list(pulls))


def state_of(*pulls: dict) -> str:
    return classify("owner/repo", "topic", TIP, runner=responder(listing(*pulls)))


class ClassifyTests(GitTestCase):
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
        for field in ("state", "headRefOid", "headRepository", "headRepositoryOwner", "mergeCommit", "baseRefName"):
            self.assertIn(field, fields)
        self.assertEqual(str(LIMIT), arguments[arguments.index("--limit") + 1])

    def test_query_problems_fail_closed(self) -> None:
        cases = {
            "gh pr list failed": responder("", returncode=1, stderr="HTTP 500"),
            "gh pr list failed: GitHub returned malformed JSON": responder("not json"),
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
            mock.patch("github_client.run_bounded", side_effect=FileNotFoundError("gh")),
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

    def test_a_rate_limit_refused_without_a_run_names_the_query_not_a_missing_gh(self) -> None:
        def refused(command: Sequence[str]) -> CommandResult:
            raise GitHubError("GitHub asked to wait 400s before retrying; rerun later", kind="rate_limit")

        with self.assertRaises(QueryError) as caught:
            classify("owner/repo", "topic", TIP, runner=refused)
        self.assertEqual(
            "gh pr list failed: GitHub asked to wait 400s before retrying; rerun later", str(caught.exception)
        )

    def test_output_that_is_not_utf8_is_replaced_instead_of_failing(self) -> None:
        # A pull request field gh prints can hold any bytes; strict decoding used to end the sweep with a traceback.
        listed = json.dumps([dict(pull("MERGED"), title="TITLE")]).replace("TITLE", "caf\xe9").encode("latin-1")
        with mock.patch("github_client.run_bounded", return_value=Finished(0, listed, b"")):
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


# The tip's pull request gained a merge of main ("Update branch") before it was merged.
UPDATED = history((TIP, BASE), (UPDATE, TIP, MAIN_NOW))


class UpdatedPullRequestTests(GitTestCase):
    def state(self, head: str, commits: str, upstream: str | None = None, state: str = "MERGED", **pull_fields) -> str:
        run = router(listing(dict(pull(state, head, **pull_fields), number=7)), commits)
        self.runner = run
        return classify("owner/repo", "topic", TIP, upstream, runner=run, bases=main_only(), default_base="main")

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

    def test_an_upstream_the_base_already_holds_is_not_newer_work(self) -> None:
        # A branch tracking its base has the base's tip as its upstream; that is no work of the branch's own.
        self.assertEqual("MERGED", self.state(UPDATE, UPDATED, upstream=MAIN_NOW))
        self.assertEqual([commits_call(7)], self.api_calls())
        self.assertEqual("UNMATCHED", self.state(UPDATE, UPDATED, upstream=RELEASE_BASE))
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
        not_json = "commits of pull request 7 failed: GitHub returned malformed JSON: Expecting value: line 1 column 1"
        cases = {
            r"commits of pull request 7 failed \(1\): HTTP 500": router(merged, "", 1, "HTTP 500"),
            "may be incomplete": router(merged, many),
            f"{not_json} \\(char 0\\)$": router(merged, ""),
            f"^gh api for the {not_json}": router(merged, "not json"),
        }
        malformed = (
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
                classify("owner/repo", "topic", TIP, runner=run, bases=main_only(), default_base="main")


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
    "number,state,headRefOid,headRepository,headRepositoryOwner,mergeCommit,baseRefName",
    "--limit",
    "1000",
]


def commits_call(number: int) -> list[str]:
    return ["api", "--paginate", "--slurp", f"repos/owner/repo/pulls/{number}/commits?per_page=100"]


class ClassifySequenceTests(GitTestCase):
    """classify called directly: each outcome with the exact gh calls it made, and each refusal's exact message."""

    def outcome(
        self,
        *pulls: dict,
        upstream: str | None = None,
        bases: Bases | None = None,
        commits: dict[int, str] | None = None,
        default_base: str | None = "main",
    ) -> tuple[str, list[list[str]]]:
        answers = commits or {}

        def answer(arguments: list[str]) -> subprocess.CompletedProcess:
            if arguments[0] == "api":
                number = int(arguments[3].split("/")[4])
                return subprocess.CompletedProcess(["gh", *arguments], 0, answers[number], "")
            return subprocess.CompletedProcess(["gh", *arguments], 0, listing(*pulls), "")

        run = Recorder(answer)
        state = classify("owner/repo", "topic", TIP, upstream, runner=run, bases=bases, default_base=default_base)
        return state, run.calls

    def refusal(
        self, run: Recorder, repository: str = "owner/repo", head: str = TIP, upstream: str | None = None
    ) -> str:
        with self.assertRaises(QueryError) as caught:
            classify(repository, "topic", head, upstream, runner=run, bases=main_only(), default_base="main")
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
            (responder("", 1, "HTTP 500: Bad"), "gh pr list failed (1): HTTP 500: Bad"),
            (
                responder("nope"),
                "gh pr list failed: GitHub returned malformed JSON: Expecting value: line 1 column 1 (char 0)",
            ),
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
            ("UNMATCHED", (updated,), {"bases": None}, [LIST_CALL]),
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
            options = {"bases": main_only(), "commits": {7: UPDATED}, **arguments}
            with self.subTest(expected=expected, pulls=pulls, arguments=arguments):
                self.assertEqual((expected, calls), self.outcome(*pulls, **options))

    def test_merged_pull_requests_are_queried_in_order_until_one_proves_the_merge(self) -> None:
        unrelated = history((WORK, BASE))
        first, second, third = (dict(pull("MERGED", UPDATE), number=number) for number in (4, 5, 6))
        self.assertEqual(
            ("MERGED", [LIST_CALL, commits_call(4), commits_call(5)]),
            self.outcome(first, second, third, bases=main_only(), commits={4: unrelated, 5: UPDATED, 6: UPDATED}),
        )
        self.assertEqual(
            ("CLOSED", [LIST_CALL, commits_call(4)]),
            self.outcome(first, pull("CLOSED"), bases=main_only(), commits={4: unrelated}),
        )
        self.assertEqual(
            ("UNMATCHED", [LIST_CALL, commits_call(4), commits_call(5)]),
            self.outcome(first, second, bases=main_only(), commits={4: unrelated, 5: unrelated}),
        )

    def test_a_merge_commit_whose_change_is_the_branches_proves_a_rewritten_tip_merged(self) -> None:
        squashed = dict(pull("MERGED", OLD), number=7, mergeCommit={"oid": SQUASH})
        other = dict(squashed, mergeCommit={"oid": OTHER_SQUASH})
        asked: list[tuple[str, str]] = []

        def same_change(tip: str, merge_commit: str) -> bool:
            asked.append((tip, merge_commit))
            return (tip, merge_commit) == (TIP, SQUASH)

        # The commit walk is tried first for every pull request, and the change only once it proves no merge.
        walked = [LIST_CALL, commits_call(7)]
        cases: tuple[tuple[str, tuple[dict, ...], dict, list[list[str]], list[tuple[str, str]]], ...] = (
            ("MERGED", (squashed,), {}, walked, [(TIP, SQUASH)]),
            ("MERGED", (squashed,), {"upstream": TIP}, walked, [(TIP, SQUASH)]),
            ("UNMATCHED", (squashed,), {"upstream": OLD}, [LIST_CALL], []),
            ("UNMATCHED", (dict(squashed, state="CLOSED"),), {}, [LIST_CALL], []),
            ("NONE", (dict(squashed, headRepositoryOwner={"login": "fork"}),), {}, [LIST_CALL], []),
            ("UNMATCHED", (dict(squashed, mergeCommit=None),), {}, walked, []),
            ("UNMATCHED", (dict(squashed, mergeCommit={}),), {}, walked, []),
            ("UNMATCHED", (dict(squashed, mergeCommit={"oid": "short"}),), {}, walked, []),
            ("UNMATCHED", (dict(squashed, mergeCommit={"oid": OTHER_SQUASH.upper()}),), {}, walked, []),
            ("UNMATCHED", (dict(squashed, mergeCommit=SQUASH),), {}, walked, []),
            ("UNMATCHED", (other,), {}, walked, [(TIP, OTHER_SQUASH)]),
            # An exact match is MERGED before any commit is read or change compared.
            ("MERGED", (squashed, pull("MERGED")), {}, [LIST_CALL], []),
            # A proof from the change comes before an exact CLOSED match; a change that differs leaves CLOSED.
            ("MERGED", (pull("CLOSED"), squashed), {}, walked, [(TIP, SQUASH)]),
            ("CLOSED", (pull("CLOSED"), other), {}, walked, [(TIP, OTHER_SQUASH)]),
            # Merged pull requests are compared in listing order until one proves the merge.
            ("MERGED", (other, squashed), {}, [*walked, commits_call(7)], [(TIP, OTHER_SQUASH), (TIP, SQUASH)]),
            ("MERGED", (squashed, other), {}, [*walked, commits_call(7)], [(TIP, SQUASH)]),
        )
        for expected, pulls, arguments, calls, compared in cases:
            asked.clear()
            options = {"bases": main_only(same_change), "commits": {7: history((OLD, BASE))}, **arguments}
            with self.subTest(expected=expected, pulls=pulls, arguments=arguments):
                self.assertEqual((expected, calls), self.outcome(*pulls, **options))
                self.assertEqual(compared, asked)

    def test_without_base_tests_a_rewritten_tip_stays_unmatched(self) -> None:
        squashed = dict(pull("MERGED", OLD), number=7, mergeCommit={"oid": SQUASH})
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(squashed))
        self.assertEqual(
            ("UNMATCHED", [LIST_CALL, commits_call(7)]),
            self.outcome(squashed, bases=main_only(), commits={7: history((OLD, BASE))}),
        )

    def test_each_pull_request_is_judged_against_its_own_base(self) -> None:
        asked: list[tuple[str, str, str]] = []

        def change_on(base: str) -> Callable[[str, str], bool]:
            def same_change(tip: str, merge_commit: str) -> bool:
                asked.append((base, tip, merge_commit))
                return (base, tip, merge_commit) == ("release/1.0", TIP, RELEASE_SQUASH)

            return same_change

        bases: Bases = {
            "main": BaseTests(in_main, change_on("main")),
            "release/1.0": BaseTests(in_release, change_on("release/1.0")),
        }.get
        into_main = dict(pull("MERGED", OLD), number=7, mergeCommit={"oid": SQUASH})
        into_release = dict(pull("MERGED", OLD, base="release/1.0"), number=8, mergeCommit={"oid": RELEASE_SQUASH})
        unrelated = history((OLD, BASE))
        commits = {7: unrelated, 8: unrelated}
        # Several merged pull requests with different bases on one head name: each is compared on its own base.
        self.assertEqual(
            ("MERGED", [LIST_CALL, commits_call(7), commits_call(8)]),
            self.outcome(into_main, into_release, bases=bases, commits=commits),
        )
        self.assertEqual([("main", TIP, SQUASH), ("release/1.0", TIP, RELEASE_SQUASH)], asked)
        asked.clear()
        self.assertEqual(
            ("UNMATCHED", [LIST_CALL, commits_call(7)]), self.outcome(into_main, bases=bases, commits=commits)
        )
        self.assertEqual([("main", TIP, SQUASH)], asked)
        asked.clear()
        # The "Update branch" walk reads the base the pull request merged into, too.
        updated_release = dict(pull("MERGED", UPDATE, base="release/1.0"), number=9)
        released = history((TIP, BASE), (UPDATE, TIP, RELEASE_BASE))
        self.assertEqual(
            ("MERGED", [LIST_CALL, commits_call(9)]),
            self.outcome(updated_release, bases=bases, commits={9: released}),
        )
        self.assertEqual(
            ("UNMATCHED", [LIST_CALL, commits_call(9)]),
            self.outcome(dict(updated_release, baseRefName="main"), bases=bases, commits={9: released}),
        )
        self.assertEqual([], asked)

    def test_a_base_not_fetched_locally_proves_nothing(self) -> None:
        squashed = dict(pull("MERGED", OLD, base="release/2.0"), number=7, mergeCommit={"oid": SQUASH})
        always = {"main": BaseTests(in_main, lambda tip, merge_commit: True)}.get
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(squashed, bases=always, commits={}))
        self.assertEqual(
            ("MERGED", [LIST_CALL, commits_call(7)]),
            self.outcome(dict(squashed, baseRefName="main"), bases=always, commits={7: history((OLD, BASE))}),
        )

    def test_a_pull_request_without_a_usable_base_name_is_judged_against_the_default(self) -> None:
        squashed = dict(pull("MERGED", OLD), number=7, mergeCommit={"oid": SQUASH})
        asked: list[str] = []

        def tests(base: str) -> BaseTests | None:
            asked.append(base)
            return BaseTests(in_main, lambda tip, merge_commit: True) if base == "main" else None

        unusable: tuple[object, ...] = (None, 7, "", "a..b", "with space", "a^b", "a:b", "a~1", "a@{1}", "a?b")
        for name in unusable:
            asked.clear()
            with self.subTest(name=name):
                self.assertEqual(
                    ("MERGED", [LIST_CALL, commits_call(7)]),
                    self.outcome(dict(squashed, baseRefName=name), bases=tests, commits={7: history((OLD, BASE))}),
                )
                self.assertEqual(["main"], asked)
                asked.clear()
                self.assertEqual(
                    ("UNMATCHED", [LIST_CALL]),
                    self.outcome(dict(squashed, baseRefName=name), bases=tests, commits={}, default_base=None),
                )
                self.assertEqual([], asked)
        # A pull request's own base name is looked up as given, in any shape git accepts.
        for name in ("main", "release/1.0", "feature-x", "topic.v2", "wip_2026", "-dash", "uni\u00e9"):
            asked.clear()
            with self.subTest(name=name):
                self.outcome(dict(squashed, baseRefName=name), bases=tests, commits={7: history((OLD, BASE))})
                self.assertEqual([name], asked)

    def test_a_closed_or_fork_pull_request_on_another_base_never_matches(self) -> None:
        bases: Bases = {"release/1.0": BaseTests(in_release, lambda tip, merge_commit: True)}.get
        closed = dict(pull("CLOSED", OLD, base="release/1.0"), number=7, mergeCommit={"oid": RELEASE_SQUASH})
        fork = dict(pull("MERGED", OLD, owner="fork", base="release/1.0"), number=7, mergeCommit={"oid": SQUASH})
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(closed, bases=bases, commits={}))
        self.assertEqual(("NONE", [LIST_CALL]), self.outcome(fork, bases=bases, commits={}))
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(closed, fork, bases=bases, commits={}))

    def test_an_upstream_the_pull_requests_base_holds_blocks_no_proof(self) -> None:
        squashed = dict(pull("MERGED", OLD), number=7, mergeCommit={"oid": SQUASH})
        bases: Bases = {
            "main": BaseTests(in_main, lambda tip, merge_commit: True),
            "release/1.0": BaseTests(in_release, lambda tip, merge_commit: True),
        }.get
        commits = {7: UPDATED}
        cases: tuple[tuple[str, tuple[dict, ...], str | None, list[list[str]]], ...] = (
            # An exact match, the walk, and the change each pass the gate when the base holds the upstream.
            ("MERGED", (pull("MERGED"),), MAIN_NOW, [LIST_CALL]),
            ("MERGED", (dict(pull("MERGED", UPDATE), number=7),), MAIN_NOW, [LIST_CALL, commits_call(7)]),
            ("MERGED", (squashed,), MAIN_NOW, [LIST_CALL, commits_call(7)]),
            ("CLOSED", (pull("CLOSED"),), MAIN_NOW, [LIST_CALL]),
            # The upstream is judged against each pull request's own base, not against every fetched base.
            ("UNMATCHED", (squashed,), RELEASE_BASE, [LIST_CALL]),
            ("MERGED", (dict(squashed, baseRefName="release/1.0"),), RELEASE_BASE, [LIST_CALL, commits_call(7)]),
            ("UNMATCHED", (pull("MERGED"),), RELEASE_BASE, [LIST_CALL]),
            ("UNMATCHED", (pull("CLOSED"),), RELEASE_BASE, [LIST_CALL]),
            # Work the base does not hold stays newer work.
            ("UNMATCHED", (pull("MERGED"), squashed), WORK, [LIST_CALL]),
        )
        for expected, pulls, upstream, calls in cases:
            with self.subTest(expected=expected, pulls=pulls, upstream=upstream):
                self.assertEqual(
                    (expected, calls), self.outcome(*pulls, upstream=upstream, bases=bases, commits=commits)
                )
        # Without base tests the gate is the old one: any other upstream commit is newer work.
        self.assertEqual(("UNMATCHED", [LIST_CALL]), self.outcome(pull("MERGED"), upstream=MAIN_NOW))


class GitFixture(GitTestCase):
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

    def change(self, checkout: Path, name: str, text: str, message: str) -> str:
        """A commit that writes `text` to the file `name`."""
        (checkout / name).write_text(text, encoding="utf-8")
        self.git("-C", str(checkout), "add", name)
        return self.commit(checkout, message)

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


class SquashFixture(GitFixture):
    """A topic branch with a file change, pushed from the clone, which another clone squash-merges."""

    BASE_REF = "refs/remotes/origin/main"

    def setUp(self) -> None:
        super().setUp()
        self.git("-C", str(self.clone), "checkout", "-b", "topic")
        self.tip = self.change(self.clone, "feature.txt", "the feature\n", "add the feature")
        self.git("-C", str(self.clone), "push", "-u", "origin", "topic")
        self.git("-C", str(self.clone), "switch", "main")
        self.git("clone", "-b", "main", str(self.remote), str(self.other))

    def squash_merge(self, fetch: bool = True) -> str:
        """Squash-merge the remote topic into main from the other clone and delete the remote branch; the squash
        commit, which the clone fetches unless `fetch` is false."""
        self.git("-C", str(self.other), "fetch", "origin")
        self.git("-C", str(self.other), "merge", "--squash", "origin/topic")
        squash = self.commit(self.other, "Squash-merge topic")
        self.git("-C", str(self.other), "push", "origin", "main")
        self.git("-C", str(self.other), "push", "origin", "--delete", "topic")
        if fetch:
            self.git("-C", str(self.clone), "fetch", "--prune", "origin")
        return squash

    def reword_tip(self) -> str:
        """Rewrite the topic's tip without changing its content: the same tree and parent, another message."""
        rewritten = self.git("-C", str(self.clone), "commit-tree", "topic^{tree}", "-p", "topic^", "-m", "reworded")
        self.git("-C", str(self.clone), "update-ref", "refs/heads/topic", rewritten)
        return rewritten

    def extend_topic(self) -> str:
        """A later commit on the topic with a real change."""
        self.git("-C", str(self.clone), "switch", "topic")
        extended = self.change(self.clone, "feature.txt", "the feature, extended\n", "extend the feature")
        self.git("-C", str(self.clone), "switch", "main")
        return extended

    def same_change(self, tip: str, merge_commit: str, base_ref: str = BASE_REF) -> bool:
        return change_match(str(self.clone), base_ref)(tip, merge_commit)


class PatchMatchTests(SquashFixture):
    def test_a_tip_rewritten_without_changing_its_content_matches_the_squash_commit(self) -> None:
        squash = self.squash_merge()
        rewritten = self.reword_tip()
        self.assertNotEqual(self.tip, rewritten)
        self.assertTrue(self.same_change(rewritten, squash))
        self.assertTrue(self.same_change(self.tip, squash), "the tip the pull request ended at matches too")

    def test_a_tip_rebased_onto_a_moved_main_matches_the_squash_commit(self) -> None:
        self.change(self.other, "elsewhere.txt", "unrelated\n", "main moves on")
        self.git("-C", str(self.other), "push", "origin", "main")
        self.git("-C", str(self.clone), "fetch", "origin")
        self.git("-C", str(self.clone), "switch", "topic")
        self.git("-C", str(self.clone), "rebase", "origin/main")
        rebased = self.git("-C", str(self.clone), "rev-parse", "HEAD")
        self.git("-C", str(self.clone), "switch", "main")
        squash = self.squash_merge()
        self.assertNotEqual(self.tip, rebased)
        self.assertTrue(self.same_change(rebased, squash))

    def test_a_later_commit_with_real_changes_does_not_match(self) -> None:
        squash = self.squash_merge()
        self.assertFalse(self.same_change(self.extend_topic(), squash))

    def test_a_squash_commit_the_clone_has_not_fetched_does_not_match(self) -> None:
        squash = self.squash_merge(fetch=False)
        self.assertFalse(self.same_change(self.reword_tip(), squash))

    def test_an_empty_change_on_either_side_never_matches(self) -> None:
        squash = self.squash_merge()
        self.git("-C", str(self.clone), "switch", "-c", "empty", "origin/main")
        empty_tip = self.commit(self.clone, "nothing")
        empty_merge = self.commit(self.clone, "nothing merged")
        self.git("-C", str(self.clone), "switch", "main")
        self.assertFalse(self.same_change(empty_tip, squash))
        self.assertFalse(self.same_change(self.tip, empty_merge))
        self.assertFalse(self.same_change(empty_tip, empty_merge))

    def test_an_unresolvable_base_fails_closed(self) -> None:
        squash = self.squash_merge()
        with self.assertRaisesRegex(QueryError, "cannot find the merge base of refs/remotes/origin/missing"):
            self.same_change(self.tip, squash, "refs/remotes/origin/missing")

    def test_a_tip_sharing_no_history_with_the_base_is_no_match(self) -> None:
        squash = self.squash_merge()
        self.git("-C", str(self.clone), "checkout", "--orphan", "rooted")
        self.git("-C", str(self.clone), "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", ".")
        for name in self.clone.iterdir():
            if name.is_file():
                name.unlink()
        orphan = self.change(self.clone, "feature.txt", "the feature\n", "the feature, rooted elsewhere")
        self.git("-C", str(self.clone), "switch", "main")
        self.assertFalse(self.same_change(orphan, squash), "no merge base is no match, not a failure")

    def test_the_branch_change_is_read_once_per_tip(self) -> None:
        squash = self.squash_merge()
        fork_point = self.git("-C", str(self.clone), "merge-base", self.BASE_REF, self.tip)
        commands: list[tuple[str, ...]] = []

        def runner(command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None) -> GitResult:
            for verb in ("merge-base", "diff-tree"):
                if verb in command:
                    commands.append(tuple(command[command.index(verb) :]))
            return subprocess_runner(command, timeout, input_bytes=input_bytes)

        same_change = change_match(str(self.clone), self.BASE_REF, GitClient(runner, input_runner=runner))
        self.assertTrue(same_change(self.tip, squash))
        self.assertTrue(same_change(self.tip, squash))
        self.assertEqual(
            [
                ("merge-base", self.BASE_REF, self.tip),
                ("diff-tree", fork_point, self.tip),
                ("diff-tree", f"{squash}^", squash),
                ("diff-tree", f"{squash}^", squash),
            ],
            [(command[0], *command[-2:]) for command in commands],
        )

    def test_the_proof_holds_whatever_diff_settings_the_user_has(self) -> None:
        squash = self.squash_merge()
        rewritten = self.reword_tip()
        # Each setting with what it makes git diff print: escape codes, or the external program's output for a patch.
        settings = (("color.diff", "always", "\x1b["), ("diff.external", "echo external", "external feature.txt"))
        for key, value, printed in settings:
            with self.subTest(setting=key):
                self.git("-C", str(self.clone), "config", key, value)
                try:
                    self.assertIn(printed, self.git("-C", str(self.clone), "diff", f"{squash}^", squash))
                    self.assertTrue(self.same_change(rewritten, squash))
                    self.assertTrue(self.same_change(self.tip, squash))
                finally:
                    self.git("-C", str(self.clone), "config", "--unset", key)


DEMO = b'def f(flag):\n    if flag:\n        return 1\n    return 2\n\n\nGREETING = "hello world"\n'
# Later changes to the demo that are whitespace only and change what it holds; git patch-id ignores each.
WHITESPACE_ONLY = {
    "indentation that moves a statement under the if": DEMO.replace(b"\n    return 2\n", b"\n        return 2\n"),
    "whitespace inside a string": DEMO.replace(b"hello world", b"hello  world"),
    "a line ending": DEMO.replace(b"return 1\n", b"return 1\r\n"),
}


class WhitespaceFixture(SquashFixture):
    """The topic also adds a Python file and is squash-merged; a test then amends the topic's tip, as a later commit
    whose only change is whitespace leaves it."""

    def setUp(self) -> None:
        super().setUp()
        self.git("-C", str(self.clone), "switch", "topic")
        self.tip = self.write_demo(DEMO)
        self.git("-C", str(self.clone), "push", "origin", "topic")
        self.git("-C", str(self.clone), "switch", "main")
        self.squash = self.squash_merge()

    def write_demo(self, content: bytes, *options: str) -> str:
        """A commit on the checked-out branch that writes `content` to demo.py byte for byte."""
        (self.clone / "demo.py").write_bytes(content)
        self.git("-C", str(self.clone), "add", "demo.py")
        self.git("-C", str(self.clone), "commit", *options, "-m", "add the demo")
        return self.git("-C", str(self.clone), "rev-parse", "HEAD")

    def amend(self, content: bytes) -> str:
        """Reset the topic to the tip the pull request ended at and amend it so demo.py holds `content`."""
        self.git("-C", str(self.clone), "switch", "-C", "topic", self.tip)
        amended = self.write_demo(content, "--amend")
        self.git("-C", str(self.clone), "switch", "main")
        return amended

    def stable_patch_id(self, start: str, end: str) -> str:
        diff = subprocess.run(
            ["git", "-C", str(self.clone), "diff", "--no-color", "--no-ext-diff", start, end],
            capture_output=True,
            check=True,
        )
        ids = subprocess.run(
            ["git", "-C", str(self.clone), "patch-id", "--stable"], input=diff.stdout, capture_output=True, check=True
        )
        return ids.stdout.decode("ascii").split()[0]


DEMO_ENTRY = b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0demo.py\0"
NOTES_ENTRY = b":000000 100644 " + b"0" * 40 + b" " + b"3" * 40 + b" A\0notes/a b.txt\0"


class ChangeIdentityTests(GitTestCase):
    def test_every_file_with_its_content_before_and_after_counts_and_only_their_order_is_left_out(self) -> None:
        listing = DEMO_ENTRY + NOTES_ENTRY
        self.assertEqual(change_identity(listing), change_identity(NOTES_ENTRY + DEMO_ENTRY), "order is left out")
        for changed in (
            DEMO_ENTRY.replace(b"1" * 40, b"4" * 40) + NOTES_ENTRY,
            DEMO_ENTRY.replace(b"2" * 40, b"4" * 40) + NOTES_ENTRY,
            DEMO_ENTRY.replace(b":100644 100644", b":100644 100755") + NOTES_ENTRY,
            DEMO_ENTRY.replace(b"demo.py", b"other.py") + NOTES_ENTRY,
            DEMO_ENTRY,
        ):
            with self.subTest(changed=changed):
                self.assertNotEqual(change_identity(listing), change_identity(changed))
        self.assertIsNone(change_identity(b""), "an empty listing is no change")

    def test_a_listing_in_any_other_shape_fails_closed(self) -> None:
        for listing in (
            DEMO_ENTRY[:-1],
            DEMO_ENTRY + b"stray",
            DEMO_ENTRY.replace(b"\0demo.py\0", b"\0\0"),
            DEMO_ENTRY.replace(b"1" * 40, b"1" * 7),
            b"diff --git a/demo.py b/demo.py\n@@ -1 +1 @@\n-old\n+new\n",
        ):
            with self.subTest(listing=listing), self.assertRaisesRegex(QueryError, "unexpected shape"):
                change_identity(listing)


class WhitespaceTests(WhitespaceFixture):
    def test_a_tip_whose_only_later_change_is_whitespace_does_not_match(self) -> None:
        self.assertTrue(self.same_change(self.tip, self.squash), "the tip the pull request ended at matches")
        landed = self.stable_patch_id(f"{self.squash}^", self.squash)
        for variant, content in WHITESPACE_ONLY.items():
            with self.subTest(variant=variant):
                amended = self.amend(content)
                fork_point = self.git("-C", str(self.clone), "merge-base", self.BASE_REF, amended)
                self.assertEqual(landed, self.stable_patch_id(fork_point, amended), "patch-id cannot tell them apart")
                self.assertFalse(self.same_change(amended, self.squash))

    def test_a_branch_whose_only_later_change_is_whitespace_stays_unmatched(self) -> None:
        def state() -> str:
            head, upstream = branch_tips(str(self.clone), "topic")
            listed = listing(dict(pull("MERGED", self.tip), mergeCommit={"oid": self.squash}))
            run = router(listed, history((self.tip, BASE)))
            return classify("owner/repo", "topic", head, upstream, run, fetched_bases(str(self.clone)), "main")

        self.reword_tip()
        self.assertEqual("MERGED", state(), "a tip reworded without changing its content is merged")
        for variant, content in WHITESPACE_ONLY.items():
            with self.subTest(variant=variant):
                self.amend(content)
                self.assertEqual("UNMATCHED", state())


def numbered(*lines: str) -> bytes:
    return "".join(f"{line}\n" for line in lines).encode("utf-8")


# The same edit at two places whose surrounding lines differ: retries is 1 on lines 2 and 7.
SPREAD = numbered("name = 'demo'", "retries = 1", "color = 'blue'", "size = 3", "", "", "retries = 1", "mode = 'fast'")
# The same edit at two places within one function, each with the same three lines before and after it.
REPEATED_BLOCK = numbered(
    "def run():",
    *("    step()", "    step()", "    step()", "    count = 7", "    step()", "    step()", "    step()"),
    *("    log('halfway')", "    log('still going')", "    log('nearly there')"),
    *("    step()", "    step()", "    step()", "    count = 7", "    step()", "    step()", "    step()"),
)
# The same edit in two functions whose bodies are the same, so only each hunk's function context tells them apart.
TWIN_FUNCTIONS = numbered(
    *("def first():", "    value = 1", "    value = 1", "    value = 1", "    value = 7"),
    *("    value = 1", "    value = 1", "    value = 1", "", ""),
    *("def second():", "    value = 1", "    value = 1", "    value = 1", "    value = 7"),
    *("    value = 1", "    value = 1", "    value = 1"),
)


def edit_at(content: bytes, old: bytes, new: bytes, occurrence: int) -> bytes:
    """`content` with the `occurrence`th (from 0) of `old` replaced by `new`."""
    parts = content.split(old)
    return old.join(parts[: occurrence + 1]) + new + old.join(parts[occurrence + 1 :])


class MovedEditTests(SquashFixture):
    """main holds a module; a branch off it changes one place in it and is squash-merged, and its local tip is then
    amended to make the same edit at another place instead, so its work never landed."""

    def land(self, before: bytes, landed: bytes) -> tuple[str, str]:
        """Commit `before` as the module on main, then squash-merge a branch `moved` that makes it `landed`: the tip the
        pull request ended at and the squash commit."""
        (self.clone / "module.py").write_bytes(before)
        self.git("-C", str(self.clone), "add", "module.py")
        self.commit(self.clone, "add the module")
        self.git("-C", str(self.clone), "push", "origin", "main")
        self.git("-C", str(self.clone), "switch", "-c", "moved")
        (self.clone / "module.py").write_bytes(landed)
        self.git("-C", str(self.clone), "commit", "-am", "change the module")
        tip = self.git("-C", str(self.clone), "rev-parse", "HEAD")
        self.git("-C", str(self.clone), "push", "-u", "origin", "moved")
        self.git("-C", str(self.other), "fetch", "origin")
        self.git("-C", str(self.other), "merge", "--ff-only", "origin/main")
        self.git("-C", str(self.other), "merge", "--squash", "origin/moved")
        squash = self.commit(self.other, "Squash-merge moved")
        self.git("-C", str(self.other), "push", "origin", "main")
        self.git("-C", str(self.other), "push", "origin", "--delete", "moved")
        self.git("-C", str(self.clone), "fetch", "--prune", "origin")
        self.git("-C", str(self.clone), "switch", "main")
        return tip, squash

    def moved_edit(self, before: bytes, landed: bytes, moved: bytes) -> tuple[str, str, str]:
        """The tip the pull request ended at, the squash commit, and the tip amended to make the module `moved`."""
        tip, squash = self.land(before, landed)
        self.git("-C", str(self.clone), "switch", "moved")
        (self.clone / "module.py").write_bytes(moved)
        self.git("-C", str(self.clone), "commit", "--amend", "-am", "change the module elsewhere")
        amended = self.git("-C", str(self.clone), "rev-parse", "HEAD")
        self.git("-C", str(self.clone), "switch", "main")
        return tip, squash, amended

    def state(self, tip: str, squash: str) -> str:
        head, upstream = branch_tips(str(self.clone), "moved")
        run = router(listing(dict(pull("MERGED", tip), mergeCommit={"oid": squash})), history((tip, BASE)))
        return classify("owner/repo", "moved", head, upstream, run, fetched_bases(str(self.clone)), "main")

    def hunk(self, start: str, end: str) -> str:
        """The first hunk header of git's patch from start to end, asking for three lines of context."""
        patch = self.git("-C", str(self.clone), "diff-tree", "-r", "-p", "--unified=3", start, end)
        return next(line for line in patch.splitlines() if line.startswith("@@"))

    def test_the_same_edit_among_other_lines_stays_unmatched_whatever_git_diff_opts_says(self) -> None:
        tip, squash, amended = self.moved_edit(
            SPREAD,
            edit_at(SPREAD, b"retries = 1", b"retries = 5", 0),
            edit_at(SPREAD, b"retries = 1", b"retries = 5", 1),
        )
        fork_point = self.git("-C", str(self.clone), "merge-base", self.BASE_REF, amended)
        self.assertTrue(self.same_change(tip, squash), "the tip the pull request ended at matches")
        with mock.patch.dict(os.environ, {"GIT_DIFF_OPTS": "--unified=0"}):
            self.assertEqual(
                " -7 +7 ", self.hunk(fork_point, amended).split("@@")[1], "the variable strips the context"
            )
            self.assertEqual(" -2 +2 ", self.hunk(f"{squash}^", squash).split("@@")[1])
            self.assertFalse(self.same_change(amended, squash))
            self.assertTrue(self.same_change(tip, squash))
            self.assertEqual("UNMATCHED", self.state(tip, squash))
        self.assertEqual("UNMATCHED", self.state(tip, squash))

    def test_the_same_edit_with_the_same_lines_around_it_stays_unmatched(self) -> None:
        tip, squash, amended = self.moved_edit(
            REPEATED_BLOCK,
            edit_at(REPEATED_BLOCK, b"count = 7", b"count = 8", 0),
            edit_at(REPEATED_BLOCK, b"count = 7", b"count = 8", 1),
        )
        fork_point = self.git("-C", str(self.clone), "merge-base", self.BASE_REF, amended)
        landed, local = self.hunk(f"{squash}^", squash), self.hunk(fork_point, amended)
        self.assertNotEqual(landed, local)
        self.assertEqual(landed.split("@@")[2], local.split("@@")[2], "the same function context")
        self.assertFalse(self.same_change(amended, squash))
        self.assertEqual("UNMATCHED", self.state(tip, squash))

    def test_the_same_edit_in_another_function_with_the_same_body_stays_unmatched(self) -> None:
        tip, squash, amended = self.moved_edit(
            TWIN_FUNCTIONS,
            edit_at(TWIN_FUNCTIONS, b"value = 7", b"value = 8", 0),
            edit_at(TWIN_FUNCTIONS, b"value = 7", b"value = 8", 1),
        )
        fork_point = self.git("-C", str(self.clone), "merge-base", self.BASE_REF, amended)
        self.assertEqual(" def first():", self.hunk(f"{squash}^", squash).split("@@")[2])
        self.assertEqual(" def second():", self.hunk(fork_point, amended).split("@@")[2])
        self.assertFalse(self.same_change(amended, squash))
        self.assertEqual("UNMATCHED", self.state(tip, squash))

    def test_a_file_the_base_changed_elsewhere_before_the_merge_keeps_a_rewritten_tip(self) -> None:
        tip, squash = self.land(SPREAD, edit_at(SPREAD, b"retries = 1", b"retries = 5", 0))
        self.assertTrue(self.same_change(tip, squash))
        # A second pull request changes the module's last line on main, and the branch's change lands after it.
        self.git("-C", str(self.other), "reset", "--hard", f"{squash}^")
        (self.other / "module.py").write_bytes(SPREAD.replace(b"fast", b"slow"))
        self.git("-C", str(self.other), "commit", "-am", "slow the demo")
        self.git("-C", str(self.other), "cherry-pick", "-x", squash)
        later = self.git("-C", str(self.other), "rev-parse", "HEAD")
        self.git("-C", str(self.other), "push", "--force", "origin", "main")
        self.git("-C", str(self.clone), "fetch", "origin")
        rewritten = self.git("-C", str(self.clone), "commit-tree", f"{tip}^{{tree}}", "-p", f"{tip}^", "-m", "x")
        self.assertFalse(self.same_change(rewritten, later), "the module held other bytes before the merge")


class ReleaseBaseFixture(SquashFixture):
    """Beside the topic, a release branch with a release-only commit and another sharing no history with main, each
    with a fix squash-merged into it and the fix's local tip reworded; the clone has fetched every base."""

    def setUp(self) -> None:
        super().setUp()
        self.git("-C", str(self.clone), "switch", "-c", "release/1.0", "main")
        self.change(self.clone, "notes.txt", "release only\n", "release-only notes")
        self.git("-C", str(self.clone), "push", "-u", "origin", "release/1.0")
        self.git("-C", str(self.clone), "checkout", "--orphan", "release/old")
        self.git("-C", str(self.clone), "rm", "-r", "--cached", "--quiet", "--ignore-unmatch", ".")
        for path in self.clone.iterdir():
            if path.is_file():
                path.unlink()
        self.change(self.clone, "old.txt", "an older line\n", "the older line's root")
        self.git("-C", str(self.clone), "push", "-u", "origin", "release/old")
        self.fixes = {
            "release/1.0": self.fix_into("release/1.0", "hotfix", "fix.txt"),
            "release/old": self.fix_into("release/old", "oldfix", "oldfix.txt"),
        }
        self.git("-C", str(self.clone), "switch", "main")

    def fix_into(self, base: str, branch: str, name: str) -> tuple[str, str, str]:
        """A fix branch off `base`, squash-merged into `base` by the other clone and deleted remotely, then reworded
        locally: its tip at the pull request, its squash commit, and its reworded local tip."""
        self.git("-C", str(self.clone), "switch", "-c", branch, base)
        tip = self.change(self.clone, name, f"the {branch}\n", f"add the {branch}")
        self.git("-C", str(self.clone), "push", "-u", "origin", branch)
        self.git("-C", str(self.other), "fetch", "origin")
        self.git("-C", str(self.other), "switch", "-c", base, f"origin/{base}")
        self.git("-C", str(self.other), "merge", "--squash", f"origin/{branch}")
        squash = self.commit(self.other, f"Squash-merge {branch}")
        self.git("-C", str(self.other), "push", "origin", base)
        self.git("-C", str(self.other), "push", "origin", "--delete", branch)
        self.git("-C", str(self.clone), "fetch", "--prune", "origin")
        reworded = self.git("-C", str(self.clone), "commit-tree", f"{branch}^{{tree}}", "-p", f"{branch}^", "-m", "x")
        self.git("-C", str(self.clone), "update-ref", f"refs/heads/{branch}", reworded)
        return tip, squash, reworded


class ReleaseBaseTests(ReleaseBaseFixture):
    def test_a_fix_into_a_release_branch_matches_on_its_own_base_and_not_on_main(self) -> None:
        for base, (tip, squash, reworded) in self.fixes.items():
            with self.subTest(base=base):
                self.assertTrue(self.same_change(reworded, squash, f"refs/remotes/origin/{base}"))
                self.assertTrue(self.same_change(tip, squash, f"refs/remotes/origin/{base}"))
                # Shared history: main's diff takes in the release-only commit. No shared history: no merge base.
                self.assertFalse(self.same_change(reworded, squash))

    def test_fetched_bases_resolve_each_base_once_and_give_none_for_a_missing_one(self) -> None:
        resolved: list[str] = []

        def runner(command: Sequence[str], timeout: float, *, input_bytes: bytes | None = None) -> GitResult:
            if "rev-parse" in command:
                resolved.append(command[-1])
            return subprocess_runner(command, timeout, input_bytes=input_bytes)

        bases = fetched_bases(str(self.clone), GitClient(runner, input_runner=runner))
        self.assertIsNone(bases("release/2.0"))
        self.assertIsNone(bases("release/2.0"))
        release = bases("release/1.0")
        self.assertIs(release, bases("release/1.0"))
        self.assertIsInstance(release, BaseTests)
        self.assertIsInstance(bases("release/old"), BaseTests)
        self.assertEqual(
            [f"refs/remotes/origin/{name}^{{commit}}" for name in ("release/2.0", "release/1.0", "release/old")],
            resolved,
        )
        _, squash, reworded = self.fixes["release/1.0"]
        if release is not None:  # the isinstance assertion above holds; this narrows the type for the checker
            self.assertTrue(release.contains(squash))
            self.assertFalse(release.contains(reworded))
            self.assertTrue(release.same_change(reworded, squash))
        self.assertIsNone(bases("hotfix"), "a branch origin no longer has is not a fetched base")
        self.assertIsNone(bases("a..b"))

    def test_a_pull_request_merged_into_a_release_branch_classifies_the_branch_merged(self) -> None:
        for base, branch in (("release/1.0", "hotfix"), ("release/old", "oldfix")):
            tip, squash, _ = self.fixes[base]
            head, upstream = branch_tips(str(self.clone), branch)
            listed = listing(dict(pull("MERGED", tip, base=base), number=7, mergeCommit={"oid": squash}))
            run = router(listed, history((tip, BASE)))
            with self.subTest(base=base):
                self.assertIsNone(upstream)
                self.assertEqual(
                    "MERGED",
                    classify("owner/repo", branch, head, upstream, run, fetched_bases(str(self.clone)), "main"),
                )
                # The base must be fetched: judged against main alone, the same pull request proves nothing.
                self.assertEqual(
                    "UNMATCHED",
                    classify("owner/repo", branch, head, upstream, run, main_only_fetched(self.clone), "main"),
                )
                unnamed = router(
                    listing(dict(pull("MERGED", tip, base=None), number=7, mergeCommit={"oid": squash})),
                    history((tip, BASE)),
                )
                bases = fetched_bases(str(self.clone))
                self.assertEqual("MERGED", classify("owner/repo", branch, head, upstream, unnamed, bases, base))
                self.assertEqual("UNMATCHED", classify("owner/repo", branch, head, upstream, unnamed, bases, "main"))


def main_only_fetched(clone: Path) -> Bases:
    """The fetched main's tests and no other base's: how every pull request was judged before bases were per pull."""
    main = fetched_bases(str(clone))("main")
    return {"main": main}.get


class BranchClassificationTests(GitFixture):
    """The branch's tips, the base's history, and its pull requests together, as repo_cleanup's
    pull_state reads them."""

    def state(self, run, with_bases: bool = False) -> str:
        head, upstream = branch_tips(str(self.clone), "topic")
        bases = fetched_bases(str(self.clone)) if with_bases else None
        return classify("owner/repo", "topic", head, upstream, run, bases, "main")

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
        self.assertEqual("MERGED", self.state(run, with_bases=True))
        self.assertEqual("UNMATCHED", self.state(run))

    def test_reused_branch_with_newer_remote_work_is_not_stale(self) -> None:
        tip = self.tracking_branch()
        self.push_newer_remote_work()
        self.assertEqual("UNMATCHED", self.state(responder(listing(pull("MERGED", tip)))))

    def test_query_failure_fails_closed(self) -> None:
        self.tracking_branch()
        with self.assertRaisesRegex(QueryError, re.escape("gh pr list failed (1): HTTP 500")):
            self.state(responder("", returncode=1, stderr="HTTP 500"))


class RewrittenBranchClassificationTests(SquashFixture):
    """A squash-merged branch whose local tip was rewritten after the pull request ended, as pull_state reads it."""

    def state(self, squash: str, with_change: bool = True) -> str:
        head, upstream = branch_tips(str(self.clone), "topic")
        run = router(listing(dict(pull("MERGED", self.tip), mergeCommit={"oid": squash})), history((self.tip, BASE)))
        bases: Bases = fetched_bases(str(self.clone))
        if not with_change:
            bases = {"main": BaseTests(base_contains(str(self.clone), self.BASE_REF), never)}.get
        return classify("owner/repo", "topic", head, upstream, run, bases, "main")

    def test_a_rewritten_tip_with_the_merged_content_is_merged(self) -> None:
        squash = self.squash_merge()
        self.reword_tip()
        self.assertEqual("MERGED", self.state(squash))
        self.assertEqual("UNMATCHED", self.state(squash, with_change=False))

    def test_a_later_commit_with_real_changes_leaves_the_branch_unmatched(self) -> None:
        squash = self.squash_merge()
        self.extend_topic()
        self.assertEqual("UNMATCHED", self.state(squash))

    def test_a_squash_commit_the_clone_has_not_fetched_leaves_the_branch_unmatched(self) -> None:
        # The remote branch is gone before the squash lands, so the clone sees no upstream and no squash commit.
        self.git("-C", str(self.other), "push", "origin", "--delete", "topic")
        self.git("-C", str(self.clone), "fetch", "--prune", "origin")
        self.git("-C", str(self.other), "fetch", "origin")
        # -x changes the message, so the cherry-pick is never the tip's own commit again, as a run within the same
        # second as the tip's commit would otherwise make it: same tree, parent, author, message, and committer date.
        self.git("-C", str(self.other), "cherry-pick", "-x", self.tip)
        squash = self.git("-C", str(self.other), "rev-parse", "HEAD")
        self.assertNotEqual(self.tip, squash)
        self.git("-C", str(self.other), "push", "origin", "main")
        self.reword_tip()
        self.assertEqual("UNMATCHED", self.state(squash))


class TrackingBaseClassificationTests(SquashFixture):
    """A topic that merged main in after main moved on, was squash-merged into main, and tracks main itself, so its
    fetched upstream is main's tip: several commits ahead of main and behind it, with no work of its own left."""

    def setUp(self) -> None:
        super().setUp()
        self.change(self.other, "elsewhere.txt", "unrelated\n", "main moves on")
        self.git("-C", str(self.other), "push", "origin", "main")
        self.git("-C", str(self.clone), "fetch", "origin")
        self.git("-C", str(self.clone), "switch", "topic")
        self.git("-C", str(self.clone), "merge", "--no-ff", "-m", "Merge branch 'main' into topic", "origin/main")
        self.merged_in = self.git("-C", str(self.clone), "rev-parse", "HEAD")
        self.git("-C", str(self.clone), "push", "origin", "topic")
        self.git("-C", str(self.clone), "switch", "main")
        self.squash = self.squash_merge()
        rewritten = self.git(
            "-C", str(self.clone), "commit-tree", "topic^{tree}", "-p", "topic^1", "-p", "topic^2", "-m", "reworded"
        )
        self.git("-C", str(self.clone), "update-ref", "refs/heads/topic", rewritten)
        self.git("-C", str(self.clone), "branch", "--set-upstream-to=origin/main", "topic")

    def state(self, bases: Bases | None) -> str:
        head, upstream = branch_tips(str(self.clone), "topic")
        self.assertEqual(self.git("-C", str(self.clone), "rev-parse", "refs/remotes/origin/main"), upstream)
        run = router(
            listing(dict(pull("MERGED", self.merged_in), number=7, mergeCommit={"oid": self.squash})),
            history((self.tip, BASE), (self.merged_in, self.tip, OLD)),
        )
        return classify("owner/repo", "topic", head, upstream, run, bases, "main")

    def test_a_branch_tracking_its_base_is_merged_when_its_whole_change_landed(self) -> None:
        ahead, behind = self.git(
            "-C", str(self.clone), "rev-list", "--left-right", "--count", "topic...refs/remotes/origin/main"
        ).split()
        self.assertEqual(("2", "1"), (ahead, behind), "ahead by the change and the merge, behind by the squash")
        self.assertNotEqual(self.merged_in, branch_tips(str(self.clone), "topic")[0])
        self.assertEqual("MERGED", self.state(fetched_bases(str(self.clone))))
        self.assertEqual("UNMATCHED", self.state(None), "without a base to judge the upstream by, it is newer work")

    def test_a_later_commit_with_real_changes_leaves_the_branch_unmatched(self) -> None:
        self.extend_topic()
        self.assertEqual("UNMATCHED", self.state(fetched_bases(str(self.clone))))

    def test_a_tip_the_pull_request_ended_at_is_merged_although_the_branch_tracks_its_base(self) -> None:
        self.git("-C", str(self.clone), "update-ref", "refs/heads/topic", self.merged_in)
        self.assertEqual("MERGED", self.state(fetched_bases(str(self.clone))))
        self.assertEqual("UNMATCHED", self.state(None))


if __name__ == "__main__":
    unittest.main()
