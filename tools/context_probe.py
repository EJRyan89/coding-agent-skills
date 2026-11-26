"""Profile a repository's shape, and how the code-review skills fared on it, as statistics only, so a defect met in a
private repository can become a generic public issue without carrying anything of that repository out.

Usage:
  python tools/context_probe.py preview <repository> [--terms <file>]
  python tools/context_probe.py write <repository> --digest <sha256> --out <file> [--terms <file>]

`preview` builds the profile in memory, checks it, scans it against the private-term list, and prints the whole of it
with a last line `DIGEST <sha256>`. `write` builds and checks it again and writes it to `--out` only when its digest is
the one the preview printed, so no profile is written that the user has not read in full; one that differs prints
`CHANGED <sha256>`, the digest it has now, and writes nothing.

The profile is allowlisted. Every string in it, key or value, is one of this module's own constants or a closed
vocabulary the code-review core defines, except the probe's own commit, a hexadecimal object name of this checkout.
Every number is rounded down to a power of two. `allowlisted` refuses any other string before the scan runs, so a field
that echoed the repository would fail closed rather than reach the output. It never holds file contents, a file or
folder name the repository chose, a path, a repository, organization, branch, or person's name, a commit message, a
review's text, or a finding category the repository configured. docs/context-probe.md says what each field holds.

The private-term list, `~/.coding-agent-skills/private-terms.txt` unless `--terms` names another, holds one term per
line, kept outside every repository. Each field's key and value is matched against every term as the release
procedure's scan matches the tree: as a fixed string, ignoring case, and as a whole word. A list that is missing or
cannot be read stops the run, since the scan never runs without one. A match prints `PRIVATE_TERM <field> key` or
`PRIVATE_TERM <field> value`, naming the field by the keys above it that matched no term, never the term, then
`STOPPED private term`, and nothing of the profile is printed or written.

A FAILED line never quotes what was read: a failure while the repository or the review records are read names the
part and the error's kind alone, one examining the repository argument names the error's kind, and any other failure
ends as `FAILED <kind>`, which run_main prints without the error's text. The paths a FAILED
line may name are the private-term list's and `--out`'s, which the user gives, and git's reason for this checkout's
own commit.

The repository is read without running anything it holds: a walk of its files, the size and the Markdown structure of
its rule sources, and the hook events its `.claude/settings.json` declares. The review archive and the flag store are
the ones the code-review configuration names (`CODE_REVIEW_CONFIG` and `CODE_REVIEW_FLAGS` choose others), read for the
configured repository whose checkout holds `<repository>`, as review-insights finds it, through `GitClient`. The
profile records this checkout's commit, read through git; a git that fails there stops the run with git's reason.

Exit status:
  0  preview: the profile and its DIGEST line; write: WROTE <file>
  1  PRIVATE_TERM lines and STOPPED private term; CHANGED <sha256>; or a last line FAILED <reason>
  2  a usage error
"""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-insights")))
sys.path.insert(0, str(sibling_scripts("audit-ai-config")))

import atomic_file
import file_presence
import folder_walk
import json_reader
import json_writer
from audit_ai_config import CLAUDE_HOOKS
from console import failed_kind, run_main
from flat_text import flat_text
from git_client import GitClient, GitError
from markdown_sections import document_headings, find_fences
from path_containment import resolved_path
from review_archive import record_files
from review_config import ConfigurationError, default_config_path, load_config
from review_flags import FlagError, default_flags_path, load_store
from review_insights import InsightError, Services, current_repository
from review_io import PersistenceError
from review_records import (
    ANALYZER_COVERAGES,
    DIFF_SOURCES,
    RE_REVIEW_SCOPES,
    SNAPSHOT_SOURCES,
    RecordError,
    ReviewRecord,
    validate_record_pair,
)
from review_vocabularies import DISPATCH_MODES, DISPOSITIONS, LEDGER_STATE_NAMES, SEVERITIES, VERDICTS
from text_lines import text_lines
from typed_fields import FieldError

CHECKOUT = Path(__file__).resolve().parents[1]
# The profile's own layout version, raised when a field changes meaning.
SCHEMA = 1
DEFAULT_TERMS = Path("~/.coding-agent-skills/private-terms.txt")
# The most folders the walk lists, so a vast tree still ends; past it the walk reports the folder it stopped at.
WALK_LIMIT = 50_000
# The most bytes of one rule source read for its lines, headings, and fences; its size is counted whole.
READ_LIMIT = 1 << 20
# A full object name in a SHA-1 or a SHA-256 repository, the one string the allowlist takes that is no constant.
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
UNKNOWN = "unknown"
OTHER = "other"

