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
                   the questions a reader answers for each page, and for a runtime the changelog the
                   runtime-changelogs.yml workflow watches and the release it was read through, for the guidance audit
                   after tagging; refused while the record names no page for a runtime
                   skills/ejryan89-skill-core/scripts/runtime_support.py lists, or none for Python. With --record TAG,
                   append TAG to every page's reads instead, from a worktree as rotation --record does, keeping the
                   record's changelogs, whose read_through the maintainer moves forward by hand in the same pull request
    twins          list each fix merged since the last release HEAD holds (or --since REF): each commit of HEAD's
                   first-parent history whose title is typed fix, with the lines it removed from files that are not
                   regression suites, which name the construct it corrected, and the lines HEAD still holds that
                   contain one of them, then write the briefs a reader judges twins and their patterns from, for the
                   twin sweep before tagging; a release with no fix prints FIXES 0 and writes no brief
    duplicates     search the open issues for each QUERY, so a finding already filed is not filed twice
    remove-briefs  delete a briefs directory a release, rotation, guidance, or twins run printed as BRIEFS, and nothing
                   else:
                   one holding the BRIEFS_MARKER file the run wrote before its first brief, naming the directory

Both records hold release tags only, as release_tags.py defines them, and a read's recency is its tag's place among
releases, whatever order a record lists its reads in: an area's last read and a page's are those of the latest
release, and NEXT and SUBJECT name the earliest of them.

An area holding more than MAX_FILES_PER_READER files is split between readers by directory: one brief per part, no
part above that size, and a directory split only when it alone holds more than a part. A twin sweep gives each reader
FIXES_PER_READER fixes, in the order they merged, and more each when that would need more than MAX_TWIN_READERS
readers, so the readers stay bounded and every fix is still read. A removed line is searched for when it is at least
MIN_CONSTRUCT_CHARS long once stripped, holds a letter or digit, is no Python import, and is not added back elsewhere
in the fix; a brief lists at most MAX_CONSTRUCT_LINES of them and MAX_HITS places. A release, rotation, or twins run
reads the open milestones and, for rotation, the next milestone's open issues through gh, from the checkout audited.

Every line is one fact:

    BASE <ref> <commit>                release and twins: where the diff or the fixes start
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
    QUESTION <name> <what it asks>     release and rotation: each question the briefs ask of every file, which a
                                       finding names in its question field when it found the finding
    TEMPLATE <field> <what it holds>   each field of a finding the briefs ask for
    SECTION <name> <what it holds>     each section of the report, in order
    READERS <area> <count>             the briefs, one per reader, an area's files, or twins' fixes, are split into
    BRIEFS "<directory>"               the new directory holding the briefs
    BRIEF <name> "<file>"              one brief, the whole prompt for one read-only reader: <area> for a whole
                                       area, <area>-<part> for one part of it, <subject> for a guidance brief,
                                       twins or twins-<part> for the twin sweep
    FIXES <count>                      twins: the fixes merged since the base
    FIX <commit> <pull> "<title>"      twins: one fix, oldest first, with its pull request's number, or none
    CONSTRUCT <commit> <count>         twins: the removed lines of the fix searched for
    CONSTRUCT_CUT <commit> <limit>     twins: the brief lists only the first <limit> of them
    HITS <commit> <count>              twins: the lines HEAD holds that contain one, up to MAX_HITS
    HITS_CUT <commit> <limit>          twins: HEAD holds more than the <limit> listed
    MECHANISM <name> <what it is>      twins: each mechanism a pattern may propose to stop it recurring
    MATCHES <query> <count>            duplicates: how many open issues the query numbered <query>, from 1, found
    MATCH <query> <number> "<title>"   duplicates: one of them
    MATCHES_CUT <query> <limit>        duplicates: the query found more open issues than the <limit> listed
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

