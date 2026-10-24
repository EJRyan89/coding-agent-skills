"""Audit, reindex, and delete from a Claude Code memory directory, and find a skill's source.

Every command prints one fact per line on stdout. An expected failure is a last line `FAILED <reason>`
with exit code 1; a usage error exits 2. Text read from a settings file, the manifest, a memory file's
name, or a memory reaches a line or a MEMORY.md entry only through ejryan89-skill-core's flat_text, and a path
holding a line break, which no line can name for a later command, is refused rather than printed.

A memory store is a directory holding MEMORY.md, outside the skills directory this script runs from,
~/.claude/skills, and ~/.agents/skills. audit, reindex, and delete take only a memory store: given any
other directory, they print one `FAILED <dir> ...` line, read and change nothing, and exit 1. resolve
reports the same check as a `REFUSED` line, so the skill learns before any change that the later
commands refuse the directory. A memory is a regular file named *.md directly in the store, other than
MEMORY.md; any other entry so named, a directory, symbolic link, or junction, is reported unreadable with
its reason and never read, added to the index, or deleted.

resolve and audit are read-only. They never decide what to keep or delete; the calling agent verifies
every candidate. resolve locates the memory directory for --repo, else for the current directory, and
prints `REPO <root>`, the top level of the Git worktree holding that folder, or the folder itself when
--repo names one in no Git repository; then `MEMORY_DIR <path>`, `SOURCE <where it came from>`,
`EXISTS yes|no`, and `REFUSED <reason>` when the directory exists but is no memory store, or
`NO_MEMORY_DIR` when no usable directory resolves; one `CANDIDATE <dir>` per memory directory under the
configuration directory's projects folder when no memory store resolved, whether settings named a
missing one, the default one is missing or refused, or none is usable; and one `NOTE <text>` per caveat.
No directory is a result, not a failure. A directory a repository's settings choose is not used while
permissions.blockReadsOutsideWorkingDirectories is on in any scope, since Claude Code then loads no
memory from it and saves none to it. Without settings, the directory is derived from the top level of
the repository's main worktree, which all its worktrees share (see main_worktree). The current
directory outside any repository, and a git that cannot run, does not finish, or fails, are each a
`FAILED <reason>` line.

audit writes its JSON report of mechanical problems and candidate overlaps to --output, else to a new
temporary file, and prints `REPORT <path>`. It never replaces a file: it refuses, before reading
anything, an --output that exists or lies inside the memory directory, the configuration directory's
projects folder, the skills directory it runs from, ~/.claude/skills, or ~/.agents/skills. The report's
`unreadable` list names each entry it could not read as a memory, with the reason, and the rest are
audited. It searches the user's Claude configuration directory (CLAUDE_CONFIG_DIR, else ~/.claude)
unless --user-dir names another.

reindex rebuilds MEMORY.md from the memory files' frontmatter, one line per memory:
`- [Title](file.md) — hook`. It writes only MEMORY.md, only with --write, and never deletes
or changes a memory file. Existing lines keep their place: other lines (headings, notes) stay
as written, an entry is dropped when its file is gone or it repeats an earlier entry, and
memories without an entry are appended in file-name order. The title is the existing entry's
title, else the frontmatter name, else the file stem. The hook is the frontmatter description,
all of a `>` or `|` block scalar's lines, else the existing entry's hook, kept as written, else
the first line of the body; a description or a body line is collapsed to one line of at most 150
characters, because Claude Code loads the index into every session and stops at its load limit.
It prints `INDEX_LINE`, `ADDED`, and `DROPPED` lines, then
`NEAR_LIMIT` or `OVER_LIMIT` when the index nears or passes Claude Code's load limit, then
`UNCHANGED`, `WOULD_WRITE <path>`, or `WROTE <path>`. OVER_LIMIT is a finding: it exits 1.
An entry whose target, after any leading `./`, is not a bare *.md name directly in the store (a `..`
segment, an absolute path, a subfolder, or a link or junction on the way) is never read or followed,
and neither is one whose file exists but is not a regular file: each stays as written, and it and each
such unindexed name print an `UNREADABLE <file>: <reason>` line before those. audit reports the same
targets in its `unreadable` list. A memory it cannot read,
or whose file name holds a line break, which no entry can link on one line, is a `FAILED` line,
and nothing is written.

delete removes the named memory files and nothing else. Each name must be a bare .md file name
directly inside the memory directory: not MEMORY.md, not a path (no separator, drive, `.`, or
`..`), holding no line break, not named twice, and an existing regular file, never a directory,
symbolic link, or junction. Every name is checked before any is deleted. When any name is refused it prints one
`FAILED <name>: <reason>` line per refusal, deletes nothing, and exits 1; otherwise it prints one
`DELETED <name>` line per file. When a deletion fails part way, the files already printed as
DELETED are gone, a last `FAILED <name>: <reason>` names the one that failed, and the rest remain.

skill-source prints `SKILL_FILE <path>` for each editable source of the named skill: the
skills/<name>/SKILL.md of the checkout the deployer's manifest records for it, and the repository's
.claude/skills/<name>/SKILL.md; with --file it checks a source file named instead. It never names a
file inside a skills directory, the deployed copy among them, because the deployer stops updating a
skill whose deployed copy changed, and prints a `NOTE` for each such file it passes over, and one
when git cannot read the repository to look for a project skill. With no source left, it prints
`FAILED <reason>` and exits 1.

Usage:
  python memory_audit.py resolve [--repo ROOT]
  python memory_audit.py audit --memory-dir DIR [--repo ROOT] [--user-dir DIR] [--instructions FILE ...] [--output FILE]
  python memory_audit.py reindex --memory-dir DIR [--write]
  python memory_audit.py delete --memory-dir DIR FILE [FILE ...]
  python memory_audit.py skill-source (NAME | --file FILE) [--repo ROOT] [--home DIR]
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import re
import string
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ejryan89-skill-core" / "scripts"))

import json_reader
from console import run_main
from file_presence import is_present
from flat_text import flat_text
from git_client import GitClient, GitError
from link_probe import is_link_or_junction, is_plain_file
from markdown_sections import CODE_SPAN
from skill_roots import skills_directory_holding

INDEX_NAME = "MEMORY.md"
# The deployer's ownership manifest beside the skills it deploys, and where a source checkout keeps a skill.
MANIFEST_NAME = ".deploy-manifest.json"
SETTINGS_NOTE = (
    "A --settings file passed when Claude Code starts can also set autoMemoryDirectory; this audit cannot see it."
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
SKIPPED_DIRECTORIES = frozenset({".git", "node_modules", "bin", "obj", ".venv", "venv", "__pycache__", "packages"})


@dataclass
class CitedPath:
    path: str
    exists: bool


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


def holds_line_break(text: str) -> bool:
    """Whether `text` holds a character `str.splitlines` breaks a line at, so no one line can carry it whole."""
    return "".join(text.splitlines()) != text


def indentation(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


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


# Kept for good instead of ejryan89-skill-core's frontmatter.py, by the decision recorded in
# https://github.com/EJRyan89/coding-agent-skills/issues/27: memories nest their type in a `metadata:` mapping, which
# the shared reader does not give, and the audit must read every memory however it is written.
def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the simple key: value frontmatter Claude Code writes, tolerating nesting and reading block scalars."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    values: dict[str, str] = {}
    index = 1
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.strip() == "---":
            return values, "\n".join(lines[index:])
        match = FRONTMATTER_KEY.match(line)
        if not match or not match.group("value"):
            continue
        value = match.group("value").strip()
        header = BLOCK_SCALAR.match(value)
        if header:
            # A block scalar's lines are never read as keys, and the closing --- is never indented past a key.
            following = list(itertools.takewhile(lambda rest: rest.strip() != "---", lines[index:]))
            value, spanned = block_scalar(header, indentation(line), following)
            index += spanned
        else:
            value = value.strip("\"'")
        if value:
            values.setdefault(match.group("key"), value)
    return {}, text


def parse_target(line: str, start: int) -> tuple[str, int] | None:
    """Decode a Markdown destination, stopping at its unescaped, balanced closing delimiter."""
    target: list[str] = []
    depth = 1
    position = start
    while position < len(line):
        character = line[position]
        if character == "\\" and position + 1 < len(line) and line[position + 1] in string.punctuation:
            position += 1
            target.append(line[position])
        elif character == "(":
            depth += 1
            target.append(character)
        elif character == ")":
            depth -= 1
            if depth == 0:
                return ("".join(target), position) if target else None
            target.append(character)
        else:
            target.append(character)
        position += 1
    return None


def parse_index_entry(line: str) -> dict[str, str] | None:
    match = INDEX_ENTRY.match(line)
    if not match:
        return None
    parsed = parse_target(line, match.end())
    if parsed is None:
        return None
    target, end = parsed
    hook = INDEX_HOOK.match(line[end + 1 :])
    return {
        "title": match.group("title"),
        "file": line[match.end() : end],
        "target": target,
        "hook": hook.group("hook") if hook else "",
        "text": line,
    }


def parse_index(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    """The index's entries and lines, none when no index is there; a directory or anything else in its place raises
    NotAFile, an OSError, rather than reading as an empty index."""
    if not is_present(path):
        return [], []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries = []
    for number, line in enumerate(lines, start=1):
        entry = parse_index_entry(line)
        if entry is not None:
            entries.append({"line": str(number), **entry})
    return entries, lines


def target_identity(memory_dir: Path, file: str) -> str:
    """Compare targets relative to the store, resolving ./ prefixes and normalizing case on Windows."""
    return os.path.normcase(str((memory_dir / file).resolve()))


# What entry_problem gives for a path that names nothing, such as an index entry whose file is gone.
MISSING = "no such file"


def entry_problem(path: Path) -> str | None:
    """Why the entry at path is no memory file to read, or None when it is a regular file.

    A directory, symbolic link, or Windows junction is never followed, whatever it points at, so an entry named like
    a memory that is one of them is never read through, indexed, or deleted.
    """
    try:
        status = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return MISSING
    except OSError as exc:
        return f"cannot inspect: {reason(exc)}"
    if not is_plain_file(path, status):
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
    name = CURRENT_FOLDER.sub("", target)
    if not bare_name(name):
        return NOT_IN_STORE
    if not name.lower().endswith(".md"):
        return NOT_MARKDOWN
    return entry_problem(memory_dir / name)


def memory_entries(memory_dir: Path) -> tuple[list[Path], list[tuple[str, str]]]:
    """The store's memory files, and each other entry named like one with why it is none, both in name order.

    The index, however its name is spelled, is neither.
    """
    index_identity = target_identity(memory_dir, INDEX_NAME)
    files: list[Path] = []
    others: list[tuple[str, str]] = []
    for path in sorted(memory_dir.glob("*.md")):
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
    for match in CODE_SPAN.finditer(body):
        candidate = match.group(1).strip()
        candidate = re.sub(r":\d+(?:-\d+)?$", "", candidate)
        if (
            candidate in seen
            or not PATH_LIKE.match(candidate)
            or not ANCHORED_PATH.search(candidate)
            or any(marker in candidate for marker in ("://", "<", "*", "{"))
        ):
            continue
        seen.add(candidate)
        expanded = Path(candidate.replace("~", str(Path.home()), 1)) if candidate.startswith("~") else Path(candidate)
        exists = expanded.exists() if expanded.is_absolute() else any((base / expanded).exists() for base in bases)
        cited.append(CitedPath(candidate, exists))
    return cited


def destination_blocks(files: list[Path]) -> list[tuple[str, int, str, set[str]]]:
    """Split destination documents into paragraphs and list items for overlap scoring."""
    blocks: list[tuple[str, int, str, set[str]]] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        current: list[str] = []
        start = 1
        for number, line in enumerate([*lines, ""], start=1):
            boundary = not line.strip() or line.lstrip().startswith(("- ", "* ", "#", "|"))
            if boundary and current:
                text = " ".join(current)
                blocks.append((str(path), start, text, words(text)))
                current = []
            if line.strip() and not line.lstrip().startswith("#"):
                if not current:
                    start = number
                current.append(line.strip())
                if line.lstrip().startswith(("- ", "* ", "|")):
                    text = " ".join(current)
                    blocks.append((str(path), start, text, words(text)))
                    current = []
    return blocks


def overlaps_for(body: str, blocks: list[tuple[str, int, str, set[str]]]) -> list[Overlap]:
    memory_words = words(body)
    if len(memory_words) < 4:
        return []
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
    return scored[:OVERLAP_LIMIT]


def destination_files(repo: Path | None, instructions: list[Path], user_dir: Path | None = None) -> list[Path]:
    files = [path for path in instructions if path.is_file()]
    for root, patterns in ((repo, DESTINATION_GLOBS), (user_dir, USER_DESTINATION_GLOBS)):
        if root is None:
            continue
        for pattern in patterns:
            files.extend(
                path
                for path in root.glob(pattern)
                if path.is_file() and not SKIPPED_DIRECTORIES.intersection(path.relative_to(root).parts[:-1])
            )
    unique: dict[str, Path] = {}
    for path in files:
        unique.setdefault(str(path.resolve()).casefold(), path)
    return list(unique.values())


def audit(
    memory_dir: Path,
    repo: Path | None = None,
    instructions: list[Path] | None = None,
    now: float | None = None,
    user_dir: Path | None = None,
) -> dict[str, Any]:
    now = time.time() if now is None else now
    index_path = memory_dir / INDEX_NAME
    entries, index_lines = parse_index(index_path)
    index_bytes = index_path.stat().st_size if index_path.is_file() else 0
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
            parsed[path] = parse_frontmatter(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            unreadable[target_identity(memory_dir, path.name)] = (path.name, reason(exc))
    files = list(parsed)
    known_names = {path.stem for path in files} | {meta.get("name", "") for meta, _ in parsed.values()}
    blocks = destination_blocks(destination_files(repo, instructions or [], user_dir))
    bases = [base for base in (repo, memory_dir) if base is not None]
    memories: list[Memory] = []
    for path in files:
        meta, body = parsed[path]
        links = sorted(set(WIKI_LINK.findall(body)))
        memories.append(
            Memory(
                file=path.name,
                name=meta.get("name", ""),
                description=meta.get("description", ""),
                type=meta.get("type", ""),
                indexed=target_identity(memory_dir, path.name) in indexed_files,
                age_days=round((now - path.stat().st_mtime) / 86400, 1),
                links=links,
                broken_links=[link for link in links if link not in known_names],
                cited_paths=cited_paths(body, bases),
                overlaps=overlaps_for(body, blocks),
            )
        )
    return {
        "memory_dir": str(memory_dir),
        "index": {
            "path": str(index_path),
            "exists": index_path.is_file(),
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
    target = original_target if original_target is not None else re.sub(r"([\\()])", r"\\\1", file)
    return f"- [{title}]({target})" + (f" — {hook}" if hook else "")


def reindex(memory_dir: Path) -> dict[str, Any]:
    """Rebuild MEMORY.md in memory without writing it; see the module docstring for the rules."""
    index_path = memory_dir / INDEX_NAME
    current = index_path.read_bytes().decode("utf-8", errors="replace") if is_present(index_path) else ""

    def frontmatter(file: str) -> tuple[dict[str, str], str]:
        return parse_frontmatter((memory_dir / file).read_text(encoding="utf-8", errors="replace"))

    def derived_hook(body: str) -> str:
        first = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
        return one_line(first)

    def derived_title(meta: dict[str, str], file: str) -> str:
        return flat_text((meta.get("name") or Path(file).stem).replace("[", "(").replace("]", ")"))

    lines: list[str] = []
    entries: list[str] = []
    seen: set[str] = set()
    dropped: list[tuple[str, str]] = []
    unreadable: list[tuple[str, str]] = []
    for line in current.splitlines():
        entry = parse_index_entry(line)
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
        meta, body = frontmatter(file)
        hook = one_line(meta["description"]) if meta.get("description") else entry["hook"] or derived_hook(body)
        lines.append(
            index_line(entry["title"].strip() or derived_title(meta, file), file, hook, original_target=entry["file"])
        )
        entries.append(entry["file"])
    files, others = memory_entries(memory_dir)
    added = sorted(path.name for path in files if target_identity(memory_dir, path.name) not in seen)
    unreadable += [(name, problem) for name, problem in others if target_identity(memory_dir, name) not in seen]
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
    """Replace MEMORY.md atomically; nothing else in the directory is touched, even when the write fails."""
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_bytes(content.encode("utf-8"))
        temporary.replace(path)
    except OSError:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise


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
    return entry_problem(memory_dir / name)


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


def managed_settings_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def encode_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


class UnknownMainWorktree(Exception):
    """A linked worktree whose shared Git directory names no working tree, so its main worktree cannot be found."""


def rev_parse_paths(directory: Path, *options: str) -> list[Path] | None:
    """The absolute path `git rev-parse` prints for each option in directory, or None when it lies in no repository.

    Raises GitError when git cannot run or finish, or fails for any other reason, such as a folder it cannot enter.
    """
    try:
        output = GitClient().output(["rev-parse", "--path-format=absolute", *options], directory=directory)
    except GitError as exc:
        if exc.kind == "not_repository":
            return None
        raise
    paths = output.splitlines()
    if len(paths) != len(options) or not all(paths):
        raise GitError(f"git rev-parse printed {len(paths)} lines for {' '.join(options)} in {directory}")
    return [Path(path) for path in paths]


def main_worktree(repo: Path) -> Path | None:
    """The top level of the main worktree of the repository holding repo, which every worktree shares, or None when
    repo lies in no repository.

    In the main worktree, its Git directory is the shared one, and the answer is its own top level, whether that Git
    directory is its `.git`, a submodule's under the superproject, or one made with --separate-git-dir. A linked
    worktree's shared Git directory belongs to the main worktree: the folder holding it when it is named `.git`, else
    the working tree its configuration names, as a submodule's does. One that names none, as a separate Git
    directory does, raises UnknownMainWorktree. Raises GitError when git cannot run, does not finish, or fails.
    """
    paths = rev_parse_paths(repo, "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    if paths is None:
        return None
    top, git_dir, common = paths
    if git_dir == common:
        return top
    if common.name == ".git":
        return common.parent
    # core.worktree, relative to the Git directory when relative, is git's own record of a submodule's working tree.
    configured = GitClient().run(["config", "--file", str(common / "config"), "--get", "core.worktree"])
    if configured.returncode == 1:
        raise UnknownMainWorktree(
            f"{top} is a linked worktree whose shared Git directory {common} names no main worktree; run resolve "
            "with --repo naming the main worktree, or pass --memory-dir."
        )
    value = configured.stdout.rstrip("\r\n")
    if configured.returncode != 0 or not value:
        message = configured.stderr.strip() or f"git config failed with exit code {configured.returncode}"
        raise GitError(message, returncode=configured.returncode)
    return (common / value).resolve()


def repository_root(directory: Path) -> Path | None:
    """The top level of the Git worktree holding directory, or None when it lies in no repository.

    Raises GitError when git cannot run, does not finish, or fails for another reason.
    """
    paths = rev_parse_paths(directory, "--show-toplevel")
    return None if paths is None else paths[0]


def config_directory(home: Path, environment: dict[str, str]) -> Path:
    """The user's Claude Code configuration directory."""
    return Path(environment["CLAUDE_CONFIG_DIR"]) if environment.get("CLAUDE_CONFIG_DIR") else home / ".claude"


