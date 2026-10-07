"""The source checkout's commit: recorded when a deployment commits its manifest, and described by check."""

from __future__ import annotations

import re
from pathlib import Path

from . import platform_support

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
    """The full name of the commit checked out in source_dir, or None when it cannot be read."""
    commit = _git(source_dir, "rev-parse", "HEAD")
    return commit if commit is not None and COMMIT_PATTERN.fullmatch(commit) else None


def describe(source_dir: Path, commit: str | None) -> str:
    """How check names the deployed commit: git describe with the full name, the full name alone, or unknown."""
    if commit is None:
        return "unknown"
    described = _git(source_dir, "describe", "--tags", "--always", commit)
    return f"{described} ({commit})" if described else commit
