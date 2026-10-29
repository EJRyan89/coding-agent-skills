"""The source checkout's commit: recorded when a deployment commits its manifest, and described by check."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from flat_text import flat_text
from git_client import GitClient, GitError, GitResult, output_lines

from . import platform_support
from .errors import DeployError

# A full object name in a SHA-1 or a SHA-256 repository. Nothing else is recorded or read back, so a manifest value
# passed to git can never be taken for an option.
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _client(git: str) -> GitClient:
    """GitClient over the deployer's run_tool, with stdout and stderr kept apart, so a warning git prints beside an
    answer is never part of it, and the client tells git's answer that a commit is not there from a failure."""

    def runner(command: Sequence[str], timeout: float) -> GitResult:
        result = platform_support.run_tool([git, *command[1:]], timeout=timeout, separate_errors=True)
        return GitResult(result.returncode, result.output, result.errors)

    return GitClient(runner, timeout=platform_support.TOOL_DEADLINE_SECONDS)


def _description(source_dir: Path, commit: str) -> str | None:
    """What git describe names the commit in source_dir, or None when there is no checkout or Git to ask, or git answers
    that the checkout holds no such commit. Any other failure, and an answer that is not one name, is refused with
    git's reason."""
    if not (source_dir / ".git").exists():
        return None
    git = platform_support.find_executable("git")
    if git is None:
        return None
    cannot = f"ERROR: Cannot describe the deployed commit {commit} in {platform_support.normalize(source_dir)}"
    client = _client(git)
    try:
        if client.commit_of(commit, directory=source_dir) is None:
            return None
        described = client.output(["describe", "--tags", "--always", commit], directory=source_dir)
    except GitError as error:
        if error.kind == "not_repository":
            return None
        reason = flat_text(str(error))
        raise DeployError(f"{cannot}: {reason}", "Put right what Git reports in the checkout, then retry.") from error
    except OSError as exc:
        raise DeployError(f"{cannot}: {exc.strerror or exc}", "Check that Git runs, then retry.") from exc
    lines = described.strip().splitlines()
    if len(lines) != 1:
        raise DeployError(f"{cannot}: git describe answered {flat_text(described)!r}, not one name")
    return lines[0].strip()


def head_commit(source_dir: Path) -> str | None:
    """The full name of the commit checked out in source_dir, or None when source_dir is no Git checkout.

    A checkout whose commit git cannot name is refused with git's reason, never recorded as having none: a deployment
    records the commit it deployed, and one that recorded none would drop the commit an earlier deployment recorded,
    while keeping that one would name a commit this deployment may not have deployed.
    """
    if not (source_dir / ".git").exists():
        return None
    cannot = f"ERROR: Cannot read the commit checked out in {platform_support.normalize(source_dir)}"
    git = platform_support.find_executable("git")
    if git is None:
        raise DeployError(
            f"{cannot}: Git was not found.",
            'Install it as "Installing the tools" in docs/installation.md says, then retry.',
        )
    try:
        result = platform_support.run_tool([git, "-C", str(source_dir), "rev-parse", "HEAD"])
    except OSError as exc:
        raise DeployError(f"{cannot}: {exc.strerror or exc}", "Check that Git runs, then retry.") from exc
    # Git's warnings share the captured output with the name, so the name is the one line that is a full name.
    commits = [line.strip() for line in output_lines(result.output) if COMMIT_PATTERN.fullmatch(line.strip())]
    if result.returncode == 0 and len(commits) == 1:
        return commits[0]
    reason = flat_text(result.output) or f"git exited with {result.returncode}"
    raise DeployError(f"{cannot}: {reason}", "Put right what Git reports in the checkout, then retry.")


def describe(source_dir: Path, commit: str | None) -> str:
    """How check names the deployed commit: git describe with the full name, the full name alone, or unknown.

    The full name stands alone only where there is nothing to describe it from: no checkout, no Git, or git's answer
    that the checkout holds no such commit. Git failing for any other reason is refused with its reason; a warning git
    prints on stderr beside a name is not part of the name.
    """
    if commit is None:
        return "unknown"
    described = _description(source_dir, commit)
    return f"{described} ({commit})" if described else commit
