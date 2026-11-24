"""Audit, reindex, and delete from a Claude Code memory directory, and find a skill's source.

Every command prints one fact per line on stdout. An expected failure is a last line `FAILED <reason>`.
Text read from a settings file, the manifest, a memory file's name, or a memory reaches a line or a MEMORY.md entry
only through ejryan89-skill-core's flat_text, and a path holding a line break, which no line can name for a later
command, is refused rather than printed.

A memory store is a directory holding MEMORY.md, outside the skills directory this script runs from,
~/.claude/skills, and ~/.agents/skills. audit, reindex, and delete take only a memory store: given any
other directory, they print one `FAILED <dir> ...` line and read and change nothing. resolve
reports the same check as a `REFUSED` line, so the skill learns before any change that the later
commands refuse the directory. A memory is a regular file named *.md directly in the store, other than
MEMORY.md; any other entry so named, a directory, symbolic link, or junction, is reported unreadable with
its reason and never read, added to the index, or deleted.

audit reads the memory store through ejryan89-skill-core's tree_reader bound to it, and the repository (--repo), where
it looks for the instruction files a memory's overlaps are measured against and the paths a memory cites, through a
reader bound to it, so no link leading out of either is followed, not even to learn what it is: an instruction file that
a link on its way leads out is passed over unread, as a linked folder always was, a cited path through one is reported
unreadable with the reader's reason, and a store whose MEMORY.md is such a link is no memory store. A cited path,
relative, absolute, or ~, that names a place in the repository or the store, its `..` taken as text, wherever it was
written from, or that meets a link inside either on its way, its `..` taken as text until then, however its `..` climbs
out and back in, is looked up through that tree's reader, so one through a link leading out of it, or whose `..` climbs
out after a link, is reported unreadable too. A cited path that names neither and meets no such link, an --instructions
file, the settings, and the deployer's manifest are read where they are named, as TREE_READ_ALLOWED states. The
instruction files in the user's configuration directory (--user-dir) are read with their links followed, as Claude Code
loads a CLAUDE.md linked from a dotfiles checkout: refusing a link guards against a repository the user did not write,
and that directory is the user's own. A cited path is read only as PATH_LIKE matches one, so a path holding `~` past its
start, as a Windows short name such as RUNNER~1 does, is not read as a cited path: a stated limit.

resolve and audit are read-only. They never decide what to keep or delete; the calling agent verifies
every candidate. resolve locates the memory directory for --repo, else for the current directory, and
prints `REPO <root>`, the top level of the Git worktree holding that folder, or the folder itself when
--repo names one in no Git repository; then `MEMORY_DIR <path>`, `SOURCE <where it came from>`,
`EXISTS yes|no`, `no` only when nothing is at the path, and `REFUSED <reason>` when something is there but
is no memory store: a folder without the index, a file or anything else in the folder's place, or a path
that cannot be examined, which counts as existing; or `NO_MEMORY_DIR` when no usable directory resolves; one
`CANDIDATE <dir>` per memory directory under the configuration directory's projects folder when no memory
store resolved, whether settings named a missing one, the default one is missing or refused, or none is
usable; and one `NOTE <text>` per caveat, among them each memory directory there that cannot be looked at.
No directory is a result, not a failure. A directory a repository's settings choose is not used while
permissions.blockReadsOutsideWorkingDirectories is on in any scope, since Claude Code then loads no
memory from it and saves none to it. Without settings, the directory is derived from the top level of
the repository's main worktree, which all its worktrees share (see main_worktree). The current
directory outside any repository, a git that cannot run, does not finish, or fails, and a settings file
that cannot be read or has a directory in its place, whose choice cannot then be known, are each a
`FAILED <reason>` line; a settings file holding no readable JSON leaves the directory unresolved, with
a NOTE naming it.

audit writes its JSON report of mechanical problems and candidate overlaps to --output, else to a new
temporary file, and prints `REPORT <path>`. It never replaces a file: it refuses, before reading
anything, an --output that exists or lies inside the memory directory, the configuration directory's
projects folder, the skills directory it runs from, ~/.claude/skills, or ~/.agents/skills, and it makes
no folder, so an --output whose folder is missing is a FAILED line. The report is made whole or not at
all, through ejryan89-skill-core's atomic_file.create_bytes, as a file its owner alone may read and
write: a write that fails part way leaves no report and no copy, unless the copy itself cannot be
removed, which the FAILED line names, so the next audit writes it. It is put
in place by a hard link, so an --output whose file system has none, such as FAT or exFAT, gets no
report, and the FAILED line names the file system's refusal. Once the report is in place it is whole:
a temporary copy beside it that cannot then be removed is a `FAILED the audit report <path> is made,
...` line naming the copy left, and the report is there to read. The report's
`unreadable` list names each entry it could not read as a memory, with the reason, and the rest are
audited. It searches the user's Claude configuration directory (CLAUDE_CONFIG_DIR, else ~/.claude)
unless --user-dir names another. A CLAUDE_CONFIG_DIR that is set but is not an absolute path, and a
home folder (USERPROFILE on Windows, HOME elsewhere) that is not one, is a `FAILED <reason>` line
naming the variable: every command reads the home and refuses such a one first, and any that
reads the configuration directory refuses such a CLAUDE_CONFIG_DIR, and nothing is read from it.

reindex rebuilds MEMORY.md from the memory files' frontmatter, one line per memory:
`- [Title](file.md) — hook`. An entry's file is read as CommonMark reads a link's destination,
through ejryan89-skill-core's link_destinations, and a new entry's is written so that it reads
back as the file's name; an entry in code is none, and neither is one whose link has a title.
A title and a hook are written as given, unless a backtick in them would pair with another into a
code span over the entry's file; only then is each of the title's backticks written as `'`, and
the hook's too when a backtick in a kept file spelling still pairs with one in the hook.
It writes only MEMORY.md, only with --write, and never deletes or changes a memory file.
Existing lines keep their place: other lines (headings, notes) stay
as written, an entry is dropped when its file is gone or it repeats an earlier entry, and
memories without an entry are appended in file-name order. The title is the existing entry's
title, else the frontmatter name, else the file stem. The hook is the frontmatter description,
all of a `>` or `|` block scalar's lines, else the existing entry's hook, else
the first line of the body, an ATX heading's text without its marks; a description or a body
line is collapsed to one line of at most 150 characters, because Claude Code loads the index
into every session and stops at its load limit.
It prints `INDEX_LINE`, `ADDED`, and `DROPPED` lines, then
`NEAR_LIMIT` or `OVER_LIMIT` when the index nears or passes Claude Code's load limit, then
`UNCHANGED`, `WOULD_WRITE <path>`, or `WROTE <path>`. OVER_LIMIT is a finding.
An entry whose target, after any leading `./`, is not a bare *.md name directly in the store (a `..`
segment, an absolute path, a subfolder, or a link or junction on the way) is never read or followed,
and neither is one whose file exists but is not a regular file: each stays as written, and it and each
such unindexed name print an `UNREADABLE <file>: <reason>` line before those. audit reports the same
targets in its `unreadable` list. A memory it cannot read, an entry it cannot inspect,
or a memory whose file name holds a line break, which no entry can link on one line, is a `FAILED` line,
and nothing is written.

delete removes the named memory files and nothing else. Each name must be a bare .md file name
directly inside the memory directory: not MEMORY.md, not a path (no separator, drive, `.`, or
`..`), holding no line break, not named twice, and an existing regular file, never a directory,
symbolic link, or junction, and not read-only: Windows will not delete a read-only file, so one is refused on every
platform alike. Every name is checked before any is deleted. When any name is refused it prints one
`FAILED <name>: <reason>` line per refusal and deletes nothing; otherwise it prints one
`DELETED <name>` line per file. When a deletion fails part way, the files already printed as
DELETED are gone, a last `FAILED <name>: <reason>` names the one that failed, and the rest remain.

skill-source prints `SKILL_FILE <path>` for each editable source of the named skill: the
skills/<name>/SKILL.md of the checkout the deployer's manifest records for it, and the repository's
.claude/skills/<name>/SKILL.md; with --file it checks a source file named instead. It never names a
file inside a skills directory, the deployed copy among them, because the deployer stops updating a
skill whose deployed copy changed, and prints a `NOTE` for each such file it passes over, for each
recorded source that is missing, a link to nothing included, is not a file, or cannot be examined, and
one when git cannot read the repository to look for a project skill. With no source left, it prints
`FAILED <reason>`.

Usage:
  python memory_audit.py resolve [--repo ROOT]
  python memory_audit.py audit --memory-dir DIR [--repo ROOT] [--user-dir DIR] [--instructions FILE ...] [--output FILE]
  python memory_audit.py reindex --memory-dir DIR [--write]
  python memory_audit.py delete --memory-dir DIR FILE [FILE ...]
  python memory_audit.py skill-source (NAME | --file FILE) [--repo ROOT] [--home DIR]

Exit status:
  0  the command did its job
  1  a FAILED <reason> line, a delete refusal among them, or reindex's OVER_LIMIT finding
  2  a usage error
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import itertools
import os
import re
import secrets
import stat
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import file_presence
import folder_walk
import json_reader
import json_writer
import private_folder
from atomic_file import TRACKED_FILE_MODE, UnfinishedCopyError, create_bytes, replace_text
from console import run_main
from dotnet_solution import FileRefused, lexical_absolute
from executable_locator import RelativeEnvironmentPath, absolute_home, environment_path, required_environment_path
from file_presence import is_directory, is_present, refusal_reason
from flat_text import flat_text, single_line
from folder_walk import followed_inside
from git_client import GitClient, GitError
from link_destinations import link_destinations, written_destination
from markdown_sections import ATX_HEADING, CODE, EMPTY_ATX_HEADING, HEADING, blocks, code_spans
from path_containment import resolved_path, within
from path_keys import name_key, opens_as, path_key
from skill_roots import skills_directory_holding
from text_lines import text_lines
from tree_reader import TreeReader, TreeRefused

INDEX_NAME = "MEMORY.md"
# The deployer's ownership manifest beside the skills it deploys, and where a source checkout keeps a skill.
MANIFEST_NAME = ".deploy-manifest.json"
SETTINGS_NOTE = (
    "A --settings file passed when Claude Code starts can also set autoMemoryDirectory; this audit cannot see it."
)
MANAGED_UNKNOWN_NOTE = (
    "The Program Files folder is not named by an absolute path, so managed settings were not read; one there can set "
    "autoMemoryDirectory."
)
BLOCK_READS = "blockReadsOutsideWorkingDirectories"
# Claude Code loads the first 200 lines or 25KB of MEMORY.md, whichever comes first.
INDEX_LINE_LIMIT = 200
INDEX_BYTE_LIMIT = 25_000
NEAR_LIMIT_FRACTION = 0.9
OVERLAP_THRESHOLD = 0.3
OVERLAP_LIMIT = 3
INDEX_ENTRY = re.compile(r"^\s*-\s*\[(?P<title>[^\]]*)\]\(")
# The separator before a hook may be an em dash, an en dash, a colon, or a hyphen, as people write index lines.
INDEX_HOOK = re.compile(r"^\s*(?:[—–:-]\s*)?(?P<hook>.*?)\s*$")  # noqa: RUF001 - the en dash is meant
HOOK_LIMIT = 150
FRONTMATTER_KEY = re.compile(r"^\s*(?P<key>[A-Za-z_][\w-]*)\s*:\s*(?P<value>.*)$")
# A literal or folded block scalar header: chomping and an indentation digit in either order, then a comment.
BLOCK_SCALAR = re.compile(r"^(?P<style>[|>])(?:[+-]?(?P<indent>[1-9])?|(?P<first>[1-9])[+-])\s*(?:#.*)?$")
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.{0,2}[\\/])?[\w.@()\- ]+(?:[\\/][\w.@()\- ]+)+[\\/]?$")
ANCHORED_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/])|[\\/]$|\.[A-Za-z0-9]{1,8}$")
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
STOP_WORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "are",
        "not",
        "use",
        "when",
        "into",
        "only",
        "must",
        "should",
        "never",
        "always",
        "all",
        "any",
        "can",
        "does",
        "each",
        "its",
        "has",
        "have",
        "was",
        "were",
        "will",
        "what",
        "which",
        "while",
        "who",
        "why",
        "how",
        "than",
        "then",
        "them",
        "they",
        "their",
        "there",
        "these",
        "those",
        "via",
        "per",
        "our",
        "out",
        "also",
        "but",
        "instead",
    ]
)
DESTINATION_GLOBS = (
    "README.md",
    "CONTRIBUTING.md",
    ".claude/rules/**/*.md",
    ".github/copilot-instructions.md",
    ".github/instructions/**/*.md",
    "docs/**/*.md",
    "Documents/**/*.md",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "**/AGENTS.md",
    "**/SKILL.md",
)
USER_DESTINATION_GLOBS = ("CLAUDE.md", "rules/**/*.md")


LINE_SPLIT_ALLOWED = {
    "holds_line_break": (
        "asks whether any character str.splitlines breaks at is in the text, which is the question, not a split of "
        "its lines"
    ),
}
OUTPUT_SPLIT_ALLOWED = {
    "holds_line_break": (
        "asks whether any character str.splitlines breaks at is in the text, which is the question, not a split of "
        "it into lines"
    ),
}


PRESENCE_ALLOWED = {
    "output_refusal": (
        "a failed stat reads as no file here, but audit_lines creates the report with create_bytes, which never "
        "replaces a file and raises for one it cannot create"
    ),
}


# The reads outside the trees the audit examines, each with why. The memory store is read through a reader bound to
# it, and the repository and the user's configuration directory, where the audit looks for instruction files and
# cited paths, through readers bound to each, so no link leading out of one is followed; these read the places a user
# or a setting names, the skill's own output, and the settings and git state that choose the store.
TREE_READ_ALLOWED = {
    "outside_present": (
        "whether anything is at a path a memory cites that names no place in the repository or the store, its `..` "
        "taken as text, and meets no link inside either on its way: the place it names, outside every tree the audit "
        "examines"
    ),
    "named_text": (
        "reads an instruction file the user names with --instructions, where it names, and one a pattern found in the "
        "user's configuration directory, its links followed, as Claude Code loads it: refusing a link guards against "
        "a repository the user did not write, and the configuration directory is the user's own"
    ),
    "named_present": (
        "whether an instruction file the user names with --instructions, or one a pattern found in the user's "
        "configuration directory, is a file, its links followed, for the reason named_text gives"
    ),
    "user_pattern_files": (
        "walks the user's configuration directory for the instruction files Claude Code loads from it, following a "
        "file link as Claude Code does: refusing a link guards against a repository the user did not write, and the "
        "configuration directory is the user's own"
    ),
    "destination_key": (
        "keys a destination file by its path with links followed, so a file named with --instructions and found by a "
        "pattern is measured once: a tree file only once its reader judged that no link on its way leads out"
    ),
    "main_worktree": (
        "compares and resolves the Git directories git names for the repository, to find the main worktree the memory "
        "directory's name derives from"
    ),
    "candidate_directories": (
        "lists the configuration directory's projects folder for the memory directories Claude Code keeps there: "
        "Claude Code's own state, outside every tree the audit examines"
    ),
    "directory_state": "whether anything is at the memory directory resolve chose, before it is taken as a store",
    "read_settings": "reads a Claude Code settings file, which chooses the memory directory",
    "project_folder": "resolves a folder in no Git repository, as Claude Code names its project",
    "output_refusal": "judges the --output path the user names for the skill's own report before it is written",
    "manifest_skill_files": "reads the deployer's manifest under the home, which names a skill's source checkout",
    "source_refusal": "judges a skill's source file the manifest or the user names, before the user edits it",
    "project_skill": "whether the repository's project skill file of the name given is there, for the user to edit",
    "create_output": "examines the --output folder and creates the skill's own report there",
    "create_temporary_report": "creates the skill's own report under the private temporary directory",
    "store_folder_reason": (
        "judges the memory directory a command is given, before a reader is bound to it, since it is the tree"
    ),
}


@dataclass
class CitedPath:
    """A path a memory cites: whether anything is there, or None with the reason when that cannot be told."""

    path: str
    exists: bool | None
    unreadable: str | None = None


@dataclass
class Overlap:
    file: str
    line: int
    score: float
    excerpt: str


@dataclass
class Memory:
    file: str
    name: str
    description: str
    type: str
    indexed: bool
    age_days: float
    links: list[str] = field(default_factory=list)
    broken_links: list[str] = field(default_factory=list)
    cited_paths: list[CitedPath] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)
    # How many overlaps past OVERLAP_LIMIT the report leaves out, so a cut list says it was cut.
    overlaps_omitted: int = 0


def holds_line_break(text: str) -> bool:
    """Whether `text` holds a character `str.splitlines` breaks a line at, so no one line can carry it whole."""
    return "".join(text.splitlines()) != text


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


INT_PARSE_ALLOWED = {
    "block_scalar": "the pattern takes one digit from 1 to 9",
}


def block_scalar(header: re.Match[str], key_indent: int, following: list[str]) -> tuple[str, int]:
    """The value of a `|` or `>` block scalar and how many following lines it spans.

    Its lines are those indented past the key, and blank lines between them. `>` folds a line break into a space
    and keeps a blank line as a line break, `|` keeps every line; the value is stripped, so chomping changes nothing.
    """
    count = 0
    while count < len(following) and (not following[count].strip() or indentation(following[count]) > key_indent):
        count += 1
    lines = following[:count]
    content = [line for line in lines if line.strip()]
    if not content:
        return "", count
    explicit = header.group("indent") or header.group("first")
    indent = key_indent + int(explicit) if explicit else min(indentation(line) for line in content)
    body = [line[indent:] if line.strip() else "" for line in lines]
    if header.group("style") == "|":
        return "\n".join(body).strip(), count
    paragraphs = " ".join(line.strip() if line.strip() else "\n" for line in body)
    return re.sub(r" ?\n ?", "\n", paragraphs).strip(), count


def is_delimiter(line: str) -> bool:
    """Whether a line is the `---` that opens or closes frontmatter, as YAML reads a document marker: `---` followed by
    nothing but its whitespace, a space or a tab. Any other character after it, such as U+2028, which YAML reads as
    text, leaves the line no delimiter."""
    return line.startswith("---") and not line[3:].strip(" \t")


# Kept for good instead of ejryan89-skill-core's frontmatter.py, by the decision recorded in
# https://github.com/EJRyan89/coding-agent-skills/issues/27: memories nest their type in a `metadata:` mapping, which
# the shared reader does not give, and the audit must read every memory however it is written.
def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the simple key: value frontmatter Claude Code writes, tolerating nesting and reading block scalars."""
    lines = text_lines(text)
    if not lines or not is_delimiter(lines[0]):
        return {}, text
    values: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if is_delimiter(line):
            return values, "\n".join(lines[index:])
        match = FRONTMATTER_KEY.match(line)
        if not match or not match.group("value"):
            continue
        value = match.group("value").strip()
        header = BLOCK_SCALAR.match(value)
        if header:
            # A block scalar's lines are never read as keys, and the closing --- is never indented past a key.
            following = list(itertools.takewhile(lambda rest: not is_delimiter(rest), lines[index:]))
            value, spanned = block_scalar(header, indentation(line), following)
            index += spanned
        else:
            value = value.strip("\"'")
        if value:
            values.setdefault(match.group("key"), value)
    return {}, text