import guidance_record
import json_reader
import whole_number
from atomic_file import UnfinishedCopyError, replace_text
from console import run_main
from flat_text import flat_text
from git_client import GitClient, GitError, classify_failure
from github_client import GitHubClient, GitHubError, Listing, subprocess_runner
from guidance_record import GUIDANCE_PATH, PYTHON_SUBJECT, Changelog, GuidanceRecord, GuidanceRecordError, Page
from link_probe import is_plain_directory, is_plain_file
from path_keys import path_key
from release_tags import is_release_tag, last_release, release_order
from suite_names import is_suite_name

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
# Where the deployer catalogues the runtimes it supports, read as literals so the audit runs no deployer code.
RUNTIME_CATALOGUE = "skills/ejryan89-skill-core/scripts/runtime_support.py"
PYTHON_TITLE = "Python"
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
# The questions every release and rotation brief asks beside the kinds of drift, each drawn from defects that kept
# shipping past the briefs' own question; a finding names the one that found it, so later audits can count them.
AUDIT_QUESTIONS = (
    (
        "absence",
        "what each guard, gate, count, or measurement reports when its evidence was never produced, and whether its "
        'output tells "nothing found" from "nothing looked at"',
    ),
    (
        "universal-claim",
        'for each "every", "each", "the one", "never", or "always" a docstring, contract, or design row makes, the '
        "instance that breaks it",
    ),
    (
        "malformed-input",
        "for each reader of a file or record it did not just write: a wrong type at the top level and at field level, "
        "a key indexed but never validated, nesting past the recursion limit, an integer past the digit limit, "
        "invalid UTF-8, and a directory in the file's place",
    ),
    ("sibling", "for each fix that touched these files since their last audit, the fixed construct's twin elsewhere"),
    (
        "successive-runs",
        "for each record a run writes that a later run reads (the deployment manifest's pin, owned items, and each "
        "skill's skill_deps; the journal; code-review records and run files; repo-cleanup's plans, seals, and sweep "
        "record; a reporting skill's history file), follow it through two runs: after run N, who owns each field, the "
        "user or the tool, and does run N+1 read it as the documents promise, including when run N was given the value "
        "in another form (a short name, another case, an equivalent path)? A test that asserts the state after one run "
        "without the next run's behavior is a finding, as the pin recorded as refs/tags/<tag> after one update, which "
        "the next release's update moved without --unpin, was (#1689)",
    ),
    (
        "installed-copy",
        "for each deployer refusal, flag, status line, or path the installed update-coding-agent-skills reads: how "
        "does the previous release's installed copy, which runs this deployer from the fast-forwarded clone, behave "
        "with it, and does each way out the documents name work for that first update? A route only the new copy can "
        "take is a finding, and the upgrade note must name the way past it, as the installed copy whose advice to "
        "rerun with --unpin repeated its stop over a user's pin showed; the test #1607 proposes, the latest release's "
        "installed update run against a newer clone, is what holds the answer",
    ),
)
# A release or rotation finding's fields: the shared ones, and the question that found it.
AUDIT_FINDING_FIELDS = (*FINDING_FIELDS, ("question", "the question that found it, by its name, or none"))
# The report's sections, in order; Findings holds one entry of the fields above per finding.
REPORT_SECTIONS = (
    ("findings", "each finding confirmed against both sides, as the TEMPLATE fields"),
    ("unconfirmed", "each suspicion not confirmed against both sides, and why"),
    ("coverage", "each file: read whole, skimmed, or not read"),
)

# A squash commit's title that names a fix, as "Pull requests" in docs/implementing-changes.md types one, and the pull
# request number GitHub appends to it.
FIX_TITLE = re.compile(r"fix(\([^()]*\))?: ")
PULL_NUMBER = re.compile(r"\(#(\d+)\)$")
# A hunk header of `git diff -U0`: the old side's count, then the new side's, each 1 when omitted.
HUNK_HEADER = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# A removed line shorter than this, once stripped, is too common to search for, such as `return None` or `)`.
MIN_CONSTRUCT_CHARS = 16
IMPORT_LINE = re.compile(r"(import \S|from \S+ import )")
# The most removed lines of one fix a brief lists, though every one is searched for, and the most places at HEAD a
# brief lists for one fix.
MAX_CONSTRUCT_LINES = 12
MAX_HITS = 30
# The most characters of pathspecs one git diff is given, well inside the Windows command line's limit.
PATHSPEC_CHARS = 8000
# The fixes one reader is given, and the most readers a sweep starts: past FIXES_PER_READER * MAX_TWIN_READERS fixes,
# each reader is given more, so no fix goes unread.
FIXES_PER_READER = 10
MAX_TWIN_READERS = 8
# A twin's fields, the mechanisms a pattern may propose, and the sweep's report sections, in order.
TWIN_FIELDS = (
    ("fix", "the fix's commit and pull request"),
    ("location", "path:line of the twin, with forward slashes"),
    ("evidence", "the line the fix corrected and the twin's line, both quoted"),
    ("pattern", "the pattern it is an instance of, by the name the Patterns section gives it"),
    ("severity", "defect (a user can hit it), drift (a statement and the code part ways), or polish"),
    ("title", "a one-line issue title"),
    ("template", "bug, documentation, or enhancement; advisory for a trust-boundary twin"),
)
MECHANISMS = (
    ("helper", "a shared helper every site calls, so the construct has one home"),
    ("policy", "a validation policy under tests/validation/ that refuses the construct"),
    ("corpus", "a corpus case, a fixture every reader or writer of its kind is run against"),
)
TWIN_SECTIONS = (
    ("twins", "each twin confirmed against the fix's diff and the twin's line, as the TEMPLATE fields"),
    (
        "patterns",
        "each pattern the twins share: its name, its twins, the docs/lessons.md pattern it is an instance of or new, "
        "and the MECHANISM that would stop it recurring, with where it would live",
    ),
    ("dismissed", "each hit that is not a twin, and why: sanctioned, held by a policy, already fixed, or unrelated"),
    ("coverage", "each fix: the construct named and searched, or why not"),
)