# Each language, by the lowercase suffixes that name its files. A suffix in no entry is counted as `other`.
LANGUAGES: dict[str, tuple[str, ...]] = {
    "Python": (".py", ".pyi", ".pyw"),
    "JavaScript": (".js", ".mjs", ".cjs", ".jsx"),
    "TypeScript": (".ts", ".mts", ".cts", ".tsx"),
    "CSharp": (".cs", ".csx"),
    "FSharp": (".fs", ".fsi", ".fsx"),
    "VisualBasic": (".vb",),
    "Razor": (".razor", ".cshtml", ".vbhtml"),
    "XAML": (".xaml",),
    "Java": (".java",),
    "Kotlin": (".kt", ".kts"),
    "Scala": (".scala", ".sc"),
    "Groovy": (".groovy", ".gradle"),
    "Go": (".go",),
    "Rust": (".rs",),
    "C": (".c", ".h"),
    "CPlusPlus": (".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx"),
    "ObjectiveC": (".m", ".mm"),
    "Swift": (".swift",),
    "Ruby": (".rb", ".erb"),
    "PHP": (".php",),
    "Perl": (".pl", ".pm"),
    "Lua": (".lua",),
    "R": (".r",),
    "Dart": (".dart",),
    "Elixir": (".ex", ".exs"),
    "Erlang": (".erl", ".hrl"),
    "Haskell": (".hs",),
    "Clojure": (".clj", ".cljs", ".cljc"),
    "Shell": (".sh", ".bash", ".zsh", ".ksh"),
    "PowerShell": (".ps1", ".psm1", ".psd1"),
    "Batch": (".bat", ".cmd"),
    "SQL": (".sql",),
    "Terraform": (".tf", ".tfvars", ".hcl"),
    "Bicep": (".bicep",),
    "Protobuf": (".proto",),
    "GraphQL": (".graphql", ".gql"),
    "HTML": (".html", ".htm"),
    "CSS": (".css", ".scss", ".sass", ".less"),
    "Vue": (".vue",),
    "Svelte": (".svelte",),
    "Markdown": (".md", ".markdown", ".mdx"),
    "reStructuredText": (".rst",),
    "Text": (".txt",),
    "JSON": (".json", ".jsonc"),
    "YAML": (".yml", ".yaml"),
    "TOML": (".toml",),
    "XML": (".xml", ".props", ".targets", ".config", ".resx"),
    "Notebook": (".ipynb",),
    "Image": (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".bmp"),
    "Archive": (".zip", ".tar", ".gz", ".tgz", ".7z", ".jar", ".nupkg", ".whl"),
    "Document": (".pdf", ".docx", ".xlsx", ".pptx"),
}
LANGUAGE_OF: dict[str, str] = {suffix: language for language, suffixes in LANGUAGES.items() for suffix in suffixes}

# The files that mark a build system, a package manager, or continuous integration, by exact name.
BUILD_NAMES = frozenset(
    {
        "package.json",
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "bun.lockb",
        "deno.json",
        "tsconfig.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "uv.lock",
        "tox.ini",
        "noxfile.py",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "CMakeLists.txt",
        "Makefile",
        "meson.build",
        "BUILD.bazel",
        "MODULE.bazel",
        "WORKSPACE",
        "Gemfile",
        "composer.json",
        "mix.exs",
        "Package.swift",
        "pubspec.yaml",
        "Directory.Build.props",
        "Directory.Packages.props",
        "global.json",
        "nuget.config",
        "Dockerfile",
        "docker-compose.yml",
        "compose.yaml",
        "Justfile",
        "Taskfile.yml",
        "Jenkinsfile",
        "azure-pipelines.yml",
        ".gitlab-ci.yml",
        ".pre-commit-config.yaml",
    }
)
# The build files named by their suffix, each counted under the key `*<suffix>`.
BUILD_SUFFIXES = (".csproj", ".fsproj", ".vbproj", ".sln", ".slnx")
GITHUB_WORKFLOWS = "github-workflows"

