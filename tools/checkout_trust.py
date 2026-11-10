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


# A branch's name on a remote: the source a fetch refspec maps a branch of origin from.
BRANCH_PREFIX = "refs/heads/"


def refspec_match(pattern: str, name: str) -> str | None:
    """What `name` matches of one side of a refspec, git's way: the text the side's one `*` stands for, the empty text
    when the side has no `*` and is `name` itself, or None when it does not match."""
    if "*" not in pattern:
        return "" if pattern == name else None
    before, after = pattern.split("*", 1)
    if len(name) < len(before) + len(after) or not (name.startswith(before) and name.endswith(after)):
        return None
    return name[len(before) : len(name) - len(after)]


def fetched_from(refspecs: list[str], ref: str) -> list[str]:
    """The names on the remote that the fetch refspecs `refspecs` write to the local `ref`, one for each refspec whose
    destination matches it, leaving out a name a negative refspec excludes."""
    negatives = [spec[1:] for spec in refspecs if spec.startswith("^")]
    sources: list[str] = []
    for spec in refspecs:
        if spec.startswith("^"):
            continue
        source, separator, destination = spec.removeprefix("+").partition(":")
        captured = refspec_match(destination, ref) if separator else None
        if captured is None:
            continue
        mapped = source.replace("*", captured, 1)
        if not any(refspec_match(negative, mapped) is not None for negative in negatives):
            sources.append(mapped)
    return sources


def is_origin_branch(refspecs: list[str], ref: str) -> bool:
    """Whether the local `ref` is a branch of origin: origin's fetch refspecs `refspecs` write it from a branch on the
    remote and from nothing else, so a name a pull request's refspec, or another ref's, may also write never counts."""
    sources = fetched_from(refspecs, ref)
    return bool(sources) and all(source.startswith(BRANCH_PREFIX) for source in sources)


def origin_branches_holding_head(git: GitClient, directory: Path) -> list[str]:
    """The full names of origin's remote-tracking branches whose history holds the checkout's HEAD, in git's order: the
    local refs origin's fetch refspecs write from its branches alone, never another ref they write, such as a pull
    request's head mirrored under refs/remotes/origin. Raises GitError when git cannot read the checkout."""
    # Exits 1 saying nothing when origin has no fetch refspec, and then no ref is a branch of origin.
    configured = git.quiet_output(["config", "--get-all", "remote.origin.fetch"], directory=directory)
    refspecs = output_lines(configured or "")
    held = output_lines(git.output(["for-each-ref", "--contains", "HEAD", "--format=%(refname)"], directory=directory))
    return [ref for ref in held if is_origin_branch(refspecs, ref)]


def foreign_checkout(git: GitClient, directory: Path) -> str | None:
    """Why the checkout at `directory` follows another repository's pull request, or None when it does not: its
    branch's upstream is a pull request's ref, a remote other than origin, or any other ref that is not a branch, or its
    HEAD is detached at a commit no branch of origin holds. Raises GitError when git cannot read the checkout."""
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
    if merge and not merge.startswith(BRANCH_PREFIX):
        return f"its branch {name} follows {merge}, not a branch"
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