# What a reader compares a runtime's pages with: each file of this repository that states facts about the runtime,
# and where in it, under the runtime's name in skills/ejryan89-skill-core/scripts/runtime_support.py; a file the
# checkout does not hold is left out of the brief. Codex and Copilot CLI read the adapters the deployer renders, while
# Claude Code is the runtime the skills are written for, so its facts are in its subagent, hook, evaluation, prompt,
# skill, and plugin files instead.
SHARED_RUNTIME_FACTS = (
    (
        "skills/ejryan89-skill-core/scripts/runtime_support.py",
        "the runtime's capabilities and the comment that gives their reasons",
    ),
    ("deploy-meta/", "each skill's runtime_support declaration for the runtime"),
    ("docs/code-review-operations-contract.md", "the Threat model rows that name the runtime"),
    ("README.md", "the supported-runtimes table"),
    ("skills/audit-skill/references/provenance.json", "each check whose source is one of these pages"),
    ("skills/audit-ai-config/references/provenance.json", "each check whose source is one of these pages"),
)
ADAPTED_RUNTIME_FACTS = (
    ("deployer/render.py", "how the runtime adapter is rendered for the runtime"),
    ("tools/runtime_canary.py", "the command line that starts the runtime"),
    (
        "skills/ejryan89-code-review-core/scripts/review_hosts.py",
        "the command line the review host starts the runtime with",
    ),
    (
        "CLAUDE.md",
        "Parallel sessions, on which hooks the runtime runs, and Maintaining AI Agent Config, on which files it reads",
    ),
    (
        "docs/design.md",
        "the hub guard's row of the Trust model, and Generated instruction projections under Considered and declined",
    ),
    ("docs/installation.md", "Codex CLI settings, and the PowerShell rows of Requirements and Installing the tools"),
    ("docs/code-review-operations.md", "Inline reviews, on the capabilities the runtime offers a review"),
    ("deployer/tools.py", "the runtime's Tool and version floor, and CODEX_SANDBOX_PROBE and codex_sandbox_mode"),
    ("deployer/check.py", "the runtime lines, and the codex sandbox and pwsh command lines it reports"),
    ("deployer/discovery.py", "parse_copilot, and codex_requests and parse_codex for the app-server skills/list"),
    ("deployer/verify.py", "how each runtime's listing is judged: adapter_line and expose"),
    (".github/workflows/deployable.yml", "the runtime version it installs and the Codex sandbox it writes"),
    (
        "tools/skill_shims.py",
        "the PROTECTED_WRITES constant and the shim text on how Codex resolves paths and what it protects",
    ),
)
RUNTIME_FACTS = {
    "claude-code": (
        *SHARED_RUNTIME_FACTS,
        ("agents/code-review-reviewer.md", "the subagent's frontmatter: its description, tools, model, and hooks"),
        ("skills/review-insights/SKILL.md", "Synthesize, on how the synthesis subagent is started"),
        (".claude/settings.json", "the hooks and attribution it declares"),
        ("tools/hub_guard.py", "the PreToolUse input it reads and the decision it returns"),
        ("tools/skill_evals.py", "how a scenario's runs are started, repeated, and judged by script"),
        ("docs/skill-evaluations.md", "Skill evaluations: the scenarios, the runs, and what is recorded"),
        (".claude/skills/evaluate-skill/SKILL.md", "when a run is owed and how it is cited"),
        ("skills/ejryan89-code-review-core/scripts/review_specialists.py", "the reviewer prompt it writes"),
        ("docs/runtime-prompts/", "each prompt a skill's script writes for a subagent"),
        ("docs/adding-a-skill.md", "Description and Granting tools"),
        ("deployer/render.py", "MAX_DESCRIPTION, the description limit"),
        (".claude-plugin/marketplace.json", "each plugin's entry and the archives it pins"),
        ("tools/plugin_build.py", "how a plugin's manifest, skills, agents, and hooks file are assembled"),
        ("docs/design.md", "the reviewer guard's row of the Trust model and the Decisions row on the plugin route"),
    ),
    "codex": (("docs/codex-support.md", "the whole document"), *SHARED_RUNTIME_FACTS, *ADAPTED_RUNTIME_FACTS),
    "copilot-cli": (("docs/copilot-support.md", "the whole document"), *SHARED_RUNTIME_FACTS, *ADAPTED_RUNTIME_FACTS),
}
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
        commit = git.commit_of(ref, directory=repository)
    except GitError as exc:
        if exc.kind == "not_repository":
            raise AuditError(f"{repository.as_posix()} is not a Git repository") from exc
        raise AuditError(f"git rev-parse: {exc}") from exc
    if commit is None:
        raise AuditError(f"{ref} names no commit in {repository.as_posix()}")
    return commit


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
    return top, path_key(common, resolve=True) == path_key(top / ".git", resolve=True)


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


def gh_listing(github: GitHubClient, arguments: Sequence[str], limit: int) -> Listing:
    """At most `limit` items of a gh listing, and whether GitHub held more; see `GitHubClient.capped_list`."""
    try:
        return github.capped_list(arguments, limit)
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
    """The version a milestone or tag name gives, or None when it names none, a part past the digit limit
    included."""
    match = VERSION.fullmatch(name or "")
    parts = [] if match is None else [whole_number.parse(part) for part in match.groups()]
    if len(parts) != 3 or parts[0] is None or parts[1] is None or parts[2] is None:
        return None
    return parts[0], parts[1], parts[2]