# The rule sources an agent runtime reads, each by the key the profile counts it under.
RULE_SOURCES = (
    "CLAUDE.md",
    "AGENTS.md",
    "copilot-instructions",
    "copilot-path-instructions",
    "claude-rules",
    "claude-commands",
    "claude-agents",
    "github-agents",
    "claude-skills",
    "agents-skills",
    "github-skills",
    "claude-settings",
    "mcp",
)
# The rule sources a folder holds as Markdown files at any depth, by the folder's two names.
RULE_FOLDERS: dict[tuple[str, ...], str] = {
    (".claude", "rules"): "claude-rules",
    (".claude", "commands"): "claude-commands",
    (".claude", "agents"): "claude-agents",
    (".github", "agents"): "github-agents",
}
# The measures counted for each rule source.
MEASURES = ("files", "bytes", "lines", "headings", "fences", "unread")
# How the walk left a folder or an entry unsearched, as folder_walk names each kind.
WALK_KINDS = (
    folder_walk.LINKED,
    folder_walk.LOOP,
    folder_walk.NESTED_CHECKOUT,
    folder_walk.UNLISTABLE,
    folder_walk.UNEXAMINED,
    folder_walk.BOUNDED,
)
# Why a profile holds no review counts.
REVIEWS_ABSENT = ("no_configuration", "configuration_unreadable", "checkout_unreadable", "not_configured")
FLAG_STATUSES = ("open", "resolved")
HOOK_MEASURES = ("events", "handlers", "unread")
REVIEW_TOTALS = (
    "records",
    "unreadable",
    "pulls",
    "findings",
    "finding_categories",
    "reviewers",
    "reviewer_retries",
    "dispositions_only",
    "unavailable_sources",
    "uncovered_files",
    "unreviewed_files",
    "excluded_files",
    "comments",
)
REVIEW_MODES = ("initial", "re-review")
SCOPE_USED = ("full", "incremental")
# Each review count by a closed vocabulary: its key and its members.
REVIEW_VOCABULARIES: dict[str, tuple[str, ...]] = {
    "verdicts": VERDICTS,
    "severities": SEVERITIES,
    "modes": REVIEW_MODES,
    "dispatch": DISPATCH_MODES,
    "dispositions": DISPOSITIONS,
    "comment_dispositions": DISPOSITIONS,
    "ledger_states": LEDGER_STATE_NAMES,
    "snapshot_sources": SNAPSHOT_SOURCES,
    "diff_fallbacks": tuple(DIFF_SOURCES),
    "scope_requested": RE_REVIEW_SCOPES,
    "scope_used": SCOPE_USED,
    "analyzer_coverages": ANALYZER_COVERAGES,
}
SECTIONS = (
    "probe",
    "schema",
    "commit",
    "files",
    "languages",
    "build_systems",
    "walk",
    "rule_sources",
    "hooks",
    "hook_events",
    "reviews",
    "reviews_absent",
    "totals",
    "unreviewed_languages",
    "flags",
    "absent",
)


def _allowed_words() -> frozenset[str]:
    words: set[str] = {UNKNOWN, OTHER, GITHUB_WORKFLOWS, *SECTIONS, *LANGUAGES, *BUILD_NAMES, *RULE_SOURCES}
    words |= {f"*{suffix}" for suffix in BUILD_SUFFIXES}
    words |= {*MEASURES, *WALK_KINDS, *REVIEWS_ABSENT, *FLAG_STATUSES, *HOOK_MEASURES, *REVIEW_TOTALS}
    words |= set(CLAUDE_HOOKS.events)
    for key, members in REVIEW_VOCABULARIES.items():
        words |= {key, *members}
    return frozenset(words)


ALLOWED_WORDS = _allowed_words()


class ProbeError(RuntimeError):
    """A run that cannot finish, with the reason its FAILED line gives."""


def bucket(count: int) -> int:
    """`count` rounded down to a power of two, 0 for none, so a profile's numbers do not fingerprint a repository."""
    return 0 if count <= 0 else 1 << (count.bit_length() - 1)


def bucketed(counts: Mapping[str, int]) -> dict[str, int]:
    return {key: bucket(value) for key, value in sorted(counts.items()) if value > 0}


