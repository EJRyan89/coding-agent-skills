"""The pre-merge review step of a shepherd tick: whether review-repository's review, which review-prs records in the
machine's code-review archive, covers each pull request's head, and the review-prs arguments that review the ones it
does not.

The review is report-only: nothing here arms, holds, or blocks a pull request, and no line it prints changes the
tick's exit status. A head is covered when the archive's latest record of the pull request reviewed it, or reviewed an
earlier head of the same branch from which every commit since is a merge, as when main was merged in: the branch's own
change is what was reviewed. A conflict a merge resolved is not reviewed again. Every other head is due, once: the
record review-prs writes covers it on the next tick, so no tick asks for a second review of a head.

Its lines, printed after the PR lines:

    REVIEWED #<number> <branch> head <commit> v<version>|legacy <verdict> [carried from <commit>]
                                       the record that covers the head, or the migrated legacy review
    REVIEW_DUE #<number> <branch> head <commit> first|since <commit>
                                       a head no record covers: first when the pull request has no review, else the
                                       head the latest record reviewed
    REVIEW_ARGUMENTS <arguments>       after the REVIEW_DUE lines: the review-prs arguments that review every one of
                                       them in one run, --pull for a first review and --re-review --scope auto after
    REVIEW_UNREAD [#<number>] <reason> the configuration, or one pull request's records, could not be read
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))

from flat_text import flat_text
from git_client import GitClient
from review_archive import ArchiveError
from review_config import ConfigurationError, load_config, validate_repository_identity
from review_io import PersistenceError
from review_operation import ReviewOperationError, reviewed_head
from review_records import RecordError


class ReviewUnread(RuntimeError):
    """The archive, or one pull request's records in it, that the tick could not read."""


@dataclass(frozen=True)
class Head:
    number: int
    branch: str
    head: str


@dataclass(frozen=True)
class Reviewed:
    head: str
    verdict: str
    version: int | None  # None for a migrated legacy review


Reader = Callable[[int], Reviewed | None]


def archive_reader(repository: str) -> Reader:
    """A reader of the latest review of each of `repository`'s pull requests in the machine's code-review archive.
    Raises ReviewUnread when the configuration cannot be read or does not configure `repository`, and the reader
    raises it for a pull request whose records cannot be read."""
    unreadable = (ArchiveError, ConfigurationError, PersistenceError, RecordError, ReviewOperationError, OSError)
    try:
        config = load_config()
        identity = validate_repository_identity(repository)
    except unreadable as exc:
        raise ReviewUnread(f"cannot read the code-review configuration: {exc}") from exc
    if identity not in config["repositories"]:
        raise ReviewUnread(f"the code-review configuration does not configure {repository}")
    root = Path(config["archive_root"])

    def read(number: int) -> Reviewed | None:
        try:
            found = reviewed_head(root, repository, number)
        except unreadable as exc:
            raise ReviewUnread(str(exc)) from exc
        if found is None:
            return None
        return Reviewed(str(found["head_sha"]), str(found["verdict"]), found["version"])

    return read


def merges_only(git: GitClient, top: Path, reviewed: str, head: str, base: str | None) -> bool:
    """Whether `head` descends from `reviewed` and every commit between them that main does not hold is a merge. A
    commit git does not hold is never carried."""
    if base is None:
        return False
    if git.run(["merge-base", "--is-ancestor", reviewed, head], directory=top).returncode != 0:
        return False
    listed = git.run(["rev-list", "--no-merges", head, f"^{reviewed}", f"^{base}"], directory=top)
    return listed.returncode == 0 and not listed.stdout.strip()


def review_lines(
    git: GitClient, top: Path, base: str | None, repository: str, heads: Sequence[Head], read: Reader
) -> list[str]:
    """The REVIEWED, REVIEW_DUE, REVIEW_UNREAD, and REVIEW_ARGUMENTS lines for `heads`, the admitted pull requests
    whose head the tick would let merge, where `base` is origin's main."""
    lines: list[str] = []
    first: list[int] = []
    again: list[int] = []
    for pull in heads:
        named = f"#{pull.number} {flat_text(pull.branch)} head {pull.head[:7]}"
        try:
            reviewed = read(pull.number)
        except ReviewUnread as exc:
            lines.append(f"REVIEW_UNREAD #{pull.number} {flat_text(str(exc))}")
            continue
        if reviewed is None:
            lines.append(f"REVIEW_DUE {named} first")
            first.append(pull.number)
            continue
        version = "legacy" if reviewed.version is None else f"v{reviewed.version}"
        recorded = f"{version} {flat_text(reviewed.verdict)}"
        if reviewed.head == pull.head:
            lines.append(f"REVIEWED {named} {recorded}")
        elif merges_only(git, top, reviewed.head, pull.head, base):
            lines.append(f"REVIEWED {named} {recorded} carried from {reviewed.head[:7]}")
        else:
            lines.append(f"REVIEW_DUE {named} since {reviewed.head[:7]}")
            again.append(pull.number)
    arguments = [argument for number in first for argument in ("--pull", f"{repository}#{number}")]
    arguments += [argument for number in again for argument in ("--re-review", f"{repository}#{number}")]
    if again:
        arguments += ["--scope", "auto"]
    if arguments:
        lines.append(f"REVIEW_ARGUMENTS {' '.join(arguments)}")
    return lines