def open_milestones_after(github: GitHubClient, released: str | None) -> list[str]:
    """The titles of the open milestones named for a version above the released tag's, lowest first; every one named
    for a version when no released tag is a version."""
    # Every page, each a list of milestones: one page alone would leave out a milestone past it.
    pages = gh_json(github, ["api", "repos/{owner}/{repo}/milestones?state=open&per_page=100", "--paginate", "--slurp"])
    if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
        raise AuditError("gh api printed milestones that are not a list of objects with a title")
    payload = [item for page in pages for item in page]
    if not all(isinstance(item, dict) and isinstance(item.get("title"), str) for item in payload):
        raise AuditError("gh api printed milestones that are not a list of objects with a title")
    floor = version_of(released)
    named = []
    for item in payload:
        version = version_of(item["title"])
        if version is not None and (floor is None or version > floor):
            named.append((version, item["title"]))
    return [title for _, title in sorted(named)]


def carried_issues(github: GitHubClient, milestone: str) -> list[Issue]:
    """The next milestone's open issues, or AuditError when it holds more than CARRIED_LIMIT, which every brief could
    not then name."""
    arguments = ["issue", "list", "--state", "open", f"--milestone={milestone}", "--json", "number,title"]
    listing = gh_listing(github, arguments, CARRIED_LIMIT)
    found = issues_of(listing.items, f"the open issues of {milestone}")
    if listing.cut:
        raise AuditError(f"{milestone} holds more than {CARRIED_LIMIT} open issues, more than a brief names")
    return found


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
    """The record's entries, in the order it lists them, or AuditError naming what is wrong with it."""
    name = path.as_posix()
    try:
        document = json_reader.read_file(path)
    except FileNotFoundError as exc:
        raise AuditError(f"no rotation record at {name}") from exc
    # ValueError covers the decode errors, a repeated key, nesting too deep, and an over-long integer.
    except (OSError, ValueError) as exc:
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
    if not isinstance(tag, str) or not is_release_tag(tag):
        raise AuditError(f"{name} audit {number} has a tag that is not a release tag vX.Y.Z")
    if area not in AREA_NAMES:
        raise AuditError(f"{name} audit {number} names area {area!r}, not one of {', '.join(AREA_NAMES)}")
    return Entry(when, tag, area)


def release_key(tag: str) -> tuple[int, ...]:
    """A recorded tag's place among releases, as ejryan89-skill-core's release_tags orders them, so a read's recency
    never depends on where a record lists it. Both readers refuse any other tag, so AuditError means one got past."""
    order = release_order(tag)
    if order is None:
        raise AuditError(f"{tag} is not a release tag vX.Y.Z")
    return order


def last_reads(entries: list[Entry]) -> dict[str, Entry]:
    """Each area's most recent whole read: the one of the latest release, wherever the record lists it."""
    reads: dict[str, Entry] = {}
    for entry in entries:
        if entry.area not in reads or release_key(entry.tag) > release_key(reads[entry.area].tag):
            reads[entry.area] = entry
    return reads


def next_area(entries: list[Entry]) -> Area:
    """The area read whole least recently: never read first, then the oldest release, ties in the rotation order."""
    reads = last_reads(entries)
    unread: tuple[int, ...] = ()
    return min(
        AREAS,
        key=lambda area: (release_key(reads[area.name].tag) if area.name in reads else unread, AREAS.index(area)),
    )


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


def read_guidance_record(path: Path, subjects: Sequence[str]) -> GuidanceRecord:
    """The guidance record's pages and changelogs, each about one of `subjects`, as ejryan89-skill-core's
    guidance_record reads them, or AuditError naming what is wrong with it."""
    try:
        return guidance_record.read(path, subjects)
    except GuidanceRecordError as exc:
        raise AuditError(str(exc)) from exc


def read_guidance(path: Path, subjects: Sequence[str]) -> list[Page]:
    """The guidance record's pages, each about one of `subjects`, or AuditError naming what is wrong with it."""
    return read_guidance_record(path, subjects).pages


def write_guidance(path: Path, record: GuidanceRecord) -> None:
    write_json(path, guidance_record.document(record))


def unread_runtimes(pages: Sequence[Page], runtimes: Sequence[str]) -> list[str]:
    """The supported runtimes the record names no page for."""
    return [runtime for runtime in runtimes if not any(page.subject == runtime for page in pages)]


def oldest_read(pages: Sequence[Page]) -> str:
    """The least recent last read among pages, each page's last read its latest release and the least recent the
    earliest release among them, whatever order the record lists them in, or never when a page has none."""
    if not pages or any(not page.read for page in pages):
        return "never"
    return min((max(page.read, key=release_key) for page in pages), key=release_key)


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
    lines += ["", "## Questions to ask of every file", ""]
    lines += [f"- **{name}**: {what}." for name, what in AUDIT_QUESTIONS]
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
        *(f"- **{field}**: {what}." for field, what in AUDIT_FINDING_FIELDS),
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
    # Grouped by directory before packing, since a subdirectory's files can sort between its parent's.
    by_directory: dict[str, list[str]] = {}
    for path in sorted(paths):
        by_directory.setdefault(parent_of(path), []).append(path)
    groups = [
        files[start : start + size] for _, files in sorted(by_directory.items()) for start in range(0, len(files), size)
    ]
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


