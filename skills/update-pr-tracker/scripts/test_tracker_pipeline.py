from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "code-review-core" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skill-core" / "scripts"))

import review_archive
import review_fixture
import tracker_pipeline as tp
from github_client import CommandResult
from review_archive import commit_record
from review_config import load_config, write_config
from review_flags import add_flag
from review_github import GitHubClient, GitHubError
from review_records import build_record, validate_adapter_result

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


class FakeGitHub:
    """Serves the GraphQL pages, authenticated user, and comparison calls the tracker pipeline makes."""

    def __init__(self) -> None:
        self.pages: dict[str, list[str]] = {}
        self.failures: dict[str, tuple[str, str]] = {}
        self.comparisons: dict[tuple[str, str], str] = {}
        # Raised, as a failed gh call would be, by every ancestry comparison, or by every other comparison and tree.
        self.ancestry_failure: GitHubError | None = None
        self.change_failure: GitHubError | None = None
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> CommandResult:
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[:3] == ["gh", "api", "graphql"]:
            fields = dict(value.split("=", 1) for value in arguments[4::2])
            repository = f"{fields['owner']}/{fields['name']}"
            if repository in self.failures:
                return CommandResult(1, "", self.failures[repository][1])
            pages = self.pages[repository]
            index = int(fields["after"].removeprefix("cursor-")) if "after" in fields else 0
            return CommandResult(0, pages[index], "")
        if arguments[-1] == "user":
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