def stray_strings(value: object, place: str = "profile") -> list[str]:
    """The field of every string in `value`, key or value, that is not an allowed word, and of every value that is
    neither a string, a whole number, nor a mapping, each named by its place, never by the string."""
    if isinstance(value, Mapping):
        found: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str) or key not in ALLOWED_WORDS:
                found.append(f"{place} key")
                continue
            if place == "profile.probe" and key == "commit" and isinstance(item, str):
                if item != UNKNOWN and not COMMIT.fullmatch(item):
                    found.append(f"{place}.commit")
                continue
            found.extend(stray_strings(item, f"{place}.{key}"))
        return found
    if isinstance(value, bool) or not isinstance(value, int | str):
        return [place]
    if isinstance(value, str) and value not in ALLOWED_WORDS:
        return [place]
    return []


def allowlisted(profile: Mapping[str, Any]) -> None:
    """Refuse a profile holding anything but the probe's own words and whole numbers."""
    stray = stray_strings(profile)
    if stray:
        raise ProbeError(f"the profile holds a value no allowlist names, at {', '.join(stray)}")


def read_terms(path: Path) -> list[str]:
    """The private terms, one a line, blank lines aside. A list that is missing or unreadable stops the run."""
    if not file_presence.is_there(path):
        raise ProbeError(f"no private-term list at {path}; the profile is never shown without its scan")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ProbeError(f"the private-term list at {path} cannot be read: {type(error).__name__}") from error
    terms = [line.strip() for line in text_lines(text)]
    return [term for term in terms if term]


def _term_pattern(terms: Sequence[str]) -> re.Pattern[str] | None:
    """One pattern matching any term as a whole word, ignoring case, as `git grep -i -w -F` does."""
    if not terms:
        return None
    alternatives = "|".join(re.escape(term) for term in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<![0-9A-Za-z_])(?:{alternatives})(?![0-9A-Za-z_])", re.IGNORECASE)


def private_hits(profile: Mapping[str, Any], terms: Sequence[str]) -> list[str]:
    """`<field> key` or `<field> value` for each key or value holding a term. A field is named by the keys above it,
    each of which matched no term, so no line repeats a term."""
    pattern = _term_pattern(terms)
    if pattern is None:
        return []
    hits: list[str] = []

    def scan(value: object, place: str) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if pattern.search(str(key)):
                    hits.append(f"{place} key")
                else:
                    scan(item, f"{place}.{key}")
        elif pattern.search(str(value)):
            hits.append(f"{place} value")

    scan(profile, "profile")
    return hits


def canonical(profile: Mapping[str, Any]) -> str:
    return json_writer.dumps(profile, sort_keys=True, separators=(",", ":"))


def digest(profile: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical(profile).encode("utf-8")).hexdigest()


def shown(profile: Mapping[str, Any]) -> str:
    """The profile as preview prints it and write writes it."""
    return json_writer.dumps(profile, indent=2, sort_keys=True) + "\n"


def regular_size(path: Path) -> int | None:
    """The size of `path` when it is a regular file and no link, else None, so no link is followed out of the tree
    and no device or pipe is opened. A path it cannot examine raises the OSError its lstat raised, for the caller to
    count as unread, never as nothing there."""
    status = path.lstat()
    return status.st_size if stat.S_ISREG(status.st_mode) else None


def rule_source(parts: tuple[str, ...]) -> str | None:
    """The rule source a file is, by its path below the repository, or None."""
    name = parts[-1]
    if name in ("CLAUDE.md", "CLAUDE.local.md"):
        return "CLAUDE.md"
    if name in ("AGENTS.md", "AGENTS.override.md"):
        return "AGENTS.md"
    if parts == (".claude", "settings.json"):
        return "claude-settings"
    if parts == (".mcp.json",):
        return "mcp"
    if parts == (".github", "copilot-instructions.md"):
        return "copilot-instructions"
    if parts[:2] == (".github", "instructions") and name.endswith(".instructions.md"):
        return "copilot-path-instructions"
    if len(parts) == 4 and parts[1] == "skills" and name == "SKILL.md":
        return {".claude": "claude-skills", ".agents": "agents-skills", ".github": "github-skills"}.get(parts[0])
    if len(parts) >= 3 and name.endswith(".md"):
        return RULE_FOLDERS.get(parts[:2])
    return None


def build_system(parts: tuple[str, ...]) -> str | None:
    name = parts[-1]
    if name in BUILD_NAMES:
        return name
    lowered = name.lower()
    for suffix in BUILD_SUFFIXES:
        if lowered.endswith(suffix):
            return f"*{suffix}"
    if parts[:2] == (".github", "workflows") and lowered.endswith((".yml", ".yaml")):
        return GITHUB_WORKFLOWS
    return None