def changelog_section(changelog: Changelog | None) -> list[str]:
    """What a guidance brief tells its reader about the subject's changelog, which the runtime-changelogs.yml workflow
    watches: nothing for a subject with none."""
    if changelog is None:
        return []
    return [
        "",
        "## Changelog",
        "",
        "Start from the latest run of the `runtime-changelogs.yml` workflow. Its step summary lists the entries of "
        f"{changelog.url} for each release after {changelog.read_through}, the release this changelog was last read "
        "through, that mention a flag, environment variable, path, or frontmatter key this repository's code uses; "
        "each such release it could not read, as UNREAD, for you to read in the changelog yourself; and each page of "
        "the record that moved or is gone. Answer questions 2 to 5 for each entry it lists, as for a page.",
        "",
        "When the audit is recorded, the maintainer moves this changelog's read_through to the latest release whose "
        "entries you read, so name that release under Coverage, with each listed entry you did not judge.",
    ]


def guidance_brief(
    title: str,
    pages: Sequence[Page],
    facts: Sequence[tuple[str, str]],
    head: str,
    changelog: Changelog | None = None,
) -> str:
    """The brief for one reader of a runtime's, or Python's, published guidance, each page with its latest read."""
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
            f"- {page.url}: {page.governs}. "
            f"Last read at {max(page.read, key=release_key) if page.read else 'no release yet'}."
            for page in pages
        ),
        *changelog_section(changelog),
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
    facts = PYTHON_FACTS if subject == PYTHON_SUBJECT else RUNTIME_FACTS.get(subject, ())
    return [
        (path, where)
        for path, where in facts
        if path in tracked or (path.endswith("/") and any(file.startswith(path) for file in tracked))
    ]


def guidance_lines(kinds: Sequence[tuple[str, str]] = DRIFT_KINDS, *, guidance_audit: bool = False) -> list[str]:
    """What the briefs ask for: a release or rotation brief's kinds of drift, questions, and fields, or with
    `guidance_audit` the guidance kinds and the shared fields, then the report's sections."""
    lines = [f"DRIFT {kind} {what}" for kind, what in kinds]
    if not guidance_audit:
        lines += [f"QUESTION {name} {what}" for name, what in AUDIT_QUESTIONS]
    fields = FINDING_FIELDS if guidance_audit else AUDIT_FINDING_FIELDS
    lines += [f"TEMPLATE {field} {what}" for field, what in fields]
    return lines + [f"SECTION {section} {what}" for section, what in REPORT_SECTIONS]


def short(commit: str) -> str:
    return commit[:7]


# --- the twin sweep -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    text: str


@dataclass(frozen=True)
class Fix:
    commit: str
    title: str
    pull: int | None
    construct: tuple[str, ...]
    hits: tuple[Hit, ...]
    hits_cut: bool


def merged_fixes(git: GitClient, repository: Path, base: str, head: str) -> list[tuple[str, str]]:
    """Each commit `main`'s first-parent history merged after base whose title names a fix, oldest first, with its
    title."""
    log = git_output(
        git, repository, ["log", "--first-parent", "--reverse", "-z", "--format=%H%x1f%s", f"{base}..{head}"]
    )
    found = []
    for record in log.split("\0"):
        commit, _, title = record.partition("\x1f")
        if commit and FIX_TITLE.match(title):
            found.append((commit, title))
    return found


def removed_lines(diff: str) -> tuple[list[str], set[str]]:
    """The lines a `git diff -U0` removes, in order, and the lines it adds, each stripped, read by each hunk's counts
    so a removed line that starts with `---` is never taken for a header. A line of neither side ends its hunk early
    and is read as any line outside one, so a hunk cut short loses no header after it."""
    removed: list[str] = []
    added: set[str] = set()
    # The lines of each side the current hunk has still to give.
    old = new = 0
    for line in diff.split("\n"):
        if line.startswith("-") and old:
            removed.append(line[1:].strip())
            old -= 1
            continue
        if line.startswith("+") and new:
            added.add(line[1:].strip())
            new -= 1
            continue
        if line.startswith("\\") and (old or new):
            continue
        header = HUNK_HEADER.match(line)
        old, new = (0, 0) if header is None else hunk_counts(header)
    return removed, added


def hunk_counts(header: re.Match[str]) -> tuple[int, int]:
    """The lines a hunk header says each side gives, 1 where it names no count."""
    refused = AuditError(f"git diff printed the hunk header {header.string}")
    return whole_number.read(header.group(1) or "1", refused), whole_number.read(header.group(2) or "1", refused)


def searchable(line: str) -> bool:
    """Whether a removed line is distinctive enough to search for: long enough, more than punctuation, and not a
    Python import, which a fix rewrites as its callers change and every module repeats."""
    if IMPORT_LINE.match(line):
        return False
    return len(line) >= MIN_CONSTRUCT_CHARS and any(char.isalnum() for char in line)


