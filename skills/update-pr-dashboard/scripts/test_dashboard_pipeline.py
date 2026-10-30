from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

import dashboard_pipeline as tp
import review_archive
import review_config
import test_review_fixture as review_fixture
from github_client import CommandResult, GitHubError, GitHubErrorKind
from pr_change import ChangeDetector
from review_archive import commit_record
from review_config import load_config, write_config
from review_flags import add_flag
from review_github import GitHubClient
from review_records import ReviewRecord, build_record, validate_adapter_result, validate_record_pair
from update_pr_dashboard import update_dashboard_rows

SCRIPT_DIRECTORY = Path(__file__).resolve().parent

HEAD = "b" * 40
OLD_HEAD = "a" * 40
USER_REVIEWED = "c" * 40
START = "<!-- code-review-pr-tracker:start -->"
END = "<!-- code-review-pr-tracker:end -->"


def pull_node(number: int, **changes: Any) -> dict[str, Any]:
    node = {
        "number": number,
        "url": f"https://github.com/example/one/pull/{number}",
        "title": f"Change {number}",
        "isDraft": False,
        "baseRefName": "main",
        "headRefOid": HEAD,
        "updatedAt": "2026-03-01T00:00:00Z",
        "reviewDecision": None,
        "author": {"login": "ada", "name": "Ada Lovelace"},
        "reviewRequests": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{"requestedReviewer": {"login": "reviewer"}}, {"requestedReviewer": {}}],
        },
        "participants": {"pageInfo": {"hasNextPage": False}, "nodes": [{"login": "ada"}]},
        "reviews": {"nodes": []},
    }
    node.update(changes)
    return node


def connection_page(connection: str, nodes: list[dict[str, Any]], cursor: str | None = None) -> str:
    """One page of a pull request's review requests or participants, as the follow-up query reads it."""
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return json.dumps({"data": {"repository": {"pullRequest": {connection: {"pageInfo": info, "nodes": nodes}}}}})


def page(nodes: list[dict[str, Any]], cursor: str | None = None) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                        "nodes": nodes,
                    }
                }
            }
        }
    )


def searches(call: list[str]) -> bool:
    """Whether a gh call is a pull-request search."""
    return any(argument.startswith("q=") for argument in call)


def search_page(repositories: list[str], cursor: str | None = None) -> str:
    """One page of a pull-request search, a pull request in each repository named."""
    nodes = [{"repository": {"nameWithOwner": repository}} for repository in repositories]
    info = {"hasNextPage": cursor is not None, "endCursor": cursor}
    return json.dumps({"data": {"search": {"pageInfo": info, "nodes": nodes}}})


class FakeGitHub:
    """Serves the GraphQL pages, authenticated user, and comparison calls the tracker pipeline makes."""

    def __init__(self) -> None:
        self.pages: dict[str, list[str]] = {}
        self.failures: dict[str, tuple[str, str]] = {}
        # The follow-up pages of one pull request's connection, by (repository, number, connection), and the error
        # stderr a connection's follow-up fails with.
        self.connection_pages: dict[tuple[str, int, str], list[str]] = {}
        self.connection_failures: dict[tuple[str, int, str], str] = {}
        # The exit code and stderr of a failed authenticated-user lookup.
        self.user_failure: tuple[int, str] | None = None
        self.comparisons: dict[tuple[str, str], str] = {}
        # Raised, as a failed gh call would be, by every ancestry comparison, or by every other comparison and tree.
        self.ancestry_failure: GitHubError | None = None
        self.change_failure: GitHubError | None = None
        # The pages of one pull-request search, by its query, each one repository name per pull request, and the stderr
        # every search fails with; a search not served finds nothing.
        self.searches: dict[str, list[str]] = {}
        self.search_failure: str | None = None
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[:3] == ["gh", "api", "graphql"]:
            fields = dict(value.split("=", 1) for value in arguments[4::2])
            repository = f"{fields.get('owner')}/{fields.get('name')}"
            index = int(fields["after"].removeprefix("cursor-")) if "after" in fields else 0
            if "q" in fields:
                if self.search_failure is not None:
                    return CommandResult(1, "", self.search_failure)
                return CommandResult(0, self.searches.get(fields["q"], [search_page([])])[index], "")
            if "number" in fields:
                connection = "participants" if "participants(" in fields["query"] else "reviewRequests"
                key = (repository, int(fields["number"]), connection)
                if key in self.connection_failures:
                    return CommandResult(1, "", self.connection_failures[key])
                return CommandResult(0, self.connection_pages[key][index], "")
            if repository in self.failures:
                return CommandResult(1, "", self.failures[repository][1])
            return CommandResult(0, self.pages[repository][index], "")
        if arguments[-1] == "user":
            if self.user_failure is not None:
                return CommandResult(self.user_failure[0], "", self.user_failure[1])
            return CommandResult(0, json.dumps({"login": "reviewer"}), "")
        if arguments[-1].endswith("?per_page=1"):
            # An ancestry comparison between two commits; one not served cannot be compared.
            commits = arguments[-1].removesuffix("?per_page=1").rsplit("/compare/", 1)[1]
            earlier, _, later = commits.partition("...")
            status = self.comparisons.get((earlier, later))
            if self.ancestry_failure is not None:
                raise self.ancestry_failure
            if status:
                return CommandResult(0, json.dumps({"status": status}), "")
        elif self.change_failure is not None:
            raise self.change_failure
        # Comparisons and trees are unavailable, so any changed head is "unknown" to the change detector.
        return CommandResult(1, "", "HTTP 404: Not Found")


def archived_record(head: str, number: int = 2) -> ReviewRecord:
    request = {
        "repository": "example/one",
        "pull_number": number,
        "pull_url": f"https://github.com/example/one/pull/{number}",
        "title": f"Change {number}",
        "base_ref": "main",
        "base_sha": "d" * 40,
        "head_sha": head,
        "mode": "initial",
        "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
    }
    result = validate_adapter_result(
        {
            "protocol_version": 1,
            "repository": "example/one",
            "pull_number": number,
            "head_sha": head,
            "summary": "Fixture",
            "reviewer": "fixture",
            "status": "complete",
            "findings": [
                {
                    "candidate_key": "a",
                    "severity": "MUST_FIX",
                    "category": "Correctness",
                    "path": "a.py",
                    "line": 1,
                    "body": "Fix.",
                    "evidence": "Evidence.",
                    "source": "fixture",
                }
            ],
            "prior_dispositions": [],
            "usage": None,
        },
        expected_repository="example/one",
        expected_number=number,
        expected_head_sha=head,
    )
    return build_record(
        request,
        result,
        version=1,
        policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        reviewed_at="2026-03-01T12:00:00+00:00",
    )


class TrackerPipelineFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "tracker root with spaces"
        self.root.mkdir()
        self.archive = self.root / "archive"
        self.dashboard = self.root / "dash board.md"
        self.dashboard.write_text(f"# Mine\n{START}\nold rows\n{END}\nNotes stay.\n", encoding="utf-8")
        self.input = self.root / "work" / "tracker input.json"
        self.github = FakeGitHub()
        self.flags_path = self.root / "flags" / "flags.json"
        self.waits: list[float] = []
        github = GitHubClient(runner=self.github, sleeper=self.waits.append)
        self.services = tp.Services(github=github, flags_path=lambda: self.flags_path)
        self.configure()

    def configure(
        self,
        *,
        login: str | None = "reviewer",
        overrides: dict[str, str] | None = None,
        author_names: dict[str, str] | None = None,
    ) -> None:
        generic = {
            "reviewer": {
                "id": "generic",
                "protocol_version": 1,
                "trusted_ref": None,
                "scope": "generic",
                "manifest_path": None,
            },
            "checkout_path": None,
        }
        self.config_path = self.root / "config.json"
        write_config(
            {
                "schema_version": 1,
                "default_repository_set": "primary",
                "repository_sets": {"primary": ["example/one"], "tracked": ["example/one", "example/two"]},
                "repositories": {"example/one": generic, "example/two": generic},
                "operation_repository_sets": {"update-pr-dashboard": "tracked"},
                "archive_root": str(self.archive),
                "local_mirror_root": None,
                "summary_root": str(self.root / "summaries"),
                "dashboard_file": str(self.dashboard),
                "github_login": login,
                "runtime": "auto",
                "dashboard": {"status_overrides": overrides or {}, "author_names": author_names or {}},
            },
            self.config_path,
        )

    def track(self, *keys: str) -> None:
        """Record `keys` as the pull requests the tracker holds, as the last update would have."""
        self.record.write_text(json.dumps({"schema_version": 1, "pulls": list(keys)}), encoding="utf-8")

    @property
    def record(self) -> Path:
        return self.config_path.parent / "tracker.json"

    def assert_one_fact_per_line(self, out: str) -> None:
        """Every line starts with a line the skill names, and the last is the FAILED line."""
        lines = out.splitlines()
        known = ("REPOSITORY ", "REPOSITORY_FAILED ", "PULL_FAILED ", "FAILED ")
        for line in lines:
            self.assertTrue(line.startswith(known), lines)
        self.assertTrue(lines[-1].startswith("FAILED "), lines)

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = tp.main(["--config", str(self.config_path), *arguments], services=self.services)
        return code, out.getvalue(), err.getvalue()

    def serve_default_pages(self) -> None:
        reviewed = pull_node(
            2,
            author={"login": "bob", "name": "  "},
            reviewDecision="REVIEW_REQUIRED",
            reviews={"nodes": [{"state": "CHANGES_REQUESTED", "commit": {"oid": USER_REVIEWED}}]},
        )
        mine = pull_node(
            3,
            author={"login": "reviewer", "name": None},
            isDraft=True,
            reviewRequests={"pageInfo": {"hasNextPage": False}, "nodes": []},
        )
        self.github.pages["example/one"] = [page([pull_node(1)], "cursor-1"), page([reviewed, mine])]
        self.github.pages["example/two"] = [
            page([pull_node(5, author=None, url="https://github.com/example/two/pull/5")])
        ]