def language(name: str) -> str:
    return LANGUAGE_OF.get(PurePosixPath(name).suffix.lower(), OTHER)


@dataclass
class Shape:
    """What the walk counts, before rounding."""

    files: int = 0
    languages: Counter[str] = field(default_factory=Counter)
    build_systems: Counter[str] = field(default_factory=Counter)
    walk: Counter[str] = field(default_factory=Counter)
    rule_sources: dict[str, Counter[str]] = field(default_factory=dict)


def measure_rule_source(path: Path, counts: Counter[str]) -> None:
    """Add a rule source's size, lines, headings, and fences to `counts`; one that is no regular file is unread."""
    counts["files"] += 1
    try:
        size = regular_size(path)
        if size is None:
            counts["unread"] += 1
            return
        with path.open("rb") as stream:
            data = stream.read(READ_LIMIT)
    except OSError:
        counts["unread"] += 1
        return
    counts["bytes"] += size
    lines = text_lines(data.decode("utf-8", errors="replace"))
    counts["lines"] += len(lines)
    if path.suffix.lower() == ".md":
        counts["headings"] += len(document_headings(lines))
        counts["fences"] += len(find_fences(lines))


def walk_shape(root: Path) -> Shape:
    shape = Shape()
    walk = folder_walk.walk_files(root, lambda _name: True, limit=WALK_LIMIT)
    for item in walk.not_entered:
        shape.walk[item.kind] += 1
    for path in walk.files:
        parts = path.relative_to(root).parts
        shape.files += 1
        shape.languages[language(parts[-1])] += 1
        system = build_system(parts)
        if system is not None:
            shape.build_systems[system] += 1
        source = rule_source(parts)
        if source is not None:
            measure_rule_source(path, shape.rule_sources.setdefault(source, Counter()))
    return shape


def hook_counts(root: Path) -> dict[str, Any]:
    """The hook events `.claude/settings.json` declares, each by its name when Claude Code documents it, else as
    `other`, and the handlers they hold; no matcher, command, or condition."""
    settings = root / ".claude" / "settings.json"
    try:
        if not file_presence.is_occupied(settings):
            return {}
        if regular_size(settings) is None:
            return {"unread": 1}
        data = json_reader.read_file(settings)
    except OSError:
        return {"unread": 1}
    except json_reader.CONTENT_ERRORS:
        return {"unread": 1}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return {}
    events: Counter[str] = Counter()
    handlers = 0
    for event, groups in hooks.items():
        events[event if event in CLAUDE_HOOKS.events else OTHER] += 1
        for group in groups if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            handlers += len(inner) if isinstance(inner, list) else 0
    return {"events": bucket(sum(events.values())), "handlers": bucket(handlers), "hook_events": bucketed(events)}


@dataclass
class ReviewTally:
    totals: Counter[str] = field(default_factory=Counter)
    vocabularies: dict[str, Counter[str]] = field(default_factory=dict)
    unreviewed_languages: Counter[str] = field(default_factory=Counter)
    categories: set[str] = field(default_factory=set)

    def count(self, vocabulary: str, value: object) -> None:
        members = REVIEW_VOCABULARIES[vocabulary]
        key = value if isinstance(value, str) and value in members else OTHER
        self.vocabularies.setdefault(vocabulary, Counter())[key] += 1

    def add(self, record: ReviewRecord) -> None:
        review = record["review"]
        self.totals["records"] += 1
        self.count("verdicts", review["verdict"])
        self.count("modes", review["mode"])
        self.count("dispatch", review.get("dispatch", UNKNOWN))
        for finding in record["findings"]:
            self.totals["findings"] += 1
            self.count("severities", finding["severity"])
            self.categories.add(finding["category"])
            analyzer = finding.get("analyzer")
            if analyzer is not None:
                self.count("analyzer_coverages", analyzer["coverage"])
        for disposition in record["prior_dispositions"]:
            self.count("dispositions", disposition["disposition"])
        for comment in record.get("comment_dispositions", []):
            self.count("comment_dispositions", comment["disposition"])
        self.totals["comments"] += len(record.get("github_comments", []))
        for entry in record.get("ledger", []):
            self.count("ledger_states", entry["state"])
        self._add_runs(record)

    def _add_runs(self, record: ReviewRecord) -> None:
        review = record["review"]
        for run in review.get("reviewers", []):
            self.totals["reviewers"] += 1
            self.totals["reviewer_retries"] += run["retries"]
            self.totals["dispositions_only"] += int(run["dispositions_only"])
        coverage = review.get("coverage")
        if coverage is not None:
            self.totals["unavailable_sources"] += len(coverage["unavailable_sources"])
            self.totals["uncovered_files"] += len(coverage.get("uncovered_files", []))
            unreviewed = coverage.get("unreviewed_files", {})
            self.totals["unreviewed_files"] += len(unreviewed)
            for path in unreviewed:
                self.unreviewed_languages[language(path)] += 1
        snapshot = review.get("snapshot")
        if snapshot is not None:
            self.count("snapshot_sources", snapshot["source"])
            self.totals["excluded_files"] += sum(snapshot.get("excluded", {}).values())
            diff = snapshot.get("diff")
            if diff is not None:
                self.count("diff_fallbacks", diff["source"])
        scope = review.get("scope")
        if scope is not None:
            self.count("scope_requested", scope["requested"])
            self.count("scope_used", scope["used"])

    def profile(self, pulls: int) -> dict[str, Any]:
        totals = Counter(self.totals)
        totals["pulls"] = pulls
        totals["finding_categories"] = len(self.categories)
        section: dict[str, Any] = {"totals": bucketed(totals)}
        for vocabulary, counts in sorted(self.vocabularies.items()):
            section[vocabulary] = bucketed(counts)
        if self.unreviewed_languages:
            section["unreviewed_languages"] = bucketed(self.unreviewed_languages)
        return section


