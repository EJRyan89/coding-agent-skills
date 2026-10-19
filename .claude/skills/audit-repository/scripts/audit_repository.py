"""The deterministic parts of this repository's audits after each release and before it, so the agent only starts
the readers, confirms what they find, and files it.

    release        group the files changed since the last release HEAD holds, the highest release tag reachable
                   from it as ejryan89-skill-core's release_tags.py defines one (or --since REF), into the four areas
                   and write the briefs for each area that changed, for the release audit before tagging
    rotation       name the area the rotation record shows read whole least recently and write its whole-area briefs,
                   naming in each the open issues the next milestone carries, for the rotation audit after tagging;
                   with --record TAG, append that area to the record for TAG instead, which is the only write to the
                   repository and is refused in the main checkout, so it lands through a worktree's pull request: it
                   writes a copy beside the record and replaces the record with it, so a write that fails leaves the
                   record as it was
    guidance       write one brief per runtime the deployer supports and for Python, naming the pages of its published
                   guidance the guidance record lists, the files of this repository that state facts about it, and
                   the questions a reader answers for each page, for the guidance audit after tagging; refused while
                   the record names no page for a runtime deployer/runtime_support.py lists. With --record TAG,
                   append TAG to every page's reads instead, from a worktree as rotation --record does
    duplicates     search the open issues for each QUERY, so a finding already filed is not filed twice
    remove-briefs  delete a briefs directory a release or rotation run printed as BRIEFS, and nothing else: one
                   holding the BRIEFS_MARKER file the run wrote before its first brief, naming the directory

An area holding more than MAX_FILES_PER_READER files is split between readers by directory: one brief per part, no
part above that size, and a directory split only when it alone holds more than a part. A release or rotation run
reads the open milestones and, for rotation, the next milestone's open issues through gh, from the checkout audited.

Every line is one fact:

    BASE <ref> <commit>                release: where the diff starts
    HEAD <commit>                      the commit the briefs describe
    CHANGED <count>                    release: the files changed since the base
    AREA <area> <count>                release: the files changed in each area, every area listed
    RECORD "<file>"                    rotation: the rotation record read, from the top of the repository
    READ <area> <tag> <date>           rotation: the last audit that read each area whole
    UNREAD <area>                      rotation: an area no recorded audit has read whole
    NEXT <area>                        rotation: the area to read whole now
    FILES <count>                      rotation: the files in that area at HEAD
    RECORDED <tag> <area> <date>       rotation --record: the entry appended to the record
    GUIDANCE_RECORD "<file>"           guidance: the guidance record read, from the top of the repository
    SUBJECT <subject> <pages> <tag>    guidance: a runtime, or python, with its pages and the oldest of their last
                                       reads, or never when a page has none
    RECORDED_PAGES <tag> <count>       guidance --record: the pages the tag was appended to
    MILESTONE <severity> "<title>"     the milestone a finding of each severity is filed into, or none when no open
                                       milestone is that one
    CARRIED <number> "<title>"         rotation: an open issue of the next milestone, also named in every brief
    PROVENANCE "<sentence>"            the sentence that opens each filed issue's body: the audit, its scope, its date
    DRIFT <kind> <what it is>          each kind of drift the briefs ask the readers to look for, which for guidance
                                       are the guidance kinds
    TEMPLATE <field> <what it holds>   each field of a finding the briefs ask for
    SECTION <name> <what it holds>     each section of the report, in order
    READERS <area> <count>             the briefs, one per reader, an area's files are split into
    BRIEFS "<directory>"               the new directory holding the briefs
    BRIEF <name> "<file>"              one brief, the whole prompt for one read-only reader: <area> for a whole
                                       area, <area>-<part> for one part of it, <subject> for a guidance brief
    MATCHES <query> <count>            duplicates: how many open issues the query numbered <query>, from 1, found
    MATCH <query> <number> "<title>"   duplicates: one of them
    REMOVED "<directory>"              remove-briefs: the briefs directory deleted

A title is printed as a JSON string, flattened to one line. A git or gh command that fails is reported with its own
reason; a ref is reported as naming no commit only when git finds none, and a repository with no release reachable
from HEAD only when git lists no release tag reaching it.

Exit status 0 means the briefs were written, the entry or the reads recorded, the search run, or the directory
deleted; 1 a last line FAILED <reason>; 2 a usage error.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "ejryan89-skill-core" / "scripts"))

from atomic_file import UnfinishedCopyError, replace_text
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError, classify_failure
from github_client import GitHubClient, GitHubError, subprocess_runner
from release_tags import last_release
from typed_fields import FieldError, FieldReader

# The rotation record, relative to the top of the repository it describes.
RECORD_PATH = ".claude/skills/audit-repository/references/rotation.json"
DATE_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")
# A release tag or milestone title: vMAJOR.MINOR.PATCH.
VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
# The most files one reader is given; a larger area is split between readers by directory.
MAX_FILES_PER_READER = 60
# Every briefs directory starts with this, directly in the temporary directory; remove-briefs deletes no other.
BRIEFS_PREFIX = "audit-repository-"
# The file a run writes in its briefs directory before any brief, naming the directory; remove-briefs deletes only a
# directory holding it, so a directory another program named with the prefix is left alone.
BRIEFS_MARKER = ".audit-repository-briefs.json"
# The guidance record, relative to the top of the repository it describes.
GUIDANCE_PATH = ".claude/skills/audit-repository/references/guidance.json"
# Where the deployer catalogues the runtimes it supports, read as literals so the audit runs no deployer code.
RUNTIME_CATALOGUE = "deployer/runtime_support.py"
PYTHON_SUBJECT = "python"
PYTHON_TITLE = "Python"
PAGE_SCHEME = "https://"
# The most open issues one duplicate search lists, and one milestone's open issues the rotation brief names.
SEARCH_LIMIT = 30
CARRIED_LIMIT = 1000


@dataclass(frozen=True)
class Area:
    """One audit area: the files it owns and what a reader consults beside them.

    A path ending in "/" owns everything below it; any other path owns that one file. An area with no paths owns
    every file no other area does.
    """

    name: str
    title: str
    paths: tuple[str, ...]
    consult: tuple[str, ...]


DEPLOYER = Area(
    "deployer",
    "the deployer",
    ("deploy.py", "deployer/", "deploy-meta/", "agents/", "source.json"),
    (
        "CLAUDE.md",
        "SECURITY.md",
        "docs/installation.md",
        "docs/recovery.md",
        "docs/releasing.md",
        "docs/upgrade-notes.md",
        "tests/deployer/",
    ),
)
CODE_REVIEW = Area(
    "code-review",
    "the code review skills",
    (
        "skills/ejryan89-code-review-core/",
        "skills/review-prs/",
        "skills/flag-review-finding/",
        "tools/skill_evals.py",
        "docs/code-review-operations.md",
        "docs/code-review-operations-contract.md",
    ),
    (
        "CLAUDE.md",
        "SECURITY.md",
        "docs/adding-a-skill.md",
        "docs/upgrade-notes.md",
        "agents/code-review-reviewer.md",
        "tests/tools/test_skill_evals.py",
    ),
)
OTHER_SKILLS = Area(
    "other-skills",
    "the other skills",
    ("skills/", "docs/skills.md"),
    (
        "CLAUDE.md",
        "SECURITY.md",
        "docs/adding-a-skill.md",
        "docs/upgrade-notes.md",
        "deploy-meta/",
        "deployer/tools.py",
    ),
)
INFRASTRUCTURE = Area(
    "infrastructure",
    "the repository's infrastructure and documents",
    (),
    ("CLAUDE.md", "SECURITY.md", "CONTRIBUTING.md", "docs/implementing-changes.md"),
)
# The rotation order, which also breaks a tie between areas read equally long ago.
AREAS = (DEPLOYER, CODE_REVIEW, OTHER_SKILLS, INFRASTRUCTURE)
# The order a file is matched in: the code review area owns files inside other areas' directories (tools/, docs/,
# skills/), and the infrastructure area takes whatever is left.
CLASSIFY_ORDER = (CODE_REVIEW, DEPLOYER, OTHER_SKILLS, INFRASTRUCTURE)
AREA_NAMES = tuple(area.name for area in AREAS)

DRIFT_KINDS = (
    ("contract", "a statement a contract or document makes that the code does not keep"),
    ("documents", "two documents that disagree"),
    ("untested-claim", "a claim no test holds"),
    ("grant", "a grant a skill declares and never uses, or a command it runs without one"),
    ("dead-code", "dead or duplicated code"),
    ("untested-change", "a changed behavior with no regression test"),
    ("error-path", "an error path that swallows a failure or misreports it"),
    ("trust-boundary", "anything an author controls that the threat model in SECURITY.md does not cover"),
)
# The severities a finding may have; milestone_lines names the milestone each is filed into.
SEVERITIES = ("defect", "drift", "polish")
FINDING_FIELDS = (
    ("location", "path:line of each side, with forward slashes"),
    ("kind", "one kind of drift"),
    ("evidence", "both sides quoted: what one says and what the other does"),
    ("severity", "defect (a user can hit it), drift (a statement and the code part ways), or polish"),
    ("title", "a one-line issue title"),
    ("template", "bug, documentation, or enhancement; advisory for a trust-boundary finding"),
)
# The report's sections, in order; Findings holds one entry of the fields above per finding.
REPORT_SECTIONS = (
    ("findings", "each finding confirmed against both sides, as the TEMPLATE fields"),
    ("unconfirmed", "each suspicion not confirmed against both sides, and why"),
    ("coverage", "each file: read whole, skimmed, or not read"),
)


# What a reader compares a runtime's pages with: each file of this repository that states facts about the runtime,
# and where in it. {runtime} is the runtime's name; a file the checkout does not hold is left out of the brief.
RUNTIME_FACTS = (
    ("docs/{runtime}-support.md", "the whole document"),
    ("deployer/runtime_support.py", "the runtime's capabilities and the comment that gives their reasons"),
    ("deploy-meta/", "each skill's runtime_support declaration for the runtime"),
    ("deployer/render.py", "how the runtime adapter is rendered for the runtime"),
    ("tools/runtime_canary.py", "the command line that starts the runtime"),
    (
        "skills/ejryan89-code-review-core/scripts/review_hosts.py",
        "the command line the review host starts the runtime with",
    ),
    ("docs/code-review-operations-contract.md", "the Threat model rows that name the runtime"),
    ("README.md", "the supported-runtimes table"),
    ("skills/audit-skill/references/provenance.json", "each check whose source is one of these pages"),
    ("skills/audit-ai-config/references/provenance.json", "each check whose source is one of these pages"),
)
PYTHON_FACTS = (
    ("pyproject.toml", "the Python version and the ruff, mypy, and pytest settings"),
    ("requirements-dev.txt", "the development dependencies and how they are pinned"),
    ("deployer/tools.py", "the Python floor and its reason"),
    ("CONTRIBUTING.md", "Python checks"),
    ("docs/dependency-updates.md", "how the pins are kept current"),
    ("conftest.py", "how the suites isolate git and their homes"),
)
# What a reader answers for each page of a guidance brief.
GUIDANCE_QUESTIONS = (
    "What does the page say now about what it governs? Fetch the live page and quote it; never answer from memory.",
    "What do the files of this repository listed above state about the same thing? Quote each statement with its line.",
    "Do they agree? A disagreement is a finding that names the file and line to change.",
    "Does each provenance entry that cites the page still hold? A passage the page no longer supports is a finding "
    "that names the check to change.",
    "Does the page give guidance this repository could follow and does not, for what the page governs?",
    "Has the page moved or gone? Give the address it moved to, which the guidance record takes when the audit is "
    "recorded.",
)
GUIDANCE_KINDS = (
    ("stale-fact", "a fact a file of this repository states about the runtime that the page no longer supports"),
    ("stale-check", "a provenance passage the page no longer supports, naming the check it governs"),
    ("unused-guidance", "guidance the page gives that this repository could follow and does not"),
    ("moved-page", "a page that redirects or is gone, with the address it moved to"),
)


class AuditError(Exception):
    """An expected failure: printed as FAILED with its reason."""


@dataclass(frozen=True)
class Change:
    status: str
    path: str


@dataclass(frozen=True)
class Entry:
    date: str
    tag: str
    area: str


def area_of(path: str) -> Area:
    """The area that owns a repository path, written with forward slashes."""
    for area in CLASSIFY_ORDER:
        if not area.paths or any(
            path == owned or (owned.endswith("/") and path.startswith(owned)) for owned in area.paths
        ):
            return area
    raise AssertionError("the infrastructure area owns every path")


# --- git ------------------------------------------------------------------------------------------


def git_failure(repository: Path, subcommand: str, exc: GitError) -> AuditError:
    if exc.kind == "not_repository":
        return AuditError(f"{repository.as_posix()} is not a Git repository")
    return AuditError(f"git {subcommand}: {exc}")


def git_output(git: GitClient, repository: Path, arguments: Sequence[str]) -> str:
    try:
        return git.output(arguments, directory=repository)
    except GitError as exc:
        raise git_failure(repository, arguments[0], exc) from exc


def commit_of(git: GitClient, repository: Path, ref: str) -> str:
    """The commit a ref names, or AuditError naming why there is none: the ref names no commit only when
    `rev-parse --verify --quiet` exits 1 and says nothing, which is how it reports one; any other failure is git's."""
    if ref.startswith("-"):
        raise AuditError(f"{ref} is not a ref")
    try:
        result = git.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], directory=repository)
    except GitError as exc:
        raise AuditError(f"git rev-parse: {exc}") from exc
    if result.returncode == 0:
        return result.stdout.strip()
    if classify_failure(result.stderr) == "not_repository":
        raise AuditError(f"{repository.as_posix()} is not a Git repository")
    if result.returncode == 1 and not result.stderr.strip():
        raise AuditError(f"{ref} names no commit in {repository.as_posix()}")
    reason = result.stderr.strip() or f"exit code {result.returncode}"
    raise AuditError(f"git rev-parse: {reason}")