def resolve(repo: Path, home: Path, environment: dict[str, str], managed: Path) -> dict[str, Any]:
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
    if result["exists"]:
        refused = store_refusal(Path(result["memory_dir"]))
        if refused:
            result["refused"] = refused
    if not result["exists"] or result.get("refused"):
        found = sorted(str(path) for path in (config_dir / "projects").glob("*/memory") if path.is_dir())
        refused_dir = result["memory_dir"] if result.get("refused") else None
        result["candidates"] = [path for path in found if not holds_line_break(path) and path != refused_dir]
        result["notes"] += [
            f"Left out a memory directory whose path holds a line break: {path!r}"
            for path in found
            if holds_line_break(path)
        ]
    return result


class UnreadableSettings(Exception):
    """A settings file that exists but holds no readable JSON."""


def unresolved(notes: list[str]) -> dict[str, Any]:
    return {"memory_dir": None, "source": None, "exists": False, "notes": notes}


def read_settings(path: Path) -> dict[str, Any]:
    """A settings file's top-level object, empty when the file is absent or holds another value; a directory or
    anything else in the file's place raises UnreadableSettings, as a file that cannot be read does."""
    try:
        if not is_present(path):
            return {}
        settings = json_reader.loads(path.read_bytes().decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError, json_reader.JsonRefused) as exc:
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
        if is_link_or_junction(repo / ".claude"):
            return True
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError:
        return True
    try:
        listed = GitClient().run(["ls-files", "--error-unmatch", "--", ".claude/settings.local.json"], directory=repo)
    except GitError:
        return True
    return listed.returncode == 0