class CollectTests(TrackerPipelineFixture):
    def test_collect_writes_the_validated_input_from_paginated_graphql_and_the_archive(self) -> None:
        commit_record(self.archive, "example/one", 2, archived_record(OLD_HEAD), expected_latest_version=None)
        self.serve_default_pages()
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual(
            ["REPOSITORY example/one pulls=3", "REPOSITORY example/two pulls=1", f"INPUT {self.input}"],
            out.splitlines(),
        )
        graphql = [call for call in self.github.calls if call[:3] == ["gh", "api", "graphql"] and not searches(call)]
        self.assertEqual(3, len(graphql), "one call per page, no per-pull calls")
        self.assertIn("after=cursor-1", graphql[1])
        self.assertIn("login=reviewer", graphql[0])
        self.assertNotIn(["gh", "api", "user"], self.github.calls, "a configured login needs no lookup")
        items = {
            f"{item['repository']}#{item['number']}": item
            for item in json.loads(self.input.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            {
                "repository": "example/one",
                "number": 1,
                "url": "https://github.com/example/one/pull/1",
                "title": "Change 1",
                "author": "ada",
                "author_name": "Ada Lovelace",
                "requested_reviewers": ["reviewer"],
                "participants": ["ada"],
                "draft": False,
                "base_ref": "main",
                "head_sha": HEAD,
                "updated_at": "2026-03-01T00:00:00Z",
                "review_decision": None,
                "user_review_state": None,
                "user_review_sha": None,
                "reviewed_head_sha": None,
                "reviewed_incomplete": False,
                "ai_review": None,
            },
            items["example/one#1"],
        )
        second = items["example/one#2"]
        self.assertIsNone(second["author_name"], "a blank display name is unset")
        self.assertEqual(("CHANGES_REQUESTED", USER_REVIEWED), (second["user_review_state"], second["user_review_sha"]))
        self.assertEqual(OLD_HEAD, second["reviewed_head_sha"])
        self.assertEqual("CHANGES_REQUESTED", second["ai_review"]["verdict"])
        self.assertEqual({"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 0}, second["ai_review"]["counts"])
        self.assertTrue(second["ai_review"]["report"].endswith("review.md"))
        self.assertTrue(items["example/one#3"]["draft"])
        self.assertEqual("ghost", items["example/two#5"]["author"], "a deleted account is GitHub's ghost user")

    def test_collect_writes_its_input_under_a_new_temporary_directory_by_default(self) -> None:
        self.serve_default_pages()
        temporary = self.root / "tmp"
        temporary.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(temporary)):
            code, out, err = self.run_main("collect")
        self.assertEqual(0, code, err)
        last = out.splitlines()[-1]
        self.assertTrue(last.startswith("INPUT "), out)
        input_path = Path(last.removeprefix("INPUT "))
        self.assertEqual(temporary, input_path.parent.parent)
        self.assertTrue(input_path.parent.name.startswith("update-pr-dashboard-input-"), input_path)
        self.assertEqual(4, len(json.loads(input_path.read_text(encoding="utf-8"))))
        code, out, err = self.run_main("update", "--input", str(input_path))
        self.assertEqual(0, code, err)

    def test_collect_refuses_an_input_file_inside_a_skill_tree(self) -> None:
        self.serve_default_pages()
        skills_root = Path(tp.__file__).resolve().parents[2]
        home = self.root / "home"
        targets = [
            skills_root / "update-pr-dashboard" / "input.json",  # beside SKILL.md, as the issue found it
            skills_root / "review-prs" / "input.json",
            home / ".claude" / "skills" / "update-pr-dashboard" / "input.json",
            home / ".agents" / "skills" / "update-pr-dashboard" / "input.json",
        ]
        with mock.patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
            for target in targets:
                self.addCleanup(target.unlink, missing_ok=True)  # if a regression wrote it
                with self.subTest(target=target):
                    code, out, err = self.run_main("collect", "--output", str(target))
                    self.assertEqual((1, ""), (code, err))
                    self.assertEqual(1, len(out.splitlines()), out)
                    self.assertTrue(out.startswith(f"FAILED {target} is inside the skills directory "), out)
                    self.assertFalse(target.exists())
        self.assertEqual([], self.github.calls)

    def test_explicit_repository_and_authenticated_login(self) -> None:
        self.configure(login=None)
        self.serve_default_pages()
        code, out, err = self.run_main("collect", "--repository", "example/two", "--output", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual(["REPOSITORY example/two pulls=1", f"INPUT {self.input}"], out.splitlines())
        self.assertIn(["gh", "api", "user"], self.github.calls)

    def test_a_failed_repository_writes_no_input(self) -> None:
        self.serve_default_pages()
        self.input.parent.mkdir(parents=True)
        self.input.write_text("[]", encoding="utf-8")
        self.github.failures["example/two"] = ("", "GraphQL: Could not resolve to a Repository (repository)")
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((1, ""), (code, err))
        lines = out.splitlines()
        self.assertEqual("REPOSITORY example/one pulls=3", lines[0])
        self.assertTrue(lines[1].startswith("REPOSITORY_FAILED example/two GraphQL: Could not resolve"), lines)
        self.assertEqual(
            "FAILED 1 of 2 repositories could not be collected; no input was written and the dashboard keeps its "
            "previous rows",
            lines[-1],
        )
        self.assertNotIn("INPUT", out)
        self.assertEqual("[]", self.input.read_text(encoding="utf-8"), "the previous input is left untouched")

    def crowded(self) -> None:
        """example/one#1 with a second page of both review requests and participants."""
        crowded = pull_node(
            1,
            reviewRequests={"pageInfo": {"hasNextPage": True}, "nodes": [{"requestedReviewer": {"login": "r0"}}]},
            participants={"pageInfo": {"hasNextPage": True}, "nodes": [{"login": "p0"}]},
        )
        self.github.pages = {"example/one": [page([crowded, pull_node(2)])], "example/two": [page([])]}
        self.github.connection_pages[("example/one", 1, "reviewRequests")] = [
            connection_page(
                "reviewRequests", [{"requestedReviewer": {"login": f"r{n}"}} for n in range(100)], "cursor-1"
            ),
            connection_page(
                "reviewRequests", [{"requestedReviewer": {"login": "reviewer"}}, {"requestedReviewer": {}}]
            ),
        ]
        self.github.connection_pages[("example/one", 1, "participants")] = [
            connection_page("participants", [{"login": f"p{n}"} for n in range(100)], "cursor-1"),
            connection_page("participants", [{"login": "ada"}]),
        ]

    def test_a_pull_request_with_more_than_100_review_requests_or_participants_is_collected_whole(self) -> None:
        self.crowded()
        code, out, err = self.run_main("collect", "--repository", "example/one", "--output", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual(["REPOSITORY example/one pulls=2", f"INPUT {self.input}"], out.splitlines())
        item = json.loads(self.input.read_text(encoding="utf-8"))[0]
        self.assertEqual([f"r{n}" for n in range(100)] + ["reviewer"], item["requested_reviewers"])
        self.assertEqual([f"p{n}" for n in range(100)] + ["ada"], item["participants"])
        follow_ups = [call for call in self.github.calls if "number=1" in call]
        self.assertEqual(4, len(follow_ups), "two pages of each connection, for the crowded pull request alone")
        self.assertTrue(all(call[call.index("number=1") - 1] == "-F" for call in follow_ups), "the number is typed")

    def test_a_failed_follow_up_page_fails_its_repository(self) -> None:
        self.crowded()
        self.github.connection_failures[("example/one", 1, "participants")] = "HTTP 502: Bad Gateway"
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((1, ""), (code, err))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("REPOSITORY_FAILED example/one HTTP 502: Bad Gateway"), lines)
        self.assertTrue(lines[-1].startswith("FAILED 1 of 2 repositories"), lines)
        self.assertFalse(self.input.exists())

    def test_a_multi_line_gh_error_is_printed_on_one_line(self) -> None:
        self.serve_default_pages()
        self.github.failures["example/two"] = ("", "GraphQL: Could not resolve\nREPOSITORY example/three pulls=9")
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((1, ""), (code, err))
        self.assert_one_fact_per_line(out)
        self.assertIn("REPOSITORY_FAILED example/two GraphQL: Could not resolve REPOSITORY example/three pulls=9", out)
        # A sign-in failure stops the collection with GitHub CLI's two-line message as one FAILED line.
        self.configure(login=None)
        self.github.user_failure = (
            4,
            "To get started with GitHub CLI, please run:  gh auth login\n"
            "Alternatively, populate the GH_TOKEN environment variable with a GitHub API authentication token.\n",
        )
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((1, ""), (code, err))
        self.assertEqual(
            [
                "FAILED To get started with GitHub CLI, please run: gh auth login Alternatively, populate the GH_TOKEN "
                "environment variable with a GitHub API authentication token."
            ],
            out.splitlines(),
        )

    def test_active_review_without_a_commit_fails_its_repository(self) -> None:
        orphan = pull_node(1, reviews={"nodes": [{"state": "APPROVED", "commit": None}]})
        self.github.pages["example/one"] = [page([orphan])]
        code, out, _ = self.run_main("collect", "--repository", "example/one", "--output", str(self.input))
        self.assertEqual(1, code)
        self.assertIn("user_review_sha is required for APPROVED", out)

    def test_rate_limit_stops_the_collection(self) -> None:
        for limited in ("example/one", "example/two"):  # first or later repository; both are collected at once
            with self.subTest(limited=limited):
                self.serve_default_pages()
                self.github.failures = {limited: ("", "API rate limit exceeded")}
                code, out, err = self.run_main("collect", "--output", str(self.input))
                self.assertEqual((1, ""), (code, err))
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertTrue(out.startswith("FAILED API rate limit exceeded"), out)
                self.assertFalse(self.input.exists())
                # The limit was waited out with ejryan89-skill-core's bounded backoff before it stopped the collection.
                self.assertEqual([5.0, 10.0, 20.0, 40.0, 80.0], self.waits)
                self.waits.clear()

    def test_a_failed_ancestry_comparison_fails_its_repository(self) -> None:
        review_fixture.commit_fixture(self.archive)
        reviews = [{"state": "COMMENTED", "commit": {"oid": USER_REVIEWED}}]  # not a review's head, so compared
        node = pull_node(12, headRefOid=review_fixture.HEADS[3], reviews={"nodes": reviews})
        self.github.pages = {"example/one": [page([node])], "example/two": [page([])]}
        cases: tuple[tuple[GitHubErrorKind, list[str]], ...] = (
            ("api", ["REPOSITORY_FAILED example/one HTTP 502: Bad Gateway", "REPOSITORY example/two pulls=0"]),
            ("forbidden", ["REPOSITORY_FAILED example/one HTTP 502: Bad Gateway", "REPOSITORY example/two pulls=0"]),
            ("network", []),  # stops the collection at once
            ("timeout", []),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind):
                self.github.ancestry_failure = GitHubError("HTTP 502: Bad Gateway", kind=kind)
                code, out, err = self.run_main("collect", "--output", str(self.input))
                self.assertEqual((1, ""), (code, err))
                summary = (
                    "FAILED 1 of 2 repositories could not be collected; no input was written and the dashboard "
                    "keeps its previous rows"
                    if expected
                    else "FAILED HTTP 502: Bad Gateway"
                )
                self.assertEqual([*expected, summary], out.splitlines())
                self.assertFalse(self.input.exists())

    def serve_searches(self) -> None:
        """Open pull requests of the configured user in tracked and untracked repositories, one search over two
        pages."""
        self.github.searches = {
            "is:pr is:open archived:false author:reviewer": [search_page(["example/one", "Other/Three"])],
            "is:pr is:open archived:false review-requested:reviewer": [
                search_page(["other/four"], "cursor-1"),
                search_page(["example/two"]),
            ],
            "is:pr is:open archived:false reviewed-by:reviewer": [search_page(["other/three"])],
        }

    def test_collect_names_each_repository_outside_the_set_where_the_user_has_an_open_pull_request(self) -> None:
        self.serve_default_pages()
        self.serve_searches()
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            [
                "REPOSITORY example/one pulls=3",
                "REPOSITORY example/two pulls=1",
                "UNTRACKED_REPOSITORY other/four",
                "UNTRACKED_REPOSITORY other/three",
                f"INPUT {self.input}",
            ],
            out.splitlines(),
        )
        self.assertEqual(4, sum(searches(call) for call in self.github.calls), "every page of each search")
        # A named set is the set compared with.
        code, out, err = self.run_main("collect", "--repository-set", "primary", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["UNTRACKED_REPOSITORY example/two", "UNTRACKED_REPOSITORY other/four", "UNTRACKED_REPOSITORY other/three"],
            [line for line in out.splitlines() if line.startswith("UNTRACKED_")],
        )

    def test_collect_of_named_repositories_searches_for_no_untracked_one(self) -> None:
        self.serve_default_pages()
        self.serve_searches()
        code, out, err = self.run_main("collect", "--repository", "example/one", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["REPOSITORY example/one pulls=3", f"INPUT {self.input}"], out.splitlines())
        self.assertFalse(any(searches(call) for call in self.github.calls))

    def test_a_failed_search_for_untracked_repositories_is_reported_without_failing_the_collection(self) -> None:
        self.serve_default_pages()
        self.github.search_failure = "HTTP 422: Validation Failed\nthe users cannot be searched"
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            [
                "REPOSITORY example/one pulls=3",
                "REPOSITORY example/two pulls=1",
                "UNTRACKED_CHECK_FAILED HTTP 422: Validation Failed the users cannot be searched",
                f"INPUT {self.input}",
            ],
            out.splitlines(),
        )
        self.assertEqual(4, len(json.loads(self.input.read_text(encoding="utf-8"))))
        # A failed repository still fails the collection, with its FAILED line last.
        self.github.failures["example/two"] = ("", "HTTP 404: Not Found")
        code, out, _ = self.run_main("collect", "--output", str(self.root / "second.json"))
        self.assertEqual(1, code)
        self.assertIn(
            "UNTRACKED_CHECK_FAILED HTTP 422: Validation Failed the users cannot be searched", out.splitlines()
        )
        self.assertTrue(out.splitlines()[-1].startswith("FAILED 1 of 2 repositories"), out)


class ExitContractTests(TrackerPipelineFixture):
    """The process-level contract: 1 with a last stdout line FAILED for a failure, 2 for usage alone."""

    def execute(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIRECTORY / "dashboard_pipeline.py"),
                "--config",
                str(self.config_path),
                *arguments,
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def test_a_runtime_failure_exits_1_with_failed_on_stdout(self) -> None:
        result = self.execute("update", "--input", str(self.root / "absent.json"))
        self.assertEqual((1, ""), (result.returncode, result.stderr))
        self.assertEqual(1, len(result.stdout.splitlines()), result.stdout)
        self.assertTrue(result.stdout.startswith("FAILED "), result.stdout)
        self.assertIn("absent.json", result.stdout)

    def test_a_usage_error_exits_2(self) -> None:
        for arguments in ((), ("unknown",), ("update",), ("override", "--set"), ("repository",)):
            with self.subTest(arguments=arguments):
                result = self.execute(*arguments)
                self.assertEqual((2, ""), (result.returncode, result.stdout))
                self.assertIn("usage:", result.stderr)


class UpdateTests(TrackerPipelineFixture):
    def collect(self) -> None:
        commit_record(self.archive, "example/one", 2, archived_record(OLD_HEAD), expected_latest_version=None)
        self.serve_default_pages()
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)

    def test_update_takes_every_other_argument_from_the_configuration(self) -> None:
        self.collect()
        self.configure(overrides={"example/two#5": "on hold"}, author_names={"BOB": "Robert Tables"})
        code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
        self.assertEqual(0, code, err)
        self.assertEqual(
            [
                f"UPDATED {self.dashboard} rows=4",
                "GITHUB_CALLS 3",  # pull 2's two commits for the user's review and its reviewed head for the AI's
                "CANDIDATE missing example/one#1",
                "CANDIDATE stale example/one#2",
                "CANDIDATE missing example/one#3",
            ],
            out.splitlines(),
        )
        content = self.dashboard.read_text(encoding="utf-8")
        self.assertTrue(content.startswith(f"# Mine\n{START}\n"))
        self.assertTrue(content.endswith(f"{END}\nNotes stay.\n"))
        self.assertNotIn("old rows", content)
        self.assertIn("[#1 Change 1](https://github.com/example/one/pull/1)", content, "home repository is short")
        self.assertIn("### Awaiting Response (1)", content)
        self.assertIn("| Robert Tables | [#2 Change 2]", content, "configured name replaces a blank profile name")
        self.assertIn("### On Hold (1)", content)
        self.assertIn("[two#5 Change 5]", content)
        self.assertIn(f"vscode://file/{self.config_path.as_posix().replace(' ', '%20')}", content)
        self.assertIn("### My PRs (1)", content)
        missing = {"state": "missing", "reviewed_head_sha": None}
        self.assertEqual(
            {
                "schema_version": 2,
                "pulls": ["example/one#1", "example/one#2", "example/one#3", "example/two#5"],
                "reviews": {
                    "example/one#1": missing,
                    "example/one#2": {"state": "stale", "reviewed_head_sha": OLD_HEAD},
                    "example/one#3": missing,
                    "example/two#5": missing,
                },
            },
            json.loads(self.record.read_text(encoding="utf-8")),
        )

    def test_update_records_every_pull_request_the_user_relates_to_whether_or_not_its_row_shows(self) -> None:
        unrelated = pull_node(7, reviewRequests={"pageInfo": {"hasNextPage": False}, "nodes": []})
        approved = pull_node(8, reviews={"nodes": [{"state": "APPROVED", "commit": {"oid": HEAD}}]})
        self.github.pages["example/one"] = [page([pull_node(1), unrelated, approved])]
        self.github.pages["example/two"] = [page([])]
        self.track("example/one#99")
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "example/one#1")
        self.assertEqual(0, code, err)
        self.assertEqual(f"UPDATED {self.dashboard} rows=0", out.splitlines()[0], "removed and approved rows hide")
        # The record replaces the last one: a removed or approved pull request is still held, an unrelated one never.
        self.assertEqual(
            {"schema_version": 2, "pulls": ["example/one#1", "example/one#8"], "reviews": {}},
            json.loads(self.record.read_text(encoding="utf-8")),
        )

    def test_an_update_that_writes_no_dashboard_records_nothing(self) -> None:
        self.collect()
        self.dashboard.write_text("no markers\n", encoding="utf-8")
        code, out, _ = self.run_main("update", "--input", str(self.input))
        self.assertEqual(1, code)
        self.assertIn("marker", out)
        self.assertFalse(self.record.exists())

    def collect_fixture(self, reviews: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
        """Collect and render example/one#12 of the three-version fixture with the configured user's reviews."""
        node = pull_node(
            12,
            url="https://github.com/example/one/pull/12",
            headRefOid=review_fixture.HEADS[3],
            reviews={"nodes": reviews},
        )
        self.github.pages["example/one"] = [page([node])]
        self.github.pages["example/two"] = [page([])]
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        item = next(item for item in json.loads(self.input.read_text(encoding="utf-8")) if item["number"] == 12)
        code, _, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        row = next(line for line in self.dashboard.read_text(encoding="utf-8").splitlines() if "#12 " in line)
        return item, row

    def test_collect_reads_each_record_once(self) -> None:
        review_fixture.commit_fixture(self.archive)
        reviews = [{"state": "COMMENTED", "commit": {"oid": review_fixture.HEADS[1]}}]
        validate = validate_record_pair
        with mock.patch.object(review_archive, "validate_record_pair", wraps=validate) as read:
            item, _ = self.collect_fixture(reviews)
        self.assertEqual(3, read.call_count, "three versions, each validated once")
        self.assertEqual(review_fixture.HEADS[3], item["reviewed_head_sha"])
        self.assertEqual({"version": 1, "new": 1, "addressed": 1}, item["ai_review"]["since_review"])

    def compares(self) -> list[list[str]]:
        return [call for call in self.github.calls if call[-1].endswith("?per_page=1")]

    def test_the_findings_cell_shows_open_findings_and_what_moved_since_the_users_review(self) -> None:
        review_fixture.commit_fixture(self.archive)
        heads = review_fixture.HEADS
        between = "e" * 40  # a commit after version 2's head and before version 3's
        self.github.comparisons = {
            (heads[3], between): "behind",
            (heads[2], between): "ahead",
            (heads[3], OLD_HEAD): "behind",
            (heads[2], OLD_HEAD): "behind",
            (heads[1], OLD_HEAD): "behind",
        }
        for sha, since, cell, compared in (
            (
                heads[1],
                {"version": 1, "new": 1, "addressed": 1},
                "1M 1S open (1 repeat) · 1 new, 1 addressed since your review",
                0,
            ),
            (between, {"version": 2, "new": 1, "addressed": 0}, "1M 1S open (1 repeat) · 1 new since your review", 2),
            (OLD_HEAD, {"version": 0, "new": 2, "addressed": 0}, "1M 1S open (1 repeat) · 2 new since your review", 3),
            (
                heads[3],
                {"version": 3, "new": 0, "addressed": 0},
                "1M 1S open (1 repeat) · unchanged since your review",
                0,
            ),
            (USER_REVIEWED, None, "1M 1S open (1 repeat) · 1 addressed", 1),  # GitHub cannot compare it
            (None, None, "1M 1S open (1 repeat) · 1 addressed", 0),
        ):
            with self.subTest(sha=sha):
                self.github.calls.clear()
                reviews = [{"state": "COMMENTED", "commit": {"oid": sha}}] if sha else []
                item, row = self.collect_fixture(reviews)
                self.assertEqual(
                    {
                        "open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 1},
                        "repeats": 1,
                        "addressed": 1,
                        "since": 1,
                        "version": 3,
                    },
                    item["ai_review"]["ledger"],
                )
                self.assertEqual((since, 0), (item["ai_review"]["since_review"], item["ai_review"]["flagged"]))
                self.assertIn(f"| Changes Requested | {cell} | [AI Review]", row)
                self.assertEqual(compared, len(self.compares()), "an exact head match needs no comparison")

    def test_a_review_recorded_before_ledgers_renders_from_the_ledger_its_records_compute(self) -> None:
        heads = review_fixture.HEADS

        def record(version: int, keys: list[tuple[str, str, int]]) -> ReviewRecord:
            mode = "initial" if version == 1 else "re-review"
            findings = [
                {
                    "candidate_key": key,
                    "severity": severity,
                    "category": "Correctness",
                    "path": "src/file.py",
                    "line": line,
                    "body": f"Problem {key}.",
                    "evidence": "The added line shows it.",
                    "source": "fixture",
                }
                for key, severity, line in keys
            ]
            result = {
                "protocol_version": 1,
                "repository": "example/one",
                "pull_number": 12,
                "head_sha": heads[version],
                "summary": "Fixture",
                "reviewer": "fixture",
                "status": "complete",
                "findings": findings,
                "prior_dispositions": [],
                "usage": None,
            }
            request = {
                "repository": "example/one",
                "pull_number": 12,
                "pull_url": "https://github.com/example/one/pull/12",
                "title": "Change 12",
                "base_ref": "main",
                "base_sha": "d" * 40,
                "head_sha": heads[version],
                "mode": mode,
                "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
            }
            if mode == "re-review":
                request["scope"] = {
                    "requested": "incremental",
                    "used": "incremental",
                    "reason": "requested",
                    "since_version": version - 1,
                    "files_changed": 1,
                    "files_total": 1,
                    "lines_changed": 1,
                    "lines_total": 3,
                }
            built = build_record(
                request,
                validate_adapter_result(
                    result, expected_repository="example/one", expected_number=12, expected_head_sha=heads[version]
                ),
                version=version,
                policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                reviewed_at=f"2026-03-0{version}T12:00:00+00:00",
            )
            del built["ledger"]
            return built

        commit_record(
            self.archive,
            "example/one",
            12,
            record(1, [("lock", "MUST_FIX", 10), ("null", "SHOULD_FIX", 20)]),
            expected_latest_version=None,
        )
        # Before ledgers, a re-review disposed bare IDs and counted only the findings it raised itself.
        legacy = record(2, [("retry", "SUGGESTION", 30)])
        legacy["prior_dispositions"] = [
            {"finding_id": "F001", "disposition": "still_present", "rationale": "Still there."},
            {"finding_id": "F002", "disposition": "addressed", "rationale": "Fixed."},
        ]
        commit_record(self.archive, "example/one", 12, legacy, expected_latest_version=1)
        for sha, since, cell in (
            (None, None, "1M 1S open · 1 addressed"),
            (heads[1], {"version": 1, "new": 1, "addressed": 1}, "1M 1S open · 1 new, 1 addressed since your review"),
        ):
            with self.subTest(sha=sha):
                reviews = [{"state": "COMMENTED", "commit": {"oid": sha}}] if sha else []
                item, row = self.collect_fixture(reviews)
                self.assertEqual({"MUST_FIX": 0, "SHOULD_FIX": 0, "SUGGESTION": 1}, item["ai_review"]["counts"])
                self.assertEqual(
                    {
                        "open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 1},
                        "repeats": 0,
                        "addressed": 1,
                        "since": 1,
                        "version": 2,
                    },
                    item["ai_review"]["ledger"],
                )
                self.assertEqual((since, 0), (item["ai_review"]["since_review"], item["ai_review"]["flagged"]))
                # The verdict is the record's own, which counted only the suggestion it raised.
                self.assertIn(f"| Approved | {cell} | [AI Review]", row)

    def test_a_legacy_review_shows_its_counts_alone(self) -> None:
        directory = self.archive / "example" / "one" / "pulls" / "12"
        directory.mkdir(parents=True)
        index = {
            "schema_version": 1,
            "kind": "legacy-review-index",
            "repository": "example/one",
            "pull_number": 12,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "reviewed_head_sha": review_fixture.HEADS[3],
            "verdict": "CHANGES_REQUESTED",
            "source_sha256": "0" * 64,
            "source_path": "C:/legacy/review-12.md",
            "source_file_sha256": "0" * 64,
        }
        (directory / "legacy-review.json").write_text(json.dumps(index), encoding="utf-8")
        (directory / "legacy-review.md").write_text(
            "<summary><strong>SHOULD FIX (1)</strong></summary>\n<summary><strong>SUGGESTIONS (2)</strong></summary>\n",
            encoding="utf-8",
        )
        item, row = self.collect_fixture([{"state": "COMMENTED", "commit": {"oid": review_fixture.HEADS[1]}}])
        self.assertEqual(
            {
                "open": {"MUST_FIX": 0, "SHOULD_FIX": 1, "SUGGESTION": 2},
                "addressed": None,
                "since": None,
                "version": None,
            },
            item["ai_review"]["ledger"],
        )
        self.assertNotIn("since_review", item["ai_review"])
        self.assertIn("| Changes Requested | 1H 2S open | [AI Review]", row)

    def test_malformed_legacy_verdicts_collect_and_render_as_missing_without_a_review_result(self) -> None:
        directory = self.archive / "example" / "one" / "pulls" / "12"
        directory.mkdir(parents=True)
        index = {
            "schema_version": 1,
            "kind": "legacy-review-index",
            "repository": "example/one",
            "pull_number": 12,
            "reviewed_at": "2026-01-01T00:00:00+00:00",
            "reviewed_head_sha": review_fixture.HEADS[3],
            "source_sha256": "0" * 64,
            "source_path": "legacy.md",
            "source_file_sha256": "0" * 64,
        }
        report = directory / "legacy-review.md"
        report.write_text("<summary><strong>MUST FIX (1)</strong></summary>\n", encoding="utf-8")
        verdicts: tuple[object, ...] = (None, True, 7, 0.5, [], {})
        path = directory / "legacy-review.json"
        for overrides in ({}, *({"verdict": value} for value in verdicts)):
            with self.subTest(overrides=overrides):
                path.write_text(json.dumps({**index, **overrides}), encoding="utf-8")
                before = path.read_bytes()
                item, row = self.collect_fixture([])
                self.assertIsNone(item["reviewed_head_sha"])
                self.assertFalse(item["reviewed_incomplete"])
                self.assertIsNone(item["ai_review"], "no verdict, counts, ledger or report is manufactured")
                self.assertIn("| - | - | - |", row)
                self.assertNotIn("[AI Review]", row)
                code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
                self.assertEqual((0, ""), (code, err))
                self.assertIn("CANDIDATE missing example/one#12", out.splitlines())
                self.assertEqual(before, path.read_bytes())
                self.assertEqual("<summary><strong>MUST FIX (1)</strong></summary>\n", report.read_text("utf-8"))
        for verdict, label in (
            ("APPROVED", "Approved"),
            ("CHANGES REQUESTED", "Changes Requested"),
            ("CHANGES_REQUESTED", "Changes Requested"),
        ):
            with self.subTest(verdict=verdict):
                path.write_text(json.dumps({**index, "verdict": verdict}), encoding="utf-8")
                item, row = self.collect_fixture([])
                self.assertEqual(review_fixture.HEADS[3], item["reviewed_head_sha"])
                self.assertEqual(verdict.replace(" ", "_"), item["ai_review"]["verdict"])
                self.assertIn(f"| {label} | 1M open | [AI Review]", row)
                rows = update_dashboard_rows(
                    self.input, self.dashboard, "reviewer", detector=ChangeDetector(self.services.github)
                )
                self.assertEqual(["current"], [row.ai_review for row in rows])

    def test_the_findings_cell_counts_flagged_open_findings(self) -> None:
        review_fixture.commit_fixture(self.archive)
        add_flag(
            self.flags_path,
            category="noise",
            body="Handled by the caller.",
            repository="example/one",
            pull_number=12,
            review_version=3,
            finding_id="F001",
        )
        item, row = self.collect_fixture([])
        self.assertEqual(1, item["ai_review"]["flagged"])
        self.assertIn("| 1M 1S open (1 repeat, 1 flagged) · 1 addressed |", row)
        self.flags_path.write_text("{}", encoding="utf-8")
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(
            (
                1,
                "FAILED flag-store must be an object with fields flags, next_id, schema_version, found an object "
                "without them\n",
                "",
            ),
            (code, out, err),
        )

    def test_remove_drops_one_row_and_no_candidates_without_the_flag(self) -> None:
        self.collect()
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "Example/One#1")
        self.assertEqual(0, code, err)
        self.assertEqual([f"UPDATED {self.dashboard} rows=3", "GITHUB_CALLS 3"], out.splitlines())
        self.assertNotIn("example/one/pull/1)", self.dashboard.read_text(encoding="utf-8"))

    def test_a_removal_naming_no_tracked_pull_request_is_refused_before_github_is_asked(self) -> None:
        self.collect()
        before = self.dashboard.read_bytes()
        reason = (
            "names no collected pull request the configured user authored, is asked to review, or takes part in; "
            "check the number, or leave it out if the pull request has closed"
        )
        # A mistyped number, a pull request in another repository, and one the user does not relate to.
        unrelated = pull_node(7, reviewRequests={"pageInfo": {"hasNextPage": False}, "nodes": []})
        self.github.pages["example/two"] = [page([unrelated])]
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        for key in ("example/one#4", "other/one#1", "Example/Two#7"):
            with self.subTest(key=key):
                calls = len(self.github.calls)
                code, out, err = self.run_main(
                    "update", "--input", str(self.input), "--remove", "example/one#1", "--remove", key
                )
                expected = f"FAILED Removal {key.lower()} {reason}"
                self.assertEqual((1, "", [expected]), (code, err, out.splitlines()))
                self.assertEqual(calls, len(self.github.calls), "GitHub is asked nothing")
                self.assertEqual(before, self.dashboard.read_bytes())
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "example/one#1")
        self.assertEqual(0, code, err)
        self.assertNotIn("example/one/pull/1)", self.dashboard.read_text(encoding="utf-8"))

    def test_without_a_configured_login_a_refusal_asks_github_at_most_for_the_login(self) -> None:
        self.collect()
        self.configure(login=None)
        before = self.dashboard.read_bytes()
        # A dashboard a replacement refuses is refused before GitHub is asked anything.
        other = self.root / "other name.md"
        os.link(self.dashboard, other)
        calls = len(self.github.calls)
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "example/one#4")
        self.assertEqual((1, ""), (code, err))
        self.assertTrue(out.startswith(f"FAILED {self.dashboard} has another hard link"), out)
        self.assertEqual([], self.github.calls[calls:])
        other.unlink()
        # A removal naming no tracked pull request needs the user, so only the login lookup runs before it.
        calls = len(self.github.calls)
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "example/one#4")
        self.assertEqual((1, ""), (code, err))
        self.assertTrue(out.startswith("FAILED Removal example/one#4 names no collected pull request"), out)
        self.assertEqual([["gh", "api", "user"]], self.github.calls[calls:])
        self.assertEqual(before, self.dashboard.read_bytes())
        self.assertFalse(self.record.exists())

    def test_a_read_only_dashboard_or_record_is_refused_before_github_is_asked(self) -> None:
        self.collect()
        self.assertEqual(0, self.run_main("update", "--input", str(self.input))[0])
        self.assertTrue(self.record.is_file())
        self.configure(login=None)
        for name, target in (("dashboard", self.dashboard), ("record", self.record)):
            with self.subTest(file=name):
                before = target.read_bytes()
                target.chmod(stat.S_IREAD)
                try:
                    calls = len(self.github.calls)
                    code, out, err = self.run_main("update", "--input", str(self.input))
                    self.assertEqual((1, ""), (code, err))
                    self.assertEqual(
                        [f"FAILED {target} is read-only; make it writable for the update to replace it"],
                        out.splitlines(),
                    )
                    self.assertEqual([], self.github.calls[calls:])
                    self.assertEqual(before, target.read_bytes())
                    self.assertEqual([], [path.name for path in target.parent.iterdir() if path.suffix == ".tmp"])
                finally:
                    target.chmod(stat.S_IREAD | stat.S_IWRITE)

    def test_the_default_configuration_keeps_the_plain_tracker_record_name(self) -> None:
        # Independent literals: review_config names the default file config.json, and its record is tracker.json.
        with mock.patch.dict(os.environ, {"CODE_REVIEW_CONFIG": ""}):
            default = review_config.default_config_path()
        self.assertEqual("config.json", default.name)
        self.assertEqual("tracker.json", tp.tracker_record_path(default).name)
        self.assertEqual("other.tracker.json", tp.tracker_record_path(default.with_name("other.json")).name)

    def test_update_names_each_review_that_went_stale_since_the_last_update(self) -> None:
        # The last update showed example/one#1 with no review, example/one#2 reviewed at its head, and example/one#3
        # already stale.
        reviewed = pull_node(
            2,
            reviewRequests={"pageInfo": {"hasNextPage": False}, "nodes": []},
            participants={"pageInfo": {"hasNextPage": False}, "nodes": [{"login": "reviewer"}]},
        )
        stale = pull_node(3, headRefOid=HEAD)
        for number, head in ((2, HEAD), (3, OLD_HEAD)):
            commit_record(
                self.archive, "example/one", number, archived_record(head, number), expected_latest_version=None
            )
        self.github.pages = {"example/one": [page([pull_node(1), reviewed, stale])], "example/two": [page([])]}
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
        self.assertEqual(0, code, err)
        self.assertEqual(["CANDIDATE missing example/one#1", "CANDIDATE stale example/one#3"], out.splitlines()[2:])
        # An update with nothing moved names nothing.
        code, out, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        self.assertNotIn("STALE_SINCE_LAST_UPDATE", out)
        # During the run example/one#1 was reviewed at its head, which then moved, as did example/one#2's head;
        # example/one#3 stays stale as it was.
        commit_record(self.archive, "example/one", 1, archived_record(OLD_HEAD, 1), expected_latest_version=None)
        moved = "e" * 40
        self.github.pages["example/one"] = [
            page([pull_node(1, headRefOid=moved), {**reviewed, "headRefOid": moved}, stale])
        ]
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        code, out, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual(
            [
                f"UPDATED {self.dashboard} rows=3",
                "GITHUB_CALLS 3",
                "STALE_SINCE_LAST_UPDATE example/one#1",
                "STALE_SINCE_LAST_UPDATE example/one#2",
            ],
            out.splitlines(),
        )

    def test_update_writes_the_file_a_linked_dashboard_leads_to_and_keeps_its_mode(self) -> None:
        self.collect()
        notes = self.root / "synced notes"
        notes.mkdir()
        target = notes / "PullRequests.md"
        target.write_bytes(self.dashboard.read_bytes())
        target.chmod(0o644)
        if sys.platform != "win32":  # a symbolic link to a file needs a privilege Windows rarely grants
            self.dashboard.unlink()
            self.dashboard.symlink_to(target)
        else:
            self.dashboard = target
            self.configure()
        code, out, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual(f"UPDATED {self.dashboard} rows=4", out.splitlines()[0])
        self.assertIn("### My PRs (1)", target.read_text(encoding="utf-8"))
        if sys.platform != "win32":
            self.assertTrue(self.dashboard.is_symlink(), "the link stays a link")
            self.assertEqual(0o644, target.stat().st_mode & 0o777)
        self.assertEqual([target.name], [path.name for path in notes.iterdir()], "no copy is left beside it")

    def test_update_refuses_a_hard_linked_dashboard_before_github_is_asked(self) -> None:
        self.collect()
        other = self.root / "other name.md"
        os.link(self.dashboard, other)
        before = self.dashboard.read_bytes()
        calls = len(self.github.calls)
        code, out, err = self.run_main("update", "--input", str(self.input))
        reason = "has another hard link, which a replacement would leave with the old content; give it one name"
        self.assertEqual((1, "", [f"FAILED {self.dashboard} {reason}"]), (code, err, out.splitlines()))
        self.assertEqual(calls, len(self.github.calls))
        self.assertEqual((before, before), (self.dashboard.read_bytes(), other.read_bytes()))
        self.assertFalse(self.record.exists())

    def test_a_multi_line_comparison_error_is_printed_on_one_line(self) -> None:
        self.collect()
        self.github.change_failure = GitHubError("HTTP 502\nFAILED shaped second line", kind="api")
        code, out, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual((1, ""), (code, err))
        self.assert_one_fact_per_line(out)
        self.assertEqual("PULL_FAILED example/one#2 HTTP 502 FAILED shaped second line", out.splitlines()[0])

    def test_a_failed_comparison_fails_the_update_instead_of_marking_its_review_stale(self) -> None:
        self.collect()
        before = self.dashboard.read_text(encoding="utf-8")
        for kind in ("forbidden", "sso_partial", "api", "malformed", "network", "timeout"):
            with self.subTest(kind=kind):
                self.github.change_failure = GitHubError("GitHub could not compare the commits", kind=kind)
                code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
                self.assertEqual((1, ""), (code, err))
                if kind in ("network", "timeout"):  # every call would fail alike, so the update stops at once
                    expected = ["FAILED GitHub could not compare the commits"]
                else:
                    expected = [
                        "PULL_FAILED example/one#2 GitHub could not compare the commits",
                        "FAILED 1 of 4 pull requests could not be compared; the dashboard keeps its previous rows",
                    ]
                self.assertEqual(expected, out.splitlines())
                self.assertNotIn("CANDIDATE", out)
                self.assertEqual(before, self.dashboard.read_text(encoding="utf-8"))
        self.github.change_failure = GitHubError("HTTP 404: Not Found", kind="not_found")
        code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
        self.assertEqual(0, code, err)
        self.assertIn("CANDIDATE stale example/one#2", out.splitlines(), "a missing commit is still unknown")

    def test_failed_update_leaves_the_dashboard_untouched(self) -> None:
        self.collect()
        original = "No markers here.\n"
        self.dashboard.write_text(original, encoding="utf-8")
        code, out, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual((1, ""), (code, err))
        self.assertEqual(1, len(out.splitlines()), out)
        self.assertTrue(out.startswith("FAILED Dashboard must contain exactly one marker pair"), out)
        self.assertEqual(original, self.dashboard.read_text(encoding="utf-8"))

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # Windows pipes default to a legacy code page; UPDATED names the configured dashboard, whatever its path.
        self.dashboard = self.root / "dash board ← ✓.md"
        self.dashboard.write_text(f"# Mine\n{START}\nold rows\n{END}\n", encoding="utf-8")
        self.configure()
        self.input.parent.mkdir(parents=True)
        self.input.write_text("[]", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIRECTORY / "dashboard_pipeline.py"),
                "--config",
                str(self.config_path),
                "update",
                "--input",
                str(self.input),
            ],
            capture_output=True,
            env={**os.environ, "PYTHONIOENCODING": "cp1252"},
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            [f"UPDATED {self.dashboard} rows=0", "GITHUB_CALLS 0"], result.stdout.decode("utf-8").splitlines()
        )


