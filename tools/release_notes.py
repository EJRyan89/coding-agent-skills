"""Write a release's section of docs/upgrade-notes.md from the upgrade notes its merged commits carry.

Usage:
  python tools/release_notes.py VERSION [--from REF] [--to REF] [--write | --check]

Each pull request states what it asks of a user who updates under a `## Upgrade note` heading in its body, and the
repository squash-merges with the body as the commit message, so every merged change carries its entry into the
history. This script reads the entries of the commits between two refs (merge commits left out), newest first: the
`###` entries of each message's `## Upgrade note` section, and, for a commit made while entries were still written
by hand, the entries it added under `## Unreleased` in docs/upgrade-notes.md. An entry whose heading a newer commit
already gave is the older text of that entry and is left out. An entry is read with each hard-wrapped heading, list
item, or paragraph joined into one line, since a squash commit's message may be wrapped. Each entry keeps that text
and gains `- Pull request: #N` from the commit's squash title when it names none.

--from defaults to the last release before --to, the highest release tag reachable from --to's parent as
ejryan89-skill-core's release_tags.py defines a release tag (vMAJOR.MINOR.PATCH and nothing after it), and --to to HEAD,
or to origin/main with --check. A version must be a release tag too. It prints the `## VERSION` section, or with --write
inserts it into docs/upgrade-notes.md above the newest version's section, refusing a version the file already has. The
section opens with the largest leading Level word of its entries, ordered by docs/releasing.md, or states that there are
no upgrade-note entries.

--check compares the `## VERSION` section of docs/upgrade-notes.md as --to holds it with the entries the commits in
the range carry, by heading, and fetches nothing. The release pull request is squash-merged, so the commit the
section was written at never reaches main, and a pull request merged into main or into the release branch after the
section was written is in the tag and in no section. It prints `CURRENT` when the section holds every entry, and
otherwise `BEHIND <n> entries` and each entry the section lacks, ready to carry into it. An entry the section holds
beyond the range's, such as an obligation the release's preparation issue records, is the releaser's own.

It exits 0 on success, 1 when --write is refused because the file already has the version or --check finds entries the
section lacks, and 2, with an `ERROR:` line naming what it could not read or write, when git cannot read the range or,
without --from, no release reaches --to's parent, when docs/releasing.md or its documented levels or an entry's Level
cannot be read, with --write when docs/upgrade-notes.md cannot be read as UTF-8 text or written, or with --check when
--to holds no `## VERSION` section. Git runs through ejryan89-skill-core's GitClient, which starts it without the
repository-selecting variables that would read another repository. The release procedure in docs/releasing.md runs it
before tagging and again at the tag, and the upgrade-notes check in tests/validation reads the same entries through the
same git.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from console import run_main
from git_client import GitClient, GitError, GitResult
from markdown_sections import HTML_COMMENT, markdown_section
from release_levels import RELEASING_DOC, VERSION, largest_level, versioning_levels
from release_tags import is_release_tag, last_release

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_NOTES = "docs/upgrade-notes.md"
BODY_HEADING = "## Upgrade note"
UNRELEASED_HEADING = "## Unreleased"
RELEASE_BRANCH = "origin/main"
VERSION_HEADING = re.compile(rf"^## {VERSION.pattern}[ \t]*$", re.MULTILINE)
NOTES_FIELD = re.compile(r"^- (Level|Contract|User action|Pull request):[ \t]*(.*?)[ \t]*$", re.MULTILINE)
NOTES_ENTRY_START = re.compile(r"^(?=#{1,3} )", re.MULTILINE)
# A line that starts a Markdown block of its own, which no wrapped line continues: a heading, a list item, a quote, a
# table row, or a fence.
BLOCK_START = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\|)")
FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")
SQUASH_TITLE_NUMBER = re.compile(r"\(#(\d+)\)\s*$")
# What git log -z writes after each of a commit's name, subject, and message: NUL, which git refuses in a commit message
# and cuts a message at, so no message text can end a field early.
FIELD_END = "\0"
COMMIT_FIELDS = 3


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
        return {item.rstrip("/") for item in re.findall(r"`([^`]+)`", self.fields.get("Contract", ""))}


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

    A squash merge may carry a body wrapped as git and GitHub wrap commit messages, so a line that starts no block
    of its own continues the line above it, unless that line is blank or the two are inside a code fence.
    """
    lines: list[str] = []
    fenced = False
    for line in (line.rstrip() for line in block.strip().split("\n")):
        starts_fence = bool(FENCE.match(line))
        if lines and lines[-1] and line and not fenced and not starts_fence and not BLOCK_START.match(line):
            lines[-1] = f"{lines[-1]} {line.strip()}"
        else:
            lines.append(line)
        fenced ^= starts_fence
    return lines