def index_entries(lines: list[str]) -> dict[int, dict[str, str]]:
    """The index's entries, `- [Title](target) hook`, by the index of their line in `lines`, from 0.

    The target is read through ejryan89-skill-core's link_destinations, as CommonMark reads a link's destination:
    `file` is the destination as written, `<` and `>` included, and `target` the path it leads to, its escapes read and
    its percent-encoding decoded, without a fragment or query. A line in code is no entry, and neither is one whose
    destination a title follows, as in `- [Title](file.md "title")`, or that holds no destination CommonMark reads.
    """
    destinations = {(destination.line - 1, destination.start): destination for destination in link_destinations(lines)}
    entries: dict[int, dict[str, str]] = {}
    for index, line in enumerate(lines):
        match = INDEX_ENTRY.match(line)
        destination = destinations.get((index, _after_blanks(line, match.end()))) if match else None
        if match is None or destination is None:
            continue
        close = _after_blanks(line, destination.end)
        if not line.startswith(")", close):
            continue
        hook = INDEX_HOOK.match(line[close + 1 :])
        entries[index] = {
            "title": match.group("title"),
            "file": destination.written,
            "target": destination.path,
            "hook": hook.group("hook") if hook else "",
            "text": line,
        }
    return entries


def _after_blanks(line: str, position: int) -> int:
    """The position after the spaces and tabs at `position`, which CommonMark allows around a link's destination."""
    rest = line[position:]
    return position + len(rest) - len(rest.lstrip(" \t"))