class ActorFieldTests(unittest.TestCase):
    """The logins and profile names a pull-request node gives: one of the wrong kind is skipped or None, never a
    refusal, since GitHub gives a team or a deleted account no login and many accounts no name."""

    # Each actor of the wrong kind, or whose login is of the wrong kind: none can name the configured user.
    WRONG_ACTORS: tuple[Any, ...] = (
        {},
        {"login": None},
        {"login": ""},
        {"login": " "},
        {"login": 5},
        {"login": True},
        {"login": ["ada"]},
        {"login": {"login": "ada"}},
        None,
        "ada",
        ["ada"],
        7,
    )

    def test_a_login_of_the_wrong_kind_is_skipped(self) -> None:
        for actor in self.WRONG_ACTORS:
            with self.subTest(actor=actor):
                requests = {
                    "pageInfo": {"hasNextPage": False},
                    "nodes": [
                        {"requestedReviewer": {"login": "ada"}},
                        {"requestedReviewer": actor},
                        {"requestedReviewer": {"login": "grace"}},
                    ],
                }
                self.assertEqual(
                    ["ada", "grace"], tp._complete_logins(requests, "requestedReviewer", "review requests")
                )
                participants = {"pageInfo": {"hasNextPage": False}, "nodes": [{"login": "ada"}, actor]}
                self.assertEqual(["ada"], tp._complete_logins(participants, None, "participants"))

    def test_a_profile_name_of_the_wrong_kind_is_none(self) -> None:
        self.assertEqual("Ada Lovelace", tp._profile_name("  Ada Lovelace "))
        for value in (None, "", "  ", 5, True, 1.5, ["Ada"], {"name": "Ada"}):
            with self.subTest(value=value):
                self.assertIsNone(tp._profile_name(value))


