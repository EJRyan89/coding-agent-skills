"""The GitHub Actions workflows: their triggers, permissions, secrets, and action pins."""

from __future__ import annotations

import re
import unittest

from validation_support import REPOSITORY_ROOT


def required_match(pattern: str, text: str) -> re.Match[str]:
    """re.search for a check that fails, naming the pattern, when the text does not match it."""
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError(f"nothing matches {pattern!r}")
    return match


def action_pins(workflow: str) -> list[tuple[str, str, str]]:
    """Each `uses:` in a workflow as (action, reference, the rest of the line)."""
    return re.findall(r"(?m)^\s*-?\s*uses:\s*(\S+)@(\S+)(.*)$", workflow)


def workflow_guard_problems(workflow: str, triggers: list[str], actions: set[str]) -> list[str]:
    """Report how a workflow departs from its expected triggers and actions, a read-only token, and no secrets.

    Every action is pinned to a full commit SHA with its version in a comment, which docs/dependency-updates.md
    states and Dependabot reviews.
    """
    problems: list[str] = []
    on_block = re.search(r"(?ms)^on:\n(.*?)^\S", workflow)
    found = re.findall(r"(?m)^  ([A-Za-z_]+):", on_block.group(1)) if on_block else []
    if found != triggers:
        problems.append(f"the triggers are {found}, expected {triggers}")
    if len(re.findall(r"(?m)^\s*permissions:", workflow)) != 1 or not re.search(
        r"(?m)^permissions:\n  contents: read\n(?!  )", workflow
    ):
        problems.append("the permissions are not exactly one top-level `contents: read`")
    if "secrets." in workflow:
        problems.append("the workflow reads a secret")
    if "GITHUB_TOKEN" in workflow or "github.token" in workflow:
        problems.append("the workflow passes the token to a step")
    pins = action_pins(workflow)
    for action, reference, rest in pins:
        if not re.fullmatch(r"[0-9a-f]{40}", reference) or not re.fullmatch(r"\s+# v\d+(?:\.\d+)*", rest):
            problems.append(f"{action}@{reference} is not pinned to a commit SHA with its version in a comment")
    used = {action for action, _, _ in pins}
    if used != actions:
        problems.append(f"the actions used are {sorted(used)}, expected {sorted(actions)}")
    return problems


class WorkflowsPolicies(unittest.TestCase):
    def test_deployable_workflow_is_a_manual_pinned_check_without_secrets(self) -> None:
        workflows = REPOSITORY_ROOT / ".github/workflows"
        workflow = (workflows / "deployable.yml").read_text(encoding="utf-8")
        reviewed = (workflows / "validate.yml").read_text(encoding="utf-8")
        # Dispatch is its only trigger, so it can never be a required status check or run on a pull request.
        self.assertEqual(
            [],
            workflow_guard_problems(
                workflow,
                ["workflow_dispatch"],
                {"actions/checkout", "actions/setup-python", "actions/setup-node", "actions/upload-artifact"},
            ),
        )
        # An action both workflows use is pinned to the commit Dependabot reviews in validate.yml.
        shared = {action for action, _, _ in action_pins(workflow)} & set(re.findall(r"uses:\s*(\S+)@", reviewed))
        self.assertTrue(shared)
        for action in shared:
            with self.subTest(shared=action):
                self.assertEqual(
                    required_match(rf"{re.escape(action)}@(\S+ +# v\S+)", reviewed).group(1),
                    required_match(rf"{re.escape(action)}@(\S+ +# v\S+)", workflow).group(1),
                )

    def test_validate_workflow_is_pinned_read_only_and_runs_on_its_own_events(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # No pull_request_target: a pull request's code never runs with the base repository's token or secrets.
        self.assertEqual(
            [],
            workflow_guard_problems(
                workflow,
                ["pull_request", "push", "schedule", "workflow_dispatch"],
                {"actions/checkout", "actions/setup-python"},
            ),
        )

    def test_deployable_workflow_installs_the_runtime_versions_the_readme_lists_for_the_fresh_runner(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/deployable.yml").read_text(encoding="utf-8")
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        for runtime, key in (
            ("Claude Code", "claude-version"),
            ("Codex CLI", "codex-version"),
            ("GitHub Copilot CLI", "copilot-version"),
        ):
            with self.subTest(runtime=runtime):
                # The third column: the maintainer's machines come first and may be ahead of the runner.
                tested = required_match(rf"(?m)^\| {runtime} \| \S+ \| (\d+(?:\.\d+)+) \|", readme).group(1)
                default = required_match(
                    rf"(?m)^      {key}:\n(?:        .*\n)*?        default: '([^']+)'", workflow
                ).group(1)
                self.assertEqual(tested, default)