def review_counts(root: Path, config_path: Path, flags_path: Path) -> dict[str, Any]:
    """The `reviews` section: counts from the archive's records and the flag store's flags for the configured
    repository whose checkout holds `root`, or `{"absent": <why>}`."""
    if not file_presence.is_there(config_path):
        return {"absent": "no_configuration"}
    try:
        config = load_config(config_path)
    except (ConfigurationError, OSError):
        return {"absent": "configuration_unreadable"}
    try:
        repository = current_repository(config, Services(cwd=lambda: root))
    except (InsightError, GitError, OSError):
        return {"absent": "checkout_unreadable"}
    if repository is None:
        return {"absent": "not_configured"}
    tally = ReviewTally()
    try:
        paths = record_files(Path(config["archive_root"]), repository)
    except OSError:
        paths = []
        tally.totals["unreadable"] += 1
    for json_path in paths:
        try:
            tally.add(validate_record_pair(json_path, json_path.with_suffix(".md")))
        except (RecordError, PersistenceError, FieldError, OSError, ValueError, KeyError):
            tally.totals["unreadable"] += 1
    section = tally.profile(len({path.parent for path in paths}))
    section["flags"] = flag_counts(flags_path, repository)
    return section


def flag_counts(path: Path, repository: str) -> dict[str, int]:
    try:
        store = load_store(path)
    except (FlagError, PersistenceError, FieldError, OSError):
        return {"unread": 1}
    statuses: Counter[str] = Counter(
        flag["status"]
        for flag in store["flags"]
        if isinstance(flag.get("repository"), str) and flag["repository"].casefold() == repository.casefold()
    )
    return bucketed(statuses)


def probe_commit(services: ProbeServices) -> str:
    """This checkout's commit, so a reader knows what produced a profile, or `unknown` when git answers that it has
    none. A git that fails stops the run, naming git's reason, which concerns this checkout alone."""
    try:
        commit = services.commit()
    except GitError as error:
        raise ProbeError(f"git could not read this checkout's commit: {flat_text(str(error))}") from error
    return commit if commit is not None and COMMIT.fullmatch(commit) else UNKNOWN


@dataclass
class ProbeServices:
    """External effects, replaceable in tests."""

    commit: Callable[[], str | None] = lambda: GitClient().commit_of("HEAD", directory=CHECKOUT)
    config_path: Callable[[], Path] = default_config_path
    flags_path: Callable[[], Path] = default_flags_path


# What a reader of files, JSON, git, the configuration, the records, or the flag store raises: OSError for a path, and
# the families every reader here derives its own errors from (ValueError for content, RuntimeError for git and the
# review core's persistence), with the lookups and types a malformed value trips. An error of any other kind is a
# defect in the probe itself, which reaches run_main, whose FAILED line names its kind alone (failed_kind).
READ_FAILURES: tuple[type[Exception], ...] = (
    OSError,
    ValueError,
    RuntimeError,
    LookupError,
    TypeError,
    AttributeError,
    ArithmeticError,
)