class OverrideTests(TrackerPipelineFixture):
    """`override` lists, sets, and clears `dashboard.status_overrides` without GitHub, validating the whole file."""

    def written(self) -> dict[str, Any]:
        value: dict[str, Any] = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsInstance(value, dict)
        return value

    def test_override_alone_lists_each_override_as_the_tracker_matches_it(self) -> None:
        self.configure(overrides={"example/two#5": "on hold", "Example/One#2": " delegated "})
        self.track("example/one#2", "example/two#5")
        code, out, err = self.run_main("override")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["OVERRIDE example/one#2 delegated", "OVERRIDE example/two#5 on hold", "OVERRIDES 2"], out.splitlines()
        )
        self.assertEqual([], self.github.calls)

    def test_the_listing_marks_each_override_the_tracker_no_longer_holds(self) -> None:
        self.configure(overrides={"example/two#5": "on hold", "example/one#2": "delegated"})
        self.track("Example/Two#5")
        code, out, err = self.run_main("override")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["OVERRIDE_UNTRACKED example/one#2 delegated", "OVERRIDE example/two#5 on hold", "OVERRIDES 2"],
            out.splitlines(),
        )
        # Before any update has recorded what the tracker holds, it holds none of them.
        self.record.unlink()
        code, out, err = self.run_main("override")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["OVERRIDE_UNTRACKED example/one#2 delegated", "OVERRIDE_UNTRACKED example/two#5 on hold", "OVERRIDES 2"],
            out.splitlines(),
        )

    def test_set_and_clear_change_only_the_overrides_and_rewrite_the_file_in_normalized_layout(self) -> None:
        raw = {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["example/one"]},
            "repositories": {"example/one": {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}}},
            "archive_root": str(self.archive),
            "summary_root": str(self.root / "summaries"),
            "dashboard_file": str(self.dashboard),
            "dashboard": {"status_overrides": {"example/one#1": "waiting"}, "author_names": {"bob": "Robert"}},
        }
        self.config_path.write_text(json.dumps(raw, indent=4), encoding="utf-8")
        self.track("example/one#2")
        code, out, err = self.run_main("override", "--set", "Example/One#2= on hold ", "--clear", "example/one#1")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["SET example/one#2 on hold", "CLEARED example/one#1", f"WROTE {self.config_path}"], out.splitlines()
        )
        expected = json.loads(json.dumps(raw))
        expected["dashboard"]["status_overrides"] = {"example/one#2": "on hold"}
        self.assertEqual(expected, self.written(), "no default is filled in")
        # Every value is kept, but the layout is the configuration writer's: keys sorted, two-space indentation.
        text = self.config_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith('{\n  "archive_root": '), text)
        self.assertIn(
            '  "dashboard": {\n    "author_names": {\n      "bob": "Robert"\n    },\n'
            '    "status_overrides": {\n      "example/one#2": "on hold"\n    }\n  },\n',
            text,
        )
        self.assertIn('\n  "schema_version": 1,\n  "summary_root": ', text)
        self.assertTrue(text.endswith('"\n}\n'), text)
        self.assertEqual({"example/one#2": "on hold"}, load_config(self.config_path)["dashboard"]["status_overrides"])
        self.assertEqual([], self.github.calls)

    def test_the_skill_names_every_line_the_command_prints(self) -> None:
        # The agent reads only SKILL.md, so a line it does not name is output the agent has to guess at.
        self.configure(overrides={"example/one#2": "waiting", "example/one#4": "on hold"})
        self.track("example/one#3", "example/one#4")
        printed: set[str] = set()
        for arguments in ([], ["--set", "example/one#3=on hold"], ["--clear", "example/one#2"]):
            code, out, err = self.run_main("override", *arguments)
            self.assertEqual((0, ""), (code, err))
            printed |= {line.split()[0] for line in out.splitlines()}
        self.assertEqual({"OVERRIDE", "OVERRIDE_UNTRACKED", "OVERRIDES", "SET", "CLEARED", "WROTE"}, printed)
        skill = (SCRIPT_DIRECTORY.parent / "SKILL.md").read_text(encoding="utf-8")
        for word in sorted(printed):
            with self.subTest(line=word):
                self.assertTrue(f"`{word}`" in skill or f"`{word} <" in skill, word)

    def test_set_replaces_the_override_of_the_same_pull_request_whatever_its_case(self) -> None:
        self.configure(overrides={"Example/One#2": "waiting", "example/two#5": "on hold"})
        self.track("example/one#2")
        code, out, err = self.run_main("override", "--set", "example/one#2=delegated")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["SET example/one#2 delegated", f"WROTE {self.config_path}"], out.splitlines())
        self.assertEqual(
            {"example/one#2": "delegated", "example/two#5": "on hold"}, self.written()["dashboard"]["status_overrides"]
        )

    def test_an_override_whose_pull_request_is_gone_clears_like_any_other(self) -> None:
        self.github.pages["example/one"] = [page([pull_node(1)])]
        self.github.pages["example/two"] = [page([])]
        self.configure(overrides={"example/two#99": "on hold"})
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        calls = len(self.github.calls)
        code, out, err = self.run_main("override", "--clear", "example/two#99")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["CLEARED example/two#99", f"WROTE {self.config_path}"], out.splitlines())
        self.assertEqual(calls, len(self.github.calls), "clearing reads nothing from GitHub")
        self.assertEqual({}, self.written()["dashboard"]["status_overrides"])

    def test_a_set_naming_a_pull_request_the_tracker_does_not_hold_is_refused_and_writes_nothing(self) -> None:
        self.configure(overrides={"example/one#2": "waiting"})
        original = self.config_path.read_bytes()
        held = f"Status override example/one#3 names no pull request the tracker holds in {self.record}"
        unwritten = f"Status override example/one#3 names no tracked pull request: no update has written {self.record}"
        for recorded, key, reason in ((True, "example/one#3", held), (False, "Example/One#3", unwritten)):
            with self.subTest(recorded=recorded):
                if recorded:
                    self.track("example/one#2", "example/two#3", "other/one#3")
                else:
                    self.record.unlink()
                # A mistyped key is refused even beside a valid change, which is not written either.
                code, out, err = self.run_main("override", "--set", f"{key}=on hold", "--clear", "example/one#2")
                self.assertEqual((1, "", [f"FAILED {reason}"]), (code, err, out.splitlines()))
                self.assertEqual(original, self.config_path.read_bytes())
        # Clearing needs no record, and a pull request the record holds is written.
        self.track("example/one#3")
        code, out, err = self.run_main("override", "--set", "example/one#3=on hold", "--clear", "example/one#2")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual({"example/one#3": "on hold"}, self.written()["dashboard"]["status_overrides"])

    def test_a_set_checks_the_record_the_last_update_wrote(self) -> None:
        self.serve_default_pages()
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        code, _, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        calls = len(self.github.calls)
        code, out, _ = self.run_main("override", "--set", "example/two#6=on hold")
        reason = f"Status override example/two#6 names no pull request the tracker holds in {self.record}"
        self.assertEqual((1, [f"FAILED {reason}"]), (code, out.splitlines()))
        code, out, err = self.run_main("override", "--set", "example/two#5=on hold")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual({"example/two#5": "on hold"}, self.written()["dashboard"]["status_overrides"])
        self.assertEqual(calls, len(self.github.calls), "override reads nothing from GitHub")

    def test_two_configurations_in_one_folder_keep_records_of_their_own(self) -> None:
        self.serve_default_pages()
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        default = self.config_path
        other = self.root / "other.json"
        other.write_bytes(default.read_bytes())
        for config in (default, other):
            self.config_path = config
            with self.subTest(config=config.name):
                code, _, err = self.run_main("update", "--input", str(self.input), "--remove", "example/two#5")
                self.assertEqual(0, code, err)
        self.assertTrue((self.root / "tracker.json").exists())
        self.assertTrue((self.root / "other.tracker.json").exists())
        # A set is checked against its own configuration's record.
        (self.root / "other.tracker.json").unlink()
        code, out, _ = self.run_main("override", "--set", "example/two#5=on hold")
        record = self.root / "other.tracker.json"
        reason = f"Status override example/two#5 names no tracked pull request: no update has written {record}"
        self.assertEqual((1, [f"FAILED {reason}"]), (code, out.splitlines()))
        self.config_path = default
        code, out, err = self.run_main("override", "--set", "example/two#5=on hold")
        self.assertEqual((0, ""), (code, err))

    def test_a_configuration_reached_through_a_link_has_the_record_update_wrote_and_stays_linked(self) -> None:
        self.serve_default_pages()
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        target = self.config_path
        target.chmod(0o644)
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        if sys.platform != "win32":  # a symbolic link to a file needs a privilege Windows rarely grants
            self.config_path = elsewhere / "config.json"
            self.config_path.symlink_to(target)
        code, _, err = self.run_main("update", "--input", str(self.input))
        self.assertEqual(0, code, err)
        self.assertEqual([], list(elsewhere.iterdir())[1:], "no record is written beside the link")
        code, out, err = self.run_main("override", "--set", "example/two#5=on hold")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["SET example/two#5 on hold", f"WROTE {self.config_path}"], out.splitlines())
        self.assertEqual({"example/two#5": "on hold"}, load_config(target)["dashboard"]["status_overrides"])
        code, out, err = self.run_main("override")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["OVERRIDE example/two#5 on hold", "OVERRIDES 1"], out.splitlines())
        if sys.platform != "win32":
            self.assertTrue(self.config_path.is_symlink(), "the link stays a link")
            self.assertEqual(0o644, target.stat().st_mode & 0o777)

    def test_an_unreadable_record_fails_the_listing_and_a_set(self) -> None:
        self.configure(overrides={"example/one#2": "waiting"})
        original = self.config_path.read_bytes()
        stale = {"state": "stale", "reviewed_head_sha": HEAD}
        for record, reason in (
            ({"schema_version": 3, "pulls": []}, "tracker.schema_version must be one of 1, 2, found integer 3"),
            ({"schema_version": 2, "pulls": []}, "tracker must be an object with field reviews"),
            ({"schema_version": 1, "pulls": [], "reviews": {}}, "tracker"),
            ({"schema_version": 1, "pulls": ["example/one"]}, "Invalid tracker pull-request identity"),
            ({"schema_version": 1, "pulls": ["example/one#2", "Example/One#2"]}, "Duplicate tracker pull-request"),
            ({"schema_version": 2, "pulls": [], "reviews": {"example/one#2": stale}}, "names no pull request"),
            (
                {
                    "schema_version": 2,
                    "pulls": ["example/one#2"],
                    "reviews": {"example/one#2": {**stale, "state": "x"}},
                },
                "tracker.reviews.example/one#2.state must be one of",
            ),
            (
                {
                    "schema_version": 2,
                    "pulls": ["example/one#2"],
                    "reviews": {"example/one#2": {**stale, "state": "missing"}},
                },
                "must name a reviewed head exactly when its review is not missing",
            ),
        ):
            self.record.write_text(json.dumps(record), encoding="utf-8")
            for arguments in ([], ["--set", "example/one#2=on hold"]):
                with self.subTest(record=record, arguments=arguments):
                    code, out, err = self.run_main("override", *arguments)
                    self.assertEqual((1, ""), (code, err))
                    self.assertEqual(1, len(out.splitlines()), out)
                    self.assertTrue(out.startswith("FAILED "), out)
                    self.assertIn(reason, out)
                    self.assertEqual(original, self.config_path.read_bytes())

    def test_a_refused_change_writes_nothing(self) -> None:
        self.configure(overrides={"example/one#2": "waiting"})
        self.track("example/one#3")
        original = self.config_path.read_bytes()
        # Every computed tracker state, in another case, as the configuration's validator refuses it.
        computed = [
            "to review",
            "awaiting response",
            "my pull requests",
            "drafts",
            "missing",
            "current",
            "stale",
            "incomplete",
        ]
        cases: list[tuple[list[str], str]] = [
            (["--set", f"example/one#3={state.title()}"], "duplicates a computed tracker state") for state in computed
        ]
        cases += [
            (["--set", "example/one#3= INCOMPLETE "], "duplicates a computed tracker state"),
            (
                ["--set", "example/one#3=  "],
                "config.dashboard.status_overrides.example/one#3 must be a non-empty string, found an empty string",
            ),
            (["--set", "example/one#3"], "must be owner/repo#number=status"),
            (["--set", "example/one=on hold"], "Invalid override pull-request identity"),
            (["--clear", "example/one#9"], "No status override for example/one#9"),
            (["--set", "example/one#3=on hold", "--clear", "Example/One#3"], "example/one#3 is named more than once"),
            (["--set", "example/one#3=on hold", "--set", "example/one#4=drafts"], "duplicates a computed tracker"),
        ]
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                code, out, err = self.run_main("override", *arguments)
                self.assertEqual((1, ""), (code, err))
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertTrue(out.startswith("FAILED "), out)
                self.assertIn(reason, out)
                self.assertEqual(original, self.config_path.read_bytes())

    def test_an_invalid_configuration_is_refused_before_any_change(self) -> None:
        self.config_path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        code, out, _ = self.run_main("override", "--set", "example/one#3=on hold")
        self.assertEqual(1, code)
        self.assertTrue(out.startswith("FAILED "), out)
        self.assertEqual({"schema_version": 1}, self.written())


