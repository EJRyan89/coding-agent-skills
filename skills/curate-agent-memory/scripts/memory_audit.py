"""Audit and reindex a Claude Code memory directory.

resolve and audit are read-only and report as JSON: mechanical problems and candidate overlaps.
They never decide what to keep or delete; the calling agent verifies every candidate. resolve prints
its JSON; audit writes it to --output, else to a new temporary file, and prints `REPORT <path>`.
audit searches the user's Claude configuration directory (CLAUDE_CONFIG_DIR, else ~/.claude) unless
--user-dir names another.

reindex rebuilds MEMORY.md from the memory files' frontmatter, one line per memory:
`- [Title](file.md) — hook`. It writes only MEMORY.md, only with --write, and never deletes
or changes a memory file. Existing lines keep their place: other lines (headings, notes) stay
as written, an entry is dropped when its file is gone or it repeats an earlier entry, and
memories without an entry are appended in file-name order. The title is the existing entry's
title, else the frontmatter name, else the file stem. The hook is the frontmatter description,
else the existing entry's hook, else the first line of the body; a derived hook is collapsed to
one line of at most 150 characters.

Usage:
  python memory_audit.py resolve --repo ROOT
  python memory_audit.py audit --memory-dir DIR [--repo ROOT] [--user-dir DIR] [--instructions FILE ...] [--output FILE]
  python memory_audit.py reindex --memory-dir DIR [--write]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

INDEX_NAME = "MEMORY.md"
# Claude Code loads the first 200 lines or 25KB of MEMORY.md, whichever comes first.
INDEX_LINE_LIMIT = 200
INDEX_BYTE_LIMIT = 25_000
NEAR_LIMIT_FRACTION = 0.9
OVERLAP_THRESHOLD = 0.3
OVERLAP_LIMIT = 3
INDEX_ENTRY = re.compile(r"^\s*-\s*\[(?P<title>[^\]]*)\]\((?P<file>[^)]+)\)")
INDEX_HOOK = re.compile(r"^\s*-\s*\[[^\]]*\]\([^)]+\)\s*(?:[—–:-]\s*)?(?P<hook>.*?)\s*$")
HOOK_LIMIT = 150
WIKI_LINK = re.compile(r"\[\[([^\]]+)\]\]")
BACKTICKED = re.compile(r"`([^`\n]+)`")
PATH_LIKE = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.{0,2}[\\/])?[\w.@()\- ]+(?:[\\/][\w.@()\- ]+)+[\\/]?$")
ANCHORED_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|\.{1,2}[\\/])|[\\/]$|\.[A-Za-z0-9]{1,8}$")
WORD = re.compile(r"[a-z][a-z0-9_]{2,}")
STOP_WORDS = frozenset(
    "the and for with that this from are not use when into only must should never always "
    "all any can does each its has have was were will what which while who why how than then "
    "them they their there these those via per our out also but instead".split()
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


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Parse the simple key: value frontmatter Claude Code writes, tolerating nesting."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return values, "\n".join(lines[index + 1 :])
        match = re.match(r"^\s*([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if match and match.group(2):
            values.setdefault(match.group(1), match.group(2).strip().strip("\"'"))
    return {}, text


def parse_index(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.is_file():
        return [], []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    entries = []
    for number, line in enumerate(lines, start=1):
        match = INDEX_ENTRY.match(line)
        if match:
            entries.append({"line": str(number), "title": match.group("title"), "file": match.group("file"), "text": line})
    return entries, lines


def words(text: str) -> set[str]:
    return {word for word in WORD.findall(text.lower()) if word not in STOP_WORDS}


def cited_paths(body: str, bases: list[Path]) -> list[CitedPath]:
    cited: list[CitedPath] = []
    seen: set[str] = set()
    for match in BACKTICKED.finditer(body):
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
) -> dict:
    now = time.time() if now is None else now
    index_path = memory_dir / INDEX_NAME
    entries, index_lines = parse_index(index_path)
    index_bytes = index_path.stat().st_size if index_path.is_file() else 0
    indexed_files = {entry["file"] for entry in entries}
    files = sorted(path for path in memory_dir.glob("*.md") if path.name != INDEX_NAME)
    parsed: dict[Path, tuple[dict[str, str], str]] = {
        path: parse_frontmatter(path.read_text(encoding="utf-8", errors="replace")) for path in files
    }
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
                indexed=path.name in indexed_files,
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
            "missing_files": [entry["file"] for entry in entries if not (memory_dir / entry["file"]).is_file()],
            "duplicate_entries": sorted({entry["file"] for entry in entries if [e["file"] for e in entries].count(entry["file"]) > 1}),
        },
        "unindexed_files": [memory.file for memory in memories if not memory.indexed],
        "memories": [asdict(memory) for memory in memories],
    }


def one_line(text: str, limit: int = HOOK_LIMIT) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,;:.") + "..."


def index_line(title: str, file: str, hook: str) -> str:
    return f"- [{title}]({file})" + (f" — {hook}" if hook else "")


def reindex(memory_dir: Path) -> dict:
    """Rebuild MEMORY.md in memory without writing it; see the module docstring for the rules."""
    index_path = memory_dir / INDEX_NAME
    current = index_path.read_text(encoding="utf-8", errors="replace") if index_path.is_file() else ""

    def frontmatter(file: str) -> tuple[dict[str, str], str]:
        return parse_frontmatter((memory_dir / file).read_text(encoding="utf-8", errors="replace"))

    def derived_hook(body: str) -> str:
        first = next((line.strip().lstrip("#").strip() for line in body.splitlines() if line.strip()), "")
        return one_line(first)

    def derived_title(meta: dict[str, str], file: str) -> str:
        return " ".join((meta.get("name") or Path(file).stem).replace("[", "(").replace("]", ")").split())

    def identity(file: str) -> str:
        # The same memory may be linked as ./a.md or, on a case-insensitive file system, A.md.
        return os.path.normcase(str((memory_dir / file).resolve()))

    lines: list[str] = []
    entries: list[str] = []
    seen: set[str] = set()
    dropped: list[tuple[str, str]] = []
    for line in current.splitlines():
        match = INDEX_ENTRY.match(line)
        if not match:
            lines.append(line)
            continue
        file = match.group("file")
        if not (memory_dir / file).is_file():
            dropped.append(("missing", file))
            continue
        if identity(file) in seen:
            dropped.append(("duplicate", file))
            continue
        seen.add(identity(file))
        meta, body = frontmatter(file)
        existing_hook = INDEX_HOOK.match(line).group("hook")
        hook = one_line(meta["description"]) if meta.get("description") else existing_hook or derived_hook(body)
        lines.append(index_line(match.group("title").strip() or derived_title(meta, file), file, hook))
        entries.append(file)
    added = sorted(
        path.name for path in memory_dir.glob("*.md") if path.name != INDEX_NAME and identity(path.name) not in seen
    )
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
        "changed": content != current,
        "over_limit": len(lines) > INDEX_LINE_LIMIT or size > INDEX_BYTE_LIMIT,
        "near_limit": len(lines) > INDEX_LINE_LIMIT * NEAR_LIMIT_FRACTION
        or size > INDEX_BYTE_LIMIT * NEAR_LIMIT_FRACTION,
        "lines": len(lines),
        "bytes": size,
    }


def write_index(path: Path, content: str) -> None:
    """Replace MEMORY.md atomically; nothing else in the directory is touched."""
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(content.encode("utf-8"))
    os.replace(temporary, path)


def managed_settings_path() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClaudeCode" / "managed-settings.json"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode/managed-settings.json")
    return Path("/etc/claude-code/managed-settings.json")


def encode_project(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", path)


def main_worktree(repo: Path) -> Path | None:
    """Return the main worktree of the repository containing repo, which every worktree shares."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    common = Path(completed.stdout.strip())
    return common.parent if common.name == ".git" else common