def blocking_scope(scopes: tuple[tuple[str, Path], ...]) -> str | None:
    """The first scope whose settings turn permissions.blockReadsOutsideWorkingDirectories on, or None.

    Claude Code honors `true` from any scope, even over a managed `false` (https://code.claude.com/docs/en/settings,
    "Exceptions to managed settings precedence").
    """
    for scope, path in scopes:
        permissions = read_settings(path).get("permissions")
        if isinstance(permissions, dict) and permissions.get(BLOCK_READS) is True:
            return scope
    return None


def locate(repo: Path, home: Path, environment: dict[str, str], managed: Path, config_dir: Path) -> dict[str, Any]:
    """The auto-memory directory Claude Code uses for repo, following its documented order."""
    notes = [SETTINGS_NOTE]
    # Claude Code reads autoMemoryDirectory from every settings scope, in their precedence order, and honors a
    # project's settings.json or settings.local.json under workspace trust, but not while reads outside the working
    # directories are blocked (https://code.claude.com/docs/en/memory, "Storage location"); the --settings scope
    # between managed and local is in the note above.
    scopes = (
        ("managed", managed),
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
            root = repo.resolve()
            source = f"derived from the folder ({root}), which is in no Git repository"
        else:
            source = f"derived from the repository's main worktree ({root})"
        directory = projects / encode_project(str(root)) / "memory"
        notes.append("The directory name is derived by convention; confirm it before changing anything.")
    return {"memory_dir": str(directory), "source": source, "exists": directory.is_dir(), "notes": notes}


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
        "exists": directory.is_dir(),
        "notes": notes,
    }


