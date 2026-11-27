"""Write a release's section of docs/upgrade-notes.md from the upgrade notes its merged commits carry.

Usage:
  python tools/release_notes.py VERSION [--from REF] [--to REF] [--write | --check --notes FILE]
  python tools/release_notes.py VERSION --counts

Each pull request states what it asks of a user who updates under a `## Upgrade note` heading in its body, and the
repository squash-merges with the body as the commit message, so every merged change carries its entry into the
history. This script reads the entries of the commits between two refs (merge commits left out), newest first: the
`###` entries of each message's `## Upgrade note` section, and, for a commit made while entries were still written
by hand, the entries it added under `## Unreleased` in docs/upgrade-notes.md. An entry a commit added to that file
whose heading a newer commit added there too is the older text of that entry, reworded in the file, and is left out.
Every entry a pull request body carries is kept, even one whose heading another entry shares: for each heading two kept
entries share, a `SHARED "<heading>": <where>; <where>` line on stderr names where each was read, so the release
preparer merges them into one entry or gives each a heading of its own. An entry is read with each hard-wrapped
heading, list item, or paragraph joined into one line, since a squash commit's message may be wrapped. Its fields are
its field list items outside every code fence, so a fenced `- Level:` line is an example's text, as GitHub renders it.
Each entry keeps that text and gains `- Pull request: #N` from the commit's squash title when it names none.

--from defaults to the last release before --to, the highest release tag reachable from --to's parent as
ejryan89-skill-core's release_tags.py defines a release tag (vMAJOR.MINOR.PATCH and nothing after it), and --to to HEAD,
or to origin/main with --check. A version must be a release tag too. It prints the `## VERSION` section, or with --write
inserts it into docs/upgrade-notes.md above the newest version's section, refusing a version the file already has. The
section opens with the largest leading Level word of its entries, ordered by docs/releasing.md, or states that there are
no upgrade-note entries.

--check compares the `## VERSION` section of docs/upgrade-notes.md as --to holds it with the entries the commits in
the range carry, by heading, and fetches no ref, so the one entry a release preparer merges from the entries a
SHARED line names holds each of them. The release pull request is squash-merged, so the commit the
section was written at never reaches main, and a pull request merged into main or into the release branch after the
section was written is in the tag and in no section. When the section lacks an entry, it prints `BEHIND <n> entries`
and each entry the section lacks, ready to carry into it, and reads nothing more. An entry the section holds beyond the
range's, such as an obligation the release's preparation issue records, is the releaser's own. It then reads the
release notes the tag will publish, the file --notes names, which --check requires, as UTF-8 text with or without a
byte order mark: they must open with the `## VERSION` heading, and their `## VERSION` section must hold the committed
section's `Level:` line as it is written, each entry the committed section holds, by heading, and the line --counts
prints at that moment, whatever its numbers, each line read without the blanks around it. It prints a
`NOTES_INCOMPLETE <what>` line for each that fails, and `CURRENT` only when the section holds every entry and the notes
hold all four. Each line is read as a line of the section's text, so a Level or counts line inside an HTML comment,
which GitHub hides, is read as present; and the section runs, as markdown_section reads it, to the next `## ` heading,
so a `# ` heading does not end it.

--counts reads, through ejryan89-skill-core's GitHubClient and the gh that serves this checkout's repository, the
milestone titled VERSION and every issue on it, through GitHub's REST API, since `gh issue list` searches and answers an
unknown milestone, or one past the search's limit, as fewer issues. It prints the line the release notes carry: the
bugs the milestone fixed, each issue labeled `bug` and closed as completed, counted by its origin label, `regression`,
`latent`, or `hardening`, or as having none; and the auditor gaps on the milestone when the count runs, each issue
labeled `auditor-gap` and not closed as not planned. An outside review's gaps land on the next milestone, so a release
counts those its previous release's pass filed. An issue closed as not planned, a duplicate among them, is in neither
count, and a pull request on the milestone is no issue. It prints an `ERROR:` line, never a count, for a title no
milestone has, a listing that holds fewer or more items than the milestone's own open and closed counts, an answer
that is not the milestones or issues asked for, a fixed issue, one closed as completed, with no kind label (`bug`,
`enhancement`, `documentation`, or `question`), since a defect filed without `bug` would be counted nowhere, a fixed
issue with an origin label and no `bug`, or a fixed bug with two origin labels. An issue closed as not planned or a
duplicate is not read for labels. It takes no range and writes nothing.

--write is refused when the file already has the version. An `ERROR:` line names what it could not read or write:
the range, when git cannot read it or, without --from, no release reaches --to's parent; docs/releasing.md, its
documented levels, or an entry's Level; with --write, docs/upgrade-notes.md, when it cannot be read as UTF-8 text or
written; or with --check, a `## VERSION` section --to does not hold, release notes that cannot be read as UTF-8
text, or anything --counts cannot read or count. Git runs through ejryan89-skill-core's
GitClient, which starts it without the repository-selecting variables that would read another repository. The
release procedure in docs/releasing.md runs it before tagging and again at the tag, and the upgrade-notes check in
tests/validation reads the same entries through the same git.

Exit status:
  0  the section was printed or written, --check found it CURRENT, or --counts printed its line
  1  --write refused a version the file already has, --check found the section BEHIND or the release notes
     NOTES_INCOMPLETE, or a last line FAILED <reason>
  2  a usage error, or an ERROR: line naming what it could not read or write
"""