class RepositoryTests(TrackerPipelineFixture):
    """`repository --add` adds a repository to the configuration and a repository set without GitHub, validating the
    whole file."""

    def written(self) -> Any:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_add_gives_a_new_repository_the_generic_reviewer_and_adds_it_to_the_dashboard_set(self) -> None:
        raw = {
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["example/one"], "tracked": ["Example/One"]},
            "repositories": {"Example/One": {"reviewer": {"id": "generic", "protocol_version": 1, "scope": "generic"}}},
            "operation_repository_sets": {"update-pr-dashboard": "tracked"},
            "archive_root": str(self.archive),
            "summary_root": str(self.root / "summaries"),
            "dashboard_file": str(self.dashboard),
        }
        self.config_path.write_text(json.dumps(raw, indent=4), encoding="utf-8")
        code, out, err = self.run_main("repository", "--add", "Other/Three", "--add", "other/four")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            [
                "ADDED_REPOSITORY other/three reviewer=generic",
                "ADDED_TO_SET tracked other/three",
                "ADDED_REPOSITORY other/four reviewer=generic",
                "ADDED_TO_SET tracked other/four",
                f"WROTE {self.config_path}",
            ],
            out.splitlines(),
        )
        generic = {
            "reviewer": {
                "id": "generic",
                "protocol_version": 1,
                "trusted_ref": None,
                "scope": "generic",
                "manifest_path": None,
            },
            "checkout_path": None,
        }
        expected = json.loads(json.dumps(raw))
        expected["repository_sets"]["tracked"] += ["other/three", "other/four"]
        expected["repositories"].update({"other/three": generic, "other/four": generic})
        self.assertEqual(expected, self.written(), "no default is filled in and no other value changes")
        self.assertTrue(self.config_path.read_text(encoding="utf-8").startswith('{\n  "archive_root": '))
        config = load_config(self.config_path)
        self.assertEqual(["example/one", "other/three", "other/four"], config["repository_sets"]["tracked"])
        self.assertEqual("generic", config["repositories"]["other/four"]["reviewer"]["scope"])
        self.assertEqual([], self.github.calls)

    def test_an_added_repository_is_collected_and_no_longer_untracked(self) -> None:
        self.serve_default_pages()
        self.github.pages["other/three"] = [page([])]
        self.github.searches = {"is:pr is:open archived:false author:reviewer": [search_page(["other/three"])]}
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertIn("UNTRACKED_REPOSITORY other/three", out.splitlines())
        code, out, err = self.run_main("repository", "--add", "other/three")
        self.assertEqual((0, ""), (code, err))
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            [
                "REPOSITORY example/one pulls=3",
                "REPOSITORY example/two pulls=1",
                "REPOSITORY other/three pulls=0",
                f"INPUT {self.input}",
            ],
            out.splitlines(),
        )

    def test_a_repository_with_an_entry_keeps_it_and_joins_the_named_set(self) -> None:
        before = self.written()["repositories"]["example/two"]
        code, out, err = self.run_main("repository", "--add", "Example/Two", "--repository-set", "primary")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["ADDED_TO_SET primary example/two", f"WROTE {self.config_path}"], out.splitlines())
        written = self.written()
        self.assertEqual(before, written["repositories"]["example/two"])
        self.assertEqual(["example/one", "example/two"], written["repository_sets"]["primary"])
        self.assertEqual(["example/one", "example/two"], written["repository_sets"]["tracked"])

    def test_the_skill_names_every_line_the_command_and_collect_print(self) -> None:
        # The agent reads only SKILL.md, so a line it does not name is output the agent has to guess at.
        printed: set[str] = set()
        code, out, err = self.run_main("repository", "--add", "other/three")
        self.assertEqual((0, ""), (code, err))
        printed |= {line.split()[0] for line in out.splitlines()}
        for change in ("--ignore", "--unignore", "--ignore"):
            code, out, err = self.run_main("repository", change, "other/five")
            self.assertEqual((0, ""), (code, err))
            printed |= {line.split()[0] for line in out.splitlines()}
        self.serve_default_pages()
        self.github.pages["other/three"] = [page([])]
        self.github.searches = {
            "is:pr is:open archived:false author:reviewer": [search_page(["other/four", "other/five"])]
        }
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        printed |= {line.split()[0] for line in out.splitlines()}
        self.github.search_failure = "HTTP 422: Validation Failed"
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        printed |= {line.split()[0] for line in out.splitlines()}
        self.assertEqual(
            {
                "ADDED_REPOSITORY",
                "ADDED_TO_SET",
                "IGNORED",
                "UNIGNORED",
                "WROTE",
                "REPOSITORY",
                "UNTRACKED_REPOSITORY",
                "UNTRACKED_IGNORED",
                "UNTRACKED_CHECK_FAILED",
                "INPUT",
            },
            printed,
        )
        skill = (SCRIPT_DIRECTORY.parent / "SKILL.md").read_text(encoding="utf-8")
        for word in sorted(printed):
            with self.subTest(line=word):
                self.assertTrue(f"`{word}`" in skill or f"`{word} <" in skill, word)

    def test_a_refused_addition_writes_nothing(self) -> None:
        original = self.config_path.read_bytes()
        cases: list[tuple[list[str], str]] = [
            (["--add", "Example/One"], "Repository example/one is already in repository set tracked"),
            (
                ["--add", "other/three", "--add", "example/two"],
                "Repository example/two is already in repository set tracked",
            ),
            (["--add", "example/one", "--repository-set", "primary"], "already in repository set primary"),
            (["--add", "other/three", "--repository-set", "absent"], "Unknown repository set: absent"),
            (["--add", "other/three", "--add", "Other/Three"], "Repository other/three is named more than once"),
            (["--add", "other"], "Invalid repository identity: 'other'"),
            (["--add", "other/three/four"], "Invalid repository identity"),
        ]
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                code, out, err = self.run_main("repository", *arguments)
                self.assertEqual((1, ""), (code, err))
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertTrue(out.startswith("FAILED "), out)
                self.assertIn(reason, out)
                self.assertEqual(original, self.config_path.read_bytes())
        self.assertEqual([], self.github.calls)

    def test_a_configuration_that_does_not_validate_is_refused_before_any_change(self) -> None:
        missing_set = json.loads(self.config_path.read_text(encoding="utf-8"))
        missing_set["repository_sets"]["tracked"].append("example/three")  # a set member with no entry
        outputs: list[str] = []
        for content in ("{", json.dumps({"schema_version": 1}), json.dumps(missing_set)):
            with self.subTest(content=content):
                self.config_path.write_text(content, encoding="utf-8")
                code, out, err = self.run_main("repository", "--add", "other/three")
                outputs.append(out)
                self.assertEqual((1, ""), (code, err))
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertTrue(out.startswith("FAILED "), out)
                self.assertEqual(content, self.config_path.read_text(encoding="utf-8"))
        self.assertIn("Missing per-repository configuration for: example/three", outputs[-1])

    def test_ignore_and_unignore_change_only_the_ignored_list(self) -> None:
        original = self.written()
        code, out, err = self.run_main("repository", "--ignore", "Other/Three", "--ignore", "sandbox/*")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["IGNORED other/three", "IGNORED sandbox/*", f"WROTE {self.config_path}"], out.splitlines())
        expected = json.loads(json.dumps(original))
        expected["dashboard"]["ignored_repositories"] = ["other/three", "sandbox/*"]
        self.assertEqual(expected, self.written())
        code, out, err = self.run_main("repository", "--unignore", "OTHER/three")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(["UNIGNORED other/three", f"WROTE {self.config_path}"], out.splitlines())
        expected["dashboard"]["ignored_repositories"] = ["sandbox/*"]
        self.assertEqual(expected, self.written())
        self.assertEqual([], self.github.calls)

    def test_collect_mutes_ignored_repositories_and_counts_them_without_changing_the_input(self) -> None:
        self.serve_default_pages()
        self.github.searches = {
            "is:pr is:open archived:false author:reviewer": [
                search_page(["example/one", "Other/Three", "sandbox/test-a", "sandbox/keep"])
            ],
            "is:pr is:open archived:false review-requested:reviewer": [search_page(["other/four"])],
        }
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        unmuted = self.input.read_bytes()
        self.assertEqual(
            [
                "UNTRACKED_REPOSITORY other/four",
                "UNTRACKED_REPOSITORY other/three",
                "UNTRACKED_REPOSITORY sandbox/keep",
                "UNTRACKED_REPOSITORY sandbox/test-a",
            ],
            [line for line in out.splitlines() if line.startswith("UNTRACKED_")],
        )
        code, _, err = self.run_main("repository", "--ignore", "other/three", "--ignore", "Sandbox/Test-*")
        self.assertEqual((0, ""), (code, err))
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            [
                "REPOSITORY example/one pulls=3",
                "REPOSITORY example/two pulls=1",
                "UNTRACKED_REPOSITORY other/four",
                "UNTRACKED_REPOSITORY sandbox/keep",
                "UNTRACKED_IGNORED 2",
                f"INPUT {self.input}",
            ],
            out.splitlines(),
        )
        self.assertEqual(unmuted, self.input.read_bytes(), "muting changes nothing collected")
        # Nothing muted prints no count, and named repositories search for no untracked one at all.
        code, _, err = self.run_main("repository", "--unignore", "other/three", "--unignore", "sandbox/test-*")
        self.assertEqual((0, ""), (code, err))
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((0, ""), (code, err))
        self.assertNotIn("UNTRACKED_IGNORED", out)

    def test_a_refused_ignore_or_unignore_writes_nothing(self) -> None:
        code, _, err = self.run_main("repository", "--ignore", "other/three")
        self.assertEqual((0, ""), (code, err))
        original = self.config_path.read_bytes()
        cases: list[tuple[list[str], str]] = [
            (["--ignore", "Example/Two"], "Repository example/two in repository set tracked matches"),
            (["--ignore", "example/*"], "entry example/*; a tracked repository cannot be ignored"),
            (["--ignore", "Other/Three"], "Repository other/three is already ignored"),
            (["--unignore", "other/four"], "Repository other/four is not ignored"),
            (
                ["--ignore", "other/four", "--ignore", "OTHER/four"],
                "Ignored repository other/four is named more than once",
            ),
            (
                ["--ignore", "other"],
                "FAILED ignored repository must be an owner/repo identity, or a pattern of one using only * and ? "
                'as wildcards, found string "other"',
            ),
            (["--ignore", "other/[ab]"], "an owner/repo identity, or a pattern"),
            # A repository an ignored entry matches cannot be added to a set either.
            (["--add", "other/three"], "Repository other/three in repository set tracked matches"),
        ]
        for arguments, reason in cases:
            with self.subTest(arguments=arguments):
                code, out, err = self.run_main("repository", *arguments)
                self.assertEqual((1, ""), (code, err))
                self.assertEqual(1, len(out.splitlines()), out)
                self.assertTrue(out.startswith("FAILED "), out)
                self.assertIn(reason, out)
                self.assertEqual(original, self.config_path.read_bytes())
        # Muting and adding are separate commands, and a set is named only for an addition.
        for arguments in (
            ["--ignore", "other/four", "--add", "other/five"],
            ["--ignore", "other/four", "--unignore", "other/three"],
            ["--ignore", "other/four", "--repository-set", "primary"],
            [],
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaises(SystemExit) as raised:
                    self.run_main("repository", *arguments)
                self.assertEqual(2, raised.exception.code)
                self.assertEqual(original, self.config_path.read_bytes())
        self.assertEqual([], self.github.calls)

    def test_a_set_named_beside_a_mute_is_a_usage_error_naming_why(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            tp.main(["--config", str(self.config_path), "repository", "--ignore", "o/x", "--repository-set", "primary"])
        self.assertEqual(2, raised.exception.code)
        self.assertTrue(err.getvalue().endswith(": error: --repository-set is taken only with --add\n"), err.getvalue())

    def test_the_repository_help_names_each_option_with_its_argument_and_purpose(self) -> None:
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"COLUMNS": "200"}),
            contextlib.redirect_stdout(out),
            self.assertRaises(SystemExit) as raised,
        ):
            tp.main(["repository", "--help"])
        self.assertEqual(0, raised.exception.code)
        text = " ".join(out.getvalue().split())
        for line in (
            "(--add OWNER/REPO | --ignore OWNER/REPO | --unignore OWNER/REPO)",
            "--add OWNER/REPO repeatable",
            "--ignore OWNER/REPO a repository or pattern; repeatable",
            "--unignore OWNER/REPO an ignored entry; repeatable",
        ):
            with self.subTest(line=line):
                self.assertIn(line, text)

    def test_ignore_writes_a_dashboard_into_a_configuration_without_one(self) -> None:
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        del raw["dashboard"]
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        code, _, err = self.run_main("repository", "--ignore", "Other/Three")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual({"ignored_repositories": ["other/three"]}, self.written()["dashboard"])
        # A dashboard without the list gains it, and keeps what it held.
        raw["dashboard"] = {"author_names": {"ada": "Ada"}}
        self.config_path.write_text(json.dumps(raw), encoding="utf-8")
        code, _, err = self.run_main("repository", "--ignore", "other/four")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            {"author_names": {"ada": "Ada"}, "ignored_repositories": ["other/four"]}, self.written()["dashboard"]
        )

    def test_a_collection_that_mutes_nothing_counts_zero(self) -> None:
        self.serve_default_pages()
        named = tp.collect(
            self.input, repositories=["example/one"], config_path=self.config_path, services=self.services
        )
        self.assertEqual((None, 0), (named.untracked, named.ignored))
        self.github.search_failure = "HTTP 422: Validation Failed"
        failed = tp.collect(self.input, config_path=self.config_path, services=self.services)
        self.assertEqual((None, 0), (failed.untracked, failed.ignored))