def released_tag(git: GitClient, repository: Path, head: str) -> str | None:
    """The last release `head` holds, as ejryan89-skill-core's release_tags defines it, or None only when no release tag
    reaches it: a nearer tag of another name, such as a pre-release, is not the last release."""
    try:
        return last_release(git, repository, head)
    except GitError as exc:
        raise git_failure(repository, "for-each-ref", exc) from exc


def checkout_top(git: GitClient, repository: Path) -> tuple[Path, bool]:
    """The top of the checkout, and whether it is the main checkout: the one whose `.git` is the common directory
    every worktree of the repository shares, rather than a linked worktree."""
    output = git_output(
        git, repository, ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir"]
    ).splitlines()
    if len(output) != 2:
        raise AuditError(f"git rev-parse printed {len(output)} lines where it prints the top and the common directory")
    top, common = Path(output[0]), Path(output[1])
    return top, os.path.normcase(common.resolve()) == os.path.normcase((top / ".git").resolve())


def changes(git: GitClient, repository: Path, base: str, head: str) -> list[Change]:
    """Each file the diff from base to head adds (A), modifies (M), deletes (D), or changes the type of (T)."""
    fields = git_output(git, repository, ["diff", "--name-status", "--no-renames", "-z", base, head]).split("\0")
    return [Change(fields[index], fields[index + 1]) for index in range(0, len(fields) - 1, 2)]


