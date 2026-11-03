"""Whether a checkout follows another repository's pull request, which a tool that deploys and runs the checkout's
own skills and fixtures in sessions signed in as the user refuses before it starts anything.

A fixture is as trusted as the checkout that holds it ("A fixture is as trusted as the checkout that holds it" in
docs/design.md), so tools/skill_evals.py, tools/runtime_canary.py, and tools/plugin_canary.py each call `refusal`
before they make a home, deploy, or build, and print its reason as their FAILED line.
"""

from __future__ import annotations

import functools
import sys
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from git_client import GitClient, GitError, output_line, output_lines

# The upstreams of a branch that is the user's own: none, a local branch, or this repository's origin.
OWN_REMOTES = frozenset({"", ".", "origin"})


def origin_branches_holding_head(git: GitClient, directory: Path) -> list[str]:
    """The full names of origin's remote-tracking branches whose history holds the checkout's HEAD, in git's order.
    Raises GitError when git cannot read the checkout."""
    held = git.output(
        ["for-each-ref", "--contains", "HEAD", "--format=%(refname)", "refs/remotes/origin"], directory=directory
    )
    return output_lines(held)


def foreign_checkout(git: GitClient, directory: Path) -> str | None:
    """Why the checkout at `directory` follows another repository's pull request, or None when it does not: its
    branch's upstream is a pull request's ref or a remote other than origin, or its HEAD is detached at a commit no
    branch of origin holds. Raises GitError when git cannot read the checkout."""
    # Each read exits 1 saying nothing for what is not there: a detached HEAD, or a branch that follows nothing.
    branch = git.quiet_output(["symbolic-ref", "-q", "--short", "HEAD"], directory=directory)
    if branch is None:
        if origin_branches_holding_head(git, directory):
            return None
        return "its HEAD is detached at a commit no branch of origin holds"
    name = output_line(branch)
    remote = output_line(git.quiet_output(["config", "--get", f"branch.{name}.remote"], directory=directory) or "")
    merge = output_line(git.quiet_output(["config", "--get", f"branch.{name}.merge"], directory=directory) or "")
    if merge.startswith("refs/pull/"):
        return f"its branch {name} follows {merge}, a pull request's ref"
    if remote not in OWN_REMOTES:
        return f"its branch {name} follows {remote}, not origin"
    return None


def reader(directory: Path) -> Callable[[], str | None]:
    """`foreign_checkout` of the checkout at `directory`, read when called."""
    return functools.partial(foreign_checkout, GitClient(), directory)


def refusal(checkout: Callable[[], str | None]) -> str | None:
    """Why a run refuses the checkout `checkout` reads, or None: it follows another repository's pull request, or git
    cannot read its branch, which is never read as a branch of the user's own."""
    try:
        foreign = checkout()
    except GitError as exc:
        return f"cannot read the checkout's branch: {exc}"
    return None if foreign is None else f"the checkout follows another repository's pull request: {foreign}"
