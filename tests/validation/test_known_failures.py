"""Fixture tests for tests/validation/known_failures.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from known_failures import (
    KNOWN_FAILURES,
    KnownFailure,
    KnownFailuresUnreadable,
    checkout_repository,
    closed_issues,
    closed_known_failure_problems,
    known_failure_problems,
    read_known_failures,
)
from validation_support import REPOSITORY_ROOT, git

from tests import run_validation

ENTRY = {"reader": "module.reader", "case": "deep_nesting", "issue": 4242}
REPOSITORY = "EJRyan89/coding-agent-skills"


class KnownFailureFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / KNOWN_FAILURES).parent.mkdir(parents=True)

    def write(self, value: object) -> None:
        (self.root / KNOWN_FAILURES).write_text(json.dumps(value), encoding="utf-8")


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
            "<!-- Closes #4242 -->\n",
            "Closes #42420\n",
            "discloses #4242\n",
        ):
            with self.subTest(body=body):
                self.assertEqual([], closed_known_failure_problems(self.root, body))

    def test_no_body_passes(self) -> None:
        self.write([ENTRY])
        self.assertEqual([], closed_known_failure_problems(self.root, None))

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


class RepositoryTests(unittest.TestCase):
    def test_the_repository_list_has_its_shape(self) -> None:
        self.assertEqual([], known_failure_problems(REPOSITORY_ROOT))

    def test_the_runner_runs_the_closing_check_as_a_pull_request_check(self) -> None:
        self.assertIn(
            "KnownFailurePullRequestPolicies", {check.__name__ for check in run_validation.PULL_REQUEST_POLICIES}
        )
