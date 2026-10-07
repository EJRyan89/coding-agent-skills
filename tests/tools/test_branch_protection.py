"""Regression suite for tools/branch_protection.py, with `gh api` replaced by a fake runner.

The expected settings are written here as literals, not read back from the module, so a change to what the tool
expects fails this suite until the documented invariants in CONTRIBUTING.md are revisited too.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools import branch_protection

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# The shape and values `gh api` returned for main on 2026-10-07, trimmed to the fields the check reads.
PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": ["validate"]},
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "enforce_admins": {"enabled": True},
    "required_linear_history": {"enabled": True},
    "allow_force_pushes": {"enabled": False},
    "allow_deletions": {"enabled": False},
    "required_conversation_resolution": {"enabled": True},
}
REPOSITORY = {"allow_squash_merge": True, "allow_merge_commit": False, "allow_rebase_merge": False}


def drifted(path: tuple[str, ...], value: object, document: str = "protection") -> list[str]:
    protection: dict[str, Any] = copy.deepcopy(PROTECTION)
    repository: dict[str, Any] = copy.deepcopy(REPOSITORY)
    target = protection if document == "protection" else repository
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return branch_protection.protection_problems(protection, repository)


class ProtectionProblemsTests(unittest.TestCase):
    def test_the_live_settings_hold_every_invariant(self) -> None:
        self.assertEqual([], branch_protection.protection_problems(PROTECTION, REPOSITORY))

    def test_each_drift_is_reported_by_name(self) -> None:
        cases: list[tuple[tuple[str, ...], object, str, str]] = [
            (("required_status_checks", "strict"), False, "protection", "strict"),
            (("required_status_checks", "contexts"), [], "protection", "validate"),
            (("required_linear_history", "enabled"), False, "protection", "linear history"),
            (("enforce_admins", "enabled"), False, "protection", "administrators"),
            (("required_conversation_resolution", "enabled"), False, "protection", "conversation resolution"),
            (("required_pull_request_reviews", "required_approving_review_count"), 1, "protection", "approvals"),
            (("allow_force_pushes", "enabled"), True, "protection", "force pushes"),
            (("allow_deletions", "enabled"), True, "protection", "deletion"),
            (("allow_merge_commit",), True, "repository", "merge commits"),
            (("allow_rebase_merge",), True, "repository", "rebase merges"),
            (("allow_squash_merge",), False, "repository", "squash merges"),
        ]
        for path, value, document, name in cases:
            with self.subTest(path=path):
                problems = drifted(path, value, document)
                self.assertEqual(1, len(problems), problems)
                self.assertIn(name, problems[0])

    def test_a_missing_section_is_drift_not_a_crash(self) -> None:
        protection = {key: value for key, value in PROTECTION.items() if key != "required_pull_request_reviews"}
        problems = branch_protection.protection_problems(protection, REPOSITORY)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("approvals", problems[0])

    def test_validate_need_not_be_the_only_required_check(self) -> None:
        self.assertEqual([], drifted(("required_status_checks", "contexts"), ["validate", "other"]))


class MainTests(unittest.TestCase):
    def run_main(self, answers: dict[str, tuple[int, str]]) -> tuple[int, str, str]:
        calls: list[list[str]] = []

        def runner(arguments: list[str]) -> tuple[int, str]:
            calls.append(arguments)
            return answers[arguments[-1]]

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = branch_protection.main([], runner=runner)
        self.assertTrue(all(call[:1] == ["api"] for call in calls), calls)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_protected_when_nothing_drifted(self) -> None:
        code, stdout, _ = self.run_main(
            {
                "repos/{owner}/{repo}/branches/main/protection": (0, json.dumps(PROTECTION)),
                "repos/{owner}/{repo}": (0, json.dumps(REPOSITORY)),
            }
        )
        self.assertEqual((0, "PROTECTED\n"), (code, stdout))

    def test_each_drift_is_printed_and_the_check_fails(self) -> None:
        loose = {**REPOSITORY, "allow_merge_commit": True, "allow_rebase_merge": True}
        code, stdout, _ = self.run_main(
            {
                "repos/{owner}/{repo}/branches/main/protection": (0, json.dumps(PROTECTION)),
                "repos/{owner}/{repo}": (0, json.dumps(loose)),
            }
        )
        self.assertEqual(1, code)
        lines = stdout.splitlines()
        self.assertEqual("DRIFTED", lines[0])
        self.assertEqual(2, len(lines[1:]), lines)

    def test_a_gh_failure_is_reported_without_a_traceback(self) -> None:
        code, stdout, stderr = self.run_main(
            {
                "repos/{owner}/{repo}/branches/main/protection": (1, "HTTP 404: Branch not protected"),
                "repos/{owner}/{repo}": (0, json.dumps(REPOSITORY)),
            }
        )
        self.assertEqual(2, code)
        self.assertEqual("", stdout)
        self.assertIn("Branch not protected", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_output_that_is_not_json_is_reported_without_a_traceback(self) -> None:
        code, _, stderr = self.run_main(
            {
                "repos/{owner}/{repo}/branches/main/protection": (0, "not json"),
                "repos/{owner}/{repo}": (0, json.dumps(REPOSITORY)),
            }
        )
        self.assertEqual(2, code)
        self.assertNotIn("Traceback", stderr)


class DocumentationTests(unittest.TestCase):
    def test_contributing_states_the_invariants_and_the_check(self) -> None:
        text = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("## How `main` is protected", text)
        section = text.split("## How `main` is protected", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "`validate`",
            "up to date",
            "linear history",
            "squash",
            "administrators",
            "conversation",
            "approval",
            "force push",
            "python tools/branch_protection.py",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section.casefold() if phrase.islower() else section)

    def test_the_release_procedure_confirms_protection_before_tagging(self) -> None:
        text = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/branch_protection.py", before_tagging)


if __name__ == "__main__":
    unittest.main()
