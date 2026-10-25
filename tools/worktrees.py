"""Coordinate parallel sessions: one hub checkout that stays on main, one worktree per task under .claude/worktrees.

Usage:
  python tools/worktrees.py new <kind> <name>   create .claude/worktrees/<kind>-<name> on branch <kind>/<name>
  python tools/worktrees.py list                one row per worktree: branch, state, ahead/behind main, dirt
  python tools/worktrees.py guard               runs the hub guard, tools/hub_guard.py, for a session that loaded
                                                the hook command from before the guard moved there

new takes as the kind a pull request title type, which the title check then holds the branch's pull request to, or inv
for an investigation, and refuses any other kind with the list of those it takes, exiting 1.

The hub guard, the Claude Code PreToolUse hook .claude/settings.json runs, is tools/hub_guard.py. It imports from here
only what both share: MAIN, same_path, checkout_of, and git with its client. This module imports nothing from it, and
its guard subcommand only runs it as a program.
"""

from __future__ import annotations

import argparse
import os
import re
import runpy
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import git_worktrees
from console import run_main
from git_client import GitClient, GitError, GitResult

from deployer import platform_support

MAIN = "main"
# Where Claude Code's EnterWorktree and the desktop app create session worktrees; ignored by the hub's .gitignore.
WORKTREES = Path(".claude") / "worktrees"
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The pull request title types. A branch whose kind is one of them holds its pull request's title to that type, which
# validation's title check reads from here.
TITLE_TYPES = ("feat", "fix", "docs", "test", "refactor", "chore", "ci", "build")
# The kinds new accepts: a title type, or an investigation's, which may produce no commits and so no pull request.
INVESTIGATION = "inv"
KINDS = (*TITLE_TYPES, INVESTIGATION)
# Folds made without skills/ejryan89-skill-core/scripts/path_keys.py's path_key, with the reason.
DUPLICATION_ALLOWED = {
    "path_key": (
        "below_a_checkout folds one segment of a path, a name, to compare it with .git; a key from "
        "skills/ejryan89-skill-core/scripts/path_keys.py is the key of a whole path"
    ),
}
# A Claude Code session keeps the hook command it loaded when it started, so one started before the hub guard
# moved into hub_guard.py still runs this script's guard subcommand, which runs the guard as its own program.
HUB_GUARD = Path(__file__).with_name("hub_guard.py")
# What list shows for an ahead or behind count git could not take.
UNKNOWN = "?"


class UsageError(Exception):
    pass


# The one git this tool runs: ejryan89-skill-core's client, which starts git at the path the locator finds on this
# process's PATH, never by its bare name, which Windows looks up in the working directory first, and never on the PATH a
# judged command is given, which the guard only reads; without git's repository-selecting variables, which would point
# it at another repository, index, or object store than the checkout's own; with no prompt; and within a time limit.
CLIENT = GitClient()
# Bounds each git call the hub guard makes through git(), so a git that hangs fails the call well inside the hook's own
# 30-second timeout in .claude/settings.json. new and list run under the client's own limit, since adding a worktree
# checks out a tree.
GUARD_GIT_TIMEOUT_SECONDS = 10.0


def git(cwd: Path, *arguments: str, timeout: float | None = GUARD_GIT_TIMEOUT_SECONDS) -> GitResult:
    """Runs git on the checkout at cwd through CLIENT, within timeout seconds, or the client's own limit for None.

    Raises GitError when git is missing, cannot start, or does not finish in time; the hub guard then skips its
    decision, as it does for any error, and new and list end with a FAILED line.
    """
    return CLIENT.run(arguments, directory=cwd, timeout=timeout)


def same_path(left: Path, right: Path) -> bool:
    return platform_support.path_key(left) == platform_support.path_key(right)


@dataclass(frozen=True)
class Checkout:
    toplevel: Path
    hub: Path

    @property
    def is_hub(self) -> bool:
        return same_path(self.toplevel, self.hub)


