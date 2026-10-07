"""Confirm that main's live branch protection and merge settings still hold the invariants CONTRIBUTING.md states.

Usage:
  python tools/branch_protection.py

It reads main's branch protection and the repository's merge settings through `gh api`, for the repository the
current checkout's remote names, and prints PROTECTED (exit 0) when every invariant holds, or DRIFTED and one line
per invariant that no longer holds (exit 1). It exits 2 when `gh` cannot read them, such as without a sign-in or
without admin rights on the repository. The release procedure in docs/releasing.md runs it before tagging.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployer import platform_support

PROTECTION_ENDPOINT = "repos/{owner}/{repo}/branches/main/protection"
REPOSITORY_ENDPOINT = "repos/{owner}/{repo}"
REQUIRED_CHECK = "validate"
Runner = Callable[[list[str]], "tuple[int, str]"]


def _get(document: Mapping[str, object], *path: str) -> object:
    value: object = document
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def protection_problems(protection: Mapping[str, object], repository: Mapping[str, object]) -> list[str]:
    """Report each invariant of main's protection, or of the repository's merge settings, that no longer holds."""
    contexts = _get(protection, "required_status_checks", "contexts")
    expectations: list[tuple[bool, str]] = [
        (
            isinstance(contexts, list) and REQUIRED_CHECK in contexts,
            f"the `{REQUIRED_CHECK}` check is not required",
        ),
        (
            _get(protection, "required_status_checks", "strict") is True,
            "required checks are not strict: a branch need not be up to date with main",
        ),
        (_get(protection, "required_linear_history", "enabled") is True, "linear history is not required"),
        (_get(protection, "enforce_admins", "enabled") is True, "the rules are not enforced for administrators"),
        (
            _get(protection, "required_conversation_resolution", "enabled") is True,
            "conversation resolution is not required",
        ),
        (
            _get(protection, "required_pull_request_reviews", "required_approving_review_count") == 0,
            "approvals are required, or pull requests are no longer required (expected 0 approvals)",
        ),
        (_get(protection, "allow_force_pushes", "enabled") is False, "force pushes to main are allowed"),
        (_get(protection, "allow_deletions", "enabled") is False, "deletion of main is allowed"),
        (repository.get("allow_squash_merge") is True, "squash merges are not allowed"),
        (repository.get("allow_merge_commit") is False, "merge commits are allowed"),
        (repository.get("allow_rebase_merge") is False, "rebase merges are allowed"),
    ]
    return [problem for holds, problem in expectations if not holds]


def run_gh(arguments: list[str]) -> tuple[int, str]:
    gh = platform_support.find_executable("gh")
    if gh is None:
        return 1, "GitHub CLI (gh) was not found on PATH"
    completed = subprocess.run(
        [gh, *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False
    )
    return completed.returncode, completed.stdout if completed.returncode == 0 else completed.stderr


def _read(runner: Runner, endpoint: str) -> dict[str, object]:
    code, output = runner(["api", endpoint])
    if code != 0:
        raise ValueError(f"gh api {endpoint} failed: {output.strip()}")
    try:
        document = json.loads(output)
    except json.JSONDecodeError as error:
        raise ValueError(f"gh api {endpoint} did not return JSON: {error}") from error
    if not isinstance(document, dict):
        raise ValueError(f"gh api {endpoint} did not return a JSON object")
    return document


def main(arguments: list[str] | None = None, runner: Runner = run_gh) -> int:
    if arguments:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        protection = _read(runner, PROTECTION_ENDPOINT)
        repository = _read(runner, REPOSITORY_ENDPOINT)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    problems = protection_problems(protection, repository)
    if not problems:
        print("PROTECTED")
        return 0
    print("DRIFTED")
    for problem in problems:
        print(f"- {problem}")
    return 1


if __name__ == "__main__":
    platform_support.use_utf8_output()
    sys.exit(main(sys.argv[1:]))