def construct_of(git: GitClient, repository: Path, commit: str) -> list[str]:
    """The lines a fix removed from files that are not regression suites, long enough to search for and not added back
    anywhere in it, a file it added included, in the order its diff gives them. The pathspecs go to git in batches, so
    a fix that touched many files stays inside the command line's limit."""
    paths = [
        change.path
        for change in changes(git, repository, f"{commit}^", commit)
        if not is_suite_name(change.path.rpartition("/")[2])
    ]
    removed: list[str] = []
    added: set[str] = set()
    for batch in pathspec_batches(paths):
        diff = git_output(
            git, repository, ["diff", "-U0", "--no-renames", "--no-color", f"{commit}^", commit, "--", *batch]
        )
        batch_removed, batch_added = removed_lines(diff)
        removed += batch_removed
        added |= batch_added
    construct: list[str] = []
    for line in removed:
        if searchable(line) and line not in added and line not in construct:
            construct.append(line)
    return construct


def pathspec_batches(paths: Sequence[str]) -> list[list[str]]:
    """Each path as a literal pathspec, in batches of at most PATHSPEC_CHARS characters, or one path alone when it is
    longer."""
    batches: list[list[str]] = []
    # Each batch's characters so far, a separator counted after each pathspec.
    sizes: list[int] = []
    for path in paths:
        pathspec = f":(literal){path}"
        if batches and sizes[-1] + len(pathspec) <= PATHSPEC_CHARS:
            batches[-1].append(pathspec)
            sizes[-1] += len(pathspec) + 1
        else:
            batches.append([pathspec])
            sizes.append(len(pathspec) + 1)
    return batches


def search_construct(git: GitClient, repository: Path, head: str, construct: Sequence[str]) -> tuple[list[Hit], bool]:
    """Each line HEAD's tree holds that contains a line of the construct, as fixed text, at most MAX_HITS, and whether
    there were more. The patterns reach git grep on its stdin, so no command line limits them, and it exits 1 when
    nothing matches, which is no hit, not a failure."""
    if not construct:
        return [], False
    arguments = ["grep", "-n", "-I", "-F", "-z", "--full-name", "-f", "-", head, "--"]
    patterns = "".join(f"{line}\n" for line in construct).encode("utf-8")
    try:
        result = git.run(arguments, directory=repository, input_bytes=patterns)
    except GitError as exc:
        raise git_failure(repository, "grep", exc) from exc
    if result.returncode == 1 and not result.stderr.strip():
        return [], False
    if result.returncode != 0:
        raise git_failure(repository, "grep", GitError(result.stderr.strip(), kind=classify_failure(result.stderr)))
    hits = []
    prefix = f"{head}:"
    for record in result.stdout.split("\n"):
        fields = record.split("\0", 2)
        number = whole_number.parse(fields[1]) if len(fields) == 3 else None
        if number is None or not fields[0].startswith(prefix):
            continue
        hits.append(Hit(fields[0][len(prefix) :], number, fields[2].strip()))
    return hits[:MAX_HITS], len(hits) > MAX_HITS


def sweep_fix(git: GitClient, repository: Path, head: str, commit: str, title: str) -> Fix:
    construct = construct_of(git, repository, commit)
    hits, hits_cut = search_construct(git, repository, head, construct)
    number = PULL_NUMBER.search(title)
    pull = whole_number.parse(number.group(1)) if number else None
    return Fix(commit, title, pull, tuple(construct), tuple(hits), hits_cut)


def fix_section(fix: Fix) -> list[str]:
    pull = f"#{fix.pull}" if fix.pull is not None else "no pull request"
    lines = ["", f"### {short(fix.commit)} ({pull})", "", f"Title: {code_literal(fix.title)}", ""]
    lines.append(f"Read it whole with `git show {fix.commit}`.")
    if not fix.construct:
        lines += ["", "No removed line was long enough to search for: name the construct from its diff and search."]
        return lines
    lines += ["", "Lines it removed, the construct as it stood:", ""]
    lines += [f"- {code_literal(line)}" for line in fix.construct[:MAX_CONSTRUCT_LINES]]
    if len(fix.construct) > MAX_CONSTRUCT_LINES:
        lines.append(f"- and {len(fix.construct) - MAX_CONSTRUCT_LINES} more, searched too: read them in its diff")
    lines += ["", "Lines HEAD still holds that contain one of them:", ""]
    lines += [f"- {code_literal(f'{hit.path}:{hit.line}')}: {code_literal(hit.text)}" for hit in fix.hits] or ["- none"]
    if fix.hits_cut:
        lines.append(f"- and more past these {MAX_HITS}: run the search yourself")
    return lines


def twin_part_note(part: int, parts: int) -> list[str]:
    """What a reader of one part of a sweep is told; nothing for a sweep one reader reads."""
    if parts == 1:
        return []
    return ["", f"This brief is part {part} of {parts} of the sweep: other readers read the other fixes."]


