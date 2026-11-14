"""What a release tag is, and the last release a commit holds: the one definition every reader of a release shares.

A release tag is `vMAJOR.MINOR.PATCH` in ASCII digits, the form `release_levels.VERSION` writes a version in and the
release procedure in docs/releasing.md tags: nothing follows the patch number, so a tag with a pre-release suffix such
as `v1.0.0-rc.1` is not a release, and neither is any other tag. Releases are ordered by their three numbers, each
read by `versions` as a whole number, so `v0.10.0` follows `v0.9.0`, a leading zero changes nothing, and a number past
`whole_number`'s digit limit makes no release. The last release a commit
holds is the highest release tag reachable from it, whatever tag of another name sits nearer to it in the history. A
release tag names a commit when it peels to that commit, so several releases can name one commit; the deployer and
update-coding-agent-skills tell the update's own pin from the user's by them.

tools/release_notes.py starts a release's notes at the last release before it, the upgrade-notes check in
tests/validation compares the contract files with the last release on origin/main, and update-coding-agent-skills'
update.sh, which is Bash and cannot import this, runs this module to find the releases its major-version stop
compares, and to compare them, so no version number is read as a Bash integer, and to list the releases naming the
pinned commit.

Usage:
  python release_tags.py <repository> <commit>
  python release_tags.py --naming <repository> <commit>
  python release_tags.py --raises <current> <target>

The first form prints `RELEASE <tag>`, the last release the commit holds in the repository at that path, or
`NO_RELEASE` when no release tag reaches it; it prints `FAILED <reason>` when git cannot read the tags reaching the
commit, such as for a commit the repository does not hold.

The --naming form prints `RELEASE <tag>` for each release tag that names the commit, in release order, or `NO_RELEASE`
when none does; it prints `FAILED <reason>` when git cannot read the tags naming it, such as for a commit the
repository does not hold.

The second form prints `RAISES` when the release tag `<target>` raises the breaking component, the major, or the minor
while the major is 0, above the release tag `<current>`, and `WITHIN` when it does not; `<current>` may be `untagged`,
a commit no release reaches, counted as v0.0.0. It prints `FAILED <reason>` when either is not a release tag.

Exit status:
  0  RELEASE <tag> or NO_RELEASE, or with --naming a RELEASE <tag> line for each release or NO_RELEASE; RAISES or
     WITHIN
  1  a last line FAILED <reason>, as when git cannot read the tags reaching or naming the commit or --raises is given a
     name that is not a release tag
  2  a usage error
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

import versions
from console import run_main
from flat_text import flat_text, single_line
from git_client import GitClient, GitError, output_lines

TAGS = "refs/tags/"
RELEASE_PREFIX = "v"
# What --raises takes for a commit no release tag reaches, which counts as version zero.
UNTAGGED = "untagged"


def release_order(name: str) -> versions.Version | None:
    """The order of a release tag among releases, its three numbers as `versions` reads them, or None when the name is
    not a release tag."""
    if not name.startswith(RELEASE_PREFIX):
        return None
    return versions.read(name.removeprefix(RELEASE_PREFIX), least=3, most=3)


def is_release_tag(name: str) -> bool:
    return release_order(name) is not None


def highest_release(names: Iterable[str]) -> str | None:
    """The highest release among tag names, any other name left out; between two names of one version, such as
    `v1.0.0` and `v01.0.0`, the one that sorts last."""
    return versions.latest(names, release_order)


def last_release(client: GitClient, repository: str | Path, commit: str) -> str | None:
    """The highest release tag reachable from the commit, or None when none is.

    Raises GitError when git cannot read the tags reaching it.
    """
    listed = client.output(["for-each-ref", f"--merged={commit}", "--format=%(refname)", TAGS], directory=repository)
    return highest_release(line.removeprefix(TAGS) for line in output_lines(listed))


def releases_naming(client: GitClient, repository: str | Path, commit: str) -> list[str]:
    """The release tags that name the commit, each peeled through any tag objects to it, in release order.

    Raises GitError when git cannot read them, a commit the repository does not hold among the causes, since the commit
    is peeled before the tags are matched with it.
    """
    listed = client.output(
        ["for-each-ref", f"--points-at={commit}^{{commit}}", "--format=%(refname)", TAGS], directory=repository
    )
    releases = []
    for line in output_lines(listed):
        name = line.removeprefix(TAGS)
        order = release_order(name)
        if order is not None:
            releases.append((order, name))
    return [name for _, name in sorted(releases)]


def breaking_component(order: versions.Version) -> tuple[int, int]:
    """The component a release may not raise without acceptance, as (major, minor): the major and 0, or 0 and the minor
    while the major is 0."""
    major, minor = order[0], order[1]
    return (major, 0) if major else (0, minor)


def raises_breaking(current: versions.Version, target: versions.Version) -> bool:
    """Whether the release `target` raises the breaking component above the release `current`."""
    return breaking_component(target) > breaking_component(current)


def _compare(current: str, target: str) -> int:
    current_order = (0, 0, 0) if current == UNTAGGED else release_order(current)
    target_order = release_order(target)
    if current_order is None or target_order is None:
        print(f"FAILED not a release tag: {flat_text(current if current_order is None else target)}")
        return 1
    print("RAISES" if raises_breaking(current_order, target_order) else "WITHIN")
    return 0


def _list_naming(repository: Path, commit: str) -> int:
    try:
        found = releases_naming(GitClient(), repository, commit)
    except GitError as error:
        print(f"FAILED git for-each-ref: {flat_text(str(error))}")
        return 1
    for name in found:
        print(single_line(f"RELEASE {name}"))
    if not found:
        print("NO_RELEASE")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Print the last release tag a commit holds, or, with --naming, every release tag naming it, or, "
        "with --raises, whether one release raises the breaking component above another."
    )
    form = parser.add_mutually_exclusive_group()
    form.add_argument(
        "--raises", action="store_true", help=f"compare two release tags, the first of which may be {UNTAGGED}"
    )
    form.add_argument("--naming", action="store_true", help="list every release tag that names the commit")
    parser.add_argument("first", metavar="repository|current", help="the repository's path, or the current release")
    parser.add_argument("second", metavar="commit|target", help="the commit or a ref naming it, or the target release")
    options = parser.parse_args(argv)
    if options.raises:
        return _compare(options.first, options.second)
    if options.naming:
        return _list_naming(Path(options.first), options.second)
    try:
        found = last_release(GitClient(), Path(options.first), options.second)
    except GitError as error:
        print(f"FAILED git for-each-ref: {flat_text(str(error))}")
        return 1
    print(single_line(f"RELEASE {found}" if found is not None else "NO_RELEASE"))
    return 0


if __name__ == "__main__":
    run_main(lambda: main(sys.argv[1:]))