def config_directory(home: Path, environment: dict[str, str]) -> Path:
    """The user's Claude Code configuration directory."""
    return Path(environment["CLAUDE_CONFIG_DIR"]) if environment.get("CLAUDE_CONFIG_DIR") else home / ".claude"


def resolve(repo: Path, home: Path, environment: dict[str, str], managed: Path) -> dict:
    """Locate the auto-memory directory Claude Code uses for repo, following its documented order."""
    config_dir = config_directory(home, environment)
    notes = ["A --settings file passed when Claude Code starts can also set autoMemoryDirectory; this audit cannot see it."]
    scopes = (
        ("managed", managed),
        ("local", repo / ".claude" / "settings.local.json"),
        ("project", repo / ".claude" / "settings.json"),
        ("user", config_dir / "settings.json"),
    )
    for scope, path in scopes:
        if not path.is_file():
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            return {"memory_dir": None, "source": None, "exists": False, "notes": [*notes, f"Cannot read {path}: {exc}"]}
        value = settings.get("autoMemoryDirectory") if isinstance(settings, dict) else None
        if value is None:
            continue
        if not isinstance(value, str) or not (Path(value).is_absolute() or value.startswith("~/")):
            return {
                "memory_dir": None,
                "source": None,
                "exists": False,
                "notes": [*notes, f"autoMemoryDirectory in {path} is not an absolute or ~/ path: {value!r}"],
            }
        directory = home / value[2:] if value.startswith("~/") else Path(value)
        if scope in ("local", "project"):
            notes.append("Claude Code honors a repository-level autoMemoryDirectory only after the folder is trusted.")
        return {"memory_dir": str(directory), "source": f"autoMemoryDirectory in {scope} settings ({path})", "exists": directory.is_dir(), "notes": notes}
    projects = config_dir / "projects"
    name = environment.get("CLAUDE_CODE_PROJECT_DIR_NAME")
    if name:
        directory = projects / name / "memory"
        source = "CLAUDE_CODE_PROJECT_DIR_NAME"
    else:
        root = main_worktree(repo) or repo.resolve()
        directory = projects / encode_project(str(root)) / "memory"
        source = f"derived from the repository's main worktree ({root})"
        notes.append("The directory name is derived by convention; confirm it before changing anything.")
    result = {"memory_dir": str(directory), "source": source, "exists": directory.is_dir(), "notes": notes}
    if not directory.is_dir():
        result["candidates"] = sorted(str(path) for path in projects.glob("*/memory") if path.is_dir())
    return result


