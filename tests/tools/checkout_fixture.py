"""Checkouts of the user's own and checkouts following another repository's pull request, for the suites of the tools
that refuse the second kind through tools/checkout_trust.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient

AUTHOR = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]

# Each way a checkout follows another repository's pull request, with the reason tools/checkout_trust.py gives.
FOREIGN = {
    "pull-request-ref": "its branch main follows refs/pull/7/head, a pull request's ref",
    "another-remote": "its branch main follows someone, not origin",
    "detached-unheld": "its HEAD is detached at a commit no branch of origin holds",
}


def own_checkout(root: Path, git: GitClient) -> Path:
    """A checkout at `root` on main, two commits deep, whose origin/main holds only the first."""
    for arguments in (
        ["init", "-q", "-b", "main"],
        [*AUTHOR, "commit", "-q", "--allow-empty", "-m", "one"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
        [*AUTHOR, "commit", "-q", "--allow-empty", "-m", "two"],
    ):
        git.output(arguments, directory=root)
    return root


def follow(root: Path, git: GitClient, remote: str, merge: str) -> None:
    git.output(["config", "branch.main.remote", remote], directory=root)
    git.output(["config", "branch.main.merge", merge], directory=root)


def make_foreign(root: Path, git: GitClient, case: str) -> str:
    """Turn the own checkout at `root` into the case of FOREIGN named `case`, and return its reason."""
    if case == "pull-request-ref":
        follow(root, git, "origin", "refs/pull/7/head")
    elif case == "another-remote":
        follow(root, git, "someone", "refs/heads/topic")
    elif case == "detached-unheld":
        git.output(["checkout", "-q", "--detach", "HEAD"], directory=root)
    else:
        raise ValueError(f"no such case {case}")
    return FOREIGN[case]
