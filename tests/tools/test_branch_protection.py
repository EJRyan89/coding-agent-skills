"""Regression suite for tools/branch_protection.py, with `gh api` replaced by a fake runner.

The expected settings are written here as literals, not read back from the module, so a change to what the tool
expects fails this suite until the documented invariants in CONTRIBUTING.md are revisited too.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import io
import json
import re
import runpy
import sys
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from github_client import CommandResult, GitHubClient, GitHubError

from tools import branch_protection

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

# The shape and values `gh api` returned for main on 2026-10-07, trimmed to the fields the check reads, with the
# required context renamed from `validate` to `Validation` when the gating job was named.
PROTECTION = {
    "required_status_checks": {"strict": True, "contexts": ["Validation"]},
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
    # The squash commit settings as `gh api` returned them on 2026-10-09.
    "squash_merge_commit_title": "PR_TITLE",
    "squash_merge_commit_message": "PR_BODY",
    # Discussions as `gh api` returned it on 2026-10-10.
    "has_discussions": False,
    "security_and_analysis": {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
        "dependabot_security_updates": {"status": "enabled"},
    },
}
# The private vulnerability reporting and workflow token responses on the same day.
REPORTING = {"enabled": True}
WORKFLOW = {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}


def issue_config(text: str) -> dict[str, Any]:
    """The contents endpoint's answer for a file: its text in base64, as GitHub sends it."""
    return {"encoding": "base64", "content": base64.b64encode(text.encode()).decode()}


# The issue template configuration on main on 2026-10-10, without its contact link.
ISSUE_CONFIG = issue_config("blank_issues_enabled: false\ncontact_links: []\n")
DOCUMENTS: dict[str, dict[str, Any]] = {
    "protection": PROTECTION,
    "repository": REPOSITORY,
    "reporting": REPORTING,
    "workflow": WORKFLOW,
    "issue_config": ISSUE_CONFIG,
}
ENDPOINTS = {
    "protection": "repos/{owner}/{repo}/branches/main/protection",
    "repository": "repos/{owner}/{repo}",
    "reporting": "repos/{owner}/{repo}/private-vulnerability-reporting",
    "workflow": "repos/{owner}/{repo}/actions/permissions/workflow",
    "issue_config": "repos/{owner}/{repo}/contents/.github/ISSUE_TEMPLATE/config.yml?ref=main",
}


ISSUE_CONFIG_ENABLED = issue_config("blank_issues_enabled: true\n")["content"]
SECURITY_OUTPUT = (
    "Security settings:\n"
    "  secret scanning: enabled\n"
    "  push protection: enabled\n"
    "  private vulnerability reporting: enabled\n"
    "  Dependabot security updates: enabled\n"
    "  default workflow token: read\n"
)
BLANK_ISSUES_DRIFT = (
    "- blank issues are enabled: .github/ISSUE_TEMPLATE/config.yml on main does not set blank_issues_enabled: false\n"
)