def tracked_files(git: GitClient, repository: Path, head: str) -> list[str]:
    return [
        path for path in git_output(git, repository, ["ls-tree", "-r", "-z", "--name-only", head]).split("\0") if path
    ]


# --- GitHub ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Issue:
    number: int
    title: str


def gh_json(github: GitHubClient, arguments: Sequence[str]) -> Any:
    try:
        return github.json(arguments)
    except GitHubError as exc:
        raise AuditError(f"gh {arguments[0]} {arguments[1]}: {exc}") from exc


def issues_of(payload: Any, what: str) -> list[Issue]:
    """The issues a `gh issue list --json number,title` printed, or AuditError when it printed something else."""
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) and isinstance(item.get("number"), int) and isinstance(item.get("title"), str)
        for item in payload
    ):
        raise AuditError(f"gh issue list printed {what} that is not a list of issues with a number and a title")
    return [Issue(item["number"], item["title"]) for item in payload]


def version_of(name: str | None) -> tuple[int, int, int] | None:
    match = VERSION.fullmatch(name or "")
    return None if match is None else (int(match[1]), int(match[2]), int(match[3]))


def open_milestones_after(github: GitHubClient, released: str | None) -> list[str]:
    """The titles of the open milestones named for a version above the released tag's, lowest first; every one named
    for a version when no released tag is a version."""
    payload = gh_json(github, ["api", "repos/{owner}/{repo}/milestones?state=open&per_page=100"])
    if not isinstance(payload, list) or not all(
        isinstance(item, dict) and isinstance(item.get("title"), str) for item in payload
    ):
        raise AuditError("gh api printed milestones that are not a list of objects with a title")
    floor = version_of(released)
    named = []
    for item in payload:
        version = version_of(item["title"])
        if version is not None and (floor is None or version > floor):
            named.append((version, item["title"]))
    return [title for _, title in sorted(named)]