from __future__ import annotations

import argparse
import functools
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError, GitResult, output_lines
from github_client import GitHubClient, GitHubError, subprocess_runner
from hidden_comments import without_hidden_comments
from markdown_sections import (
    ATX_HEADING,
    HEADING,
    LIST_ITEM_BLOCK,
    MARKDOWN_SPACE,
    PARAGRAPH,
    blocks,
    code_spans,
    document_headings,
    find_fences,
    markdown_section,
)
from release_levels import RELEASING_DOC, VERSION, largest_level, versioning_levels
from release_tags import is_release_tag, last_release
from text_lines import LINE_BREAK, lines_and_breaks

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_NOTES = "docs/upgrade-notes.md"
BODY_HEADING = "## Upgrade note"
UNRELEASED_HEADING = "## Unreleased"
RELEASE_BRANCH = "origin/main"
NOTES_FIELD = re.compile(r"- (Level|Contract|User action|Pull request):[ \t]*(.*?)[ \t]*")
# The deepest heading that starts an entry, or that, above one, ends the entry before it, and an entry's own marks.
ENTRY_LEVEL = 3
ENTRY_MARKS = "#" * ENTRY_LEVEL
# What a section's summary line opens with, just below its version heading, before the entries.
LEVEL_LINE = "Level: "
# The blocks whose lines a hard wrap may split, which unwrapped joins back into one line.
JOINED_BLOCKS = frozenset({PARAGRAPH, LIST_ITEM_BLOCK})
SQUASH_TITLE_NUMBER = re.compile(r"\(#([0-9]+)\)[ \t]*$")
# What git log -z writes after each of a commit's name, subject, and message: NUL, which git refuses in a commit message
# and cuts a message at, so no message text can end a field early.
FIELD_END = "\0"
COMMIT_FIELDS = 3
# The labels --counts reads. Each issue template applies its kind label; a bug's origin label is the one the bug
# template's `## Introduced` comment asks the filer to apply, and an auditor gap is the issue an outside review's
# auditor-gap step files ("Outside review" in docs/releasing.md).
DEFECT_LABEL = "bug"
KIND_LABELS = (DEFECT_LABEL, "enhancement", "documentation", "question")
ORIGIN_LABELS = ("regression", "latent", "hardening")
AUDITOR_GAP_LABEL = "auditor-gap"
# The REST API's issue states and close reasons. An issue closed before GitHub recorded reasons answers null, which
# GitHub shows as completed, its default; a pull request also answers null and is dropped before reasons are read.
OPEN, CLOSED = "open", "closed"
CLOSE_REASONS = ("completed", "not_planned", "duplicate", "reopened", None)
COMPLETED_REASONS = ("completed", None)
MILESTONES = "repos/{owner}/{repo}/milestones?state=all&per_page=100"
MILESTONE_ISSUES = "repos/{owner}/{repo}/issues?milestone={number}&state=all&per_page=100"