@contextmanager
def reading(part: str) -> Iterator[None]:
    """Turn any failure while `part` of the examined repository or the review records is read into a ProbeError that
    names the part and the error's kind alone: an error's text can quote a path, a ref, or content from what was
    read, which may be private, so it never reaches a FAILED line."""
    try:
        yield
    except ProbeError:
        raise
    except READ_FAILURES as error:
        raise ProbeError(f"the {part} could not be read: {type(error).__name__}") from None


def build_profile(root: Path, services: ProbeServices) -> dict[str, Any]:
    with reading("repository's files"):
        shape = walk_shape(root)
    rule_sources = {
        source: {measure: bucket(counts[measure]) for measure in MEASURES if counts[measure] > 0}
        for source, counts in sorted(shape.rule_sources.items())
    }
    profile: dict[str, Any] = {
        "probe": {"schema": SCHEMA, "commit": probe_commit(services)},
        "files": bucket(shape.files),
        "languages": bucketed(shape.languages),
        "build_systems": bucketed(shape.build_systems),
        "walk": bucketed(shape.walk),
        "rule_sources": rule_sources,
    }
    with reading("repository's hook settings"):
        profile["hooks"] = hook_counts(root)
    with reading("review records"):
        reviews = review_counts(root, services.config_path(), services.flags_path())
    if "absent" in reviews:
        profile["reviews_absent"] = reviews["absent"]
    else:
        profile["reviews"] = reviews
    return profile


def checked_profile(root: Path, terms: Sequence[str], services: ProbeServices) -> tuple[dict[str, Any], list[str]]:
    """The profile and the private-term hits in it, the allowlist checked first."""
    profile = build_profile(root, services)
    allowlisted(profile)
    return profile, private_hits(profile, terms)


def repository_folder(value: str) -> Path:
    """The repository argument, for argparse's type: a folder, with every link and `..` resolved."""
    path = Path(value)
    try:
        if not file_presence.is_directory(path):
            raise argparse.ArgumentTypeError("the repository must be a folder")
    except OSError as error:
        # The error's text names the path, which is the private repository's own.
        raise argparse.ArgumentTypeError(f"the repository cannot be examined: {type(error).__name__}") from None
    return resolved_path(path)


def parser() -> argparse.ArgumentParser:
    parse = argparse.ArgumentParser(prog="context_probe.py", description=__doc__.split("\n\n")[0])
    commands = parse.add_subparsers(dest="command", required=True)
    for name in ("preview", "write"):
        command = commands.add_parser(name)
        command.add_argument("repository", type=repository_folder)
        command.add_argument("--terms", type=Path, default=None, help="the private-term list, one term a line")
        if name == "write":
            command.add_argument("--digest", required=True, help="the DIGEST the preview printed")
            command.add_argument("--out", type=Path, required=True, help="the file to write the profile to")
    return parse


def lines(arguments: argparse.Namespace, services: ProbeServices) -> Iterator[str]:
    terms = read_terms((arguments.terms or DEFAULT_TERMS).expanduser())
    profile, hits = checked_profile(arguments.repository, terms, services)
    if hits:
        yield from (f"PRIVATE_TERM {hit}" for hit in hits)
        yield "STOPPED private term"
        return
    found = digest(profile)
    if arguments.command == "preview":
        yield shown(profile).rstrip("\n")
        yield f"DIGEST {found}"
        return
    if found != arguments.digest.strip().lower():
        yield f"CHANGED {found}"
        return
    atomic_file.replace_text(arguments.out, shown(profile))
    yield f"WROTE {arguments.out}"


def main(argv: Sequence[str] | None = None, services: ProbeServices | None = None) -> int:
    arguments = parser().parse_args(argv)
    code = 0
    try:
        for line in lines(arguments, services or ProbeServices()):
            if line.startswith(("PRIVATE_TERM ", "STOPPED ", "CHANGED ")):
                code = 1
            print(line, flush=True)
    except (ProbeError, OSError, json_writer.JsonWriteRefused) as error:
        print(f"FAILED {error}", flush=True)
        return 1
    return code


if __name__ == "__main__":
    # A failure no reader's handler expects is named by its kind alone, since its text may quote what was read.
    run_main(main, failure=failed_kind)
