"""Write a release's section of docs/upgrade-notes.md from the upgrade notes its merged commits carry.

Usage:
  python tools/release_notes.py VERSION [--from REF] [--to REF] [--write]

Each pull request states what it asks of a user who updates under a `## Upgrade note` heading in its body, and the
repository squash-merges with the body as the commit message, so every merged change carries its entry into the
history. This script reads the entries of the commits between two refs (merge commits left out), newest first: the
`###` entries of each message's `## Upgrade note` section, and, for a commit made while entries were still written
by hand, the entries it added under `## Unreleased` in docs/upgrade-notes.md. An entry whose heading a newer commit
already gave is the older text of that entry and is left out. An entry is read with each hard-wrapped heading, list
item, or paragraph joined into one line, since a squash commit's message may be wrapped. Each entry keeps that text
and gains `- Pull request: #N` from the commit's squash title when it names none.

--from defaults to the last tag before --to, and --to to HEAD. It prints the `## VERSION` section, or with --write
inserts it into docs/upgrade-notes.md above the newest version's section, refusing a version the file already has.
The section opens with the largest leading Level word of its entries, ordered by docs/releasing.md, or states that
there are no upgrade-note entries. It exits 0 on success, 1 when --write is refused, and 2 when git cannot read the
range or the documented levels or an entry's Level cannot be read. The release procedure in docs/releasing.md runs
it before tagging, and the upgrade-notes check in tests/validation reads the same entries.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from bounded_process import without_repository_variables
from console import run_main
from executable_locator import located_command
from markdown_sections import markdown_section
from release_levels import RELEASING_DOC, VERSION, largest_level, versioning_levels

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
UPGRADE_NOTES = "docs/upgrade-notes.md"
BODY_HEADING = "## Upgrade note"
UNRELEASED_HEADING = "## Unreleased"
VERSION_HEADING = re.compile(rf"^## {VERSION.pattern}[ \t]*$", re.MULTILINE)
NOTES_FIELD = re.compile(r"^- (Level|Contract|User action|Pull request):[ \t]*(.*?)[ \t]*$", re.MULTILINE)
NOTES_ENTRY_START = re.compile(r"^(?=#{1,3} )", re.MULTILINE)
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
# A line that starts a Markdown block of its own, which no wrapped line continues: a heading, a list item, a quote, a
# table row, or a fence.
BLOCK_START = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\|)")
FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})")
SQUASH_TITLE_NUMBER = re.compile(r"\(#(\d+)\)\s*$")
# Separators git log writes between a commit's fields and between commits; neither occurs in a commit message.
FIELD_END, RECORD_END = "\x1f", "\x1e"


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


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """git run on the checkout at root, without the repository-selecting variables that would read another one, and
    started at the path skill-core's locator finds rather than by name, which Windows looks up in the working directory
    first."""
    return subprocess.run(
        located_command(["git", "-C", str(root), *arguments]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=without_repository_variables(os.environ),
        check=False,
    )


def _git_output(root: Path, *arguments: str) -> str:
    completed = _git(root, *arguments)
    if completed.returncode != 0:
        raise ValueError(f"git {' '.join(arguments)} failed: {completed.stderr.strip()}")
    return completed.stdout


def commits(root: Path, start: str, end: str) -> list[Commit]:
    """The commits reachable from end and not from start, newest first, merge commits left out."""
    listed = _git_output(
        root, "log", "--no-merges", f"--format=%H{FIELD_END}%s{FIELD_END}%B{RECORD_END}", f"{start}..{end}"
    )
    found: list[Commit] = []
    for record in listed.split(RECORD_END):
        if record.strip():
            sha, subject, message = record.lstrip("\n").split(FIELD_END, 2)
            found.append(Commit(sha, subject, message))
    return found


def _notes_at(root: Path, revision: str) -> str | None:
    shown = _git(root, "show", f"{revision}:{UPGRADE_NOTES}")
    return shown.stdout if shown.returncode == 0 else None


def added_unreleased_entries(root: Path, sha: str) -> list[NotesEntry]:
    """The entries a commit added under `## Unreleased` in the notes file: ones whose text its parent does not have."""
    before = {entry.text for entry in notes_entries((_notes_at(root, f"{sha}^") or "").replace("\r\n", "\n"))}
    return [entry for entry in unreleased_entries(_notes_at(root, sha)) if entry.text not in before]


def range_entries(root: Path, start: str, end: str) -> list[SourcedEntry]:
    """Every entry the commits between two refs carry, newest first, each heading once."""
    touched = set(
        _git_output(root, "log", "--no-merges", "--format=%H", f"{start}..{end}", "--", UPGRADE_NOTES).split()
    )
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
    level = largest_level((item.entry.fields.get("Level", "") for item in entries), levels)
    summary = f"Level: {level}." if level is not None else "Level: none (no upgrade-note entries)."
    return "\n\n".join([f"## {version}", summary, *(item.rendered() for item in entries)]) + "\n"


def last_tag_before(root: Path, ref: str) -> str:
    return _git_output(root, "describe", "--tags", "--abbrev=0", f"{ref}^").strip()


def insert_section(notes: str, version: str, section: str) -> str:
    """The notes with the section above the newest version's section, or at the end when there is none."""
    if re.search(rf"^## {re.escape(version)}[ \t]*$", notes, re.MULTILINE):
        raise ValueError(f"{UPGRADE_NOTES} already has a ## {version} section")
    newest = VERSION_HEADING.search(notes)
    if newest is None:
        return notes.rstrip("\n") + "\n\n" + section
    return notes[: newest.start()] + section + "\n" + notes[newest.start() :]


def main(argv: list[str] | None = None, root: Path = REPOSITORY_ROOT) -> int:
    parser = argparse.ArgumentParser(description="Write a release's upgrade-notes section from its merged commits.")
    parser.add_argument("version", help="the version being released, such as v0.5.0")
    parser.add_argument("--from", dest="start", help="the previous release's ref; defaults to the last tag before --to")
    parser.add_argument("--to", dest="end", default="HEAD", help="the release's ref; defaults to HEAD")
    parser.add_argument("--write", action="store_true", help=f"insert the section into {UPGRADE_NOTES}")
    options = parser.parse_args(argv)
    if not VERSION.fullmatch(options.version):
        parser.error(f"the version must look like v1.2.3, not {options.version!r}")
    try:
        start = options.start or last_tag_before(root, options.end)
        levels = versioning_levels((root / RELEASING_DOC).read_text(encoding="utf-8"))
        section = version_section(options.version, range_entries(root, start, options.end), levels)
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if not options.write:
        print(section, end="")
        return 0
    path = root / UPGRADE_NOTES
    try:
        updated = insert_section(path.read_text(encoding="utf-8").replace("\r\n", "\n"), options.version, section)
    except ValueError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    path.write_text(updated, encoding="utf-8", newline="\n")
    print(f"Wrote ## {options.version} from {start}..{options.end} into {UPGRADE_NOTES}.")
    return 0


if __name__ == "__main__":
    # The section is Markdown the repository keeps with LF endings, so a redirected run writes LF too.
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace", newline="\n")