GIT_STATUS_ALLOWED = {
    "git": "passes git's result on unread, decoded, to callers that judge it",
    "git_output": "fails closed: any failure raises a ReleaseNotesError naming git's stderr",
}


@dataclass(frozen=True)
class NotesEntry:
    heading: str
    fields: dict[str, str]
    text: str

    @property
    def contract_items(self) -> set[str]:
        """The text of each code span in the entry's contract field, a trailing `/` dropped, read through
        markdown_sections' code_spans, which pairs whole backtick runs as CommonMark does."""
        return {span.text.rstrip("/") for span in code_spans(self.fields.get("Contract", ""))}


@dataclass(frozen=True)
class SourcedEntry:
    """An entry with where it was read: a commit's message, the notes a commit left, or a pull request body."""

    entry: NotesEntry
    where: str
    pull_request: str | None
    from_notes: bool

    def rendered(self) -> str:
        if self.entry.fields.get("Pull request") or self.pull_request is None:
            return self.entry.text
        return f"{self.entry.text}\n- Pull request: {self.pull_request}"


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str
    message: str

    @property
    def pull_request(self) -> str | None:
        match = SQUASH_TITLE_NUMBER.search(self.subject)
        return f"#{match.group(1)}" if match else None


def unwrapped(block: str) -> list[str]:
    """The block's lines with each hard-wrapped heading, list item, or paragraph joined back into one line.

    A squash merge may carry a body wrapped as git and GitHub wrap commit messages, so the lines are read as
    markdown_sections' `blocks` reads them, and the lines of one paragraph or one list item are joined, while every
    other line, a blank one, a code block's, a table row, a quote's, or a thematic break, stays its own. A paragraph
    that starts on the line right below a heading joins the heading, since a wrapped heading's words run on there.
    What starts a block, and which list item holds a line, is `blocks`' reading; its limits are this reading's, such as
    a setext heading's `=` underline joining the line above it, as no upgrade note writes one.
    """
    raw = [line.rstrip(MARKDOWN_SPACE) for line in LINE_BREAK.split(block.strip(MARKDOWN_SPACE + "\r\n"))]
    lines: list[str | None] = list(raw)
    heading: int | None = None
    for read in blocks(raw):
        start, stop = read.start, read.start + len(read.lines)
        if read.kind == PARAGRAPH and heading == start - 1:
            start = heading
        if read.kind in JOINED_BLOCKS or start != read.start:
            lines[start] = " ".join([raw[start], *(line.strip(MARKDOWN_SPACE) for line in raw[start + 1 : stop])])
            lines[start + 1 : stop] = [None] * (stop - start - 1)
        heading = read.start if read.kind == HEADING else None
    return [line for line in lines if line is not None]


def notes_blocks(text: str) -> list[str]:
    """The text cut before each heading of the first ENTRY_LEVEL levels, as an entry or a heading above one starts,
    as markdown_sections' document_headings reads the text's own headings: a heading-shaped line a code block, a list
    item, or a blockquote holds is its text, as GitHub renders it."""
    lines = LINE_BREAK.split(text)
    starts = {index for index, heading in document_headings(lines) if len(heading.group("marks")) <= ENTRY_LEVEL}
    blocks: list[list[str]] = [[]]
    for index, line in enumerate(lines):
        if index in starts:
            blocks.append([])
        blocks[-1].append(line)
    return ["\n".join(block) for block in blocks]


def entry_fields(body: list[str]) -> dict[str, str]:
    """An entry's fields: each field line outside every code fence, the last of a name holding. A field-shaped line
    inside a fence is the fence's text, as GitHub renders it, such as an example of another entry."""
    fenced = {index for fence in find_fences(body) for index in fence.lines()}
    matches = (NOTES_FIELD.fullmatch(line) for index, line in enumerate(body) if index not in fenced)
    return {match.group(1): match.group(2) for match in matches if match is not None}