def twin_brief(fixes: Sequence[Fix], base: str, base_commit: str, head: str, part: int, parts: int) -> str:
    lines = [
        "# Twin sweep",
        "",
        "Sweep this repository read-only: do not edit, commit, deploy, or open issues. Report what you find.",
        "",
        f"Each fix below merged between {code_literal(base)} ({base_commit}) and {head}. For each, read its diff and "
        "name the construct it corrected, then find the same construct still uncorrected at HEAD: the lines listed "
        "are where its removed lines still stand, a start and not the whole search. Search also for its other forms: "
        "the other callers of the same reader or writer, the same call with other names, and its twin in the other "
        "shell or runtime, as the Search for of its pattern in `docs/lessons.md` says.",
        *twin_part_note(part, parts),
        "",
        "A twin is a site that does what the fix stopped doing. A site is not a twin when a `*_ALLOWED` table "
        "sanctions it beside the code, when a validation policy under `tests/validation/` already holds it, when a "
        "regression suite holds it as the case the fix refuses, or when the fix itself, or a later one, keeps it.",
        "",
        QUOTING_NOTE,
        "",
        "## Fixes",
        *(line for fix in fixes for line in fix_section(fix)),
        "",
        "## Mechanisms",
        "",
        "Group the twins by pattern, and for each pattern propose the one mechanism that would stop it recurring:",
        "",
        *(f"- **{name}**: {what}." for name, what in MECHANISMS),
        "",
        "## Report",
        "",
        "Report these sections in this order:",
        "",
        *(f"- **{section.capitalize()}**: {what}." for section, what in TWIN_SECTIONS),
        "",
        "Give each twin these fields:",
        "",
        *(f"- **{field}**: {what}." for field, what in TWIN_FIELDS),
    ]
    return "\n".join(lines) + "\n"


# --- commands -------------------------------------------------------------------------------------


def release_base(git: GitClient, repository: Path, head: str, since: str | None) -> tuple[str | None, str, str]:
    """The last release HEAD holds, or None, and the base a run reads from, `since` or that release, with its
    commit."""
    released = released_tag(git, repository, head)
    if since is not None:
        base = since
    elif released is not None:
        base = released
    else:
        raise AuditError("no release tag is reachable from HEAD; pass --since <ref>")
    return released, base, commit_of(git, repository, base)


def release(
    git: GitClient, github: GitHubClient, repository: Path, since: str | None, today: Callable[[], date]
) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    released, base, base_commit = release_base(git, repository, head, since)
    found = changes(git, repository, base_commit, head)
    if not found:
        raise AuditError(f"no file changed since {base}, so a release audit would read nothing; pass --since <ref>")
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
            entry = reads[area.name]
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
    if not files:
        raise AuditError(f"HEAD tracks no file of the {area.name} area, so its reader would read nothing")
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
    record_read = read_guidance_record(path, list(titles))
    pages = record_read.pages
    unread = unread_runtimes(pages, list(runtimes))
    if unread:
        raise AuditError(
            f"{GUIDANCE_PATH} names no page for {', '.join(unread)}, which {RUNTIME_CATALOGUE} supports; add the "
            "runtime's pages before the audit"
        )
    if not any(page.subject == PYTHON_SUBJECT for page in pages):
        raise AuditError(f"{GUIDANCE_PATH} names no page for {PYTHON_SUBJECT}; add Python's pages before the audit")
    unlisted = [runtime for runtime in runtimes if runtime not in RUNTIME_FACTS]
    if unlisted:
        raise AuditError(
            f"{Path(__file__).name}'s RUNTIME_FACTS names no file for {', '.join(unlisted)}, which "
            f"{RUNTIME_CATALOGUE} supports; add the files that state the runtime's facts before the audit"
        )
    lines = [f'GUIDANCE_RECORD "{GUIDANCE_PATH}"', f"HEAD {head}"]
    by_subject = {subject: [page for page in pages if page.subject == subject] for subject in titles}
    lines += [f"SUBJECT {subject} {len(found)} {oldest_read(found)}" for subject, found in by_subject.items()]
    if record is not None:
        if main_checkout:
            raise AuditError(
                f"{top.as_posix()} is the main checkout; record the guidance audit from a worktree, such as one "
                "`python tools/worktrees.py new chore <name>` creates, and merge it through a pull request"
            )
        return [*lines, record_reads(git, repository, path, record_read, record)]
    released = released_tag(git, repository, head)
    milestones = open_milestones_after(github, released)
    following = milestones[0] if milestones else None
    after = f" after {released}" if released is not None else ""
    sentence = f"Found by the guidance audit{after}, read against commit {short(head)}, on {today()}."
    lines += [*milestone_lines(following, following), f"PROVENANCE {json_text(sentence)}"]
    lines += guidance_lines(GUIDANCE_KINDS, guidance_audit=True)
    tracked = set(tracked_files(git, repository, head))
    changelogs = {changelog.subject: changelog for changelog in record_read.changelogs}
    briefs = [
        (
            subject,
            guidance_brief(titles[subject], found, subject_facts(subject, tracked), head, changelogs.get(subject)),
        )
        for subject, found in by_subject.items()
    ]
    return lines + write_briefs(briefs)


