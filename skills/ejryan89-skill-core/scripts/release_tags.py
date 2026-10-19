"""What a release tag is, and the last release a commit holds: the one definition every reader of a release shares.

A release tag is `vMAJOR.MINOR.PATCH` in ASCII digits, the form `release_levels.VERSION` writes a version in and the
release procedure in docs/releasing.md tags: nothing follows the patch number, so a tag with a pre-release suffix such
as `v1.0.0-rc.1` is not a release, and neither is any other tag. Releases are ordered by their three numbers, each
compared as a whole number, so `v0.10.0` follows `v0.9.0` and a leading zero changes nothing. The last release a commit
holds is the highest release tag reachable from it, whatever tag of another name sits nearer to it in the history.

tools/release_notes.py starts a release's notes at the last release before it, the upgrade-notes check in
tests/validation compares the contract files with the last release on origin/main, and update-coding-agent-skills'
update.sh, which is Bash and cannot import this, runs this module to find the releases its major-version stop
compares.

Usage:
  python release_tags.py <repository> <commit>

It prints `RELEASE <tag>`, the last release the commit holds in the repository at that path, or `NO_RELEASE` when no
release tag reaches it, and exits 0; it prints `FAILED <reason>` and exits 1 when git cannot read the tags reaching
the commit, such as for a commit the repository does not hold.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from release_levels import VERSION

TAGS = "refs/tags/"


def _number(digits: str) -> tuple[int, str]:
    """A version component's order as a whole number, kept as text so a component of any length compares exactly."""
    significant = digits.lstrip("0")
    return len(significant), significant


def release_order(name: str) -> tuple[tuple[int, str], ...] | None:
    """The order of a release tag among releases, or None when the name is not a release tag."""
    if not name.isascii() or not VERSION.fullmatch(name):
        return None
    return tuple(_number(part) for part in name[1:].split("."))


def is_release_tag(name: str) -> bool:
    return release_order(name) is not None


def highest_release(names: Iterable[str]) -> str | None:
    """The highest release among tag names, any other name left out; between two names of one version, such as
    `v1.0.0` and `v01.0.0`, the one that sorts last."""
    releases = [(order, name) for name in names if (order := release_order(name)) is not None]
    return max(releases)[1] if releases else None


def last_release(client: GitClient, repository: str | Path, commit: str) -> str | None:
    """The highest release tag reachable from the commit, or None when none is.

    Raises GitError when git cannot read the tags reaching it.
    """
    listed = client.output(["for-each-ref", f"--merged={commit}", "--format=%(refname)", TAGS], directory=repository)
    return highest_release(line.removeprefix(TAGS) for line in listed.splitlines())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Print the last release tag a commit holds.")
    parser.add_argument("repository", help="the repository's path")
    parser.add_argument("commit", help="the commit, or a ref naming it")
    options = parser.parse_args(argv)
    try:
        found = last_release(GitClient(), Path(options.repository), options.commit)
    except GitError as error:
        print(f"FAILED git for-each-ref: {flat_text(str(error))}")
        return 1
    print(f"RELEASE {found}" if found is not None else "NO_RELEASE")
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