def notes_entries(text: str | None) -> list[NotesEntry]:
    """The upgrade-notes entries: each third-level heading with its field lines, read with wrapped lines joined, the
    heading's text as markdown_sections' ATX_HEADING reads it past its marks, its blanks, and any closing marks. Each
    block notes_blocks cuts but the first starts at a heading of the text's own."""
    entries: list[NotesEntry] = []
    for block in notes_blocks(text or ""):
        heading, *body = unwrapped(block)
        match = ATX_HEADING.fullmatch(heading)
        if match is not None and match.group("marks") == ENTRY_MARKS:
            fields = entry_fields(body)
            entries.append(NotesEntry(match.group("text"), fields, "\n".join([heading, *body])))
    return entries


def body_entries(body: str) -> list[NotesEntry]:
    """The entries under a pull request body's `## Upgrade note` heading; the HTML comments GitHub hides are not read,
    while a `<!--` that code shows is text, as hidden_comments reads them."""
    text = without_hidden_comments(body.replace("\r\n", "\n"))
    return notes_entries(markdown_section(text, BODY_HEADING))


def unreleased_entries(notes: str | None) -> list[NotesEntry]:
    return notes_entries(markdown_section((notes or "").replace("\r\n", "\n"), UNRELEASED_HEADING))


class ReleaseNotesError(Exception):
    """A failure these notes expect and report on an `ERROR:` line: git that cannot read the range, a file that cannot
    be read or written, or an entry or section that is malformed or missing. Anything else, such as a ValueError from
    a defect in this module, reaches run_main and ends as its `FAILED` line."""


# The one git these notes and the validation runner in tests/validation run.
CLIENT = GitClient()


def git(root: Path, *arguments: str) -> GitResult:
    """git run on the checkout at root through ejryan89-skill-core's GitClient, so without the repository-selecting
    variables that would read another one and at the path the locator finds, with its output read as UTF-8.

    Raises ReleaseNotesError when git cannot run or finish, or prints output that is not UTF-8.
    """
    try:
        result = CLIENT.run(arguments, directory=root)
        return GitResult(result.returncode, result.output_bytes().decode("utf-8"), result.stderr)
    except GitError as error:
        raise ReleaseNotesError(str(error)) from error
    except UnicodeDecodeError as error:
        raise ReleaseNotesError(f"git {' '.join(arguments)} printed output that is not UTF-8: {error}") from error


def git_output(root: Path, *arguments: str) -> str:
    """git's output on the checkout at root, as `git` reads it; ReleaseNotesError naming the command when git cannot
    run or exits nonzero."""
    completed = git(root, *arguments)
    if completed.returncode != 0:
        reason = flat_text(completed.stderr) or f"exit code {completed.returncode}"
        raise ReleaseNotesError(f"git {' '.join(arguments)} failed: {reason}")
    return completed.stdout


def commits(root: Path, start: str, end: str) -> list[Commit]:
    """The commits reachable from end and not from start, newest first, merge commits left out.

    Raises ReleaseNotesError when git's listing is not whole commits of a name, a subject, and a message.
    """
    listed = git_output(root, "log", "-z", "--no-merges", "--format=%H%x00%s%x00%B", f"{start}..{end}")
    fields = listed.split(FIELD_END)
    if fields.pop() != "" or len(fields) % COMMIT_FIELDS:
        raise ReleaseNotesError(f"git log {start}..{end} printed a listing that is not whole commits")
    return [Commit(*fields[index : index + COMMIT_FIELDS]) for index in range(0, len(fields), COMMIT_FIELDS)]


def _notes_at(root: Path, revision: str) -> str | None:
    """The notes file as the revision holds it, or None when the revision holds no such file. ReleaseNotesError, with
    git's reason, when git cannot read the revision, so a failure never reads as a missing file."""
    listed = git_output(root, "ls-tree", "-z", "--name-only", revision, "--", UPGRADE_NOTES)
    if not listed:
        return None
    return git_output(root, "show", f"{revision}:{UPGRADE_NOTES}")