def notes_entries(text: str | None) -> list[NotesEntry]:
    """The upgrade-notes entries: each ### heading with its field lines, read with wrapped lines joined."""
    entries: list[NotesEntry] = []
    for block in NOTES_ENTRY_START.split(text or ""):
        if block.startswith("### "):
            heading, *body = unwrapped(block)
            fields = {match.group(1): match.group(2) for match in NOTES_FIELD.finditer("\n".join(body))}
            entries.append(NotesEntry(heading[4:].strip(), fields, "\n".join([heading, *body])))
    return entries


def body_entries(body: str) -> list[NotesEntry]:
    """The entries under a pull request body's `## Upgrade note` heading; its HTML comments are not read."""
    text = HTML_COMMENT.sub("", body.replace("\r\n", "\n"))
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
        raise ReleaseNotesError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
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
    listed = git_output(root, "ls-tree", "--name-only", revision, "--", UPGRADE_NOTES)
    if not listed.strip():
        return None
    return git_output(root, "show", f"{revision}:{UPGRADE_NOTES}")


def added_unreleased_entries(root: Path, sha: str) -> list[NotesEntry]:
    """The entries a commit added under `## Unreleased` in the notes file: ones whose text its parent does not have."""
    before = {entry.text for entry in notes_entries((_notes_at(root, f"{sha}^") or "").replace("\r\n", "\n"))}
    return [entry for entry in unreleased_entries(_notes_at(root, sha)) if entry.text not in before]


def notes_commits(root: Path, start: str, end: str) -> set[str]:
    """The commits reachable from end and not from start that change the notes file, merge commits left out."""
    return set(git_output(root, "log", "--no-merges", "--format=%H", f"{start}..{end}", "--", UPGRADE_NOTES).split())


def range_entries(root: Path, start: str, end: str) -> list[SourcedEntry]:
    """Every entry the commits between two refs carry, newest first, each heading once."""
    touched = notes_commits(root, start, end)
    found: list[SourcedEntry] = []
    seen: set[str] = set()
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
            if item.entry.heading not in seen:
                seen.add(item.entry.heading)
                found.append(item)
    return found


def version_section(version: str, entries: list[SourcedEntry], levels: list[str]) -> str:
    try:
        level = largest_level((item.entry.fields.get("Level", "") for item in entries), levels)
    except ValueError as error:
        raise ReleaseNotesError(str(error)) from error
    summary = f"Level: {level}." if level is not None else "Level: none (no upgrade-note entries)."
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
    """The notes with the section above the newest version's section, or at the end when there is none."""
    if re.search(rf"^## {re.escape(version)}[ \t]*$", notes, re.MULTILINE):
        raise ReleaseNotesError(f"{UPGRADE_NOTES} already has a ## {version} section")
    newest = VERSION_HEADING.search(notes)
    if newest is None:
        return notes.rstrip("\n") + "\n\n" + section
    return notes[: newest.start()] + section + "\n" + notes[newest.start() :]


def missing_entries(root: Path, version: str, start: str, end: str) -> list[SourcedEntry]:
    """The entries the commits between two refs carry that the version's section of the notes file at end lacks, by
    heading, newest first."""
    section = markdown_section(_notes_at(root, end) or "", f"## {version}")
    if section is None:
        raise ReleaseNotesError(f"{UPGRADE_NOTES} at {end} has no ## {version} section; write it with --write first")
    held = {entry.heading for entry in notes_entries(section)}
    return [item for item in range_entries(root, start, end) if item.entry.heading not in held]


def _check(root: Path, version: str, start: str, end: str) -> int:
    try:
        missing = missing_entries(root, version, start, end)
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if not missing:
        print(f"CURRENT ## {version} in {UPGRADE_NOTES} at {end} holds every upgrade-note entry from {start} to {end}.")
        return 0
    print(
        f"BEHIND {len(missing)} {'entry' if len(missing) == 1 else 'entries'}: the commits from {start} to {end} carry "
        f"upgrade-note entries the ## {version} section of {UPGRADE_NOTES} at {end} lacks; carry them into it through "
        "a pull request before tagging."
    )
    for item in missing:
        print(f"\n{item.rendered()}")
    return 1


def _read_releasing(root: Path) -> str:
    try:
        return (root / RELEASING_DOC).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseNotesError(f"cannot read {RELEASING_DOC}: {error}") from error


def main(argv: list[str] | None = None, root: Path = REPOSITORY_ROOT) -> int:
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
    options = parser.parse_args(argv)
    if not is_release_tag(options.version):
        parser.error(f"the version must be a release tag such as v1.2.3, not {options.version!r}")
    options.end = options.end or (RELEASE_BRANCH if options.check else "HEAD")
    try:
        start = options.start or last_release_before(root, options.end)
    except ReleaseNotesError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if options.check:
        return _check(root, options.version, start, options.end)
    try:
        levels = versioning_levels(_read_releasing(root))
        section = version_section(options.version, range_entries(root, start, options.end), levels)
    except (OSError, ReleaseNotesError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
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
