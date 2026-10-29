"""The pull request title guard: a title opens with a known type, an optional scope, a colon, and a space."""

from __future__ import annotations

import os
import re

from validation_support import REPOSITORY_ROOT, PullRequestChecks, listed, markdown_section

from tools.release_notes import ReleaseNotesError, git_output

# The squash commit takes the pull request's title, so main's history reads by these types, which tools/worktrees.py
# also takes as a worktree's kind. A breaking-change `!` is refused: the Level in the body's `## Upgrade note` is the
# one statement of a release level.
from tools.worktrees import TITLE_TYPES

TITLE = re.compile(r"(?P<type>[A-Za-z]+)(?P<scope>\([^()\s][^()]*\))?(?P<mark>!?): \S.*")
# The documents that state the convention, by the section that states it; each must name every type.
TITLE_GUIDES = (
    ("docs/implementing-changes.md", "## Pull requests"),
    ("CONTRIBUTING.md", "## Issues and pull requests"),
)


def _either(words: tuple[str, ...]) -> str:
    return listed([f"`{word}`" for word in words])


def title_problems(title: str, branch: str | None) -> list[str]:
    """What is wrong with a pull request title, given the branch it comes from when one is known.

    A branch named `<kind>/<name>` whose kind is a type constrains the title's type to it; any other branch does not.
    """
    title = title.strip()
    match = TITLE.fullmatch(title)
    if match is None:
        return [
            f"the pull request title {title!r} does not open with a type, an optional scope in parentheses, a colon, "
            f"and a space, as in `fix(deployer): ...`; the types are {_either(TITLE_TYPES)}"
        ]
    kind = match.group("type")
    if kind not in TITLE_TYPES:
        return [
            f"the pull request title {title!r} opens with {kind!r}, which is not a known type: {_either(TITLE_TYPES)}"
        ]
    problems = []
    if match.group("mark"):
        problems.append(
            f"the pull request title {title!r} marks a breaking change with `!`; drop it, since the Level under "
            "`## Upgrade note` in the body states the release level"
        )
    branch_kind = branch.split("/", 1)[0] if branch and "/" in branch else None
    if branch_kind in TITLE_TYPES and branch_kind != kind:
        problems.append(
            f"the pull request title {title!r} has the type `{kind}`, but it comes from the branch {branch}, whose "
            f"kind is `{branch_kind}`; make them agree"
        )
    return problems


def title_types_problems(document: str, text: str) -> list[str]:
    """Each title type a guide's text leaves unnamed."""
    return [
        f"{document} does not name the pull request title type `{kind}`"
        for kind in TITLE_TYPES
        if f"`{kind}`" not in text
    ]


def head_branch() -> str | None:
    """The pull request's branch in CI, otherwise the checkout's current branch, or None on a detached HEAD, which
    names none. Raises ReleaseNotesError when git cannot answer, so a failure never reads as no branch."""
    if os.environ.get("GITHUB_HEAD_REF"):
        return os.environ["GITHUB_HEAD_REF"]
    return git_output(REPOSITORY_ROOT, "branch", "--show-current").strip() or None


class PullRequestTitlePolicies(PullRequestChecks):
    def test_the_pull_request_title_opens_with_a_known_type(self) -> None:
        # Without a title there is nothing to judge, so a local run passes: this check alone has no fallback, while the
        # upgrade-notes and silent-revert checks read the branch's commits when they have no body.
        title = self.pull_request.title_text()
        if title is None:
            return
        try:
            branch = head_branch()
        except ReleaseNotesError as error:
            self.fail(f"the title check cannot read the checkout's branch: {error}")
        self.assertEqual([], title_problems(title, branch))

    def test_the_guides_name_every_title_type(self) -> None:
        problems = []
        for document, heading in TITLE_GUIDES:
            section = markdown_section((REPOSITORY_ROOT / document).read_text(encoding="utf-8"), heading)
            if section is None:
                problems.append(f"{document} has no {heading!r} section to state the pull request title convention")
            else:
                problems += title_types_problems(document, section)
        self.assertEqual([], problems)