def added_unreleased_entries(root: Path, sha: str) -> list[NotesEntry]:
    """The entries a commit added under `## Unreleased` in the notes file: ones whose text its parent does not have."""
    before = {entry.text for entry in notes_entries((_notes_at(root, f"{sha}^") or "").replace("\r\n", "\n"))}
    return [entry for entry in unreleased_entries(_notes_at(root, sha)) if entry.text not in before]


def notes_commits(root: Path, start: str, end: str) -> set[str]:
    """The commits reachable from end and not from start that change the notes file, merge commits left out."""
    return set(
        output_lines(git_output(root, "log", "--no-merges", "--format=%H", f"{start}..{end}", "--", UPGRADE_NOTES))
    )


def range_entries(root: Path, start: str, end: str) -> list[SourcedEntry]:
    """Every entry the commits between two refs carry, newest first.

    An entry a commit added to the notes file is left out when a newer commit added one with its heading there: the
    file held one entry per heading, so the newer is the same entry reworded. An entry from a pull request body is that
    pull request's own and is always kept, so two entries may share a heading; shared_headings names them.
    """
    touched = notes_commits(root, start, end)
    found: list[SourcedEntry] = []
    reworded: set[str] = set()
    for commit in commits(root, start, end):
        short = commit.sha[:7]
        sourced = [
            SourcedEntry(entry, f"commit {short}'s {BODY_HEADING}", commit.pull_request, from_notes=False)
            for entry in body_entries(commit.message)
        ]
        if commit.sha in touched:
            sourced += [
                SourcedEntry(entry, f"{UPGRADE_NOTES} as commit {short} left it", commit.pull_request, from_notes=True)
                for entry in added_unreleased_entries(root, commit.sha)
            ]
        for item in sourced:
            if not item.from_notes:
                found.append(item)
            elif item.entry.heading not in reworded:
                reworded.add(item.entry.heading)
                found.append(item)
    return found


def shared_headings(entries: list[SourcedEntry]) -> list[str]:
    """A `SHARED` line for each heading two or more of the entries share, naming where each was read, in their order."""
    places: dict[str, list[str]] = {}
    for item in entries:
        places.setdefault(item.entry.heading, []).append(item.where)
    return [f'SHARED "{heading}": {"; ".join(where)}' for heading, where in places.items() if len(where) > 1]


def version_section(version: str, entries: list[SourcedEntry], levels: list[str]) -> str:
    try:
        level = largest_level((item.entry.fields.get("Level", "") for item in entries), levels)
    except ValueError as error:
        raise ReleaseNotesError(str(error)) from error
    summary = f"{LEVEL_LINE}{level}." if level is not None else f"{LEVEL_LINE}none (no upgrade-note entries)."
    return "\n\n".join([f"## {version}", summary, *(item.rendered() for item in entries)]) + "\n"


def release_reaching(root: Path, ref: str) -> str | None:
    """The last release ref holds, as ejryan89-skill-core's release_tags reads it, or None when no release tag reaches
    it.

    Raises ReleaseNotesError when git cannot read the tags reaching it.
    """
    try:
        return last_release(CLIENT, root, ref)
    except GitError as error:
        raise ReleaseNotesError(f"cannot read the release tags reaching {ref}: {error}") from error


def last_release_before(root: Path, ref: str) -> str:
    """The last release before ref: the one its parent holds, so a ref that is itself a release starts after the one
    before it. Raises ReleaseNotesError when there is none or git cannot read the tags."""
    found = release_reaching(root, f"{ref}^")
    if found is None:
        raise ReleaseNotesError(f"no release tag reaches {ref}^; name the previous release with --from")
    return found


def insert_section(notes: str, version: str, section: str) -> str:
    """The notes with the section above the newest version's section, or at the end when there is none. A heading-shaped
    line a code block, a list item, or a blockquote holds is its text, as GitHub renders it and markdown_sections'
    document_headings reads it, so it is neither a version's section nor the place the new one goes."""
    lines, breaks = lines_and_breaks(notes)
    versions = [
        (index, heading.group("text"))
        for index, heading in document_headings(lines)
        if heading.group("marks") == "##" and VERSION.fullmatch(heading.group("text"))
    ]
    headings = [index for index, _ in versions]
    if any(text == version for _, text in versions):
        raise ReleaseNotesError(f"{UPGRADE_NOTES} already has a ## {version} section")
    if not headings:
        return notes.rstrip("\n") + "\n\n" + section
    start = sum(map(len, lines[: headings[0]])) + sum(map(len, breaks[: headings[0]]))
    return notes[:start] + section + "\n" + notes[start:]


