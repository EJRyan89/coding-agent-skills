"""The deterministic parts of this repository's two audits per release, so the agent only starts the readers,
confirms what they find, and files it.

    release   group the files changed since the latest tag reachable from HEAD (or --since REF) into the four areas
              and write one brief per area that changed, for the release audit before tagging
    rotation  name the area the rotation record shows read whole least recently and write its whole-area brief, for
              the rotation audit after tagging; with --record TAG, append that area to the record for TAG instead,
              which is the only write to the repository

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
    DRIFT <kind> <what it is>          each kind of drift the briefs ask the readers to look for
    TEMPLATE <field> <what it holds>   each field of a finding the briefs ask for
    SECTION <name> <what it holds>     each section of the report, in order
    BRIEFS "<directory>"               the new directory holding the briefs
    BRIEF <area> "<file>"              one brief, the whole prompt for one read-only reader

Exit status 0 means the briefs were written, or the entry recorded; 1 a last line FAILED <reason>; 2 a usage error.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "skills" / "skill-core" / "scripts"))

from console import use_utf8_output
from git_client import GitClient, GitError

# The rotation record, relative to the top of the repository it describes.
RECORD_PATH = ".claude/skills/audit-repository/references/rotation.json"
DATE_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}")


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
        "skills/code-review-core/",
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


def git_output(git: GitClient, repository: Path, arguments: Sequence[str]) -> str:
    try:
        return git.output(arguments, directory=repository)
    except GitError as exc:
        if exc.kind == "not_repository":
            raise AuditError(f"{repository.as_posix()} is not a Git repository") from exc
        raise AuditError(f"git {arguments[0]}: {exc}") from exc


def commit_of(git: GitClient, repository: Path, ref: str) -> str:
    """The commit a ref names, or AuditError when it names none."""
    if ref.startswith("-"):
        raise AuditError(f"{ref} is not a ref")
    try:
        result = git.run(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], directory=repository)
    except GitError as exc:
        raise AuditError(f"git rev-parse: {exc}") from exc
    if result.returncode != 0:
        if "not a git repository" in result.stderr:
            raise AuditError(f"{repository.as_posix()} is not a Git repository")
        raise AuditError(f"{ref} names no commit in {repository.as_posix()}")
    return result.stdout.strip()


def latest_tag(git: GitClient, repository: Path, head: str) -> str:
    try:
        result = git.run(["describe", "--tags", "--abbrev=0", head], directory=repository)
    except GitError as exc:
        raise AuditError(f"git describe: {exc}") from exc
    if result.returncode != 0:
        raise AuditError("no tag is reachable from HEAD; pass --since <ref>")
    return result.stdout.strip()


def changes(git: GitClient, repository: Path, base: str, head: str) -> list[Change]:
    """Each file the diff from base to head adds (A), modifies (M), deletes (D), or changes the type of (T)."""
    fields = git_output(git, repository, ["diff", "--name-status", "--no-renames", "-z", base, head]).split("\0")
    return [Change(fields[index], fields[index + 1]) for index in range(0, len(fields) - 1, 2)]


def tracked_files(git: GitClient, repository: Path, head: str) -> list[str]:
    return [
        path for path in git_output(git, repository, ["ls-tree", "-r", "-z", "--name-only", head]).split("\0") if path
    ]


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
    document = {"audits": [{"date": entry.date, "tag": entry.tag, "area": entry.area} for entry in entries]}
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")


# --- briefs ---------------------------------------------------------------------------------------


def common_sections(area: Area) -> list[str]:
    lines = ["", "## Read beside them", "", *(f"- `{path}`" for path in area.consult), "", "## Kinds of drift", ""]
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


def release_brief(area: Area, files: list[Change], base: str, base_commit: str, head: str) -> str:
    lines = [
        f"# Release audit: {area.title}",
        "",
        "Audit one area of this repository read-only: do not edit, commit, deploy, or open issues. Report what you "
        "find.",
        "",
        f"The release under audit is every commit from {base} ({base_commit}) to {head}. The files below are this "
        f"area's changes in it. Read each one whole with its diff (`git diff {base_commit} {head} -- <file>`), beside "
        "the contracts, documents, tests, and upgrade notes that describe it; "
        f"`git log --oneline {base_commit}..{head} -- <file>` names the pull requests that changed it. A deleted file "
        "(D) is read through its diff and whatever still refers to it.",
        "",
        "Ask whether the release keeps what it says: each change does what its issue and its upgrade note say, no "
        "contract or document contradicts the code it describes, every changed behavior has its test, and every grant "
        "a changed skill declares is used.",
        "",
        "## Changed files",
        "",
        *(f"- {change.status} `{change.path}`" for change in files),
        *common_sections(area),
    ]
    return "\n".join(lines) + "\n"


def rotation_brief(area: Area, files: list[str], head: str) -> str:
    lines = [
        f"# Rotation audit: {area.title}",
        "",
        "Audit one area of this repository read-only: do not edit, commit, deploy, or open issues. Report what you "
        "find.",
        "",
        f"This area is read whole: every file below as it is at {head}, not only what changed lately. Read each one "
        "beside the contracts, documents, and tests that describe it, and any carried issues named after this brief.",
        "",
        "Ask whether the area keeps what it says: no contract or document contradicts the code it describes, every "
        "behavior a document promises has its test, and nothing is left that no caller reaches.",
        "",
        "## Files",
        "",
        *(f"- `{path}`" for path in files),
        *common_sections(area),
    ]
    return "\n".join(lines) + "\n"


def write_briefs(briefs: list[tuple[Area, str]]) -> list[str]:
    """Write each brief to a new temporary directory and return the lines that name them."""
    directory = Path(tempfile.mkdtemp(prefix="audit-repository-"))
    lines = [f'BRIEFS "{directory.as_posix()}"']
    for area, text in briefs:
        path = directory / f"{area.name}.md"
        path.write_text(text, encoding="utf-8", errors="replace", newline="\n")
        lines.append(f'BRIEF {area.name} "{path.as_posix()}"')
    return lines


def guidance_lines() -> list[str]:
    lines = [f"DRIFT {kind} {what}" for kind, what in DRIFT_KINDS]
    lines += [f"TEMPLATE {field} {what}" for field, what in FINDING_FIELDS]
    return lines + [f"SECTION {section} {what}" for section, what in REPORT_SECTIONS]


# --- commands -------------------------------------------------------------------------------------


def release(git: GitClient, repository: Path, since: str | None) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    base = since if since is not None else latest_tag(git, repository, head)
    base_commit = commit_of(git, repository, base)
    found = changes(git, repository, base_commit, head)
    by_area = {area.name: [change for change in found if area_of(change.path) is area] for area in AREAS}
    lines = [f"BASE {base} {base_commit}", f"HEAD {head}", f"CHANGED {len(found)}"]
    lines += [f"AREA {area.name} {len(by_area[area.name])}" for area in AREAS]
    lines += guidance_lines()
    briefs = [
        (area, release_brief(area, by_area[area.name], base, base_commit, head)) for area in AREAS if by_area[area.name]
    ]
    return lines + (write_briefs(briefs) if briefs else [])


def rotation(git: GitClient, repository: Path, record: str | None, today: Callable[[], date]) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    top = Path(git_output(git, repository, ["rev-parse", "--show-toplevel"]).strip())
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
        return [*lines, append_entry(git, repository, path, entries, Entry(today().isoformat(), record, area.name))]
    files = [file for file in tracked_files(git, repository, head) if area_of(file) is area]
    lines += [f"FILES {len(files)}", *guidance_lines()]
    return lines + write_briefs([(area, rotation_brief(area, files, head))])


def append_entry(git: GitClient, repository: Path, path: Path, entries: list[Entry], entry: Entry) -> str:
    if any(existing.tag == entry.tag for existing in entries):
        raise AuditError(f"{entry.tag} is already in the rotation record")
    commit_of(git, repository, f"refs/tags/{entry.tag}")
    try:
        write_record(path, [*entries, entry])
    except OSError as exc:
        raise AuditError(f"cannot write the rotation record {path.as_posix()}: {exc}") from exc
    return f"RECORDED {entry.tag} {entry.area} {entry.date}"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--repository", type=Path, default=Path(), help="the checkout to audit (default: here)")
    parser = argparse.ArgumentParser(description="Print the briefs for this repository's release and rotation audits.")
    commands = parser.add_subparsers(dest="command", required=True)
    release_parser = commands.add_parser("release", parents=[common], help="brief each area changed since a tag")
    release_parser.add_argument("--since", metavar="REF", help="the base (default: the latest tag reachable from HEAD)")
    rotation_parser = commands.add_parser("rotation", parents=[common], help="brief the next area to read whole")
    rotation_parser.add_argument("--record", metavar="TAG", help="append the next area to the record for TAG")
    return parser


def main(
    arguments: Sequence[str] | None = None, *, git: GitClient | None = None, today: Callable[[], date] = date.today
) -> int:
    options = build_parser().parse_args(arguments)
    client = git or GitClient()
    try:
        if options.command == "release":
            lines = release(client, options.repository, options.since)
        else:
            lines = rotation(client, options.repository, options.record, today)
    except AuditError as exc:
        print(f"FAILED {exc}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    use_utf8_output(errors="replace", newline="\n")
    raise SystemExit(main())