def checkout_of(path: Path) -> Checkout | None:
    """The worktree containing path, and the hub that owns it; None outside a non-bare repository.

    Raises GitError when git answers nonzero for a directory below a checkout, one that a `.git` beside it or above
    it marks, outside any git directory: a checkout git cannot read, as one it refuses to trust, is not one outside
    every repository."""
    directory = path
    while not directory.is_dir():
        if directory.parent == directory:
            return None
        directory = directory.parent
    result = git(directory, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir")
    lines = result.stdout.splitlines()
    if result.returncode != 0 and below_a_checkout(directory):
        said = " ".join(result.stderr.split()) or f"git rev-parse exited with {result.returncode}"
        raise GitError(f"git could not read the checkout at {directory}: {said}", returncode=result.returncode)
    if result.returncode != 0 or len(lines) != 2:
        return None
    common = Path(lines[1])
    if common.name != ".git":
        return None
    return Checkout(Path(lines[0]), common.parent)


def below_a_checkout(directory: Path) -> bool:
    """Whether a `.git` in directory or above it marks it as part of a checkout, outside any git directory, where git
    has no work tree to show."""
    absolute = directory.absolute()
    if any(os.path.normcase(part) == ".git" for part in absolute.parts):
        return False
    return any((folder / ".git").exists() for folder in (absolute, *absolute.parents))


# ---------------------------------------------------------------------------------------------------------------
# Subcommands


def guard() -> int:
    """Runs the hub guard as its own program, as the hook it moved to would, with this process's stdin and
    environment; it exits with the guard's status."""
    runpy.run_path(str(HUB_GUARD), run_name="__main__")
    return 0


def current_hub() -> Path:
    checkout = checkout_of(Path.cwd())
    if checkout is None:
        raise UsageError("Run this inside the repository or one of its worktrees.")
    return checkout.hub


def new(kind: str, name: str) -> int:
    if kind not in KINDS:
        raise UsageError(
            f"The kind '{kind}' must be one of {', '.join(KINDS)}: a pull request title type, or {INVESTIGATION}."
        )
    if not SLUG.fullmatch(name):
        raise UsageError(f"The name '{name}' must be lowercase letters, digits, and hyphens.")
    hub = current_hub()
    branch = f"{kind}/{name}"
    path = hub / WORKTREES / f"{kind}-{name}"
    if CLIENT.quiet_output(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], directory=hub) is not None:
        raise UsageError(f"Branch {branch} already exists.")
    if path.exists():
        raise UsageError(f"{platform_support.normalize(path)} already exists.")
    if git(hub, "check-ignore", "-q", "--no-index", str(path)).returncode != 0:
        raise UsageError(f"{WORKTREES.as_posix()}/ must be ignored in the hub's .gitignore before it holds worktrees.")
    result = git(hub, "worktree", "add", "--no-track", "-b", branch, str(path), MAIN, timeout=None)
    if result.returncode != 0:
        raise UsageError(result.stderr.strip() or "git worktree add failed.")
    print(f"Created {platform_support.normalize(path)} on {branch} from {MAIN}.")
    print("Enter it before editing (Claude Code: EnterWorktree with this path).")
    return 0


@dataclass(frozen=True)
class Row:
    branch: str
    state: str
    ahead: str
    behind: str
    dirty: str
    path: str


def row(hub: Path, tree: git_worktrees.Worktree) -> Row:
    path = Path(tree.path)
    branch = tree.branch or "(detached)"
    shown = platform_support.normalize(path)
    if not path.is_dir():
        return Row(branch, "missing", "-", "-", "-", shown)
    counted = git(hub, "rev-list", "--left-right", "--count", f"{MAIN}...{tree.head or 'HEAD'}", timeout=None)
    counts = counted.stdout.split()
    # A count git could not take is unknown, never read as a branch with commits of its own.
    known = counted.returncode == 0 and len(counts) == 2 and all(count.isdigit() for count in counts)
    behind, ahead = counts if known else (UNKNOWN, UNKNOWN)
    try:
        dirty = git_worktrees.changed_paths(CLIENT, path)
    except GitError as exc:
        if exc.returncode is None:
            raise
        # Git ran and could not read the checkout, as when its .git file names a folder that is gone.
        return Row(branch, "unreadable", ahead, behind, "-", shown)
    if same_path(path, hub):
        problems = [*(["off main"] if branch != MAIN else []), *(["dirty"] if dirty else [])]
        state = " · ".join(["hub", *problems])
    else:
        state = "unknown" if not known else "no commits" if ahead == "0" else "in progress"
    return Row(branch, state, ahead, behind, str(dirty), shown)


def list_worktrees() -> int:
    hub = current_hub()
    rows = [
        Row("BRANCH", "STATE", "AHEAD", "BEHIND", "DIRTY", "PATH"),
        *(row(hub, t) for t in git_worktrees.list_worktrees(CLIENT, hub)),
    ]
    widths = [max(len(getattr(r, field)) for r in rows) for field in ("branch", "state", "ahead", "behind", "dirty")]
    for r in rows:
        cells = (r.branch, r.state, r.ahead, r.behind, r.dirty)
        print("  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True)) + "  " + r.path)
    return 0


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="worktrees.py", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("new", help="create a worktree for a task under .claude/worktrees")
    create.add_argument("kind", help=f"branch prefix: {', '.join(KINDS)}")
    create.add_argument("name", help="task name")
    commands.add_parser("list", help="survey every worktree of this repository")
    commands.add_parser("guard", help="run the hub guard, tools/hub_guard.py, for a session started before it moved")
    options = parser.parse_args(arguments)
    try:
        if options.command == "guard":
            return guard()
        if options.command == "new":
            return new(options.kind, options.name)
        return list_worktrees()
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")