def held_section(root: Path, version: str, end: str) -> str:
    """The version's section of the notes file at end; ReleaseNotesError when end holds none."""
    section = markdown_section(_notes_at(root, end) or "", f"## {version}")
    if section is None:
        raise ReleaseNotesError(f"{UPGRADE_NOTES} at {end} has no ## {version} section; write it with --write first")
    return section


def missing_entries(root: Path, version: str, start: str, end: str) -> list[SourcedEntry]:
    """The entries the commits between two refs carry that the version's section of the notes file at end lacks, by
    heading, newest first."""
    held = {entry.heading for entry in notes_entries(held_section(root, version, end))}
    return [item for item in range_entries(root, start, end) if item.entry.heading not in held]


def notes_problems(notes: str, version: str, held: str, counts: str, end: str) -> list[str]:
    """What the release notes lack: the version's heading as their first line that is not blank, and, in their own
    section of the version, the held section's Level line, each entry it holds, by heading, and the counts line, each
    line read without the blanks around it."""
    heading = f"## {version}"
    opening = next((line for line in LINE_BREAK.split(notes) if line.strip(" \t")), "")
    problems = [] if opening.rstrip(" \t") == heading else [f"the release notes do not open with {heading}"]
    section = markdown_section(notes, heading) or ""
    lines = {line.strip(" \t") for line in section.split("\n")}
    level = next((line.strip(" \t") for line in held.split("\n") if line.startswith(LEVEL_LINE)), None)
    if level is not None and level not in lines:
        problems.append(
            f"the release notes' {heading} section lacks the line {flat_text(level)} that {heading} in "
            f"{UPGRADE_NOTES} at {end} holds"
        )
    published = {entry.heading for entry in notes_entries(section)}
    problems.extend(
        f"the release notes' {heading} section lacks the entry {ENTRY_MARKS} {flat_text(entry.heading)} that "
        f"{heading} in {UPGRADE_NOTES} at {end} holds"
        for entry in notes_entries(held)
        if entry.heading not in published
    )
    if counts not in lines:
        problems.append(f"the release notes' {heading} section lacks the line --counts prints: {counts}")
    return problems


def _read_notes(path: Path) -> str:
    try:
        # utf-8-sig: a notes file Windows PowerShell 5's Set-Content -Encoding UTF8 writes opens with a byte order mark.
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as error:
        raise ReleaseNotesError(
            f'cannot read the release notes "{flat_text(str(path))}": {flat_text(str(error))}'
        ) from error


def _check(root: Path, version: str, start: str, end: str, notes: Path, client: GitHubClient) -> int:
    problems: list[str] = []
    try:
        missing = missing_entries(root, version, start, end)
        if not missing:
            problems = notes_problems(
                _read_notes(notes), version, held_section(root, version, end), milestone_counts(client, version), end
            )
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if missing:
        print(
            f"BEHIND {len(missing)} {'entry' if len(missing) == 1 else 'entries'}: the commits from {start} to {end} "
            f"carry upgrade-note entries the ## {version} section of {UPGRADE_NOTES} at {end} lacks; carry them into "
            "it through a pull request before tagging."
        )
        for item in missing:
            print(f"\n{item.rendered()}")
        return 1
    for problem in problems:
        print(f"NOTES_INCOMPLETE {problem}")
    if problems:
        return 1
    print(
        f"CURRENT ## {version} in {UPGRADE_NOTES} at {end} holds every upgrade-note entry from {start} to {end}, and "
        "the release notes open with it and carry the line --counts prints."
    )
    return 0


@dataclass(frozen=True)
class MilestoneIssue:
    number: int
    labels: frozenset[str]
    open: bool
    completed: bool