def reason(exc: OSError) -> str:
    """An expected OSError's reason on one line."""
    return flat_text(exc.strerror or str(exc))


def describe(exc: OSError) -> str:
    """An expected OSError as one line: its reason and the file it names."""
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
    if os.path.lexists(explicit):
        return (
            f"{explicit} already exists, and audit never replaces a file; omit --output to write a new temporary file"
        )
    target = explicit.resolve()
    for kind, store in (("the memory directory", memory_dir), ("Claude Code's projects folder", projects)):
        if target.is_relative_to(store.resolve()):
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
        present = is_present(memory_dir / INDEX_NAME)
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
    place.
    """
    manifest = home / ".claude" / "skills" / MANIFEST_NAME
    if not is_present(manifest):
        return []
    data = json_reader.read_file(manifest)
    sources = data.get("sources") if isinstance(data, dict) else None
    entries = sources.values() if isinstance(sources, dict) else []
    return [
        Path(entry["source_dir"]) / "skills" / name / "SKILL.md"
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("source_dir"), str)
        and isinstance(entry.get("skills"), dict)
        and name in entry["skills"]
    ]


def source_refusal(path: Path) -> str | None:
    """Why a skill's file at `path` must not be edited as its source, or None when it may be."""
    if holds_line_break(str(path)):
        return f"{path!r} holds a line break"
    root = skills_directory_holding(path)
    if root:
        return f"{path} is inside the skills directory {root}; the deployer stops updating a skill changed there"
    if not path.is_file():
        return f"{path} is not a file"
    return None


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
        project = None if repo is None else repo / ".claude" / "skills" / args.name / "SKILL.md"
        candidates += [project] if project is not None and project.is_file() else []
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