def problems(documents: dict[str, dict[str, Any]]) -> list[str]:
    return branch_protection.protection_problems(
        documents["protection"],
        documents["repository"],
        documents["reporting"],
        documents["workflow"],
        documents["issue_config"],
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
            (("required_status_checks", "contexts"), [], "protection", "Validation"),
            (("required_linear_history", "enabled"), False, "protection", "linear history"),
            (("enforce_admins", "enabled"), False, "protection", "administrators"),
            (("required_conversation_resolution", "enabled"), False, "protection", "conversation resolution"),
            (("required_pull_request_reviews", "required_approving_review_count"), 1, "protection", "approvals"),
            (("allow_force_pushes", "enabled"), True, "protection", "force pushes"),
            (("allow_deletions", "enabled"), True, "protection", "deletion"),
            (("allow_merge_commit",), True, "repository", "merge commits"),
            (("allow_rebase_merge",), True, "repository", "rebase merges"),
            (("allow_squash_merge",), False, "repository", "squash merges"),
            (("squash_merge_commit_title",), "COMMIT_OR_PR_TITLE", "repository", "expected PR_TITLE"),
            (("squash_merge_commit_message",), "COMMIT_MESSAGES", "repository", "expected PR_BODY"),
            (("squash_merge_commit_message",), "BLANK", "repository", "carries its upgrade note"),
            (("has_discussions",), True, "repository", "Discussions are enabled"),
            (("content",), ISSUE_CONFIG_ENABLED, "issue_config", "blank issues are enabled"),
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

    def test_an_issue_configuration_that_does_not_turn_blank_issues_off_is_drift_not_a_crash(self) -> None:
        # GitHub offers a blank issue unless config.yml sets blank_issues_enabled to false.
        for name, document in (
            ("no setting", issue_config("contact_links: []\n")),
            ("quoted", issue_config('blank_issues_enabled: "false"\n')),
            ("quoted with comment", issue_config('blank_issues_enabled: "false" # a string\n')),
            ("no comment separator", issue_config("blank_issues_enabled: false#not-a-comment\n")),
            ("extra scalar text", issue_config("blank_issues_enabled: false extra # comment\n")),
            ("empty with comment", issue_config("blank_issues_enabled: # no value\n")),
            ("commented out", issue_config("# blank_issues_enabled: false\n")),
            ("nested", issue_config("other:\n  blank_issues_enabled: false # nested\n")),
            ("unsupported scalar", issue_config("blank_issues_enabled: [false] # a list\n")),
            ("not base64", {"encoding": "base64", "content": "%%%"}),
            ("not UTF-8", {"encoding": "base64", "content": base64.b64encode(b"\xff").decode()}),
            ("no content", {}),
        ):
            with self.subTest(name=name):
                found = problems({**DOCUMENTS, "issue_config": document})
                self.assertEqual(1, len(found), found)
                self.assertIn("blank issues are enabled", found[0])
        self.assertEqual([], problems({**DOCUMENTS, "issue_config": issue_config("blank_issues_enabled: False\r\n")}))

    def test_a_repository_that_does_not_say_discussions_are_off_is_drift(self) -> None:
        repository = {key: value for key, value in REPOSITORY.items() if key != "has_discussions"}
        found = problems({**DOCUMENTS, "repository": repository})
        self.assertEqual(1, len(found), found)
        self.assertIn("Discussions", found[0])

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

    def test_validation_need_not_be_the_only_required_check(self) -> None:
        self.assertEqual([], drifted(("required_status_checks", "contexts"), ["Validation", "other"]))

    def test_the_job_id_is_not_the_required_context(self) -> None:
        self.assertEqual(
            ["the `Validation` check is not required"], drifted(("required_status_checks", "contexts"), ["validate"])
        )

    def test_the_gating_job_reports_the_required_context(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "validate.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  validate:\n    name: Validation\n")


class MainTests(unittest.TestCase):
    def run_main(
        self, answers: dict[str, tuple[int, str]], issue_error: GitHubError | None = None
    ) -> tuple[int, str, str]:
        calls: list[list[str]] = []

        def runner(arguments: Sequence[str]) -> CommandResult:
            calls.append(list(arguments))
            if arguments[-1] == ENDPOINTS["issue_config"] and issue_error is not None:
                raise issue_error
            code, output = answers[arguments[-1]]
            return CommandResult(code, output if code == 0 else "", output if code != 0 else "")

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = branch_protection.main([], client=GitHubClient(runner=runner, sleeper=lambda _: None))
        self.assertTrue(all(call[:2] == ["gh", "api"] and len(call) == 3 for call in calls), calls)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_protected_when_nothing_drifted(self) -> None:
        self.assertEqual((0, "PROTECTED\n" + SECURITY_OUTPUT, ""), self.run_main(answers()))

    def test_a_missing_issue_configuration_is_drift_with_exit_one(self) -> None:
        self.assertEqual(
            (1, "DRIFTED\n" + BLANK_ISSUES_DRIFT + SECURITY_OUTPUT, ""),
            self.run_main(answers(issue_config=(1, "gh: Not Found (HTTP 404)"))),
        )

    def test_a_trailing_comment_on_false_is_protected_with_exit_zero(self) -> None:
        for text in (
            "blank_issues_enabled: false  # no blank issues\n",
            "blank_issues_enabled: false\t# no blank issues\r\n",
            "blank_issues_enabled: False # blank_issues_enabled: true\ncontact_links: []\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    (0, "PROTECTED\n" + SECURITY_OUTPUT, ""),
                    self.run_main(answers(issue_config=(0, json.dumps(issue_config(text))))),
                )

    def test_a_trailing_comment_does_not_turn_true_into_false(self) -> None:
        self.assertEqual(
            (1, "DRIFTED\n" + BLANK_ISSUES_DRIFT + SECURITY_OUTPUT, ""),
            self.run_main(answers(issue_config=(0, json.dumps(issue_config("blank_issues_enabled: true # false\n"))))),
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
                message = "HTTP 403: Resource not accessible by integration"
                self.assertEqual(
                    (2, "", f"ERROR: gh api {ENDPOINTS[endpoint]} failed: {message}\n"),
                    self.run_main(answers(**{endpoint: (1, message)})),
                )

    def test_not_found_is_an_error_for_every_other_endpoint(self) -> None:
        for endpoint in ENDPOINTS:
            if endpoint == "issue_config":
                continue
            with self.subTest(endpoint=endpoint):
                message = "HTTP 404: Not Found"
                self.assertEqual(
                    (2, "", f"ERROR: gh api {ENDPOINTS[endpoint]} failed: {message}\n"),
                    self.run_main(answers(**{endpoint: (1, message)})),
                )

    def test_real_issue_configuration_read_failures_keep_their_diagnostics_and_exit_two(self) -> None:
        for message in (
            "HTTP 401: Requires authentication",
            "HTTP 403: Forbidden (Not Found)",
            "error connecting to api.github.com: connection refused",
            "HTTP 500: Internal Server Error",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    (2, "", f"ERROR: gh api {ENDPOINTS['issue_config']} failed: {message}\n"),
                    self.run_main(answers(issue_config=(1, message))),
                )

    def test_client_execution_failures_are_not_mistaken_for_missing_configuration(self) -> None:
        for kind, message in (
            ("prerequisite", "GitHub CLI executable 'gh' was not found; install GitHub CLI and authenticate first"),
            ("execution", "GitHub CLI could not be started: file not found"),
            ("timeout", "GitHub CLI did not finish within 300 seconds"),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    (2, "", f"ERROR: gh api {ENDPOINTS['issue_config']} failed: {message}\n"),
                    self.run_main(answers(), issue_error=GitHubError(message, kind=kind)),
                )

    def test_rate_limit_retries_do_not_turn_a_read_failure_into_missing_configuration(self) -> None:
        message = "HTTP 429: rate limit exceeded (Not Found)"
        self.assertEqual(
            (
                2,
                "",
                f"ERROR: gh api {ENDPOINTS['issue_config']} failed: {message} (gave up after 5 retries)\n",
            ),
            self.run_main(answers(issue_config=(1, message))),
        )

    def test_malformed_issue_configuration_responses_remain_errors_with_exit_two(self) -> None:
        for output in ("[]", "null"):
            with self.subTest(output=output):
                self.assertEqual(
                    (2, "", f"ERROR: gh api {ENDPOINTS['issue_config']} did not return a JSON object\n"),
                    self.run_main(answers(issue_config=(0, output))),
                )
        code, stdout, stderr = self.run_main(answers(issue_config=(0, "not json")))
        self.assertEqual((2, ""), (code, stdout))
        self.assertTrue(stderr.startswith(f"ERROR: gh api {ENDPOINTS['issue_config']} did not return JSON: "))
        self.assertNotIn("Traceback", stderr)

    def test_output_that_is_not_json_is_reported_without_a_traceback(self) -> None:
        code, _, stderr = self.run_main(answers(protection=(0, "not json")))
        self.assertEqual(2, code)
        self.assertNotIn("Traceback", stderr)

    def test_cli_entry_point_has_the_exact_status_output_and_process_exit_code(self) -> None:
        for response, expected in (
            ((1, "gh: Not Found (HTTP 404)"), (1, "DRIFTED\n" + BLANK_ISSUES_DRIFT + SECURITY_OUTPUT, "")),
            (
                (0, json.dumps(issue_config("blank_issues_enabled: false  # no blank issues\n"))),
                (0, "PROTECTED\n" + SECURITY_OUTPUT, ""),
            ),
            (
                (1, "HTTP 403: Forbidden"),
                (2, "", f"ERROR: gh api {ENDPOINTS['issue_config']} failed: HTTP 403: Forbidden\n"),
            ),
            (
                (1, "error connecting to api.github.com: connection refused"),
                (
                    2,
                    "",
                    f"ERROR: gh api {ENDPOINTS['issue_config']} failed: "
                    "error connecting to api.github.com: connection refused\n",
                ),
            ),
        ):
            with self.subTest(response=response):
                recorded = answers(issue_config=response)
                results = [
                    CommandResult(code, output if code == 0 else "", output if code != 0 else "")
                    for code, output in recorded.values()
                ]
                stdout, stderr = io.StringIO(), io.StringIO()
                with (
                    mock.patch("github_client.subprocess_runner", side_effect=results) as runner,
                    mock.patch.object(sys, "argv", ["branch_protection.py"]),
                    contextlib.redirect_stdout(stdout),
                    contextlib.redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as raised,
                ):
                    runpy.run_path(str(REPOSITORY_ROOT / "tools" / "branch_protection.py"), run_name="__main__")
                self.assertEqual(expected, (raised.exception.code, stdout.getvalue(), stderr.getvalue()))
                self.assertEqual(
                    [["gh", "api", endpoint] for endpoint in ENDPOINTS.values()],
                    [call.args[0] for call in runner.call_args_list],
                )


class DocumentationTests(unittest.TestCase):
    def test_contributing_states_the_invariants_and_the_check(self) -> None:
        text = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("## How `main` is protected", text)
        section = text.split("## How `main` is protected", 1)[1].split("\n## ", 1)[0]
        for phrase in (
            "`Validation`",
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

    def test_contributing_states_the_issue_settings_and_the_check(self) -> None:
        text = (REPOSITORY_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        section = text.split("\n## Questions\n", 1)[1].split("\n## ", 1)[0]
        for phrase in ("Discussions stay off", "blank issues stay off", "python tools/branch_protection.py"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, section)

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
