"""Whether a pull request carried a commit, the one judgment repo-cleanup's branch states and the merge shepherd's
retirement lines share.

The judgment reads commits alone, never a branch's name: a branch created later under a merged pull request's head
name, from main or with commits of its own, holds a commit that pull request never carried, so the name proves
nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

# Whether a commit is reachable from the pull request's base branch.
InBase = Callable[[str], bool]


def carried(tip: str, pull_head: str, commits: Mapping[str, Sequence[str]], in_base: InBase) -> bool:
    """Whether a pull request whose head commit is `pull_head` carried `tip`: the tip is that head, or the head follows
    it only through merges that brought in base commits, as GitHub's "Update branch" makes.

    `commits` maps each of the pull request's own commits to its parents. The walk takes first parents from the head
    back to the tip, which must itself be one of those commits; every commit passed on the way must be one of them
    with two or more parents, each of whose non-first parents `in_base` holds.
    """
    if tip == pull_head:
        return True
    if tip not in commits:
        return False
    current = pull_head
    for _ in range(len(commits)):
        if current == tip:
            return True
        parents = commits.get(current)
        if parents is None or len(parents) < 2 or not all(in_base(parent) for parent in parents[1:]):
            return False
        current = parents[0]
    return False