def carried_issues(github: GitHubClient, milestone: str) -> list[Issue]:
    arguments = ["issue", "list", "--state", "open", f"--milestone={milestone}", f"--limit={CARRIED_LIMIT}"]
    return issues_of(gh_json(github, [*arguments, "--json", "number,title"]), f"the open issues of {milestone}")


def json_text(text: str) -> str:
    """Text from GitHub on one line of output, flattened and written as a JSON string."""
    return json.dumps(flat_text(text), ensure_ascii=False)


def milestone_lines(release_milestone: str | None, next_milestone: str | None) -> list[str]:
    """Where each severity is filed: a defect into the release's milestone, and drift and polish into the next."""
    lines = []
    for severity in SEVERITIES:
        title = release_milestone if severity == "defect" else next_milestone
        lines.append(f"MILESTONE {severity} {json_text(title) if title is not None else 'none'}")
    return lines


# --- the rotation record --------------------------------------------------------------------------


def read_record(path: Path) -> list[Entry]:
    """The record's entries, oldest first, or AuditError naming what is wrong with it."""
    name = path.as_posix()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditError(f"no rotation record at {name}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AuditError(f"cannot read the rotation record {name}: {exc}") from exc
    audits = document.get("audits") if isinstance(document, dict) else None
    if not isinstance(audits, list):
        raise AuditError(f"{name} must be an object whose audits is a list")
    entries = [record_entry(name, number, item) for number, item in enumerate(audits, start=1)]
    tags = [entry.tag for entry in entries]
    repeated = sorted({tag for tag in tags if tags.count(tag) > 1})
    if repeated:
        raise AuditError(f"{name} records {', '.join(repeated)} more than once")
    return entries


def record_entry(name: str, number: int, item: object) -> Entry:
    if not isinstance(item, dict) or set(item) != {"date", "tag", "area"}:
        raise AuditError(f"{name} audit {number} must have exactly a date, a tag, and an area")
    when, tag, area = item["date"], item["tag"], item["area"]
    if not isinstance(when, str) or not DATE_FORMAT.fullmatch(when):
        raise AuditError(f"{name} audit {number} has a date that is not YYYY-MM-DD")
    if not isinstance(tag, str) or not tag.strip():
        raise AuditError(f"{name} audit {number} has no tag")
    if area not in AREA_NAMES:
        raise AuditError(f"{name} audit {number} names area {area!r}, not one of {', '.join(AREA_NAMES)}")
    return Entry(when, tag, area)


def last_reads(entries: list[Entry]) -> dict[str, tuple[int, Entry]]:
    """Each area's most recent whole read, with its position in the record."""
    return {entry.area: (position, entry) for position, entry in enumerate(entries)}


def next_area(entries: list[Entry]) -> Area:
    """The area read whole least recently: never read first, then oldest, ties in the rotation order."""
    reads = last_reads(entries)
    return min(AREAS, key=lambda area: (reads[area.name][0] if area.name in reads else -1, AREAS.index(area)))


def write_record(path: Path, entries: list[Entry]) -> None:
    """Replace the record with `entries` through a copy written beside it, so a write that fails leaves the record as
    it was and removes the copy. Raises OSError, naming the copy too when it cannot be removed."""
    write_json(path, {"audits": [{"date": entry.date, "tag": entry.tag, "area": entry.area} for entry in entries]})


def write_json(path: Path, document: object) -> None:
    """Replace a record with `document` through a copy written beside it, as write_record describes."""
    try:
        replace_text(path, json.dumps(document, indent=2, ensure_ascii=False) + "\n")
    except UnfinishedCopyError as exc:
        raise OSError(f"{exc.failure}; the unfinished copy {exc.copy.as_posix()} remains: {exc.removal}") from exc


# --- the guidance record --------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """One page of a runtime's, or Python's, published guidance, and the release tags at which it was read."""

    url: str
    subject: str
    governs: str
    read: tuple[str, ...]


READ = FieldReader()


def supported_runtimes(top: Path) -> dict[str, str]:
    """The runtimes the deployer supports, with their titles, read as the literal RUNTIMES and RUNTIME_TITLES of its
    catalogue in the checkout."""
    path = top / RUNTIME_CATALOGUE
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        raise AuditError(f"cannot read the runtime catalogue {path.as_posix()}: {exc}") from exc
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            with contextlib.suppress(ValueError):
                values[node.targets[0].id] = ast.literal_eval(node.value)
    runtimes, titles = values.get("RUNTIMES"), values.get("RUNTIME_TITLES")
    if (
        not isinstance(runtimes, tuple)
        or not runtimes
        or not isinstance(titles, dict)
        or not all(isinstance(runtime, str) and isinstance(titles.get(runtime), str) for runtime in runtimes)
    ):
        raise AuditError(f"{path.as_posix()} holds no literal RUNTIMES and RUNTIME_TITLES that title each runtime")
    return {runtime: titles[runtime] for runtime in runtimes}


def read_guidance(path: Path, subjects: Sequence[str]) -> list[Page]:
    """The guidance record's pages, each about one of `subjects`, or AuditError naming what is wrong with it."""
    name = path.as_posix()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AuditError(f"no guidance record at {name}") from exc
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise AuditError(f"cannot read the guidance record {name}: {exc}") from exc
    try:
        items = READ.json_object(document, "$", required={"pages"})["pages"]
        pages = READ.array(items, "$.pages", partial(guidance_page, subjects=subjects), nonempty=True)
    except FieldError as exc:
        raise AuditError(f"{name}: {exc}") from exc
    urls = [page.url for page in pages]
    repeated = sorted({url for url in urls if urls.count(url) > 1})
    if repeated:
        raise AuditError(f"{name} lists {', '.join(repeated)} more than once")
    return pages


def guidance_page(value: object, path: str, subjects: Sequence[str]) -> Page:
    fields = READ.json_object(value, path, required={"url", "subject", "governs", "read"})
    url = READ.string(fields["url"], f"{path}.url", single_line=True, trimmed=True)
    if not url.startswith(PAGE_SCHEME) or any(character.isspace() for character in url):
        raise READ.fail(f"{path}.url", f"a URL starting {PAGE_SCHEME} without whitespace", json.dumps(url))
    subject = READ.one_of(fields["subject"], f"{path}.subject", subjects)
    governs = READ.string(fields["governs"], f"{path}.governs", single_line=True, trimmed=True)
    read = READ.array(fields["read"], f"{path}.read", partial(READ.string, single_line=True, trimmed=True))
    if len(set(read)) != len(read):
        raise READ.fail(f"{path}.read", "tags each named once", "a tag named twice")
    return Page(url, subject, governs, tuple(read))


def write_guidance(path: Path, pages: Sequence[Page]) -> None:
    pages_json = [{"url": p.url, "subject": p.subject, "governs": p.governs, "read": list(p.read)} for p in pages]
    write_json(path, {"pages": pages_json})


def unread_runtimes(pages: Sequence[Page], runtimes: Sequence[str]) -> list[str]:
    """The supported runtimes the record names no page for."""
    return [runtime for runtime in runtimes if not any(page.subject == runtime for page in pages)]


def oldest_read(pages: Sequence[Page]) -> str:
    """The least recent last read among pages, by the record's order of tags, or never when a page has none."""
    if not pages or any(not page.read for page in pages):
        return "never"
    order = list(dict.fromkeys(tag for page in pages for tag in page.read))
    return min((page.read[-1] for page in pages), key=order.index)


# --- briefs ---------------------------------------------------------------------------------------

# What a brief tells its reader about the paths and refs it names, each written by code_literal.
QUOTING_NOTE = (
    "Each path and ref in this brief is a JSON string in code formatting: unquote it before you use it, and quote it "
    "again for the shell."
)


def code_literal(value: str) -> str:
    """A path or ref a brief names, as a JSON string in a code span.

    The value comes from the repository, so every character that is not printable, the line breaks U+0085, U+2028,
    and U+2029 among them, and the backtick are written as JSON escapes: the value stays on its line, cannot open or
    close the code span or start a heading, and a JSON parser reads it back exactly.
    """
    quoted = json.dumps(value, ensure_ascii=False)
    return "`" + "".join(char if char.isprintable() and char != "`" else json_escape(char) for char in quoted) + "`"


def json_escape(char: str) -> str:
    """`char` as JSON's `\\uXXXX` escape, a surrogate pair for a character beyond the Basic Multilingual Plane."""
    units = char.encode("utf-16-be", "surrogatepass")
    return "".join(f"\\u{units[index] << 8 | units[index + 1]:04x}" for index in range(0, len(units), 2))


def common_sections(area: Area) -> list[str]:
    lines = ["", "## Read beside them", "", *(f"- {code_literal(path)}" for path in area.consult)]
    lines += ["", "## Kinds of drift", ""]
    lines += [f"- **{kind}**: {what}." for kind, what in DRIFT_KINDS]
    lines += [
        "",
        "## Before you report a finding",
        "",
        "Confirm it against both sides: open the line that makes the statement and the line that breaks it, and quote "
        "both. Run the code or its test when reading cannot settle it. A suspicion you cannot confirm goes under "
        "Unconfirmed, never under Findings.",
        "",
        "## Report",
        "",
        "Report these sections in this order:",
        "",
        *(f"- **{section.capitalize()}**: {what}." for section, what in REPORT_SECTIONS),
        "",
        "Give each finding these fields:",
        "",
        *(f"- **{field}**: {what}." for field, what in FINDING_FIELDS),
    ]
    return lines


def part_note(part: int, parts: int) -> list[str]:
    """What a reader of one part of a split area is told; nothing for an area one reader reads."""
    if parts == 1:
        return []
    return [
        "",
        f"This brief is part {part} of {parts} of the area, split by directory: other readers read the other parts, "
        "so read the files listed here.",
    ]


def release_brief(
    area: Area, files: list[Change], base: str, base_commit: str, head: str, part: int = 1, parts: int = 1
) -> str:
    lines = [
        f"# Release audit: {area.title}",
        "",
        "Audit one area of this repository read-only: do not edit, commit, deploy, or open issues. Report what you "
        "find.",
        "",
        f"The release under audit is every commit from {code_literal(base)} ({base_commit}) to {head}. The files below "
        "are this area's changes in it. Read each one whole with its diff "
        f"(`git diff {base_commit} {head} -- <file>`), beside the contracts, documents, tests, and upgrade notes that "
        "describe it; "
        f"`git log --oneline {base_commit}..{head} -- <file>` names the pull requests that changed it. A deleted file "
        "(D) is read through its diff and whatever still refers to it.",
        *part_note(part, parts),
        "",
        "Ask whether the release keeps what it says: each change does what its issue and its upgrade note say, no "
        "contract or document contradicts the code it describes, every changed behavior has its test, and every grant "
        "a changed skill declares is used.",
        "",
        "## Changed files",
        "",
        QUOTING_NOTE,
        "",
        *(f"- {change.status} {code_literal(change.path)}" for change in files),
        *common_sections(area),
    ]
    return "\n".join(lines) + "\n"


def rotation_brief(
    area: Area, files: list[str], head: str, carried: Sequence[Issue] = (), part: int = 1, parts: int = 1
) -> str:
    lines = [
        f"# Rotation audit: {area.title}",
        "",
        "Audit one area of this repository read-only: do not edit, commit, deploy, or open issues. Report what you "
        "find.",
        "",
        f"This area is read whole: every file below as it is at {head}, not only what changed lately. Read each one "
        "beside the contracts, documents, and tests that describe it, and any carried issues named after this brief.",
        *part_note(part, parts),
        "",
        "Ask whether the area keeps what it says: no contract or document contradicts the code it describes, every "
        "behavior a document promises has its test, and nothing is left that no caller reaches.",
        "",
        "## Files",
        "",
        QUOTING_NOTE,
        "",
        *(f"- {code_literal(path)}" for path in files),
        *common_sections(area),
    ]
    if carried:
        lines += ["", "## Carried issues", "", *(f"- #{issue.number} {code_literal(issue.title)}" for issue in carried)]
    return "\n".join(lines) + "\n"


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


def split_for_readers(paths: list[str]) -> list[list[str]]:
    """An area's files in parts of at most MAX_FILES_PER_READER, as even as whole directories allow: files of one
    directory stay in one part unless the directory alone holds more than a part does. One part when they fit."""
    if len(paths) <= MAX_FILES_PER_READER:
        return [paths]
    size = -(-len(paths) // -(-len(paths) // MAX_FILES_PER_READER))
    groups: list[list[str]] = []
    for path in sorted(paths):
        if groups and parent_of(groups[-1][0]) == parent_of(path) and len(groups[-1]) < size:
            groups[-1].append(path)
        else:
            groups.append([path])
    parts: list[list[str]] = []
    for group in groups:
        if parts and len(parts[-1]) + len(group) <= size:
            parts[-1].extend(group)
        else:
            parts.append(group)
    return parts


def brief_names(area: Area, parts: int) -> list[str]:
    return [area.name] if parts == 1 else [f"{area.name}-{part}" for part in range(1, parts + 1)]


def write_briefs(briefs: list[tuple[str, str]]) -> list[str]:
    """Write each named brief to a new temporary directory and return the lines that name them."""
    try:
        directory = Path(tempfile.mkdtemp(prefix=BRIEFS_PREFIX))
    except OSError as exc:
        raise AuditError(f"cannot create the briefs directory: {exc}") from exc
    marker = directory / BRIEFS_MARKER
    try:
        marker.write_text(json.dumps({"directory": directory.resolve().as_posix()}) + "\n", encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"cannot write the briefs marker {marker.as_posix()}: {exc}") from exc
    lines = [f'BRIEFS "{directory.as_posix()}"']
    for name, text in briefs:
        path = directory / f"{name}.md"
        try:
            path.write_text(text, encoding="utf-8", errors="replace", newline="\n")
        except OSError as exc:
            raise AuditError(f"cannot write the brief {path.as_posix()}: {exc}") from exc
        lines.append(f'BRIEF {name} "{path.as_posix()}"')
    return lines


def guidance_brief(subject: str, title: str, pages: Sequence[Page], facts: Sequence[tuple[str, str]], head: str) -> str:
    """The brief for one reader of a runtime's, or Python's, published guidance."""
    lines = [
        f"# Guidance audit: {title}",
        "",
        "Audit this repository read-only against the published guidance below: do not edit, commit, deploy, or open "
        "issues. Report what you find.",
        "",
        f"Read each page below live, as it is today, and never from memory, and cite its address in each finding. Then "
        f"read the files of this repository listed after the pages as they are at {head}, and answer the questions "
        f"for each page. A page that cannot be fetched goes under Coverage, never under Findings.",
        "",
        "## Pages",
        "",
        *(
            f"- {page.url}: {page.governs}. Last read at {page.read[-1] if page.read else 'no release yet'}."
            for page in pages
        ),
        "",
        f"## What this repository states about {title}",
        "",
        QUOTING_NOTE,
        "",
        *(f"- {code_literal(path)}: {where}." for path, where in facts),
        "",
        "## Questions for each page",
        "",
        *(f"{number}. {question}" for number, question in enumerate(GUIDANCE_QUESTIONS, start=1)),
        "",
        "## Kinds of finding",
        "",
        *(f"- **{kind}**: {what}." for kind, what in GUIDANCE_KINDS),
        "",
        "## Report",
        "",
        "Report these sections in this order, where Coverage lists each page: read, or why it could not be:",
        "",
        *(f"- **{section.capitalize()}**: {what}." for section, what in REPORT_SECTIONS),
        "",
        "Give each finding these fields, its location being the page's address and the repository's path:line:",
        "",
        *(f"- **{field}**: {what}." for field, what in FINDING_FIELDS),
    ]
    return "\n".join(lines) + "\n"


def subject_facts(subject: str, tracked: set[str]) -> list[tuple[str, str]]:
    """The files of the checkout that state facts about a subject, each with where to look, as the checkout holds
    them: a file it tracks, or a folder holding one."""
    facts = PYTHON_FACTS if subject == PYTHON_SUBJECT else RUNTIME_FACTS
    found = []
    for path, where in facts:
        named = path.format(runtime=subject)
        if named in tracked or (named.endswith("/") and any(file.startswith(named) for file in tracked)):
            found.append((named, where))
    return found


def guidance_lines(kinds: Sequence[tuple[str, str]] = DRIFT_KINDS) -> list[str]:
    lines = [f"DRIFT {kind} {what}" for kind, what in kinds]
    lines += [f"TEMPLATE {field} {what}" for field, what in FINDING_FIELDS]
    return lines + [f"SECTION {section} {what}" for section, what in REPORT_SECTIONS]


def short(commit: str) -> str:
    return commit[:7]


# --- commands -------------------------------------------------------------------------------------


def release(
    git: GitClient, github: GitHubClient, repository: Path, since: str | None, today: Callable[[], date]
) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    released = released_tag(git, repository, head)
    if since is not None:
        base = since
    elif released is not None:
        base = released
    else:
        raise AuditError("no release tag is reachable from HEAD; pass --since <ref>")
    base_commit = commit_of(git, repository, base)
    found = changes(git, repository, base_commit, head)
    by_area = {area.name: [change for change in found if area_of(change.path) is area] for area in AREAS}
    milestones = open_milestones_after(github, released)
    lines = [f"BASE {base} {base_commit}", f"HEAD {head}", f"CHANGED {len(found)}"]
    lines += [f"AREA {area.name} {len(by_area[area.name])}" for area in AREAS]
    lines += milestone_lines(milestones[0] if milestones else None, milestones[1] if len(milestones) > 1 else None)
    sentence = f"Found by the release audit of the changes since {base}, at commit {short(head)}, on {today()}."
    lines += [f"PROVENANCE {json_text(sentence)}", *guidance_lines()]
    briefs: list[tuple[str, str]] = []
    for area in AREAS:
        if not by_area[area.name]:
            continue
        status = {change.path: change.status for change in by_area[area.name]}
        parts = split_for_readers(list(status))
        lines.append(f"READERS {area.name} {len(parts)}")
        for number, (name, paths) in enumerate(zip(brief_names(area, len(parts)), parts, strict=True), start=1):
            files = [Change(status[path], path) for path in paths]
            briefs.append((name, release_brief(area, files, base, base_commit, head, number, len(parts))))
    return lines + (write_briefs(briefs) if briefs else [])


def rotation(
    git: GitClient, github: GitHubClient, repository: Path, record: str | None, today: Callable[[], date]
) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    top, main_checkout = checkout_top(git, repository)
    path = top / RECORD_PATH
    entries = read_record(path)
    reads = last_reads(entries)
    lines = [f'RECORD "{RECORD_PATH}"', f"HEAD {head}"]
    for area in AREAS:
        if area.name in reads:
            entry = reads[area.name][1]
            lines.append(f"READ {area.name} {entry.tag} {entry.date}")
        else:
            lines.append(f"UNREAD {area.name}")
    area = next_area(entries)
    lines.append(f"NEXT {area.name}")
    if record is not None:
        if main_checkout:
            raise AuditError(
                f"{top.as_posix()} is the main checkout; record the rotation area from a worktree, such as one "
                "`python tools/worktrees.py new chore <name>` creates, and merge it through a pull request"
            )
        return [*lines, append_entry(git, repository, path, entries, Entry(today().isoformat(), record, area.name))]
    files = [file for file in tracked_files(git, repository, head) if area_of(file) is area]
    released = released_tag(git, repository, head)
    milestones = open_milestones_after(github, released)
    following = milestones[0] if milestones else None
    carried = carried_issues(github, following) if following is not None else []
    lines += [f"FILES {len(files)}", *milestone_lines(following, following)]
    lines += [f"CARRIED {issue.number} {json_text(issue.title)}" for issue in carried]
    after = f" after {released}" if released is not None else ""
    sentence = (
        f"Found by the rotation audit{after}, the {area.name} area read whole at commit {short(head)}, on {today()}."
    )
    lines += [f"PROVENANCE {json_text(sentence)}", *guidance_lines()]
    parts = split_for_readers(files)
    lines.append(f"READERS {area.name} {len(parts)}")
    briefs = [
        (name, rotation_brief(area, paths, head, carried, number, len(parts)))
        for number, (name, paths) in enumerate(zip(brief_names(area, len(parts)), parts, strict=True), start=1)
    ]
    return lines + write_briefs(briefs)


def guidance(
    git: GitClient, github: GitHubClient, repository: Path, record: str | None, today: Callable[[], date]
) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    top, main_checkout = checkout_top(git, repository)
    runtimes = supported_runtimes(top)
    titles = {**runtimes, PYTHON_SUBJECT: PYTHON_TITLE}
    path = top / GUIDANCE_PATH
    pages = read_guidance(path, list(titles))
    unread = unread_runtimes(pages, list(runtimes))
    if unread:
        raise AuditError(
            f"{GUIDANCE_PATH} names no page for {', '.join(unread)}, which {RUNTIME_CATALOGUE} supports; add the "
            "runtime's pages before the audit"
        )
    lines = [f'GUIDANCE_RECORD "{GUIDANCE_PATH}"', f"HEAD {head}"]
    by_subject = {subject: [page for page in pages if page.subject == subject] for subject in titles}
    lines += [f"SUBJECT {subject} {len(found)} {oldest_read(found)}" for subject, found in by_subject.items() if found]
    if record is not None:
        if main_checkout:
            raise AuditError(
                f"{top.as_posix()} is the main checkout; record the guidance audit from a worktree, such as one "
                "`python tools/worktrees.py new chore <name>` creates, and merge it through a pull request"
            )
        return [*lines, record_reads(git, repository, path, pages, record)]
    released = released_tag(git, repository, head)
    milestones = open_milestones_after(github, released)
    following = milestones[0] if milestones else None
    after = f" after {released}" if released is not None else ""
    sentence = f"Found by the guidance audit{after}, read against commit {short(head)}, on {today()}."
    lines += [*milestone_lines(following, following), f"PROVENANCE {json_text(sentence)}"]
    lines += guidance_lines(GUIDANCE_KINDS)
    tracked = set(tracked_files(git, repository, head))
    briefs = [
        (subject, guidance_brief(subject, titles[subject], found, subject_facts(subject, tracked), head))
        for subject, found in by_subject.items()
        if found
    ]
    return lines + write_briefs(briefs)


def record_reads(git: GitClient, repository: Path, path: Path, pages: list[Page], tag: str) -> str:
    """Append `tag` to every page's reads, refusing a tag a page already holds or that names no tag."""
    if any(tag in page.read for page in pages):
        raise AuditError(f"{tag} is already in the guidance record")
    commit_of(git, repository, f"refs/tags/{tag}")
    try:
        write_guidance(path, [Page(page.url, page.subject, page.governs, (*page.read, tag)) for page in pages])
    except OSError as exc:
        raise AuditError(f"cannot write the guidance record {path.as_posix()}, which is unchanged: {exc}") from exc
    return f"RECORDED_PAGES {tag} {len(pages)}"


def append_entry(git: GitClient, repository: Path, path: Path, entries: list[Entry], entry: Entry) -> str:
    if any(existing.tag == entry.tag for existing in entries):
        raise AuditError(f"{entry.tag} is already in the rotation record")
    commit_of(git, repository, f"refs/tags/{entry.tag}")
    try:
        write_record(path, [*entries, entry])
    except OSError as exc:
        raise AuditError(f"cannot write the rotation record {path.as_posix()}, which is unchanged: {exc}") from exc
    return f"RECORDED {entry.tag} {entry.area} {entry.date}"


def duplicates(github: GitHubClient, queries: Sequence[str]) -> list[str]:
    """Each query's open issues, as GitHub's issue search finds them."""
    lines = []
    for number, query in enumerate(queries, start=1):
        arguments = ["issue", "list", "--state", "open", f"--search={query}", f"--limit={SEARCH_LIMIT}"]
        found = issues_of(gh_json(github, [*arguments, "--json", "number,title"]), f"for query {number}")
        lines.append(f"MATCHES {number} {len(found)}")
        lines += [f"MATCH {number} {issue.number} {json_text(issue.title)}" for issue in found]
    return lines


def remove_briefs(directory: Path) -> list[str]:
    """Delete a briefs directory this script made: a BRIEFS_PREFIX directory directly in the temporary directory,
    reached through no link, holding the BRIEFS_MARKER that names it and otherwise only Markdown files. Anything else
    is refused and left as it is. The marker goes last, so a removal that fails part way can be run again."""
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = directory.resolve()
    if (
        directory.is_symlink()
        or not resolved.is_dir()
        or resolved.parent != temporary
        or not resolved.name.startswith(BRIEFS_PREFIX)
    ):
        raise AuditError(f"{directory.as_posix()} is not a briefs directory this script made in {temporary.as_posix()}")
    marker = resolved / BRIEFS_MARKER
    if not names_directory(marker, resolved):
        raise AuditError(f"{resolved.as_posix()} holds no {BRIEFS_MARKER} naming it, so this script did not make it")
    try:
        briefs = [entry for entry in resolved.iterdir() if entry != marker]
        strays = [entry.name for entry in briefs if entry.is_symlink() or not entry.is_file() or entry.suffix != ".md"]
        if strays:
            raise AuditError(
                f"{resolved.as_posix()} holds more than briefs, such as {strays[0]}, so it is left as it is"
            )
        for entry in briefs:
            entry.unlink()
        marker.unlink()
        resolved.rmdir()
    except OSError as exc:
        raise AuditError(f"cannot remove {resolved.as_posix()}: {exc}") from exc
    return [f'REMOVED "{resolved.as_posix()}"']


def names_directory(marker: Path, directory: Path) -> bool:
    """Whether the marker is a file, reached through no link, whose record names the directory."""
    if marker.is_symlink() or not marker.is_file():
        return False
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and record.get("directory") == directory.as_posix()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repository", type=Path, default=Path(), help="the checkout to audit (default: here)")
    parser = argparse.ArgumentParser(
        description="Print the briefs for this repository's release, rotation, and guidance audits."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    release_parser = commands.add_parser("release", parents=[common], help="brief each area changed since a tag")
    release_parser.add_argument("--since", metavar="REF", help="the base (default: the last release HEAD holds)")
    rotation_parser = commands.add_parser("rotation", parents=[common], help="brief the next area to read whole")
    rotation_parser.add_argument("--record", metavar="TAG", help="append the next area to the record for TAG")
    guidance_parser = commands.add_parser("guidance", parents=[common], help="brief each runtime's published guidance")
    guidance_parser.add_argument("--record", metavar="TAG", help="append TAG to every page's reads")
    duplicates_parser = commands.add_parser("duplicates", parents=[common], help="search the open issues")
    duplicates_parser.add_argument("queries", nargs="+", metavar="QUERY", help="a GitHub issue search")
    remove_parser = commands.add_parser("remove-briefs", help="delete a briefs directory a run printed")
    remove_parser.add_argument("directory", type=Path, metavar="DIR", help="the directory a BRIEFS line named")
    return parser


def main(
    arguments: Sequence[str] | None = None,
    *,
    git: GitClient | None = None,
    github: GitHubClient | None = None,
    today: Callable[[], date] = date.today,
) -> int:
    options = build_parser().parse_args(arguments)
    client = git or GitClient()
    try:
        if options.command == "remove-briefs":
            lines = remove_briefs(options.directory)
        else:
            gh = github or GitHubClient(partial(subprocess_runner, cwd=options.repository))
            if options.command == "release":
                lines = release(client, gh, options.repository, options.since, today)
            elif options.command == "rotation":
                lines = rotation(client, gh, options.repository, options.record, today)
            elif options.command == "guidance":
                lines = guidance(client, gh, options.repository, options.record, today)
            else:
                lines = duplicates(gh, options.queries)
    except AuditError as exc:
        print(f"FAILED {exc}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main, errors="replace", newline="\n")