def twins(
    git: GitClient, github: GitHubClient, repository: Path, since: str | None, today: Callable[[], date]
) -> list[str]:
    head = commit_of(git, repository, "HEAD")
    released, base, base_commit = release_base(git, repository, head, since)
    fixes = [
        sweep_fix(git, repository, head, commit, title)
        for commit, title in merged_fixes(git, repository, base_commit, head)
    ]
    lines = [f"BASE {base} {base_commit}", f"HEAD {head}", f"FIXES {len(fixes)}"]
    for fix in fixes:
        lines += [
            f"FIX {fix.commit} {fix.pull if fix.pull is not None else 'none'} {json_text(fix.title)}",
            f"CONSTRUCT {fix.commit} {len(fix.construct)}",
            *(
                [f"CONSTRUCT_CUT {fix.commit} {MAX_CONSTRUCT_LINES}"]
                if len(fix.construct) > MAX_CONSTRUCT_LINES
                else []
            ),
            f"HITS {fix.commit} {len(fix.hits)}",
            *([f"HITS_CUT {fix.commit} {MAX_HITS}"] if fix.hits_cut else []),
        ]
    if not fixes:
        return lines
    milestones = open_milestones_after(github, released)
    lines += milestone_lines(milestones[0] if milestones else None, milestones[1] if len(milestones) > 1 else None)
    sentence = f"Found by the twin sweep of the fixes since {base}, at commit {short(head)}, on {today()}."
    lines.append(f"PROVENANCE {json_text(sentence)}")
    lines += [f"MECHANISM {name} {what}" for name, what in MECHANISMS]
    lines += [f"TEMPLATE {field} {what}" for field, what in TWIN_FIELDS]
    lines += [f"SECTION {section} {what}" for section, what in TWIN_SECTIONS]
    size = max(FIXES_PER_READER, -(-len(fixes) // MAX_TWIN_READERS))
    parts = [fixes[start : start + size] for start in range(0, len(fixes), size)]
    lines.append(f"READERS twins {len(parts)}")
    names = ["twins"] if len(parts) == 1 else [f"twins-{number}" for number in range(1, len(parts) + 1)]
    briefs = [
        (name, twin_brief(part, base, base_commit, head, number, len(parts)))
        for number, (name, part) in enumerate(zip(names, parts, strict=True), start=1)
    ]
    return lines + write_briefs(briefs)


def record_reads(git: GitClient, repository: Path, path: Path, record: GuidanceRecord, tag: str) -> str:
    """Append `tag` to every page's reads, refusing a tag a page already holds or that names no tag, and keep the
    record's changelogs as they are."""
    pages = record.pages
    if not is_release_tag(tag):
        raise AuditError(f"{tag} is not a release tag vX.Y.Z")
    if any(tag in page.read for page in pages):
        raise AuditError(f"{tag} is already in the guidance record")
    commit_of(git, repository, f"refs/tags/{tag}")
    try:
        read = [Page(page.url, page.subject, page.governs, (*page.read, tag)) for page in pages]
        write_guidance(path, GuidanceRecord(read, record.changelogs))
    except OSError as exc:
        raise AuditError(f"cannot write the guidance record {path.as_posix()}, which is unchanged: {exc}") from exc
    return f"RECORDED_PAGES {tag} {len(pages)}"


def append_entry(git: GitClient, repository: Path, path: Path, entries: list[Entry], entry: Entry) -> str:
    if not is_release_tag(entry.tag):
        raise AuditError(f"{entry.tag} is not a release tag vX.Y.Z")
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
        arguments = ["issue", "list", "--state", "open", f"--search={query}", "--json", "number,title"]
        listing = gh_listing(github, arguments, SEARCH_LIMIT)
        found = issues_of(listing.items, f"for query {number}")
        lines.append(f"MATCHES {number} {len(found)}")
        lines += [f"MATCH {number} {issue.number} {json_text(issue.title)}" for issue in found]
        if listing.cut:
            lines.append(f"MATCHES_CUT {number} {SEARCH_LIMIT}")
    return lines


def remove_briefs(directory: Path) -> list[str]:
    """Delete a briefs directory this script made: a BRIEFS_PREFIX directory directly in the temporary directory,
    reached through no link, holding the BRIEFS_MARKER that names it and otherwise only Markdown files. Anything else
    is refused and left as it is. The marker goes last, so a removal that fails part way can be run again."""
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = directory.resolve()
    if (
        not is_plain_directory(directory)
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
        strays = [entry.name for entry in briefs if not is_plain_file(entry) or entry.suffix != ".md"]
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
    if not is_plain_file(marker):
        return False
    try:
        record = json_reader.read_file(marker)
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
    twins_parser = commands.add_parser("twins", parents=[common], help="brief each fix since a tag for its twins")
    twins_parser.add_argument("--since", metavar="REF", help="the base (default: the last release HEAD holds)")
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
            elif options.command == "twins":
                lines = twins(client, gh, options.repository, options.since, today)
            else:
                lines = duplicates(gh, options.queries)
    except AuditError as exc:
        print(f"FAILED {exc}")
        return 1
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    run_main(main, errors="replace", newline="\n")