def _pages(client: GitHubClient, endpoint: str) -> list[object]:
    """Every item of a paginated REST listing, read with `--paginate --slurp`, which wraps each page's array in one;
    ReleaseNotesError when gh fails or answers with something that is not pages of items."""
    try:
        pages = client.json(["api", endpoint, "--paginate", "--slurp"])
    except GitHubError as error:
        raise ReleaseNotesError(f"cannot read {endpoint}: {error}") from error
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise ReleaseNotesError(f"gh api {endpoint} answered with something that is not pages of items")
    return [item for page in pages for item in page]


def _milestone(client: GitHubClient, version: str) -> tuple[int, int]:
    """The milestone titled version, as its number and the count of items on it, open and closed, pull requests
    included, as GitHub keeps them; ReleaseNotesError when no milestone, or more than one, has the title."""
    found = [item for item in _pages(client, MILESTONES) if isinstance(item, dict) and item.get("title") == version]
    if len(found) != 1:
        raise ReleaseNotesError(f"{len(found)} milestones are titled {version}; --counts reads exactly one")
    values = [found[0].get(key) for key in ("number", "open_issues", "closed_issues")]
    counts = [value for value in values if isinstance(value, int) and not isinstance(value, bool)]
    if len(counts) != len(values):
        raise ReleaseNotesError(f"gh answered with a milestone {version} that has no number and counts: {found[0]!r}")
    number, opened, closed = counts
    return number, opened + closed


def _milestone_issue(item: dict[str, object]) -> MilestoneIssue:
    """An issue of the REST listing; ReleaseNotesError when the item is not one."""
    number, labels, state, reason = (item.get(key) for key in ("number", "labels", "state", "state_reason"))
    if isinstance(number, int) and isinstance(labels, list) and state in (OPEN, CLOSED) and reason in CLOSE_REASONS:
        names = [label.get("name") if isinstance(label, dict) else None for label in labels]
        if all(isinstance(name, str) for name in names):
            found = frozenset(name for name in names if isinstance(name, str))
            return MilestoneIssue(number, found, state == OPEN, state == CLOSED and reason in COMPLETED_REASONS)
    raise ReleaseNotesError(f"gh answered with an issue that is not a number, labels, and a state: {item!r}")


def _refused(version: str, issues: list[MilestoneIssue]) -> list[str]:
    """Why the milestone's fixed issues cannot be counted: each one with no kind label, an origin label and no `bug`, or
    two origin labels, named by number."""
    fixed = [issue for issue in issues if issue.completed]
    problems = (
        ("carry no kind label", [issue.number for issue in fixed if not issue.labels & set(KIND_LABELS)]),
        (
            f"carry an origin label without `{DEFECT_LABEL}`",
            [issue.number for issue in fixed if issue.labels & set(ORIGIN_LABELS) and DEFECT_LABEL not in issue.labels],
        ),
        (
            "carry more than one origin label",
            [issue.number for issue in fixed if len(issue.labels & set(ORIGIN_LABELS)) > 1],
        ),
    )
    return [
        f"closed issues on milestone {version} {what}: {', '.join(f'#{number}' for number in sorted(numbers))}"
        for what, numbers in problems
        if numbers
    ]


def milestone_counts(client: GitHubClient, version: str) -> str:
    """The release notes' line counting the milestone's fixed bugs by origin label and the auditor gaps on it.

    Raises ReleaseNotesError when gh cannot read the milestone or its issues, no milestone has the title, the listing
    disagrees with the milestone's own counts, an item is not an issue, or a fixed issue's labels cannot be counted.
    """
    number, held = _milestone(client, version)
    items = _pages(client, MILESTONE_ISSUES.replace("{number}", str(number)))
    if len(items) != held:
        raise ReleaseNotesError(
            f"milestone {version} holds {held} issues and pull requests, but its listing gave {len(items)}; "
            "count them by hand"
        )
    if not all(isinstance(item, dict) for item in items):
        raise ReleaseNotesError(f"gh answered with an item of milestone {version} that is not an object")
    issues = [_milestone_issue(item) for item in items if isinstance(item, dict) and "pull_request" not in item]
    refused = _refused(version, issues)
    if refused:
        raise ReleaseNotesError("; ".join(refused))
    bugs = [issue.labels & set(ORIGIN_LABELS) for issue in issues if issue.completed and DEFECT_LABEL in issue.labels]
    by_origin = [f"{origin} {sum(origin in origins for origins in bugs)}" for origin in ORIGIN_LABELS]
    gaps = sum(AUDITOR_GAP_LABEL in issue.labels and (issue.open or issue.completed) for issue in issues)
    return (
        f"Defects fixed: {len(bugs)} ({', '.join(by_origin)}, no origin label {sum(not origins for origins in bugs)}). "
        f"Auditor gaps: {gaps}."
    )


