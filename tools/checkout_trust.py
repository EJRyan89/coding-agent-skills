"""Whether a checkout follows another repository's pull request, which a tool that deploys and runs the checkout's
own skills and fixtures in sessions signed in as the user refuses before it starts anything.

A fixture is as trusted as the checkout that holds it ("A fixture is as trusted as the checkout that holds it" in
docs/design.md), so tools/skill_evals.py, tools/runtime_canary.py, and tools/plugin_canary.py each call `refusal`
before they make a home, deploy, or build, and print its reason as their FAILED line.

A branch that follows a branch of a remote other than origin is refused even when the remote is the user's own fork:
`gh pr checkout` writes that same upstream for another author's pull request from a fork, tracking the fork's branch
under its remote's name when the fork is a configured remote and under its URL when maintainers may edit the pull
request (cli/cli pkg/cmd/pr/checkout/checkout.go, cmdsForExistingRemote and cmdsForMissingRemote), so the upstream
cannot tell the two apart. A branch with no upstream is the user's own, so the refusal names the way out: unset the
upstream, which a later push without -u leaves unset, since a push without -u never clears one.

Each setting is read as every value git holds for it, each whole: git takes a branch's upstream from the first value of
`branch.<name>.merge` and reads the last of `branch.<name>.remote`, while `git config --get` answers the last, so a
branch that sets either more than once is refused rather than read as one value.
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

# The way out for a branch of the user's own that follows their fork, after the refusal names why it is refused.
OWN_FORK_STEP = (
    "if the branch is your own, run git branch --unset-upstream, and push it to your fork without -u from then on"
)


# A branch's name on a remote: the source a fetch refspec maps a branch of origin from.
BRANCH_PREFIX = "refs/heads/"


def config_values(git: GitClient, directory: Path, key: str) -> list[str]:
    """Every value the configuration of the checkout at `directory` holds for `key`, in the order git reads them, none
    when it holds none. Read with -z, which ends each value with NUL, a byte no value holds, so a value holding a
    newline is one value, as git reads it; a valueless key reads as the empty value. Raises GitError when git cannot
    read it."""
    # Exits 1 saying nothing when the configuration holds no value for the key.
    values = git.quiet_output(["config", "-z", "--get-all", key], directory=directory)
    return values.removesuffix("\0").split("\0") if values else []


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
    # No fetch refspec of origin leaves no ref a branch of origin.
    refspecs = config_values(git, directory, "remote.origin.fetch")
    held = output_lines(git.output(["for-each-ref", "--contains", "HEAD", "--format=%(refname)"], directory=directory))
    return [ref for ref in held if is_origin_branch(refspecs, ref)]


def foreign_checkout(git: GitClient, directory: Path) -> str | None:
    """Why the checkout at `directory` follows another repository's pull request, or None when it does not: its
    branch's upstream is a pull request's ref, a remote other than origin, which gh pr checkout sets for a fork's pull
    request and which therefore cannot be told from the user's own fork, or any other ref that is not a branch, its
    branch sets its remote or its merge ref more than once, or its HEAD is detached at a commit no branch of origin
    holds. Raises GitError when git cannot read the checkout."""
    # Each read exits 1 saying nothing for what is not there: a detached HEAD, or a branch that follows nothing.
    # HEAD's ref is read by its full name: --short gives a branch beside a tag of the same name as heads/<branch>, whose
    # settings are none, which would read as a branch that follows nothing.
    head = git.quiet_output(["symbolic-ref", "--quiet", "HEAD"], directory=directory)
    if head is None:
        if origin_branches_holding_head(git, directory):
            return None
        return "its HEAD is detached at a commit no branch of origin holds"
    ref = output_line(head)
    if not ref.startswith(BRANCH_PREFIX):
        return f"its HEAD names {ref}, not a branch"
    name = ref.removeprefix(BRANCH_PREFIX)
    settings = {key: config_values(git, directory, f"branch.{name}.{key}") for key in ("remote", "merge")}
    pull_request = next((merge for merge in settings["merge"] if merge.startswith("refs/pull/")), None)
    if pull_request is not None:
        return f"its branch {name} follows {pull_request}, a pull request's ref"
    for key, values in settings.items():
        if len(values) > 1:
            return f"its branch {name} sets branch.{name}.{key} more than once: {', '.join(values)}"
    remote = next(iter(settings["remote"]), "")
    merge = next(iter(settings["merge"]), None)
    if remote not in OWN_REMOTES:
        return (
            f"its branch {name} follows {remote}, a remote other than origin, as gh pr checkout sets for a fork's pull"
            f" request; {OWN_FORK_STEP}"
        )
    if merge is not None and not merge.startswith(BRANCH_PREFIX):
        return f"its branch {name} follows {merge or 'an empty name'}, not a branch"
    return None


def reader(directory: Path) -> Callable[[], str | None]:
    """`foreign_checkout` of the checkout at `directory`, read when called."""
    return functools.partial(foreign_checkout, GitClient(), directory)


def refusal(checkout: Callable[[], str | None]) -> str | None:
    """Why a run refuses the checkout `checkout` reads, or None: it may follow another repository's pull request, since
    it does or since its branch cannot be told from one that does, or git cannot read its branch, which is never read as
    a branch of the user's own."""
    try:
        foreign = checkout()
    except GitError as exc:
        return f"cannot read the checkout's branch: {exc}"
    return None if foreign is None else f"the checkout may follow another repository's pull request: {foreign}"