class ReviewArgumentsTests(TrackerPipelineFixture):
    """`review-arguments` turns the candidates the user confirmed into the `review-prs` arguments, without GitHub."""

    def recorded(self, reviews: dict[str, str]) -> None:
        """Record each key's AI review state as the last update would have shown its row."""
        states = {
            key: {"state": state, "reviewed_head_sha": None if state == "missing" else HEAD}
            for key, state in reviews.items()
        }
        record = {"schema_version": 2, "pulls": sorted(reviews), "reviews": states}
        self.record.write_text(json.dumps(record), encoding="utf-8")

    def confirm(self, *keys: str, scope: str | None = None) -> tuple[int, list[str], str]:
        arguments = [argument for key in keys for argument in ("--confirm", key)]
        code, out, err = self.run_main("review-arguments", *arguments, *(["--scope", scope] if scope else []))
        return code, out.splitlines(), err

    def test_the_candidates_an_update_prints_become_one_review_prs_argument_line(self) -> None:
        stale = pull_node(3, headRefOid=HEAD)
        commit_record(self.archive, "example/one", 3, archived_record(OLD_HEAD, 3), expected_latest_version=None)
        self.github.pages = {"example/one": [page([pull_node(1), stale])], "example/two": [page([])]}
        code, _, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual(0, code, err)
        code, out, err = self.run_main("update", "--input", str(self.input), "--candidates")
        self.assertEqual(0, code, err)
        self.assertEqual(["CANDIDATE missing example/one#1", "CANDIDATE stale example/one#3"], out.splitlines()[2:])
        calls = len(self.github.calls)
        # A missing review is asked for with --pull, and needs no scope.
        self.assertEqual((0, ["REVIEW_ARGUMENTS --pull example/one#1"], ""), self.confirm("example/one#1"))
        # A stale one needs the scope the user chooses, so without it the command names the stale ones instead.
        self.assertEqual((0, ["SCOPE_NEEDED example/one#3"], ""), self.confirm("Example/One#3", "example/one#1"))
        for scope in ("auto", "full", "incremental"):
            with self.subTest(scope=scope):
                self.assertEqual(
                    (0, [f"REVIEW_ARGUMENTS --pull example/one#1 --re-review example/one#3 --scope {scope}"], ""),
                    self.confirm("example/one#3", "example/one#1", scope=scope),
                )
        self.assertEqual(
            (0, ["REVIEW_ARGUMENTS --re-review example/one#3 --scope full"], ""),
            self.confirm("example/one#3", scope="full"),
        )
        self.assertEqual(calls, len(self.github.calls), "review-arguments reads nothing from GitHub")

    def test_every_missing_and_stale_key_is_passed_once_in_key_order_with_one_scope(self) -> None:
        self.recorded(
            {
                "example/two#5": "stale",
                "example/one#2": "missing",
                "example/one#10": "stale",
                "example/one#1": "missing",
            }
        )
        code, lines, err = self.confirm("example/two#5", "example/one#2", "example/one#10", "example/one#1")
        self.assertEqual((0, ["SCOPE_NEEDED example/one#10", "SCOPE_NEEDED example/two#5"], ""), (code, lines, err))
        code, lines, err = self.confirm(
            "example/two#5", "example/one#2", "example/one#10", "example/one#1", scope="auto"
        )
        expected = (
            "REVIEW_ARGUMENTS --pull example/one#1 --pull example/one#2 --re-review example/one#10 "
            "--re-review example/two#5 --scope auto"
        )
        self.assertEqual((0, [expected], ""), (code, lines, err))

    def test_a_key_the_last_update_did_not_offer_for_review_is_refused(self) -> None:
        self.configure(overrides={"example/one#4": "on hold"})
        self.recorded(
            {
                "example/one#1": "missing",
                "example/one#2": "current",
                "example/one#3": "incomplete",
                "example/one#4": "stale",
            }
        )
        reason = (
            "names no pull request the last update offered for review, one whose AI review is missing or stale and "
            f"that no status override pins, in {self.record}"
        )
        # A current or incomplete review, one an override pins, and one the last update did not show.
        for key in ("example/one#2", "example/one#3", "Example/One#4", "example/one#9", "other/one#1"):
            with self.subTest(key=key):
                expected = f"FAILED Confirmed pull request {key.lower()} {reason}"
                self.assertEqual((1, [expected], ""), self.confirm("example/one#1", key))
        self.assertEqual(
            (1, ["FAILED Duplicate confirmed pull-request identity: example/one#1"], ""),
            self.confirm("example/one#1", "Example/One#1"),
        )
        self.record.unlink()
        unwritten = (
            f"FAILED Confirmed pull request example/one#1 names no candidate: no update has written {self.record}"
        )
        self.assertEqual((1, [unwritten], ""), self.confirm("example/one#1"))

    def test_a_scope_is_refused_unless_a_confirmed_pull_request_is_stale(self) -> None:
        self.recorded({"example/one#1": "missing", "example/one#2": "stale"})
        self.assertEqual(
            (1, ["FAILED --scope is taken only when a confirmed pull request is stale; leave it out"], ""),
            self.confirm("example/one#1", scope="full"),
        )
        # A scope that is not one review-prs takes, and no confirmed pull request, are usage errors.
        for arguments in (["--confirm", "example/one#2", "--scope", "partial"], []):
            with (
                self.subTest(arguments=arguments),
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                tp.main(["--config", str(self.config_path), "review-arguments", *arguments])
            self.assertEqual(2, raised.exception.code)

    def test_the_skill_names_every_line_the_command_prints(self) -> None:
        # The agent reads only SKILL.md, so a line it does not name is output the agent has to guess at.
        self.recorded({"example/one#1": "missing", "example/one#2": "stale"})
        printed: set[str] = set()
        for scope in (None, "auto"):
            code, lines, err = self.confirm("example/one#1", "example/one#2", scope=scope)
            self.assertEqual((0, ""), (code, err))
            printed |= {line.split()[0] for line in lines}
        self.assertEqual({"REVIEW_ARGUMENTS", "SCOPE_NEEDED"}, printed)
        skill = (SCRIPT_DIRECTORY.parent / "SKILL.md").read_text(encoding="utf-8")
        for word in sorted(printed):
            with self.subTest(line=word):
                self.assertTrue(f"`{word} <" in skill, word)
