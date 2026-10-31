"""Fixture tests for tests/validation/known_failures.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient
from known_failures import (
    KNOWN_FAILURES,
    PENDING_MODULE,
    KnownFailure,
    KnownFailuresUnreadable,
    checkout_repository,
    closed_issues,
    closed_known_failure_problems,
    known_failure_problems,
    open_issue_problems,
    pending_problems,
    read_known_failures,
    read_pending,
)
from validation_support import REPOSITORY_ROOT, git

from tests import run_validation

ENTRY = {"reader": "module.reader", "case": "deep_nesting", "issue": 4242}
REPOSITORY = "EJRyan89/coding-agent-skills"
# What gh answers for an open issue asked for its state and its URL.
ISSUE_ANSWER = f'{{"state": "OPEN", "url": "https://github.com/{REPOSITORY}/issues/4242"}}'


class KnownFailureFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / KNOWN_FAILURES).parent.mkdir(parents=True)

    def write(self, value: object) -> None:
        (self.root / KNOWN_FAILURES).write_text(json.dumps(value), encoding="utf-8")

    def write_pending(self, source: str) -> None:
        (self.root / PENDING_MODULE).write_text(source, encoding="utf-8")


class ListShapeTests(KnownFailureFixture):
    def test_an_entry_naming_its_reader_case_and_issue_in_order_passes(self) -> None:
        self.write([ENTRY, {**ENTRY, "case": "long_integer"}])
        self.assertEqual([], known_failure_problems(self.root))
        self.assertEqual(
            [KnownFailure("module.reader", "deep_nesting", 4242), KnownFailure("module.reader", "long_integer", 4242)],
            read_known_failures(self.root / KNOWN_FAILURES),
        )

    def test_an_entry_without_its_issue_fails(self) -> None:
        for issue in (None, 0, -3, "4242", True):
            with self.subTest(issue=issue):
                entry: dict[str, object] = {**ENTRY, "issue": issue}
                if issue is None:
                    del entry["issue"]
                self.write([entry])
                problems = known_failure_problems(self.root)
                self.assertEqual(1, len(problems), problems)
                self.assertIn("entry 1", problems[0])

    def test_an_entry_with_another_key_or_an_empty_name_fails(self) -> None:
        for entry in ({**ENTRY, "skip": True}, {**ENTRY, "reader": ""}, {**ENTRY, "case": 3}, ["module.reader"]):
            with self.subTest(entry=entry):
                self.write([entry])
                self.assertEqual(1, len(known_failure_problems(self.root)))

    def test_a_case_listed_twice_or_out_of_order_fails(self) -> None:
        self.write([ENTRY, {**ENTRY, "issue": 4343}])
        self.assertEqual(
            [f"{KNOWN_FAILURES.as_posix()} lists module.reader on case deep_nesting more than once"],
            known_failure_problems(self.root),
        )
        self.write([{**ENTRY, "case": "long_integer"}, ENTRY])
        self.assertEqual(
            [f"{KNOWN_FAILURES.as_posix()} must list its entries by reader, then case, then issue"],
            known_failure_problems(self.root),
        )

    def test_an_unreadable_list_is_named_never_raised(self) -> None:
        for content in (b"", b"\xff", b"{}", b"[" * 100_000, b"[" + b"9" * 5000 + b"]"):
            with self.subTest(content=content[:8]):
                (self.root / KNOWN_FAILURES).write_bytes(content)
                problems = known_failure_problems(self.root)
                self.assertEqual(1, len(problems), problems)
                self.assertTrue(problems[0].startswith(KNOWN_FAILURES.as_posix()))
        (self.root / KNOWN_FAILURES).unlink()
        (self.root / KNOWN_FAILURES).mkdir()
        with self.assertRaises(KnownFailuresUnreadable):
            read_known_failures(self.root / KNOWN_FAILURES)

    def test_an_entry_repeating_a_key_is_refused_not_read_by_its_last_value(self) -> None:
        # A plain parse would keep the last issue and pass the entry; another reader of the file may keep the first.
        (self.root / KNOWN_FAILURES).write_text(
            '[{"reader": "module.reader", "case": "deep_nesting", "issue": 0, "issue": 4242}]', encoding="utf-8"
        )
        with self.assertRaisesRegex(KnownFailuresUnreadable, "is not JSON this module reads: JsonRefused"):
            read_known_failures(self.root / KNOWN_FAILURES)


class ClosingTests(KnownFailureFixture):
    def test_a_body_closing_an_issue_a_known_failure_still_names_fails(self) -> None:
        self.write([ENTRY])
        for body in ("Closes #4242\n", "- fixes: #4242\n", "Resolved #4242 by hand.\n", "CLOSED #4242"):
            with self.subTest(body=body):
                problems = closed_known_failure_problems(self.root, body)
                self.assertEqual(1, len(problems), problems)
                self.assertIn("#4242", problems[0])
                self.assertIn("module.reader", problems[0])

    def test_a_body_closing_other_issues_or_mentioning_the_issue_passes(self) -> None:
        self.write([ENTRY])
        for body in (
            "Closes #4243\n",
            "Part of #4242\n",
            "Closes #42420\n",
            "discloses #4242\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([], closed_known_failure_problems(self.root, body))

    def test_a_reference_inside_an_html_comment_fails_as_one_outside_does(self) -> None:
        # The squash commit carries the body as written, comments included, and GitHub closes an issue its message
        # names.
        self.write([ENTRY])
        [problem] = closed_known_failure_problems(self.root, "Fixes the reader.\n<!-- Closes #4242 -->\n")
        self.assertIn("closes #4242", problem)

    def test_no_body_and_no_title_passes(self) -> None:
        self.write([ENTRY])
        self.assertEqual([], closed_known_failure_problems(self.root, None))
        self.assertEqual([], closed_known_failure_problems(self.root, None, None))

    def test_a_title_closing_the_issue_fails_as_a_body_does(self) -> None:
        # The squash commit's message is the title and the body, and GitHub closes an issue either names.
        self.write([ENTRY])
        for body in ("Fixes the reader.\n", None):
            with self.subTest(body=body):
                [problem] = closed_known_failure_problems(self.root, body, "fix: resolves #4242 in the reader")
                self.assertIn("closes #4242", problem)
        self.assertEqual([], closed_known_failure_problems(self.root, "Fixes the reader.\n", "fix: the reader (#4242)"))

    def test_a_body_closing_the_issue_fails_beside_a_title_that_closes_none(self) -> None:
        # validate.yml always passes both, so a check that read the title alone once one is given would pass this.
        self.write([ENTRY])
        [problem] = closed_known_failure_problems(self.root, "Closes #4242\n", "fix: the reader")
        self.assertIn("closes #4242", problem)

    def test_a_pull_request_closing_an_issue_pending_names_fails(self) -> None:
        self.write([])
        self.write_pending('PENDING: dict[str, int] = {"module.reader": 4242}\n')
        [problem] = closed_known_failure_problems(self.root, "Closes #4242\n")
        self.assertIn("#4242", problem)
        self.assertIn("names module.reader as owed its table entry", problem)
        self.assertEqual([], closed_known_failure_problems(self.root, "Closes #4243\n"))

    def test_closed_issues_reads_every_closing_line(self) -> None:
        self.assertEqual({1, 22, 333}, closed_issues("Closes #1\nfixes #22 and resolves: #333\nSee #4\n", REPOSITORY))

    def test_a_closing_number_past_the_digit_limit_or_in_other_digits_names_no_issue(self) -> None:
        body = f"Closes #7\nCloses #1{'0' * 5000}\nFixes #{chr(0x0667)}\n"
        self.assertEqual({7}, closed_issues(body, REPOSITORY))

    def test_every_form_github_closes_an_issue_by_is_read(self) -> None:
        # "Validation" in docs/adding-a-skill.md: a pull request "that closes an issue an entry still names" is
        # refused, in every form GitHub honours, each in any case of the repository's name.
        self.write([ENTRY])
        for body in (
            "Closes #4242\n",
            f"Closes {REPOSITORY}#4242\n",
            "closes ejryan89/CODING-AGENT-SKILLS#4242\n",
            f"Fixes https://github.com/{REPOSITORY}/issues/4242\n",
            "Closes GH-4242\n",
            f"Closes <https://github.com/{REPOSITORY}/issues/4242>\n",
            "**Closes** #4242\n",
            "CLOSES:#4242\n",
            f"Closes http://github.com/{REPOSITORY}/issues/4242\n",
            f"Closes https://www.github.com/{REPOSITORY}/issues/4242\n",
        ):
            with self.subTest(body=body):
                self.assertEqual({4242}, closed_issues(body, REPOSITORY))
                self.assertEqual(1, len(closed_known_failure_problems(self.root, body)))

    def test_a_reference_to_another_repository_closes_none_of_this_ones(self) -> None:
        for body in ("Closes other/repository#4242\n", "Fixes https://github.com/other/repository/issues/4242\n"):
            with self.subTest(body=body):
                self.assertEqual(set(), closed_issues(body, REPOSITORY))
                # Where the checkout names no repository, every reference counts, so none is missed.
                self.assertEqual({4242}, closed_issues(body, None))

    def test_the_checkouts_repository_is_read_from_its_origin(self) -> None:
        checkout = self.root / "checkout"
        checkout.mkdir()
        self.assertIsNone(checkout_repository(checkout))
        git(checkout, "init", "--quiet")
        self.assertIsNone(checkout_repository(checkout))
        git(checkout, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
        self.assertEqual(REPOSITORY, checkout_repository(checkout))
        self.write([ENTRY])
        (checkout / KNOWN_FAILURES).parent.mkdir(parents=True)
        (checkout / KNOWN_FAILURES).write_text(json.dumps([ENTRY]), encoding="utf-8")
        self.assertEqual([], closed_known_failure_problems(checkout, "Closes other/repository#4242\n"))
        self.assertEqual(1, len(closed_known_failure_problems(checkout, f"Closes {REPOSITORY}#4242\n")))


class PendingShapeTests(KnownFailureFixture):
    def test_a_literal_table_of_names_and_issues_is_read_without_running_the_module(self) -> None:
        self.write_pending("import nothing_by_this_name\n\nPENDING: dict[str, int] = {'b.reader': 2, 'a.reader': 1}\n")
        self.assertEqual({"a.reader": 1, "b.reader": 2}, read_pending(self.root / PENDING_MODULE))
        self.assertEqual([], pending_problems(self.root))

    def test_a_table_the_module_cannot_read_fails(self) -> None:
        for source, reason in (
            ("", "must assign PENDING once"),
            ("PENDING = {}\nPENDING = {}\n", "must assign PENDING once"),
            ("PENDING = dict(a=1)\n", "a literal dict"),
            ("PENDING = {[]: 1}\n", "a literal dict"),
            ("PENDING = {'a': 0}\n", "a positive issue number"),
            ("PENDING = {'a': True}\n", "a positive issue number"),
            ("PENDING = {'a': '7'}\n", "a positive issue number"),
            ("PENDING = [('a', 1)]\n", "a positive issue number"),
            ("PENDING = {\n", "cannot be read"),
            ("PENDING, OTHER = {}, 1\n", "must assign PENDING once"),
            ("OTHER = PENDING = {}\n", "must assign PENDING once"),
            ("PENDING = {}\nPENDING: dict[str, int] = {'a': 1}\n", "must assign PENDING once"),
        ):
            with self.subTest(source=source):
                self.write_pending(source)
                [problem] = pending_problems(self.root)
                self.assertIn(reason, problem)

    def test_any_other_write_binding_or_mention_of_the_table_fails(self) -> None:
        # Each would leave the module running with a table other than the literal read, so it is refused, wherever in
        # the module it stands: one row for every way Python spells a write, a binding, or a reach to the name.
        writes = (
            'PENDING["x"] = 4242',
            'PENDING["x"] += 1',
            'del PENDING["x"]',
            "del PENDING",
            'PENDING.update({"x": 4242})',
            'PENDING.setdefault("x", 4242)',
            'PENDING.__setitem__("x", 4242)',
            'PENDING |= {"x": 4242}',
            'if True:\n    PENDING = {"x": 4242}',
            'for PENDING in [{"x": 4242}]:\n    pass',
            'while False:\n    PENDING = {"x": 4242}',
            "try:\n    pass\nexcept OSError as PENDING:\n    pass",
            "with open(__file__) as PENDING:\n    pass",
            '(PENDING := {"x": 4242})',
            "PENDING: dict[str, int]",
            "TABLE = PENDING",
            'TABLE = PENDING\nTABLE["x"] = 4242',
            'register(PENDING, "x")',
            'def add():\n    global PENDING\n    PENDING = {"x": 4242}',
            "def add(PENDING):\n    pass",
            "def PENDING():\n    pass",
            "class PENDING:\n    pass",
            "import os as PENDING",
            "from os import PENDING",
            "from os import environ as PENDING",
            'globals()["PENDING"] = {"x": 4242}',
            'globals().update(PENDING={"x": 4242})',
            'setattr(sys.modules[__name__], "PENDING", {"x": 4242})',
            'sys.modules[__name__].PENDING = {"x": 4242}',
            "match {}:\n    case {**PENDING}:\n        pass",
            "match {}:\n    case PENDING:\n        pass",
            "match []:\n    case [*PENDING]:\n        pass",
            "TABLES = [PENDING for PENDING in ()]",
        )
        for write in writes:
            with self.subTest(write=write):
                self.write_pending(f"import sys\n\nPENDING: dict[str, int] = {{}}\n\n{write}\n")
                [problem] = pending_problems(self.root)
                self.assertIn("must name PENDING only in its one assignment, but names it again on line", problem)

    def test_the_table_named_in_all_and_in_prose_passes(self) -> None:
        self.write_pending(
            '"""PENDING names each reader owed a table entry."""\n\nPENDING = {"a.reader": 1}\n\n'
            '__all__ = ["PENDING", "READERS"]\n__all__ += ["PENDING"]\n'
        )
        self.assertEqual({"a.reader": 1}, read_pending(self.root / PENDING_MODULE))


class OpenIssueTests(KnownFailureFixture):
    """Each issue an entry names is open, as GitHub answers through gh; an empty list asks GitHub nothing."""

    def setUp(self) -> None:
        super().setUp()
        git(self.root, "init", "--quiet")
        git(self.root, "remote", "add", "origin", f"https://github.com/{REPOSITORY}.git")
        self.calls: list[list[str]] = []
        self.answer = CommandResult(0, ISSUE_ANSWER, "")

    def run_gh(self, arguments: Sequence[str]) -> CommandResult:
        self.calls.append(list(arguments))
        return self.answer

    def problems(self) -> list[str]:
        return open_issue_problems(self.root, GitHubClient(runner=self.run_gh, sleeper=lambda _: None))

    def test_an_empty_list_asks_github_nothing(self) -> None:
        self.write([])
        self.assertEqual([], self.problems())
        self.assertEqual([], self.calls)

    def test_an_open_issue_passes_after_asking_this_repository(self) -> None:
        self.write([ENTRY, {**ENTRY, "case": "long_integer"}])
        self.assertEqual([], self.problems())
        # One question per issue, of the repository the checkout's origin names.
        self.assertEqual([["gh", "issue", "view", "4242", "--repo", REPOSITORY, "--json", "state,url"]], self.calls)

    def test_a_closed_issue_fails(self) -> None:
        self.write([ENTRY])
        self.answer = CommandResult(0, ISSUE_ANSWER.replace("OPEN", "CLOSED"), "")
        [problem] = self.problems()
        self.assertIn("#4242, which GitHub reports as CLOSED, not OPEN", problem)

    def test_an_open_pull_request_fails_as_no_issue(self) -> None:
        # gh's issue view answers for a pull request's number too, with its state and its /pull/ URL.
        self.write([ENTRY])
        for answer in (ISSUE_ANSWER.replace("/issues/", "/pull/"), '{"state": "OPEN"}', '{"state": "OPEN", "url": 7}'):
            with self.subTest(answer=answer):
                self.answer = CommandResult(0, answer, "")
                [problem] = self.problems()
                self.assertIn("names #4242, which GitHub reports as a pull request or without its URL", problem)

    def test_each_issue_pending_names_is_asked_about_too(self) -> None:
        self.write([])
        self.write_pending('PENDING: dict[str, int] = {"module.reader": 4242, "other.reader": 7}\n')
        self.answer = CommandResult(0, ISSUE_ANSWER.replace("OPEN", "CLOSED"), "")
        problems = self.problems()
        self.assertEqual(["7", "4242"], [call[3] for call in self.calls])
        self.assertEqual(2, len(problems))
        self.assertIn(
            "tests/malformed_input/readers.py's PENDING names #7, which GitHub reports as CLOSED", problems[0]
        )

    def test_an_issue_both_lists_name_is_asked_about_once(self) -> None:
        self.write([ENTRY])
        self.write_pending('PENDING = {"other.reader": 4242}\n')
        self.answer = CommandResult(0, ISSUE_ANSWER.replace("/issues/", "/pull/"), "")
        [problem] = self.problems()
        self.assertEqual(1, len(self.calls))
        self.assertIn(
            "tests/malformed_input/known_failures.json and tests/malformed_input/readers.py's PENDING name #4242",
            problem,
        )

    def test_a_gh_that_cannot_answer_fails_never_reads_as_open(self) -> None:
        self.write([ENTRY])
        for answer in (
            CommandResult(1, "", "HTTP 401: Bad credentials\n"),
            CommandResult(1, "", "GraphQL: Could not resolve to an issue or pull request with the number of 4242.\n"),
            CommandResult(0, "not json", ""),
            CommandResult(0, '{"title": "no state"}', ""),
        ):
            with self.subTest(answer=answer):
                self.answer = answer
                [problem] = self.problems()
                self.assertIn("#4242", problem)

    def test_a_checkout_naming_no_github_repository_fails(self) -> None:
        self.write([ENTRY])
        git(self.root, "remote", "set-url", "origin", "https://example.com/other.git")
        [problem] = self.problems()
        self.assertIn("names no GitHub repository", problem)
        self.assertEqual([], self.calls)


class RepositoryTests(unittest.TestCase):
    def test_the_repository_list_has_its_shape(self) -> None:
        self.assertEqual([], known_failure_problems(REPOSITORY_ROOT))

    def test_the_runner_runs_the_closing_check_as_a_pull_request_check(self) -> None:
        self.assertIn(
            "KnownFailurePullRequestPolicies", {check.__name__ for check in run_validation.PULL_REQUEST_POLICIES}
        )
