"""The pre-merge review step of a shepherd tick: whether review-repository's review, which review-prs records in the
machine's code-review archive, covers each pull request as it would merge, and the review-prs arguments that review
the ones it does not.

The review is report-only: nothing here arms, holds, or blocks a pull request, and no line it prints changes the
tick's exit status. A review covers a pull request by review-prs's own rule (code-review-core's
`ReviewedCommits.covers` and `review_batches.unchanged_contributions`): at the head and the base commit it read, the
base being origin's main as the tick reads it; or, when either moved, when GitHub's comparison finds the pull
request's own changes as the review read them. The reviewed head on another base, as once GitHub retargets a pull
request whose lower one in a stack was squash-merged, is compared by its merge base with each base, and a moved head,
as when main was merged in, by the files its changes leave. A migrated legacy review recorded no base, so it covers
its head on any base and never a moved head. A comparison GitHub cannot make or answer, or one with no base to read,
covers nothing. Every other pull request is due on each tick until a record covers it: the record review-prs writes
covers it from the next tick on, and a head whose review failed stays due.

Its lines, printed after the PR lines:

    REVIEWED #<number> <branch> head <commit> v<version>|legacy <verdict> report "<path>"|none [carried from <commit>]
                                       the record that covers the pull request, or the migrated legacy review, with
                                       its Markdown report, none for a legacy review whose report is gone; carried
                                       from the reviewed head when the head moved and its changes compare as that
                                       review read them
    REVIEW_DUE #<number> <branch> head <commit> base <commit>|unknown first|since <commit>
                                       a pull request no record covers, with the base commit its review would read,
                                       origin's main as the tick reads it, unknown when the tick could not: first
                                       when it has no review, else the head the latest record reviewed, which is the
                                       head itself when only its base moved. A head and a base name the review due,
                                       so a head retargeted to another base is another review
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
from github_client import GitHubClient, GitHubError
from review_archive import ArchiveError
from review_batches import unchanged_contributions
from review_config import ConfigurationError, load_config, validate_repository_identity
from review_github import GitHubClient as ReviewGitHubClient
from review_io import PersistenceError
from review_operation import MovedPull, ReviewedCommits, ReviewOperationError, reviewed_head
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
    base: str | None  # the base commit the review compared its head with; None for a migrated legacy review
    verdict: str
    version: int | None  # None for a migrated legacy review
    report: str | None  # the Markdown report's path; None for a legacy review whose report is gone


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
        report = found["report"]
        base = found["base_sha"]
        return Reviewed(
            str(found["head_sha"]),
            None if base is None else str(base),
            str(found["verdict"]),
            found["version"],
            None if report is None else str(report),
        )

    return read


def unchanged(client: GitHubClient, repository: str, moved: list[MovedPull]) -> set[int]:
    """The numbers among `moved` whose own changes GitHub compares as their review read them, through review-prs's
    comparison; none when a GitHub failure every comparison would meet, such as a lost login, ends it."""
    if not moved:
        return set()
    github = ReviewGitHubClient(client.runner, sleeper=client.sleeper, clock=client.clock)
    try:
        return unchanged_contributions(github, repository, moved)
    except GitHubError:
        return set()


def review_lines(
    client: GitHubClient, base: str | None, repository: str, heads: Sequence[Head], read: Reader
) -> list[str]:
    """The REVIEWED, REVIEW_DUE, REVIEW_UNREAD, and REVIEW_ARGUMENTS lines for `heads`, the admitted pull requests
    whose head the tick would let merge, where `base` is origin's main, None when the tick could not read it."""
    found: list[tuple[Head, Reviewed | ReviewUnread | None]] = []
    moved: list[MovedPull] = []
    covered: set[int] = set()
    for pull in heads:
        try:
            reviewed = read(pull.number)
        except ReviewUnread as exc:
            found.append((pull, exc))
            continue
        found.append((pull, reviewed))
        if reviewed is None:
            continue
        listed = {"number": pull.number, "headRefOid": pull.head, "baseRefOid": base}
        commits = ReviewedCommits(reviewed.head, reviewed.base)
        if commits.covers(listed):
            covered.add(pull.number)
        elif reviewed.base is not None and base is not None:
            moved.append((listed, commits))
    covered |= unchanged(client, repository, moved)
    lines: list[str] = []
    first: list[int] = []
    again: list[int] = []
    due_base = "unknown" if base is None else base[:7]
    for pull, outcome in found:
        named = f"#{pull.number} {flat_text(pull.branch)} head {pull.head[:7]}"
        if isinstance(outcome, ReviewUnread):
            lines.append(f"REVIEW_UNREAD #{pull.number} {flat_text(str(outcome))}")
        elif outcome is None:
            lines.append(f"REVIEW_DUE {named} base {due_base} first")
            first.append(pull.number)
        elif pull.number not in covered:
            lines.append(f"REVIEW_DUE {named} base {due_base} since {outcome.head[:7]}")
            again.append(pull.number)
        else:
            version = "legacy" if outcome.version is None else f"v{outcome.version}"
            report = "none" if outcome.report is None else f'"{flat_text(Path(outcome.report).as_posix())}"'
            carried = "" if outcome.head == pull.head else f" carried from {outcome.head[:7]}"
            lines.append(f"REVIEWED {named} {version} {flat_text(outcome.verdict)} report {report}{carried}")
    arguments = [argument for number in first for argument in ("--pull", f"{repository}#{number}")]
    arguments += [argument for number in again for argument in ("--re-review", f"{repository}#{number}")]
    if again:
        arguments += ["--scope", "auto"]
    if arguments:
        lines.append(f"REVIEW_ARGUMENTS {' '.join(arguments)}")
    return lines