def print_reindex(memory_dir: Path, write: bool) -> int:
    """Print one fact per line: the rebuilt entries, what changed, and what was written."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = reindex(memory_dir)
    output = [f"INDEX_LINE {file}" for file in result["entries"]]
    output += [f"ADDED {file}" for file in result["added"]]
    output += [f"DROPPED {reason} {file}" for reason, file in result["dropped"]]
    if result["over_limit"] or result["near_limit"]:
        label = "OVER_LIMIT" if result["over_limit"] else "NEAR_LIMIT"
        output.append(f"{label} lines={result['lines']}/{INDEX_LINE_LIMIT} bytes={result['bytes']}/{INDEX_BYTE_LIMIT}")
    if not result["changed"]:
        output.append("UNCHANGED")
    elif write:
        write_index(result["path"], result["content"])
        output.append(f"WROTE {result['path'].as_posix()}")
    else:
        output.append(f"WOULD_WRITE {result['path'].as_posix()}")
    print("\n".join(output))
    return 0


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    locate = commands.add_parser("resolve", help="locate the memory directory Claude Code uses for a repository")
    locate.add_argument("--repo", type=Path, required=True)
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
    args = parser.parse_args(arguments)
    if args.command == "resolve":
        result = resolve(args.repo, args.home, dict(os.environ), managed_settings_path())
        print(json.dumps(result, indent=2))
        return 0 if result["memory_dir"] else 2
    if not args.memory_dir.is_dir():
        print(f"Memory directory not found: {args.memory_dir}", file=sys.stderr)
        return 2
    if args.command == "reindex":
        return print_reindex(args.memory_dir, args.write)
    user_dir = args.user_dir or config_directory(Path.home(), dict(os.environ))
    report = json.dumps(audit(args.memory_dir, args.repo, args.instructions, user_dir=user_dir), indent=2) + "\n"
    if args.output:
        args.output.write_text(report, encoding="utf-8")
        path = args.output
    else:
        descriptor, name = tempfile.mkstemp(prefix="memory-audit-", suffix=".json")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(report)
        path = Path(name)
    print(f"REPORT {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
