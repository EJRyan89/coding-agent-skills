"""The source checkout's commit: recorded when a deployment commits its manifest, and described by check."""

from __future__ import annotations

import re
from pathlib import Path

from flat_text import flat_text

from . import platform_support
from .errors import DeployError

# A full object name in a SHA-1 or a SHA-256 repository. Nothing else is recorded or read back, so a manifest value
# passed to git can never be taken for an option.
COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


def _git(source_dir: Path, *arguments: str) -> str | None:
    """One line of git's output in source_dir, or None when it is no checkout, git is missing, or git fails."""
    if not (source_dir / ".git").exists():
        return None
    git = platform_support.find_executable("git")
    if git is None:
        return None
    try:
        result = platform_support.run_tool([git, "-C", str(source_dir), *arguments])
    except OSError:
        return None
    lines = result.output.strip().splitlines()
    return lines[0].strip() if result.returncode == 0 and len(lines) == 1 else None


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
    commits = [line.strip() for line in result.output.splitlines() if COMMIT_PATTERN.fullmatch(line.strip())]
    if result.returncode == 0 and len(commits) == 1:
        return commits[0]
    reason = flat_text(result.output) or f"git exited with {result.returncode}"
    raise DeployError(f"{cannot}: {reason}", "Put right what Git reports in the checkout, then retry.")


def describe(source_dir: Path, commit: str | None) -> str:
    """How check names the deployed commit: git describe with the full name, the full name alone, or unknown."""
    if commit is None:
        return "unknown"
    described = _git(source_dir, "describe", "--tags", "--always", commit)
    return f"{described} ({commit})" if described else commit