def _client(root: Path, client: GitHubClient | None) -> GitHubClient:
    """The client given, or one whose gh runs in the checkout, since gh reads the repository from its directory."""
    return client or GitHubClient(runner=functools.partial(subprocess_runner, cwd=root))


def _counts(version: str, client: GitHubClient) -> int:
    try:
        print(milestone_counts(client, version))
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


def _read_releasing(root: Path) -> str:
    try:
        return (root / RELEASING_DOC).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseNotesError(f"cannot read {RELEASING_DOC}: {error}") from error


def main(argv: list[str] | None = None, root: Path = REPOSITORY_ROOT, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a release's upgrade-notes section from its merged commits.")
    parser.add_argument("version", help="the version being released, such as v0.5.0")
    parser.add_argument("--from", dest="start", help="the previous release's ref; defaults to the last tag before --to")
    parser.add_argument(
        "--to", dest="end", help=f"the release's ref; defaults to HEAD, or to {RELEASE_BRANCH} with --check"
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--write", action="store_true", help=f"insert the section into {UPGRADE_NOTES}")
    action.add_argument(
        "--check",
        action="store_true",
        help=f"compare the section {UPGRADE_NOTES} holds at --to with the entries the range carries",
    )
    action.add_argument(
        "--counts",
        action="store_true",
        help="print the release notes' counts of the milestone's fixed bugs by origin and the auditor gaps on it",
    )
    parser.add_argument(
        "--notes",
        type=Path,
        metavar="FILE",
        help="with --check, the release notes to publish, which must hold the section and the line --counts prints",
    )
    options = parser.parse_args(argv)
    if not is_release_tag(options.version):
        parser.error(f"the version must be a release tag such as v1.2.3, not {options.version!r}")
    if options.check and options.notes is None:
        parser.error("--check reads the release notes too: give --notes FILE")
    if options.notes is not None and not options.check:
        parser.error("--notes is read only with --check")
    if options.counts:
        if options.start is not None or options.end is not None:
            parser.error("--counts reads the milestone, not a range: drop --from and --to")
        return _counts(options.version, _client(root, client))
    options.end = options.end or (RELEASE_BRANCH if options.check else "HEAD")
    try:
        start = options.start or last_release_before(root, options.end)
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if options.check:
        return _check(root, options.version, start, options.end, options.notes, _client(root, client))
    try:
        levels = versioning_levels(_read_releasing(root))
        entries = range_entries(root, start, options.end)
        section = version_section(options.version, entries, levels)
    except (OSError, ReleaseNotesError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    for line in shared_headings(entries):
        print(line, file=sys.stderr)
    if not options.write:
        print(section, end="")
        return 0
    path = root / UPGRADE_NOTES
    try:
        notes = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except (OSError, UnicodeError) as error:
        print(f"ERROR: cannot read {UPGRADE_NOTES}: {error}", file=sys.stderr)
        return 2
    try:
        updated = insert_section(notes, options.version, section)
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    try:
        path.write_text(updated, encoding="utf-8", newline="\n")
    except OSError as error:
        print(f"ERROR: cannot write {UPGRADE_NOTES}: {error}", file=sys.stderr)
        return 2
    print(f"Wrote ## {options.version} from {start}..{options.end} into {UPGRADE_NOTES}.")
    return 0


if __name__ == "__main__":
    # The section is Markdown the repository keeps with LF endings, so a redirected run writes LF too.
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace", newline="\n")