def parse_index(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """The index's entries and lines, none when no index is there, read through a reader bound to the store holding
    it; a directory or anything else in its place raises NotAFile, an OSError, rather than reading as an empty index."""
    reader = TreeReader(path.parent)
    if not reader.present(path):
        return [], []
    lines = text_lines(memory_text(reader, path))
    entries = [{"line": str(index + 1), **entry} for index, entry in sorted(index_entries(lines).items())]
    return entries, lines


def target_identity(memory_dir: Path, file: str) -> str:
    """Compare targets relative to the store, without ./ prefixes and folding case where the store's file system does,
    as a reader bound to the store keys them (`TreeReader.key`): a target no link leads out of the store by its
    file system's key, any other by its text, so no link is followed to compare it."""
    return TreeReader(memory_dir).key(memory_dir / store_name(file))


def memory_text(reader: TreeReader, path: Path) -> str:
    """The text of a file `reader` reads, within its bound and from a regular file only, decoded as UTF-8 with each
    undecodable byte replaced and each line break read as a newline, as `Path.read_text` reads one."""
    text = reader.contents(path).decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def store_name(target: str) -> str:
    """An index entry's target as a name in the store: without any leading `./` or `.\\`, which POSIX reads as part of
    the name, so the file judged, compared, and read is the one the entry names on every platform."""
    return CURRENT_FOLDER.sub("", target)


# What entry_problem gives for a path that names nothing, such as an index entry whose file is gone.
MISSING = "no such file"
# What entry_problem's reason starts with for a path whose lstat fails: whether a memory is there cannot be told.
CANNOT_INSPECT = "cannot inspect: "


def entry_problem(path: Path) -> str | None:
    """Why the entry at path is no memory file to read, or None when it is a regular file.

    The entry is judged through a reader bound to the store holding it, from its own metadata first: a directory,
    symbolic link, or Windows junction is never followed, whatever it points at, so an entry named like a memory that
    is one of them is never read through, indexed, or deleted.
    """
    reader = TreeReader(path.parent)
    try:
        if reader.is_link(path):
            return "not a regular file"
        status = reader.status(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except OSError as exc:
        return f"{CANNOT_INSPECT}{reason(exc)}"
    if not stat.S_ISREG(status.st_mode):
        return "not a regular file"
    return None


# Why a name is refused before any file is touched: delete's names and an index entry's targets share these reasons.
NOT_IN_STORE = "not a file name directly inside the memory directory"
NOT_MARKDOWN = "not a .md file"
CURRENT_FOLDER = re.compile(r"^(?:\.[\\/])+")


def index_target_problem(memory_dir: Path, target: str) -> str | None:
    """Why an index entry's target is no memory file to read, or None when it names one directly in the store.

    The target is judged by its text before any file is touched: after any leading `./`, it must be a bare *.md name,
    so a `..` segment, an absolute path, or a path through a subfolder, symbolic link, or junction is never followed.
    """
    name = store_name(target)
    if not bare_name(name):
        return NOT_IN_STORE
    if not name.lower().endswith(".md"):
        return NOT_MARKDOWN
    return entry_problem(memory_dir / name)


def memory_entries(memory_dir: Path) -> tuple[list[Path], list[tuple[str, str]]]:
    """The store's memory files, and each other entry named like one with why it is none, both in name order.

    The index, however its name is spelled, is neither.
    """
    reader = TreeReader(memory_dir)
    index_identity = target_identity(memory_dir, INDEX_NAME)
    files: list[Path] = []
    others: list[tuple[str, str]] = []
    listing = reader.listing(memory_dir) if reader.folder(memory_dir) else []
    for path in (entry for entry in listing if fnmatch.fnmatch(entry.name, "*.md")):
        if target_identity(memory_dir, path.name) == index_identity:
            continue
        problem = entry_problem(path)
        if problem is None:
            files.append(path)
        elif problem != MISSING:
            others.append((path.name, problem))
    return files, others


def memory_files(memory_dir: Path) -> list[Path]:
    """The store's memory files: its regular *.md files other than the index."""
    return memory_entries(memory_dir)[0]


def words(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower()) if word not in STOP_WORDS}


def cited_paths(body: str, bases: list[Path]) -> list[CitedPath]:
    cited: list[CitedPath] = []
    seen: set[str] = set()
    for span in code_spans(body):
        candidate = span.text.strip()
        candidate = re.sub(r":\d+(?:-\d+)?$", "", candidate)
        if (
            candidate in seen
            or not PATH_LIKE.match(candidate)
            or not ANCHORED_PATH.search(candidate)
            or any(marker in candidate for marker in ("://", "<", "*", "{"))
        ):
            continue
        seen.add(candidate)
        expanded = (
            Path(candidate.replace("~", str(absolute_home()), 1)) if candidate.startswith("~") else Path(candidate)
        )
        exists, unreadable = cited_presence(cited_places(expanded, bases))
        cited.append(CitedPath(candidate, exists, unreadable))
    return cited


def cited_places(cited: Path, bases: list[Path]) -> list[tuple[TreeReader | None, Path]]:
    """The places a cited path may name, each with the reader that looks it up: a relative one below each of `bases`,
    the repository and the memory store, and an absolute or `~` one where it names, each through the reader of a base
    it names (`cited_place`), and looked up where it names only when it names none."""
    candidates = [base / cited for base in bases] if not cited.is_absolute() else [cited]
    return [cited_place(place, bases) for place in candidates]


def cited_place(place: Path, bases: list[Path]) -> tuple[TreeReader | None, Path]:
    """`place` with the reader of the base it names, the deepest when several do, so a link on its way that leads out
    of that base, or a `..` that climbs out after one, is refused rather than followed; None for one that names no base,
    which is looked up where it names. A place names a base when its form with every `..` taken as text lies in it,
    whatever base it was written from, when a link on its way, its own `..` taken after it, leads out of the base
    (`folder_walk.followed_inside`), or when the first link it meets, its `..` taken as text until then, lies inside
    the base (`link_on_way`), however its own `..` climbs out and back in; each name is judged from its own metadata
    and each link from its text, so nothing outside a base is looked at to decide."""
    owners = [
        base
        for base in bases
        if within(lexical_absolute(place), lexical_absolute(base))
        or followed_inside(place.absolute(), lexical_absolute(base))[1] is not None
        or link_on_way(place, base)
    ]
    if not owners:
        return None, place
    return TreeReader(max(owners, key=lambda base: len(lexical_absolute(base).parts))), place


def link_on_way(place: Path, base: Path) -> bool:
    """Whether a link inside `base` lies on the way to `place`: its names are taken in order, each `..` as text, as the
    system takes them until it meets a link, and the first name that is a link inside the base, judged from its own
    metadata through the base's reader and never followed, answers True. So a place whose own `..` climbs out of the
    base, before or after the link, is still the base's to judge, as the system opens it from where the link leads; one
    that meets no link inside the base, or a name that is not there, is not; one with a name whose metadata cannot be
    read is, so the base's reader refuses it, naming why."""
    reader = TreeReader(base)
    current = Path(place.absolute().anchor)
    for name in place.absolute().parts[1:]:
        if name == "..":
            current = current.parent
            continue
        current = current / name
        if name == "." or not within(current, reader.tree, strict=True):
            continue
        try:
            if reader.is_link(current):
                return True
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError:
            return True
    return False


def cited_presence(places: list[tuple[TreeReader | None, Path]]) -> tuple[bool | None, str | None]:
    """Whether a file or a folder is at one of the places a cited path may name, following links, each looked up
    through the reader bound to its tree, so a link leading out of it is never followed, or where it names when it has
    none; None with the reason when none is found and a stat failed or the reader refused it, so a path it cannot
    examine is never reported as not found."""
    reason: str | None = None
    for reader, place in places:
        try:
            if place_present(reader, place):
                return True, None
        except OSError as error:
            reason = reason or flat_text(examined(error))
    return (None, reason) if reason is not None else (False, None)


def place_present(reader: TreeReader | None, place: Path) -> bool:
    """Whether a file or a folder is at `place`, through `reader` when it has one."""
    if reader is not None:
        return reader.present(place, other_is_absent=True) or reader.folder(place, other_is_absent=True)
    return outside_present(place)


def outside_present(place: Path) -> bool:
    """Whether a file or a folder is at a cited place outside the repository and the memory store, following links."""
    return is_present(place, other_is_absent=True) or is_directory(place, other_is_absent=True)


def destination_blocks(
    files: list[Path], readers: dict[Path, TreeReader] | None = None
) -> list[tuple[str, int, str, set[str]]]:
    """Split destination documents into passages for overlap scoring (`passages`), each read through the reader
    `readers` gives it, bound to the repository, and an instruction file the user names or the user's configuration
    directory holds where it is, its links followed. A document that cannot be read raises its OSError, which fails the
    audit, rather than silently losing its overlaps."""
    blocks: list[tuple[str, int, str, set[str]]] = []
    for path in files:
        reader = (readers or {}).get(path)
        text = memory_text(reader, path) if reader is not None else named_text(path)
        for start, passage in passages(text_lines(text)):
            blocks.append((str(path), start, passage, words(passage)))
    return blocks


def named_text(path: Path) -> str:
    """The text of an instruction file the user names with --instructions, read where it names."""
    return path.read_text(encoding="utf-8", errors="replace")


def passages(lines: list[str]) -> list[tuple[int, str]]:
    """Each paragraph, list item, blockquote, and table row of Markdown, with the line it starts on, its lines joined,
    as markdown_sections' `blocks` walks them, as GitHub renders them; a list item keeps its marker. Headings and code,
    fenced or indented, are left out: a memory is compared with the prose that states a rule, and a code block's
    identifiers and flags would match a memory's words by coincidence, while the prose around the block still carries
    the overlap. The walker states its limits."""
    return [(block.start + 1, block.text()) for block in blocks(lines) if block.kind not in {CODE, HEADING}]


def overlaps_for(body: str, blocks: list[tuple[str, int, str, set[str]]]) -> tuple[list[Overlap], int]:
    """The memory's highest-scoring overlaps, at most OVERLAP_LIMIT, and how many more scored but were left out."""
    memory_words = words(body)
    if len(memory_words) < 4:
        return [], 0
    scored = []
    for file, line, text, block_words in blocks:
        if len(block_words) < 4:
            continue
        shared = memory_words & block_words
        # Set cosine: a very large block shares words with everything, so it must not dominate.
        score = len(shared) / (len(memory_words) * len(block_words)) ** 0.5
        if score >= OVERLAP_THRESHOLD and len(shared) >= 4:
            scored.append(Overlap(file, line, round(score, 2), text[:240]))
    scored.sort(key=lambda overlap: overlap.score, reverse=True)
    return scored[:OVERLAP_LIMIT], max(len(scored) - OVERLAP_LIMIT, 0)


def destination_files(repo: Path | None, instructions: list[Path], user_dir: Path | None = None) -> list[Path]:
    """The instruction files a memory's overlaps are measured against, as `destination_sources` finds them."""
    return list(destination_sources(repo, instructions, user_dir))


def destination_sources(
    repo: Path | None, instructions: list[Path], user_dir: Path | None = None
) -> dict[Path, TreeReader | None]:
    """The instruction files a memory's overlaps are measured against, each with the reader that reads it: each named
    one, read where it names; each a destination pattern finds (`pattern_files`) below the repository, through a reader
    bound to it, so one that a link on its way leads out of is passed over unread, as a linked folder always was; and
    each a pattern finds below the user's configuration directory (`user_pattern_files`), read where it is, its links
    followed, as Claude Code loads a `CLAUDE.md` linked from a dotfiles checkout: refusing a link guards against a
    repository the user did not write, and the configuration directory is the user's own. A folder among them is
    passed over, and a stat that fails raises, so an instruction file the audit cannot look at fails it rather than
    silently losing its overlaps."""
    files: list[tuple[Path, TreeReader | None]] = [(path, None) for path in instructions if named_present(path)]
    if repo is not None:
        reader = TreeReader(repo)
        for pattern in DESTINATION_GLOBS:
            files.extend((path, reader) for path in pattern_files(repo, pattern) if tree_present(reader, path))
    if user_dir is not None:
        for pattern in USER_DESTINATION_GLOBS:
            files.extend((path, None) for path in user_pattern_files(user_dir, pattern) if named_present(path))
    unique: dict[str, tuple[Path, TreeReader | None]] = {}
    for path, source in files:
        unique.setdefault(destination_key(path), (path, source))
    return dict(unique.values())


def named_present(path: Path) -> bool:
    """Whether a regular file is at an instruction file the user names with --instructions, or one a pattern found in
    the user's configuration directory, following links."""
    return is_present(path, other_is_absent=True)


def destination_key(path: Path) -> str:
    """What two destination files are compared by: the path with its links followed, case-folded where its file
    system ignores case, so a file named with --instructions and found by a pattern is measured once."""
    return path_key(path, resolve=True)


def tree_present(reader: TreeReader, path: Path) -> bool:
    """Whether a regular file is at `path` in `reader`'s tree, following links inside it; False for one that leads out
    of the tree (`TreeReader.leads_out`), which is passed over unread. Any other refusal, such as a name whose metadata
    cannot be read or a loop of links, raises the reader's `TreeRefused`, so the audit fails rather than losing the
    file's overlaps."""
    if reader.leads_out(path):
        return False
    return reader.present(path, other_is_absent=True)


def pattern_files(root: Path, pattern: str) -> list[Path]:
    """The paths below `root` a destination pattern names, in path order.

    A pattern without `**/` names one file, which `destination_sources` looks up through a reader bound to `root`.
    `<folder>/**/<name>` names each file below the folder that a reader bound to `root` finds with ejryan89-skill-core's
    folder walk, which skips its one stated set of folders and every nested checkout, and enters no link, nor follows
    one leading out of `root` even to learn what it is: a name with a wildcard as fnmatch matches it, and a fixed name
    as the folder holding the file gives the entry for it (`path_keys.opens_as`), as a runtime opens it, so a
    `claude.md` is the folder's CLAUDE.md where it folds case. A folder that leads out of `root`
    (`TreeReader.leads_out`) names none, and one the reader refuses otherwise raises its `TreeRefused`. A folder the
    walk cannot list, or an entry it cannot examine, raises the error it met."""
    folder, below, name = pattern.rpartition("**/")
    if not below:
        return [root / pattern]
    reader = TreeReader(root)
    base = root / folder if folder else root
    if reader.leads_out(base):
        return []
    if not reader.folder(base):
        return []
    if "*" in name:
        walk = reader.search(base, lambda found: fnmatch.fnmatch(found, name))
        files = walk.files
    else:
        walk = reader.search(base, lambda found: found.casefold() == name.casefold())
        files = [path for path in walk.files if reader.opens_as(path, name)]
    for entry in walk.not_entered:
        if entry.error is not None:
            raise entry.error
    return sorted(files, key=os.fspath)


def user_pattern_files(user_dir: Path, pattern: str) -> list[Path]:
    """The paths below the user's configuration directory a destination pattern names, in path order, as Claude Code
    loads them, a link followed: a pattern without `**/` names one file, looked up where it is, and `<folder>/**/<name>`
    names each file below the folder that ejryan89-skill-core's folder walk finds, which enters no folder link, a
    fixed name as the folder holding the file gives the entry for it (`path_keys.opens_as`). A folder the walk cannot
    list, or an entry it cannot examine, raises the error it met."""
    folder, below, name = pattern.rpartition("**/")
    if not below:
        return [user_dir / pattern]
    base = user_dir / folder if folder else user_dir
    if not is_directory(base):
        return []
    if "*" in name:
        walk = folder_walk.walk_files(base, lambda found: fnmatch.fnmatch(found, name))
        files = walk.files
    else:
        walk = folder_walk.walk_files(base, lambda found: found.casefold() == name.casefold())
        files = [path for path in walk.files if opens_as(path, name)]
    for entry in walk.not_entered:
        if entry.error is not None:
            raise entry.error
    return sorted(files, key=os.fspath)


def audit(
    memory_dir: Path,
    repo: Path | None = None,
    instructions: list[Path] | None = None,
    now: float | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    store = TreeReader(memory_dir)
    index_path = memory_dir / INDEX_NAME
    entries, index_lines = parse_index(index_path)
    index_exists = store.present(index_path)
    index_bytes = store.status(index_path).st_size if index_exists else 0
    indexed_files = Counter(target_identity(memory_dir, entry["target"]) for entry in entries)
    candidates, others = memory_entries(memory_dir)
    unreadable = {target_identity(memory_dir, name): (name, problem) for name, problem in others}
    for entry in entries:
        problem = index_target_problem(memory_dir, entry["target"])
        if problem is not None and problem != MISSING:
            unreadable.setdefault(target_identity(memory_dir, entry["target"]), (entry["file"], problem))
    parsed: dict[Path, tuple[dict[str, str], str]] = {}
    for path in candidates:
        try:
            parsed[path] = parse_frontmatter(memory_text(store, path))
        except OSError as exc:
            unreadable[target_identity(memory_dir, path.name)] = (path.name, reason(exc))
    files = list(parsed)
    known_names = {path.stem for path in files} | {meta.get("name", "") for meta, _ in parsed.values()}
    sources = destination_sources(repo, instructions or [], user_dir)
    blocks = destination_blocks(list(sources), {path: reader for path, reader in sources.items() if reader is not None})
    bases = [base for base in (repo, memory_dir) if base is not None]
    memories: list[Memory] = []
    for path in files:
        meta, body = parsed[path]
        links = sorted(set(WIKI_LINK.findall(body)))
        overlaps, omitted = overlaps_for(body, blocks)
        memories.append(
            Memory(
                file=path.name,
                name=meta.get("name", ""),
                description=meta.get("description", ""),
                type=meta.get("type", ""),
                indexed=target_identity(memory_dir, path.name) in indexed_files,
                age_days=round((now - store.status(path).st_mtime) / 86400, 1),
                links=links,
                broken_links=[link for link in links if link not in known_names],
                cited_paths=cited_paths(body, bases),
                overlaps=overlaps,
                overlaps_omitted=omitted,
            )
        )
    return {
        "memory_dir": str(memory_dir),
        "index": {
            "path": str(index_path),
            "exists": index_exists,
            "entries": len(entries),
            "lines": len(index_lines),
            "line_limit": INDEX_LINE_LIMIT,
            "bytes": index_bytes,
            "byte_limit": INDEX_BYTE_LIMIT,
            "over_limit": len(index_lines) > INDEX_LINE_LIMIT or index_bytes > INDEX_BYTE_LIMIT,
            "near_limit": len(index_lines) > INDEX_LINE_LIMIT * NEAR_LIMIT_FRACTION
            or index_bytes > INDEX_BYTE_LIMIT * NEAR_LIMIT_FRACTION,
            "missing_files": [
                entry["file"] for entry in entries if index_target_problem(memory_dir, entry["target"]) == MISSING
            ],
            "duplicate_entries": sorted(
                {entry["file"] for entry in entries if indexed_files[target_identity(memory_dir, entry["target"])] > 1}
            ),
        },
        "unindexed_files": [memory.file for memory in memories if not memory.indexed],
        "unreadable": [{"file": file, "reason": problem} for file, problem in sorted(unreadable.values())],
        "memories": [asdict(memory) for memory in memories],
    }


def one_line(text: str, limit: int = HOOK_LIMIT) -> str:
    text = flat_text(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:.") + "..."


def index_line(title: str, file: str, hook: str, *, original_target: str | None = None) -> str:
    """An index entry, written so that index_entries reads it back: a new entry's file through link_destinations'
    written_destination, and the title and the hook as given, unless a backtick in them pairs with another into a code
    span over the destination, which a backslash cannot escape. Only then is each of the title's backticks written as
    `'`, and the hook's too when a backtick in a kept destination still pairs with one in the hook."""
    target = original_target if original_target is not None else written_destination(file)
    plain_title, plain_hook = title.replace("`", "'"), hook.replace("`", "'")
    for written_title, written_hook in ((title, hook), (plain_title, hook)):
        line = _entry_line(written_title, target, written_hook)
        read = index_entries([line]).get(0)
        if read is not None and read["file"] == target:
            return line
    return _entry_line(plain_title, target, plain_hook)


def _entry_line(title: str, target: str, hook: str) -> str:
    return f"- [{title}]({target})" + (f" — {hook}" if hook else "")


def derived_hook(body: str) -> str:
    """A memory's hook from its body: the first line, or its heading's text when it is an ATX heading, which
    markdown_sections reads, so `#hashtag` keeps its `#`."""
    first = next((line.strip() for line in text_lines(body) if line.strip()), "")
    heading = ATX_HEADING.fullmatch(first)
    return one_line(heading.group("text") if heading else "" if EMPTY_ATX_HEADING.fullmatch(first) else first)


def index_text(path: Path) -> str:
    """The index's text as written, its line breaks kept, read through a reader bound to the store holding it, or
    empty when no index is there."""
    reader = TreeReader(path.parent)
    return reader.contents(path).decode("utf-8", errors="replace") if reader.present(path) else ""


def reindex(memory_dir: Path) -> dict[str, Any]:
    """Rebuild MEMORY.md in memory without writing it; see the module docstring for the rules."""
    index_path = memory_dir / INDEX_NAME
    current = index_text(index_path)

    def frontmatter(file: str) -> tuple[dict[str, str], str]:
        return parse_frontmatter(memory_text(TreeReader(memory_dir), memory_dir / file))

    def derived_title(meta: dict[str, str], file: str) -> str:
        return flat_text((meta.get("name") or Path(file).stem).replace("[", "(").replace("]", ")"))

    lines: list[str] = []
    entries: list[str] = []
    seen: set[str] = set()
    dropped: list[tuple[str, str]] = []
    unreadable: list[tuple[str, str]] = []
    current_lines = text_lines(current)
    found = index_entries(current_lines)
    for index, line in enumerate(current_lines):
        entry = found.get(index)
        if entry is None:
            lines.append(line)
            continue
        file = entry["target"]
        problem = index_target_problem(memory_dir, file)
        if problem == MISSING:
            dropped.append(("missing", entry["file"]))
            continue
        identity = target_identity(memory_dir, file)
        if identity in seen:
            dropped.append(("duplicate", entry["file"]))
            continue
        seen.add(identity)
        if problem is not None:
            unreadable.append((entry["file"], problem))
            lines.append(line)
            entries.append(entry["file"])
            continue
        name = store_name(file)
        meta, body = frontmatter(name)
        hook = one_line(meta["description"]) if meta.get("description") else entry["hook"] or derived_hook(body)
        lines.append(
            index_line(entry["title"].strip() or derived_title(meta, name), name, hook, original_target=entry["file"])
        )
        entries.append(entry["file"])
    files, others = memory_entries(memory_dir)
    added = sorted(path.name for path in files if target_identity(memory_dir, path.name) not in seen)
    unreadable += [(name, problem) for name, problem in others if target_identity(memory_dir, name) not in seen]
    for name, problem in unreadable:
        # An entry whose stat fails may be a memory it cannot read, which fails the rebuild, never a non-memory to
        # keep as written.
        if problem.startswith(CANNOT_INSPECT):
            raise OSError(f"a memory cannot be read: {name}: {problem.removeprefix(CANNOT_INSPECT)}")
    if added:
        while lines and not lines[-1].strip():
            lines.pop()
    for file in added:
        meta, body = frontmatter(file)
        hook = one_line(meta["description"]) if meta.get("description") else derived_hook(body)
        lines.append(index_line(derived_title(meta, file), file, hook))
        entries.append(file)
    content = "\n".join(lines) + "\n" if lines else ""
    size = len(content.encode("utf-8"))
    return {
        "path": index_path,
        "content": content,
        "entries": entries,
        "added": added,
        "dropped": dropped,
        "unreadable": unreadable,
        "changed": content != current,
        "over_limit": len(lines) > INDEX_LINE_LIMIT or size > INDEX_BYTE_LIMIT,
        "near_limit": len(lines) > INDEX_LINE_LIMIT * NEAR_LIMIT_FRACTION
        or size > INDEX_BYTE_LIMIT * NEAR_LIMIT_FRACTION,
        "lines": len(lines),
        "bytes": size,
    }


def write_index(path: Path, content: str) -> None:
    """Replace MEMORY.md atomically, keeping its permissions, through ejryan89-skill-core's `atomic_file`, which stages
    the new index in a file it creates under a fresh name; nothing else in the directory is touched or left behind,
    even when the write fails."""
    try:
        mode = stat.S_IMODE(TreeReader(path.parent).status(path).st_mode)
    except FileNotFoundError:
        mode = TRACKED_FILE_MODE
    replace_text(path, content, mode=mode)


def bare_name(name: str) -> bool:
    """Whether name names one entry directly inside a folder: no separator, drive, `.`, or `..`."""
    return bool(name) and "/" not in name and "\\" not in name and not Path(name).anchor and name not in (".", "..")


def refusal(memory_dir: Path, name: str) -> str | None:
    """Why name must not be deleted, or None when it is a memory file directly inside memory_dir."""
    if not bare_name(name):
        return NOT_IN_STORE
    if holds_line_break(name):
        return "the name holds a line break, so no line can name it; rename the file first"
    if not name.lower().endswith(".md"):
        return NOT_MARKDOWN
    if name.casefold() == INDEX_NAME.casefold():
        return "the index is rebuilt with reindex, never deleted"
    problem = entry_problem(memory_dir / name)
    # Windows will not delete a read-only file, so one is refused with the rest, before any deletion, everywhere.
    if problem is None and TreeReader(memory_dir).read_only(memory_dir / name):
        return "the file is read-only; make it writable to delete it"
    return problem


def delete(memory_dir: Path, names: list[str]) -> tuple[int, list[str]]:
    """Delete the named memory files, or nothing when any name is refused; see the module docstring."""
    output: list[str] = []
    seen: set[str] = set()
    for name in names:
        refused = "named more than once" if name.casefold() in seen else refusal(memory_dir, name)
        seen.add(name.casefold())
        if refused:
            output.append(f"FAILED {flat_text(name)}: {refused}")
    if output:
        return 1, output
    for name in names:
        try:
            (memory_dir / name).unlink()
        except OSError as exc:
            output.append(f"FAILED {flat_text(name)}: {reason(exc)}")
            return 1, output
        output.append(f"DELETED {flat_text(name)}")
    return 0, output


def managed_settings_path() -> Path | None:
    """Claude Code's managed settings file, or None on Windows when the Program Files folder is not named by an
    absolute path, so no settings are read relative to the working directory."""
    if sys.platform == "win32":
        program_files = environment_path("PROGRAMFILES")
        return None if program_files is None else program_files / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def encode_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


class UnknownMainWorktree(Exception):
    """A linked worktree whose shared Git directory names no working tree, so its main worktree cannot be found."""


def main_worktree(repo: Path) -> Path | None:
    """The top level of the main worktree of the repository holding repo, which every worktree shares, or None when
    repo lies in no repository.

    In the main worktree, its Git directory is the shared one, and the answer is its own top level, whether that Git
    directory is its `.git`, a submodule's under the superproject, or one made with --separate-git-dir. A linked
    worktree's shared Git directory belongs to the main worktree: the folder holding it when it is named `.git`, else
    the working tree its configuration names, as a submodule's does. One that names none, as a separate Git
    directory does, raises UnknownMainWorktree. Raises GitError when git cannot run, does not finish, or fails.
    """
    paths = GitClient().rev_parse_paths(["--show-toplevel", "--absolute-git-dir", "--git-common-dir"], directory=repo)
    if paths is None:
        return None
    top, git_dir, common = paths
    if git_dir == common:
        return top
    if name_key(common.name, common.parent) == ".git":
        return common.parent
    # core.worktree, relative to the Git directory when relative, is git's own record of a submodule's working tree.
    # Exit 1 with nothing on stderr is git's answer that the setting is unset; any other failure raises.
    configured = GitClient().quiet_output(["config", "--file", str(common / "config"), "--get", "core.worktree"])
    if configured is None:
        raise UnknownMainWorktree(
            f"{top} is a linked worktree whose shared Git directory {common} names no main worktree; run resolve "
            "with --repo naming the main worktree, or pass --memory-dir."
        )
    value = configured.rstrip("\r\n")
    if not value:
        raise GitError(f"git config gave core.worktree in {common / 'config'} an empty value")
    return resolved_path(common / value)


def repository_root(directory: Path) -> Path | None:
    """The top level of the Git worktree holding directory, or None when it lies in no repository.

    Raises GitError when git cannot run, does not finish, or fails for another reason.
    """
    return GitClient().rev_parse_path("--show-toplevel", directory=directory)


def config_directory(home: Path, environment: dict[str, str]) -> Path:
    """The user's Claude Code configuration directory: CLAUDE_CONFIG_DIR, else the home's .claude folder. Raises
    RelativeEnvironmentPath for a CLAUDE_CONFIG_DIR that is not an absolute path, which only the directory Claude Code
    started in could complete, so no directory is read relative to this script's working directory."""
    return required_environment_path("CLAUDE_CONFIG_DIR", environment) or home / ".claude"


def resolve(repo: Path, home: Path, environment: dict[str, str], managed: Path | None) -> dict[str, Any]:
    """Locate the auto-memory directory for repo, with the memory directories found when none exists there."""
    config_dir = config_directory(home, environment)
    result = locate(repo, home, environment, managed, config_dir)
    if result["memory_dir"] is not None and holds_line_break(result["memory_dir"]):
        result = unresolved(
            [
                *result["notes"],
                f"The memory directory from {result['source']} holds a line break: {result['memory_dir']!r}",
            ]
        )
    if result["exists"] and not result.get("refused"):
        refused = store_refusal(Path(result["memory_dir"]))
        if refused:
            result["refused"] = refused
    if not result["exists"] or result.get("refused"):
        found, unreadable = candidate_directories(config_dir / "projects")
        refused_dir = result["memory_dir"] if result.get("refused") else None
        result["candidates"] = [path for path in found if not holds_line_break(path) and path != refused_dir]
        result["notes"] += [
            f"Left out a memory directory whose path holds a line break: {path!r}"
            for path in found
            if holds_line_break(path)
        ]
        result["notes"] += unreadable
    return result


def candidate_directories(projects: Path) -> tuple[list[str], list[str]]:
    """The memory directory of each project under `projects`, in order, and a note for each one, or for `projects`
    itself, that cannot be looked at, which is left out rather than taken for missing."""
    try:
        entries = list(projects.iterdir()) if is_directory(projects, other_is_absent=True) else []
    except OSError as exc:
        return [], [f"Left out the memory directories under {str(projects)!r}, which cannot be read: {reason(exc)}"]
    found: list[str] = []
    unreadable: list[str] = []
    for memory in sorted(str(entry / "memory") for entry in entries):
        try:
            if is_directory(Path(memory), other_is_absent=True):
                found.append(memory)
        except OSError as exc:
            unreadable.append(f"Left out a memory directory that cannot be looked at: {memory!r}: {reason(exc)}")
    return found, unreadable


def directory_state(directory: Path) -> dict[str, Any]:
    """Whether anything is at the memory directory's path, as `exists`: False only when nothing is, a link to nothing
    included. A file or anything else in its place, or a path a stat cannot examine, is never reported as missing: it
    counts as there, and `refused` names what is there, which the later steps refuse."""
    found = refusal_reason(directory, folder=True)
    if found == file_presence.MISSING:
        return {"exists": False}
    if found is None:
        return {"exists": True}
    return {"exists": True, "refused": f"{directory} {flat_text(found)}"}


class UnreadableSettings(Exception):
    """A settings file that exists but holds no readable JSON."""


def unresolved(notes: list[str]) -> dict[str, Any]:
    return {"memory_dir": None, "source": None, "exists": False, "notes": notes}


def read_settings(path: Path) -> dict[str, Any]:
    """A settings file's top-level object, empty when the file is absent or holds another value. One that holds no
    readable JSON raises UnreadableSettings, which leaves the directory unresolved; one that cannot be read, or a
    directory or anything else in the file's place, raises OSError naming it, a FAILED line, since the directory it
    may choose cannot then be known and must never be read as unset."""
    try:
        if not is_present(path):
            return {}
        data = path.read_bytes()
    except OSError as exc:
        raise OSError(exc.errno, f"cannot read the settings file {path}: {reason(exc)}") from exc
    try:
        settings = json_reader.loads(data.decode("utf-8-sig"))
    except ValueError as exc:
        raise UnreadableSettings(f"Cannot read {path}: {exc}") from exc
    return settings if isinstance(settings, dict) else {}


def repository_supplied(repo: Path, scope: str) -> bool:
    """Whether Claude Code counts a scope's file as supplied by the repository rather than by the user.

    The project's settings.json always is; its settings.local.json is when git tracks it or `.claude` is a symbolic
    link (https://code.claude.com/docs/en/permissions, "When your local settings file needs trust"), which a junction
    is too as Node reads one on Windows. When git, or the folder's own metadata, cannot tell, the file counts as the
    repository's, the answer that never uses a directory Claude Code may ignore.
    """
    if scope != "local":
        return scope == "project"
    try:
        if TreeReader(repo).is_link(repo / ".claude"):
            return True
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return True
    # ls-files lists a tracked file and prints nothing for one git does not track, so only a failure cannot tell.
    try:
        listed = GitClient().output(["ls-files", "-z", "--", ".claude/settings.local.json"], directory=repo)
    except GitError as exc:
        return exc.kind != "not_repository"
    return bool(listed)


def blocking_scope(scopes: tuple[tuple[str, Path], ...]) -> str | None:
    """The first scope whose settings turn permissions.blockReadsOutsideWorkingDirectories on, or None.

    Claude Code honors `true` from any scope, even over a managed `false` (https://code.claude.com/docs/en/settings,
    "Exceptions to managed settings precedence"). A `permissions` that is not an object raises UnreadableSettings:
    whether reads are blocked cannot be known from it.
    """
    for scope, path in scopes:
        permissions = read_settings(path).get("permissions", {})
        if not isinstance(permissions, dict):
            raise UnreadableSettings(f"Cannot read {path}: its permissions is not an object")
        if permissions.get(BLOCK_READS) is True:
            return scope
    return None


def locate(
    repo: Path, home: Path, environment: dict[str, str], managed: Path | None, config_dir: Path
) -> dict[str, Any]:
    """The auto-memory directory Claude Code uses for repo, following its documented order, with no managed scope
    when `managed` is None."""
    notes = [SETTINGS_NOTE]
    if managed is None:
        notes.append(MANAGED_UNKNOWN_NOTE)
    # Claude Code reads autoMemoryDirectory from every settings scope, in their precedence order, and honors a
    # project's settings.json or settings.local.json under workspace trust, but not while reads outside the working
    # directories are blocked (https://code.claude.com/docs/en/memory, "Storage location"); the --settings scope
    # between managed and local is in the note above.
    scopes = (
        *([] if managed is None else [("managed", managed)]),
        ("local", repo / ".claude" / "settings.local.json"),
        ("project", repo / ".claude" / "settings.json"),
        ("user", config_dir / "settings.json"),
    )
    try:
        for scope, path in scopes:
            value = read_settings(path).get("autoMemoryDirectory")
            if value is not None:
                return chosen_directory(repo, home, scopes, (scope, path, value), notes)
    except UnreadableSettings as exc:
        return unresolved([*notes, str(exc)])
    projects = config_dir / "projects"
    name = environment.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if name:
        directory = projects / name / "memory"
        source = "CLAUDE_CODE_PROJECT_DIR_NAME"
    else:
        try:
            root = main_worktree(repo)
        except UnknownMainWorktree as exc:
            return unresolved([*notes, str(exc)])
        if root is None:
            # Claude Code uses the project's own folder for a project in no Git repository.
            root = project_folder(repo)
            source = f"derived from the folder ({root}), which is in no Git repository"
        else:
            source = f"derived from the repository's main worktree ({root})"
        directory = projects / encode_project(str(root)) / "memory"
        notes.append("The directory name is derived by convention; confirm it before changing anything.")
    return {"memory_dir": str(directory), "source": source, **directory_state(directory), "notes": notes}


def project_folder(folder: Path) -> Path:
    """A folder in no Git repository, a project of its own, made absolute with its links followed, as Claude Code names
    its project."""
    return resolved_path(folder)


def chosen_directory(
    repo: Path, home: Path, scopes: tuple[tuple[str, Path], ...], chosen: tuple[str, Path, object], notes: list[str]
) -> dict[str, Any]:
    """The directory autoMemoryDirectory names in the first scope that sets it, unless Claude Code would not use it.

    Raises UnreadableSettings when a repository's choice needs every scope read for the block on outside reads.
    """
    scope, path, value = chosen
    if not isinstance(value, str) or not (Path(value).is_absolute() or value.startswith("~/")):
        return unresolved([*notes, f"autoMemoryDirectory in {path} is not an absolute or ~/ path: {value!r}"])
    if holds_line_break(value):
        return unresolved([*notes, f"autoMemoryDirectory in {path} holds a line break: {value!r}"])
    directory = home / value[2:] if value.startswith("~/") else Path(value)
    supplied = repository_supplied(repo, scope)
    blocked_by = blocking_scope(scopes) if supplied else None
    if blocked_by:
        return unresolved(
            [
                *notes,
                f"permissions.{BLOCK_READS} is on in {blocked_by} settings, so Claude Code loads no auto memory from "
                f"{str(directory)!r}, which autoMemoryDirectory in {scope} settings ({path}) chooses, and saves none "
                "to it.",
            ]
        )
    if scope in ("local", "project"):
        notes.append("Claude Code honors a repository-level autoMemoryDirectory only after the folder is trusted.")
    if supplied:
        notes.append(f"Claude Code ignores it while permissions.{BLOCK_READS} is on, which a --settings file can set.")
    return {
        "memory_dir": str(directory),
        "source": f"autoMemoryDirectory in {scope} settings ({path})",
        **directory_state(directory),
        "notes": notes,
    }


def examined(exc: OSError) -> str:
    """Why an OSError was raised: a tree reader's refusal, or a read it did not make, by its reason alone, as the
    operating system's reason is given; any other by that reason, or its message when it has none."""
    if isinstance(exc, TreeRefused | FileRefused):
        return exc.reason
    return exc.strerror or str(exc)


def reason(exc: OSError) -> str:
    """An expected OSError's reason on one line."""
    return flat_text(examined(exc))


def describe(exc: OSError) -> str:
    """An expected OSError as one line: its reason and the file it names."""
    if isinstance(exc, TreeRefused | FileRefused):
        return f"{reason(exc)}: {flat_text(str(exc.path))}"
    return f"{reason(exc)}: {flat_text(str(exc.filename))}" if exc.strerror and exc.filename else reason(exc)


def output_refusal(explicit: Path, memory_dir: Path, projects: Path) -> str | None:
    """Why the audit report must not be written to explicit, or None when it may be.

    audit is read-only, so it never replaces a file, a memory, MEMORY.md, or CLAUDE.md among them, and never leaves
    its report in a memory store, where it would pass for a memory. A file left inside a skill directory makes the
    deployer see that skill as modified and stop updating it, so an explicit path inside any skills directory is
    refused too.
    """
    root = skills_directory_holding(explicit)
    if root:
        return f"{explicit} is inside the skills directory {root}; omit --output to write a new temporary file"
    # A failed stat reads as no file here, but audit_lines creates the report with create_bytes, which never replaces
    # a file.
    if os.path.lexists(explicit):
        return (
            f"{explicit} already exists, and audit never replaces a file; omit --output to write a new temporary file"
        )
    target = resolved_path(explicit)
    for kind, store in (("the memory directory", memory_dir), ("Claude Code's projects folder", projects)):
        if within(target, resolved_path(store)):
            return f"{explicit} is inside {kind} {store}; omit --output to write a new temporary file"
    return None


def store_refusal(memory_dir: Path) -> str | None:
    """Why audit, reindex, and delete must not take memory_dir, or None when it is a memory store.

    A deletion is permanent, so they take only a directory holding the MEMORY.md index Claude Code keeps beside its
    memories, as a file: a directory or anything else in its place is an index that cannot be read, never an empty
    one. They never take one inside a skills directory, where a SKILL.md would pass for a memory. audit takes the same,
    so the skill learns that the later commands refuse a directory before it changes anything.
    """
    root = skills_directory_holding(memory_dir)
    if root:
        return f"{memory_dir} is inside the skills directory {root}; name the memory directory resolve printed"
    try:
        present = TreeReader(memory_dir).present(memory_dir / INDEX_NAME)
    except OSError as exc:
        return f"{memory_dir} is not a memory directory: its {INDEX_NAME} cannot be read: {reason(exc)}"
    if not present:
        return f"{memory_dir} is not a memory directory: it holds no {INDEX_NAME}"
    return None


def resolve_lines(repo: Path, result: dict[str, Any]) -> list[str]:
    """One fact per line: the repository, the directory and where it came from, candidates, then notes.

    The repository, the directory, and each candidate are paths a later command takes, so `resolve` refuses one that
    holds a line break and they print as they are; the source and the notes are flattened.
    """
    lines = [f"REPO {repo}"]
    if result["memory_dir"]:
        lines += [
            f"MEMORY_DIR {result['memory_dir']}",
            f"SOURCE {flat_text(result['source'])}",
            f"EXISTS {'yes' if result['exists'] else 'no'}",
        ]
        lines += [f"REFUSED {result['refused']}"] if result.get("refused") else []
    else:
        lines.append("NO_MEMORY_DIR")
    lines += [f"CANDIDATE {candidate}" for candidate in result.get("candidates", [])]
    lines += [f"NOTE {flat_text(note)}" for note in result["notes"]]
    return lines


def reindex_lines(memory_dir: Path, write: bool) -> tuple[int, list[str]]:
    """The rebuilt entries, what changed, and what was written; exit 1 when the index is over its load limit."""
    unlinkable = [path.name for path in memory_files(memory_dir) if holds_line_break(path.name)]
    if unlinkable:
        names = ", ".join(repr(name) for name in unlinkable)
        return 1, [f"FAILED {flat_text(f'a memory file name holds a line break, so no entry can link it: {names}')}"]
    result = reindex(memory_dir)
    output = [f"INDEX_LINE {flat_text(file)}" for file in result["entries"]]
    output += [f"ADDED {flat_text(file)}" for file in result["added"]]
    output += [f"DROPPED {why} {flat_text(file)}" for why, file in result["dropped"]]
    output += [f"UNREADABLE {flat_text(file)}: {problem}" for file, problem in result["unreadable"]]
    if result["over_limit"] or result["near_limit"]:
        label = "OVER_LIMIT" if result["over_limit"] else "NEAR_LIMIT"
        output.append(f"{label} lines={result['lines']}/{INDEX_LINE_LIMIT} bytes={result['bytes']}/{INDEX_BYTE_LIMIT}")
    code = 1 if result["over_limit"] else 0
    if not result["changed"]:
        output.append("UNCHANGED")
    elif write:
        try:
            write_index(result["path"], result["content"])
        except OSError as exc:
            output.append(f"FAILED cannot write {result['path'].as_posix()}: {reason(exc)}")
            return 1, output
        output.append(f"WROTE {result['path'].as_posix()}")
    else:
        output.append(f"WOULD_WRITE {result['path'].as_posix()}")
    return code, output


def manifest_skill_files(home: Path, name: str) -> list[Path]:
    """The skills/<name>/SKILL.md of each source checkout the deployer's manifest records as deploying `name`.

    Raises OSError or ValueError when the manifest exists but cannot be read as JSON, as when a directory stands in its
    place, or holds a source entry of another shape than the deployer accepts, whose skills cannot then be known. An
    entry the deployer accepts may lack its skills, which then are none, and its source_dir, which only matters for an
    entry that deploys `name`: that one is refused, since the checkout holding the skill is then unknown.
    """
    manifest = home / ".claude" / "skills" / MANIFEST_NAME
    if not is_present(manifest):
        return []
    data = json_reader.read_file(manifest)
    if not isinstance(data, dict):
        raise ValueError("it is not a JSON object")
    sources = data.get("sources", {})
    if not isinstance(sources, dict):
        raise ValueError("its sources is not an object")
    found: list[Path] = []
    for source, entry in sources.items():
        skills = entry.get("skills", {}) if isinstance(entry, dict) else None
        if not isinstance(skills, dict):
            raise ValueError(f"its source {source!r} is not an object whose skills is an object")
        if name not in skills:
            continue
        source_dir = entry.get("source_dir")
        if not isinstance(source_dir, str):
            raise ValueError(f"its source {source!r} deploys {name} but records no source_dir")
        found.append(Path(source_dir) / "skills" / name / "SKILL.md")
    return found


def source_refusal(path: Path) -> str | None:
    """Why a skill's file at `path` must not be edited as its source, or None when it may be."""
    if holds_line_break(str(path)):
        return f"{path!r} holds a line break"
    root = skills_directory_holding(path)
    if root:
        return f"{path} is inside the skills directory {root}; the deployer stops updating a skill changed there"
    reason = refusal_reason(path)
    return None if reason is None else f"{path} {reason}"


def project_skill(repo: Path, name: str) -> Path | None:
    """The repository's .claude/skills/<name>/SKILL.md when a file is there, following links, a source the user edits
    and the next step judges with `source_refusal`."""
    project = repo / ".claude" / "skills" / name / "SKILL.md"
    return project if is_present(project) else None


def skill_source_lines(args: argparse.Namespace) -> tuple[int, list[str]]:
    """Each editable source file of a skill, or the file named with --file, never one inside a skills directory."""
    unread = ""
    if args.file is not None:
        candidates = [args.file]
        missing = f"{args.file} cannot be edited as a skill's source"
    elif not bare_name(args.name) or holds_line_break(args.name):
        return 1, [f"FAILED {flat_text(repr(args.name))} is not a skill name"]
    else:
        try:
            candidates = manifest_skill_files(args.home, args.name)
        except (OSError, UnicodeError, ValueError) as exc:
            return 1, [f"FAILED {flat_text(f'cannot read the deployer manifest: {exc}')}"]
        try:
            repo = args.repo or repository_root(Path.cwd())
        except GitError as exc:
            repo = None
            unread = f"NOTE {flat_text(f'no project skill was looked for: git could not read the repository: {exc}')}"
        project = None if repo is None else project_skill(repo, args.name)
        candidates += [project] if project is not None else []
        missing = (
            f"no editable source of skill {args.name}: the deployer's manifest records none, and the repository has "
            "no project skill of that name; name its source file with --file"
        )
    output: list[str] = [unread] if unread else []
    for path in candidates:
        refused = source_refusal(path)
        output.append(f"NOTE {flat_text(refused)}" if refused else f"SKILL_FILE {path}")
    if not any(line.startswith("SKILL_FILE ") for line in output):
        return 1, [*output, f"FAILED {flat_text(missing)}"]
    return 0, output


# How many fresh names a default report tries before audit gives up; each is 64 random bits, so a second is rare.
REPORT_NAME_TRIES = 100


def made_line(exc: OSError) -> list[str]:
    """The FAILED line of a report create_bytes put in place whole but could not remove the copy of, or none."""
    if isinstance(exc, UnfinishedCopyError) and exc.made:
        return [f"FAILED the audit report {flat_text(str(exc))}"]
    return []


def create_output(output: Path, data: bytes) -> tuple[int, list[str]]:
    """Create the report at --output, whole or not at all, never over an entry there and never making its folder."""
    try:
        # create_bytes makes a missing folder, which audit never does, and reads a file or a link in the folder's place
        # as an entry already at the output; so the folder is examined first, with the reason an open would give.
        # A folder removed or replaced between this and the create is the one race left, and no code here makes one.
        if not stat.S_ISDIR(output.parent.stat().st_mode):
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), str(output.parent))
        create_bytes(output, data)
    except FileExistsError:
        return 1, [f"FAILED {output} already exists, and audit never replaces a file"]
    except OSError as exc:
        return 1, made_line(exc) or [f"FAILED cannot write {output}: {reason(exc)}"]
    return 0, [f"REPORT {output}"]


def create_temporary_report(data: bytes) -> tuple[int, list[str]]:
    """Create the report under a fresh name in the temporary directory, whole or not at all."""
    try:
        folder = private_folder.temporary_directory()
        for _ in range(REPORT_NAME_TRIES):
            target = folder / f"memory-audit-{secrets.token_hex(8)}.json"
            try:
                create_bytes(target, data)
            except FileExistsError:
                continue
            return 0, [f"REPORT {target}"]
        raise FileExistsError(errno.EEXIST, f"no name tried in {folder} was free after {REPORT_NAME_TRIES} tries")
    except OSError as exc:
        return 1, made_line(exc) or [f"FAILED the audit report cannot be written: {reason(exc)}"]


def audit_lines(args: argparse.Namespace) -> tuple[int, list[str]]:
    """Write the JSON audit report to a new file, --output or a temporary one, and name it."""
    user_dir = args.user_dir or config_directory(absolute_home(), dict(os.environ))
    found = audit(args.memory_dir, args.repo, args.instructions, user_dir=user_dir)
    try:
        report = json_writer.dumps(found, indent=2) + "\n"
    except json_writer.JsonWriteRefused as exc:
        return 1, [f"FAILED the audit report cannot be written: {flat_text(str(exc))}"]
    # json_writer refused what UTF-8 cannot encode, so the report encodes; its lines end in \n on every platform.
    data = report.encode("utf-8")
    return create_output(args.output, data) if args.output else create_temporary_report(data)


def resolve_command(args: argparse.Namespace) -> tuple[int, list[str]]:
    """resolve's lines for the top level of the worktree holding --repo or the current directory."""
    if args.repo is not None and holds_line_break(str(args.repo)):
        return 1, [f"FAILED {flat_text(f'the repository path holds a line break: {str(args.repo)!r}')}"]
    try:
        repo = repository_root(args.repo or Path.cwd())
        if repo is None and args.repo is None:
            return 1, ["FAILED not inside a Git repository; pass --repo"]
        # A folder in no Git repository is a project of its own.
        repo = repo or project_folder(args.repo)
        if holds_line_break(str(repo)):
            return 1, [f"FAILED {flat_text(f'the repository path holds a line break: {str(repo)!r}')}"]
        return 0, resolve_lines(repo, resolve(repo, args.home, dict(os.environ), managed_settings_path()))
    except GitError as exc:
        return 1, [f"FAILED {flat_text(f'git could not read the repository: {exc}')}"]


def store_folder_reason(memory_dir: Path) -> str | None:
    """`file_presence.refusal_reason` of the memory directory a command is given, the tree every later read of the
    store is bound to, judged before any reader is bound to it."""
    return file_presence.refusal_reason(memory_dir, folder=True)


def run(args: argparse.Namespace) -> tuple[int, list[str]]:
    if args.command == "resolve":
        return resolve_command(args)
    if args.command == "skill-source":
        return skill_source_lines(args)
    # A memory directory, or audit's repository, holding `..` is made absolute with each `..` taken as text before a
    # reader is bound to it, since the reader refuses a `..` that climbs above its tree; any other spelling is kept.
    if ".." in args.memory_dir.parts:
        args.memory_dir = lexical_absolute(args.memory_dir)
    if args.command == "audit" and args.repo is not None and ".." in args.repo.parts:
        args.repo = lexical_absolute(args.repo)
    if args.command == "audit" and args.output:
        projects = config_directory(absolute_home(), dict(os.environ)) / "projects"
        refused = output_refusal(args.output, args.memory_dir, projects)
        if refused:
            return 1, [f"FAILED {refused}"]
    # Only a directory with nothing there is not found: a file in its place, or one a stat cannot examine, is named so.
    reason = store_folder_reason(args.memory_dir)
    if reason == file_presence.MISSING:
        return 1, [f"FAILED memory directory not found: {args.memory_dir}"]
    if reason is not None:
        return 1, [f"FAILED memory directory {args.memory_dir} {reason}"]
    refused = store_refusal(args.memory_dir)
    if refused:
        return 1, [f"FAILED {refused}"]
    if args.command == "reindex":
        return reindex_lines(args.memory_dir, args.write)
    if args.command == "delete":
        return delete(args.memory_dir, args.names)
    return audit_lines(args)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    locate = commands.add_parser("resolve", help="locate the memory directory Claude Code uses for a repository")
    locate.add_argument(
        "--repo", type=Path, help="default: the root of the Git repository holding the current directory"
    )
    locate.add_argument("--home", type=Path, help="default: the home folder")
    inspect = commands.add_parser("audit", help="audit one memory directory")
    inspect.add_argument("--memory-dir", type=Path, required=True)
    inspect.add_argument("--repo", type=Path)
    inspect.add_argument("--instructions", type=Path, action="append", default=[])
    inspect.add_argument("--user-dir", type=Path, help="default: CLAUDE_CONFIG_DIR, else ~/.claude")
    inspect.add_argument("--output", type=Path, help="report file; default: a new temporary file")
    rebuild = commands.add_parser("reindex", help="rebuild MEMORY.md from the memories' frontmatter")
    rebuild.add_argument("--memory-dir", type=Path, required=True)
    rebuild.add_argument("--write", action="store_true", help="replace MEMORY.md; otherwise only report")
    remove = commands.add_parser("delete", help="delete approved memory files from the memory directory")
    remove.add_argument("--memory-dir", type=Path, required=True)
    remove.add_argument("names", nargs="+", metavar="FILE", help="a memory file name, such as old-rule.md")
    source = commands.add_parser("skill-source", help="name the file to edit when moving a rule into a skill")
    named = source.add_mutually_exclusive_group(required=True)
    named.add_argument("name", nargs="?", metavar="NAME", help="the skill's name, such as review-prs")
    named.add_argument("--file", type=Path, help="a skill source file the user named, to check instead")
    source.add_argument(
        "--repo", type=Path, help="default: the root of the Git repository holding the current directory"
    )
    source.add_argument("--home", type=Path, help="default: the home folder")
    args = parser.parse_args(arguments)
    try:
        # Every command reads the home folder, if only for the skills directories the deployer owns under it that a
        # store may not lie in, so a home that is not an absolute path is refused first, on a FAILED line rather than
        # in building the parser.
        home = absolute_home()
        if hasattr(args, "home") and args.home is None:
            args.home = home
        code, lines = run(args)
    except OSError as exc:
        code, lines = 1, [f"FAILED {describe(exc)}"]
    except RelativeEnvironmentPath as exc:
        remedy = (
            "set it to the absolute path of the Claude configuration directory, or unset it"
            if exc.variable == "CLAUDE_CONFIG_DIR"
            else "set it to the absolute path of your home folder"
        )
        code, lines = 1, [f"FAILED {flat_text(f'{exc}; {remedy}')}"]
    print("\n".join(single_line(line) for line in lines))
    return code


if __name__ == "__main__":
    run_main(main)