def audit_lines(args: argparse.Namespace) -> tuple[int, list[str]]:
    """Write the JSON audit report to a new file, --output or a temporary one, and name it."""
    user_dir = args.user_dir or config_directory(Path.home(), dict(os.environ))
    report = json.dumps(audit(args.memory_dir, args.repo, args.instructions, user_dir=user_dir), indent=2) + "\n"
    if args.output:
        try:
            # Created exclusively, so a file that appeared since the check is never replaced either.
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(report)
        except FileExistsError:
            return 1, [f"FAILED {args.output} already exists, and audit never replaces a file"]
        except OSError as exc:
            return 1, [f"FAILED cannot write {args.output}: {reason(exc)}"]
        return 0, [f"REPORT {args.output}"]
    descriptor, name = tempfile.mkstemp(prefix="memory-audit-", suffix=".json")
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(report)
    return 0, [f"REPORT {name}"]


def resolve_command(args: argparse.Namespace) -> tuple[int, list[str]]:
    """resolve's lines for the top level of the worktree holding --repo or the current directory."""
    if args.repo is not None and holds_line_break(str(args.repo)):
        return 1, [f"FAILED {flat_text(f'the repository path holds a line break: {str(args.repo)!r}')}"]
    try:
        repo = repository_root(args.repo or Path.cwd())
        if repo is None and args.repo is None:
            return 1, ["FAILED not inside a Git repository; pass --repo"]
        # A folder in no Git repository is a project of its own.
        repo = repo or args.repo.resolve()
        if holds_line_break(str(repo)):
            return 1, [f"FAILED {flat_text(f'the repository path holds a line break: {str(repo)!r}')}"]
        return 0, resolve_lines(repo, resolve(repo, args.home, dict(os.environ), managed_settings_path()))
    except GitError as exc:
        return 1, [f"FAILED {flat_text(f'git could not read the repository: {exc}')}"]


def run(args: argparse.Namespace) -> tuple[int, list[str]]:
    if args.command == "resolve":
        return resolve_command(args)
    if args.command == "skill-source":
        return skill_source_lines(args)
    if args.command == "audit" and args.output:
        projects = config_directory(Path.home(), dict(os.environ)) / "projects"
        refused = output_refusal(args.output, args.memory_dir, projects)
        if refused:
            return 1, [f"FAILED {refused}"]
    if not args.memory_dir.is_dir():
        return 1, [f"FAILED memory directory not found: {args.memory_dir}"]
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
    locate.add_argument("--home", type=Path, default=Path.home())
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
    source.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(arguments)
    try:
        code, lines = run(args)
    except OSError as exc:
        code, lines = 1, [f"FAILED {describe(exc)}"]
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    run_main(main)
