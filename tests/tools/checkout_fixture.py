"""Checkouts of the user's own and checkouts following another repository's pull request, for the suites of the tools
that refuse the second kind through tools/checkout_trust.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient

AUTHOR = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def another_remote(branch: str, remote: str) -> str:
    """The reason tools/checkout_trust.py gives for the branch `branch` following a branch of `remote`, not origin."""
    return (
        f"its branch {branch} follows {remote}, a remote other than origin, as gh pr checkout sets for a fork's pull"
        " request; if the branch is your own, run git branch --unset-upstream, or push it to your fork without -u"
    )


# Each way a checkout follows another repository's pull request, or cannot be told from one, with the reason
# tools/checkout_trust.py gives. A branch pushed to the user's own fork with -u follows that fork's branch, as
# gh pr checkout tracks another author's pull request from a fork, so it is refused too.
FOREIGN = {
    "pull-request-ref": "its branch main follows refs/pull/7/head, a pull request's ref",
    "pull-request-ref-on-another-remote": "its branch main follows refs/pull/7/head, a pull request's ref",
    "another-remote": another_remote("main", "someone"),
    "own-fork": another_remote("main", "fork"),
    "detached-unheld": "its HEAD is detached at a commit no branch of origin holds",
    "detached-mirrored-pull-request": "its HEAD is detached at a commit no branch of origin holds",
}

# The fetch refspec git clone gives origin, and the one that mirrors its pull requests' heads beside its branches.
BRANCHES_REFSPEC = "+refs/heads/*:refs/remotes/origin/*"
PULL_REQUESTS_REFSPEC = "+refs/pull/*/head:refs/remotes/origin/pr/*"


def own_checkout(root: Path, git: GitClient) -> Path:
    """A checkout at `root` on main, two commits deep, whose origin/main holds only the first, with the fetch refspec
    git clone gives origin."""
    for arguments in (
        ["init", "-q", "-b", "main"],
        ["config", "remote.origin.fetch", BRANCHES_REFSPEC],
        [*AUTHOR, "commit", "-q", "--allow-empty", "-m", "one"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
        [*AUTHOR, "commit", "-q", "--allow-empty", "-m", "two"],
    ):
        git.output(arguments, directory=root)
    return root


def follow(root: Path, git: GitClient, remote: str, merge: str) -> None:
    git.output(["config", "branch.main.remote", remote], directory=root)
    git.output(["config", "branch.main.merge", merge], directory=root)


def push_to_own_fork(root: Path, git: GitClient, *options: str) -> None:
    """Add a bare repository beside `root` as the remote fork, as CONTRIBUTING.md has a contributor without push access
    do, and push main to it with `options`, such as -u."""
    fork = root.parent / f"{root.name} fork.git"
    git.output(["init", "-q", "--bare", str(fork)])
    git.output(["remote", "add", "fork", str(fork)], directory=root)
    git.output(["push", "-q", *options, "fork", "main"], directory=root)


def detach_at_mirrored_pull_request(root: Path, git: GitClient) -> None:
    """Mirror pull request 7's head, at the commit origin/main does not hold, under refs/remotes/origin/pr/7 as
    PULL_REQUESTS_REFSPEC fetches it, and detach HEAD there."""
    git.output(["config", "--add", "remote.origin.fetch", PULL_REQUESTS_REFSPEC], directory=root)
    git.output(["update-ref", "refs/remotes/origin/pr/7", "HEAD"], directory=root)
    git.output(["checkout", "-q", "--detach", "refs/remotes/origin/pr/7"], directory=root)


def make_foreign(root: Path, git: GitClient, case: str) -> str:
    """Turn the own checkout at `root` into the case of FOREIGN named `case`, and return its reason."""
    if case == "pull-request-ref":
        follow(root, git, "origin", "refs/pull/7/head")
    elif case == "pull-request-ref-on-another-remote":
        follow(root, git, "someone", "refs/pull/7/head")
    elif case == "another-remote":
        follow(root, git, "someone", "refs/heads/topic")
    elif case == "own-fork":
        push_to_own_fork(root, git, "-u")
    elif case == "detached-unheld":
        git.output(["checkout", "-q", "--detach", "HEAD"], directory=root)
    elif case == "detached-mirrored-pull-request":
        detach_at_mirrored_pull_request(root, git)
    else:
        raise ValueError(f"no such case {case}")
    return FOREIGN[case]
