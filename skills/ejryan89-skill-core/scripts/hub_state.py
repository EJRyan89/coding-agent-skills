"""How a repository's main checkout differs from the state it keeps between changes: clean, on its default branch,
and at the commit its remote's default branch held when git last fetched it.

Where parallel sessions work beside a main checkout, the hub, each change belongs in a worktree, and the hub only
follows the remote. A write to the hub a session makes by mistake, which no guard saw, shows here as one of these
differences. The shepherd-pull-requests tick and repo-cleanup run this check after their own steps, so such a write
is reported at the next run, naming what differs.

Each difference is a kind and a sentence:

    changed   it has <n> uncommitted or untracked path(s): <path>, ...   status lists them, the first few named
    branch    it is on <branch>, not <default>, or on a detached HEAD
    unknown   git holds no <remote>/<default> to compare it with
    ahead     it is <n> commit(s) ahead of <remote>/<default>
    behind    it is <n> commit(s) behind <remote>/<default>

A checkout git cannot read raises GitError with git's reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import whole_number
from flat_text import flat_text
from git_client import GitClient, GitError, output_line, output_lines

# How many changed paths a difference names before it counts the rest.
NAMED_PATHS = 3
# The status letters of an entry that names the path it came from in a field of its own after its own.
FROM_FIELD = frozenset("RC")


@dataclass(frozen=True)
class Difference:
    """One way the checkout differs: its kind, one of the module's, and a sentence that says it on one line."""

    kind: str
    text: str


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def changed_paths(fields: list[str]) -> list[str]:
    """The paths `git status --porcelain -z` lists, each once: a rename's or a copy's path, never the one it came
    from."""
    paths: list[str] = []
    entries = iter(fields)
    for entry in entries:
        paths.append(entry[3:])
        if entry[:1] in FROM_FIELD or entry[1:2] in FROM_FIELD:
            next(entries, None)
    return paths


def tracking_commit(git: GitClient, checkout: Path, tracking: str) -> str | None:
    """The commit the one ref `tracking` names, read by its full name alone, or None where git holds no such ref."""
    listed = git.output(["for-each-ref", "--format=%(objectname)%00%(refname)", tracking], directory=checkout)
    for line in output_lines(listed):
        commit, _, name = line.partition("\0")
        if name == tracking:
            return commit
    return None


def differences(git: GitClient, checkout: Path, branch: str, remote: str = "origin") -> list[Difference]:
    """How `checkout` differs from a clean checkout of `branch` at the commit `<remote>/<branch>` holds: none when it
    stands so."""
    found: list[Difference] = []
    paths = changed_paths(git.names(["status", "--porcelain", "--untracked-files=normal"], directory=checkout))
    if paths:
        named = ", ".join(flat_text(path) for path in paths[:NAMED_PATHS])
        rest = f", and {len(paths) - NAMED_PATHS} more" if len(paths) > NAMED_PATHS else ""
        found.append(
            Difference("changed", f"it has {plural(len(paths), 'uncommitted or untracked path')}: {named}{rest}")
        )
    # Exit 1 with nothing on stderr is git's answer that HEAD is detached; any other failure raises with git's reason.
    name = output_line(git.quiet_output(["symbolic-ref", "--quiet", "--short", "HEAD"], directory=checkout) or "")
    if name != branch:
        found.append(Difference("branch", f"it is on {flat_text(name) or 'a detached HEAD'}, not {branch}"))
    tracking = f"refs/remotes/{remote}/{branch}"
    commit = tracking_commit(git, checkout, tracking)
    if commit is None:
        found.append(Difference("unknown", f"git holds no {remote}/{branch} to compare it with"))
        return found
    counts = output_line(git.output(["rev-list", "--left-right", "--count", f"HEAD...{commit}"], directory=checkout))
    unread = GitError(f"git rev-list answered {counts!r} for how far HEAD is from {remote}/{branch}, not two counts")
    left, _, right = counts.partition("\t")
    ahead, behind = whole_number.read(left, unread), whole_number.read(right, unread)
    if ahead:
        found.append(Difference("ahead", f"it is {plural(ahead, 'commit')} ahead of {remote}/{branch}"))
    if behind:
        found.append(Difference("behind", f"it is {plural(behind, 'commit')} behind {remote}/{branch}"))
    return found
