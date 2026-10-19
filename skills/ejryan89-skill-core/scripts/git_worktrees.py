"""What git says a repository's worktrees are and what each checkout has changed: the one reading the worktree survey
in tools/worktrees.py and repo-cleanup share.

`list_worktrees` reads `git worktree list --porcelain -z`, whose records and fields end in NUL, so a path that holds
a line break is read whole. `changes` reads `git status --porcelain -z` with `--untracked-files=normal`, so an
untracked file counts whatever `status.showUntrackedFiles` says, and reads a rename or a copy, which that layout gives
as the new path followed by the one it came from, as one change.

Every call passes `--no-optional-locks`: a status otherwise refreshes the checkout's index and writes it back under
its lock, and the checkouts read are often ones another session is committing in. A git that exits nonzero raises the
client's classified `GitError` with its `returncode`, so an unreadable checkout is never read as one with nothing
changed; one that cannot run or finish raises it without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from git_client import GitClient

# Reads without the index refresh a status otherwise writes back, so reading a checkout never takes its index lock.
NO_OPTIONAL_LOCKS = "--no-optional-locks"
# The status listing every reading of a checkout's changes uses. status.showUntrackedFiles=no would hide untracked
# files from it, and fail it with --ignored, so the untracked-files mode is named rather than taken from the user's
# configuration.
STATUS_ARGUMENTS = ("status", "--porcelain", "-z", "--untracked-files=normal")
BRANCH_PREFIX = "refs/heads/"


@dataclass(frozen=True)
class Worktree:
    """One worktree: its path, the commit its HEAD names (empty for a bare repository), the branch checked out in it
    without `refs/heads/` (None when its HEAD is detached, or for a bare repository), and whether it is the main
    worktree, which git lists first."""

    path: str
    head: str
    branch: str | None
    main: bool


@dataclass(frozen=True)
class Change:
    """One changed, untracked, or ignored path: its two-letter status code, its path, and, for a rename or a copy,
    the path it came from."""

    code: str
    path: str
    original: str | None = None


def list_worktrees(client: GitClient, directory: str | Path) -> list[Worktree]:
    """Every worktree of the repository holding `directory`, the main worktree first."""
    records: list[dict[str, str]] = []
    listing = client.output([NO_OPTIONAL_LOCKS, "worktree", "list", "--porcelain", "-z"], directory=directory)
    for field in listing.split("\0"):
        key, _, value = field.partition(" ")
        if key == "worktree":
            records.append({"path": value})
        elif records and key in ("HEAD", "branch"):
            records[-1][key] = value
    return [
        Worktree(
            record["path"],
            record.get("HEAD", ""),
            branch.removeprefix(BRANCH_PREFIX) if (branch := record.get("branch")) else None,
            index == 0,
        )
        for index, record in enumerate(records)
    ]


def changes(client: GitClient, directory: str | Path, *options: str) -> list[Change]:
    """Each path `git status` lists for the checkout at `directory`, given `options` after `STATUS_ARGUMENTS`, such
    as `--ignored=matching`, which adds each ignored path with the code `!!`."""
    found: list[Change] = []
    fields = iter(client.output([NO_OPTIONAL_LOCKS, *STATUS_ARGUMENTS, *options], directory=directory).split("\0"))
    for field in fields:
        if not field:
            continue
        code, path = field[:2], field[3:]
        found.append(Change(code, path, next(fields, "") if "R" in code or "C" in code else None))
    return found


def changed_paths(client: GitClient, directory: str | Path) -> int:
    """How many paths in the checkout at `directory` are changed or untracked, a rename or a copy counted once."""
    return len(changes(client, directory))