def archived_record(head: str) -> dict[str, Any]:
    request = {
        "repository": "example/one",
        "pull_number": 2,
        "pull_url": "https://github.com/example/one/pull/2",
        "title": "Change 2",
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
            "pull_number": 2,
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
        expected_number=2,
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
                "operation_repository_sets": {"update-pr-tracker": "tracked"},
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
        graphql = [call for call in self.github.calls if call[:3] == ["gh", "api", "graphql"]]
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
        self.assertTrue(input_path.parent.name.startswith("update-pr-tracker-input-"), input_path)
        self.assertEqual(4, len(json.loads(input_path.read_text(encoding="utf-8"))))
        code, out, err = self.run_main("update", "--input", str(input_path))
        self.assertEqual(0, code, err)

    def test_collect_refuses_an_input_file_inside_a_skill_tree(self) -> None:
        self.serve_default_pages()
        skills_root = Path(tp.__file__).resolve().parents[2]
        home = self.root / "home"
        targets = [
            skills_root / "update-pr-tracker" / "input.json",  # beside SKILL.md, as the issue found it
            skills_root / "review-prs" / "input.json",
            home / ".claude" / "skills" / "update-pr-tracker" / "input.json",
            home / ".agents" / "skills" / "update-pr-tracker" / "input.json",
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

    def test_partial_nested_data_fails_its_repository(self) -> None:
        crowded = pull_node(1, participants={"pageInfo": {"hasNextPage": True}, "nodes": []})
        self.github.pages["example/one"] = [page([crowded])]
        code, out, _ = self.run_main("collect", "--repository", "example/one", "--output", str(self.input))
        self.assertEqual(1, code)
        self.assertIn("REPOSITORY_FAILED example/one example/one#1 has more than 100 participants", out)
        self.assertTrue(out.splitlines()[-1].startswith("FAILED 1 of 1 repositories"), out)
        self.assertFalse(self.input.exists())

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
                # The limit was waited out with skill-core's bounded backoff before it stopped the collection.
                self.assertEqual([5.0, 10.0, 20.0, 40.0, 80.0], self.waits)
                self.waits.clear()

    def test_a_failed_ancestry_comparison_fails_its_repository(self) -> None:
        review_fixture.commit_fixture(self.archive)
        reviews = [{"state": "COMMENTED", "commit": {"oid": USER_REVIEWED}}]  # not a review's head, so compared
        node = pull_node(12, headRefOid=review_fixture.HEADS[3], reviews={"nodes": reviews})
        self.github.pages = {"example/one": [page([node])], "example/two": [page([])]}
        for kind, expected in (
            ("api", ["REPOSITORY_FAILED example/one HTTP 502: Bad Gateway", "REPOSITORY example/two pulls=0"]),
            ("forbidden", ["REPOSITORY_FAILED example/one HTTP 502: Bad Gateway", "REPOSITORY example/two pulls=0"]),
            ("network", []),  # stops the collection at once
            ("timeout", []),
        ):
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


class ExitContractTests(TrackerPipelineFixture):
    """The process-level contract: 1 with a last stdout line FAILED for a failure, 2 for usage alone."""

    def execute(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPT_DIRECTORY / "tracker_pipeline.py"),
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
        for arguments in ((), ("unknown",), ("update",), ("override", "--set")):
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
        validate = review_archive.validate_record_pair
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
                "1M 1S open · 1 new, 1 addressed since your review",
                0,
            ),
            (between, {"version": 2, "new": 1, "addressed": 0}, "1M 1S open · 1 new since your review", 2),
            (OLD_HEAD, {"version": 0, "new": 2, "addressed": 0}, "1M 1S open · 2 new since your review", 3),
            (heads[3], {"version": 3, "new": 0, "addressed": 0}, "1M 1S open · unchanged since your review", 0),
            (USER_REVIEWED, None, "1M 1S open · 1 addressed", 1),  # GitHub cannot compare it
            (None, None, "1M 1S open · 1 addressed", 0),
        ):
            with self.subTest(sha=sha):
                self.github.calls.clear()
                reviews = [{"state": "COMMENTED", "commit": {"oid": sha}}] if sha else []
                item, row = self.collect_fixture(reviews)
                self.assertEqual(
                    {
                        "open": {"MUST_FIX": 1, "SHOULD_FIX": 0, "SUGGESTION": 1},
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

        def record(version: int, keys: list[tuple[str, str, int]]) -> dict[str, Any]:
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
        self.assertIn("| 1M 1S open (1 flagged) · 1 addressed |", row)
        self.flags_path.write_text("{}", encoding="utf-8")
        code, out, err = self.run_main("collect", "--output", str(self.input))
        self.assertEqual((1, "FAILED Flag store shape is invalid\n", ""), (code, out, err))

    def test_remove_drops_one_row_and_no_candidates_without_the_flag(self) -> None:
        self.collect()
        code, out, err = self.run_main("update", "--input", str(self.input), "--remove", "Example/One#1")
        self.assertEqual(0, code, err)
        self.assertEqual([f"UPDATED {self.dashboard} rows=3", "GITHUB_CALLS 3"], out.splitlines())
        self.assertNotIn("example/one/pull/1)", self.dashboard.read_text(encoding="utf-8"))

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
                str(SCRIPT_DIRECTORY / "tracker_pipeline.py"),
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


class OverrideTests(TrackerPipelineFixture):
    """`override` lists, sets, and clears `dashboard.status_overrides` without GitHub, validating the whole file."""

    def written(self) -> dict[str, Any]:
        value = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertIsInstance(value, dict)
        return value

    def test_override_alone_lists_each_override_as_the_tracker_matches_it(self) -> None:
        self.configure(overrides={"example/two#5": "on hold", "Example/One#2": " delegated "})
        code, out, err = self.run_main("override")
        self.assertEqual((0, ""), (code, err))
        self.assertEqual(
            ["OVERRIDE example/one#2 delegated", "OVERRIDE example/two#5 on hold", "OVERRIDES 2"], out.splitlines()
        )
        self.assertEqual([], self.github.calls)

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
        self.configure(overrides={"example/one#2": "waiting"})
        printed: set[str] = set()
        for arguments in ([], ["--set", "example/one#3=on hold"], ["--clear", "example/one#2"]):
            code, out, err = self.run_main("override", *arguments)
            self.assertEqual((0, ""), (code, err))
            printed |= {line.split()[0] for line in out.splitlines()}
        self.assertEqual({"OVERRIDE", "OVERRIDES", "SET", "CLEARED", "WROTE"}, printed)
        skill = (SCRIPT_DIRECTORY.parent / "SKILL.md").read_text(encoding="utf-8")
        for word in sorted(printed):
            with self.subTest(line=word):
                self.assertTrue(f"`{word}`" in skill or f"`{word} <" in skill, word)

    def test_set_replaces_the_override_of_the_same_pull_request_whatever_its_case(self) -> None:
        self.configure(overrides={"Example/One#2": "waiting", "example/two#5": "on hold"})
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

    def test_a_refused_change_writes_nothing(self) -> None:
        self.configure(overrides={"example/one#2": "waiting"})
        original = self.config_path.read_bytes()
        # Every computed tracker state, in another case, as the configuration's validator refuses it.
        computed = ["to review", "awaiting response", "my pull requests", "drafts", "missing", "current", "stale"]
        cases: list[tuple[list[str], str]] = [
            (["--set", f"example/one#3={state.title()}"], "duplicates a computed tracker state") for state in computed
        ]
        cases += [
            (["--set", "example/one#3=  "], "must be non-empty"),
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


if __name__ == "__main__":
    unittest.main()
