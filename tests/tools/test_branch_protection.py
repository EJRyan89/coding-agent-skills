"""Regression suite for tools/branch_protection.py, with `gh api` replaced by a fake runner.

The expected settings are written here as literals, not read back from the module, so a change to what the tool
expects fails this suite until the documented invariants in CONTRIBUTING.md are revisited too.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
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
REPOSITORY = {
    "allow_squash_merge": True,
    "allow_merge_commit": False,
    "allow_rebase_merge": False,
    "security_and_analysis": {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
        "dependabot_security_updates": {"status": "enabled"},
    },
}
# The private vulnerability reporting and workflow token responses on the same day.
REPORTING = {"enabled": True}
WORKFLOW = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}
DOCUMENTS: dict[str, dict[str, Any]] = {
    "protection": PROTECTION,
    "repository": REPOSITORY,
    "reporting": REPORTING,
    "workflow": WORKFLOW,
}
ENDPOINTS = {
    "protection": "repos/{owner}/{repo}/branches/main/protection",
    "repository": "repos/{owner}/{repo}",
    "reporting": "repos/{owner}/{repo}/private-vulnerability-reporting",
    "workflow": "repos/{owner}/{repo}/actions/permissions/workflow",
}


def problems(documents: dict[str, dict[str, Any]]) -> list[str]:
    return branch_protection.protection_problems(
        documents["protection"], documents["repository"], documents["reporting"], documents["workflow"]
    )


def drifted(path: tuple[str, ...], value: object, document: str = "protection") -> list[str]:
    documents: dict[str, dict[str, Any]] = copy.deepcopy(DOCUMENTS)
    target = documents[document]
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return problems(documents)


def required_step(pattern: str, text: str) -> str:
    """The line matching pattern, failing the test with the text when none does."""
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"no line matches {pattern!r} in:\n{text}")
    return match.group(0)


def answers(**replaced: tuple[int, str]) -> dict[str, tuple[int, str]]:
    """What each endpoint answers: the recorded documents, with any named one replaced."""
    recorded = {name: (0, json.dumps(document)) for name, document in DOCUMENTS.items()}
    return {ENDPOINTS[name]: answer for name, answer in {**recorded, **replaced}.items()}


class ProtectionProblemsTests(unittest.TestCase):
    def test_the_live_settings_hold_every_invariant(self) -> None:
        self.assertEqual([], problems(DOCUMENTS))

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
            (("security_and_analysis", "secret_scanning", "status"), "disabled", "repository", "secret scanning"),
            (
                ("security_and_analysis", "secret_scanning_push_protection", "status"),
                "disabled",
                "repository",
                "push protection",
            ),
            (
                ("security_and_analysis", "dependabot_security_updates", "status"),
                "disabled",
                "repository",
                "Dependabot security updates",
            ),
            (("enabled",), False, "reporting", "private vulnerability reporting"),
            (("default_workflow_permissions",), "write", "workflow", "workflow token"),
        ]
        for path, value, document, name in cases:
            with self.subTest(path=path):
                problems = drifted(path, value, document)
                self.assertEqual(1, len(problems), problems)
                self.assertIn(name, problems[0])

    def test_a_missing_section_is_drift_not_a_crash(self) -> None:
        protection = {key: value for key, value in PROTECTION.items() if key != "required_pull_request_reviews"}
        found = problems({**DOCUMENTS, "protection": protection})
        self.assertEqual(1, len(found), found)
        self.assertIn("approvals", found[0])

    def test_security_settings_hidden_from_the_reader_are_drift_not_a_crash(self) -> None:
        # GitHub omits security_and_analysis for a reader without admin rights.
        repository = {key: value for key, value in REPOSITORY.items() if key != "security_and_analysis"}
        found = problems({**DOCUMENTS, "repository": repository, "workflow": {}})
        self.assertEqual(4, len(found), found)
        for name in ("secret scanning", "push protection", "Dependabot security updates", "workflow token"):
            with self.subTest(name=name):
                self.assertTrue(any(name in problem for problem in found), found)

    def test_each_security_setting_is_reported_with_its_value(self) -> None:
        self.assertEqual(
            [
                ("secret scanning", "enabled"),
                ("push protection", "enabled"),
                ("private vulnerability reporting", "enabled"),
                ("Dependabot security updates", "enabled"),
                ("default workflow token", "read"),
            ],
            branch_protection.security_settings(REPOSITORY, REPORTING, WORKFLOW),
        )
        self.assertEqual(
            [
                ("secret scanning", "unknown"),
                ("push protection", "unknown"),
                ("private vulnerability reporting", "disabled"),
                ("Dependabot security updates", "unknown"),
                ("default workflow token", "unknown"),
            ],
            branch_protection.security_settings({}, {"enabled": False}, {}),
        )

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
        code, stdout, _ = self.run_main(answers())
        self.assertEqual(
            (
                0,
                "PROTECTED\n"
                "Security settings:\n"
                "  secret scanning: enabled\n"
                "  push protection: enabled\n"
                "  private vulnerability reporting: enabled\n"
                "  Dependabot security updates: enabled\n"
                "  default workflow token: read\n",
            ),
            (code, stdout),
        )

    def test_each_drift_is_printed_and_the_check_fails(self) -> None:
        loose = {**REPOSITORY, "allow_merge_commit": True, "allow_rebase_merge": True}
        code, stdout, _ = self.run_main(
            answers(
                repository=(0, json.dumps(loose)),
                workflow=(0, json.dumps({**WORKFLOW, "default_workflow_permissions": "write"})),
            )
        )
        self.assertEqual(1, code)
        lines = stdout.splitlines()
        self.assertEqual("DRIFTED", lines[0])
        self.assertEqual(3, len([line for line in lines if line.startswith("- ")]), lines)
        self.assertIn("  default workflow token: write", lines)

    def test_a_gh_failure_is_reported_without_a_traceback(self) -> None:
        for endpoint in ENDPOINTS:
            with self.subTest(endpoint=endpoint):
                code, stdout, stderr = self.run_main(answers(**{endpoint: (1, "HTTP 404: Not Found")}))
                self.assertEqual(2, code)
                self.assertEqual("", stdout)
                self.assertIn(f"gh api {ENDPOINTS[endpoint]} failed: HTTP 404: Not Found", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_output_that_is_not_json_is_reported_without_a_traceback(self) -> None:
        code, _, stderr = self.run_main(answers(protection=(0, "not json")))
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
            "secret scanning",
            "push protection",
            "private vulnerability reporting",
            "Dependabot security updates",
            "read-only",
            "python tools/branch_protection.py",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section.casefold() if phrase.islower() else section)

    def test_the_release_procedure_confirms_protection_before_tagging(self) -> None:
        text = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        self.assertIn("python tools/branch_protection.py", before_tagging)

    def test_the_release_procedure_checks_the_security_settings_with_the_tool(self) -> None:
        text = (REPOSITORY_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
        before_tagging = text.split("## Before tagging", 1)[1].split("\n## ", 1)[0]
        step = required_step(r"(?m)^5\. \*\*Security settings\.\*\* .*$", before_tagging)
        for phrase in (
            "python tools/branch_protection.py",
            "secret scanning",
            "push protection",
            "private vulnerability reporting",
            "Dependabot security updates",
            "read-only",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, step)


if __name__ == "__main__":
    unittest.main()
