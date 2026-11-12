"""The fault drivers of deploy.py, the programs under tests/, and the repository skills' entry points under
.claude/skills/; see fault_table.py."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from fault_table import MISSING, OPENED_ONLY, STAT_RAISES, UNREADABLE, Driver, Fixture

AUDIT = ".claude/skills/audit-repository/scripts/audit_repository.py"
CHANGE_SKILL = ".claude/skills/change-skill/scripts/change_skill.py"
PULL_REQUEST = ".claude/skills/implement-change/scripts/pull_request.py"
DOC_PASSAGES = ".claude/skills/review-repository/scripts/doc_passages.py"
UNTESTED = ".claude/skills/review-repository/scripts/untested_functions.py"
SHEPHERD = ".claude/skills/shepherd-pull-requests/scripts/shepherd_pull_requests.py"
# The configuration deploy.py reads for this repository's source ID, under the fixture's home.
SOURCE_ID = "ejryan89/coding-agent-skills"
CONFIG = f"{{home}}/.claude/deployer/config/{hashlib.sha256(SOURCE_ID.encode()).hexdigest()[:12]}.config"
# The rotation and guidance records audit_repository.py reads from the checkout it audits.
ROTATION = "{checkout}/.claude/skills/audit-repository/references/rotation.json"
GUIDANCE = "{checkout}/.claude/skills/audit-repository/references/guidance.json"
CATALOGUE = "{checkout}/skills/ejryan89-skill-core/scripts/runtime_support.py"
BRIEFS = "{root}/audit-repository-fixture"
KNOWN_FAILURES = "{checkout}/tests/malformed_input/known_failures.json"
MARKER = f"{BRIEFS}/.audit-repository-briefs.json"
# A review condition's snapshot, its diff, and the file it writes.
SNAPSHOT = "{root}/snapshot"
DIFF = "{root}/change.diff"
CONDITION_OUTPUT = "{root}/condition.out"
CONDITION_ENVIRONMENT = {"CODE_REVIEW_DIFF_FILE": DIFF, "CODE_REVIEW_CONDITION_OUTPUT": CONDITION_OUTPUT}
# A pull request's template, title, and body, and the remote its branch is pushed to.
TEMPLATE = "## Summary\n\n<!-- What changed. -->\n"
TITLE_FILE = "{root}/title.txt"
BODY_FILE = "{root}/body.md"
BASE = ("--base", "origin/main")
# A GitHub repository origin may name, which no run reaches: gh's runner is the fault's, and git's never contacts it.
GITHUB_ORIGIN = "https://github.com/example/fixture.git"


def formatted(fixture: Fixture, template: str) -> Path:
    return Path(template.format(**fixture.values()))


def write(path: Path, text: str) -> None:
    """Write text with LF line ends on every platform, as a checkout's files and the deployer's configuration hold."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def configured_home(fixture: Fixture) -> None:
    """The configuration deploy.py reads for this source, naming a folder of the fixture's home as REPOS_ROOT."""
    repositories = fixture.home / "repos"
    repositories.mkdir()
    write(formatted(fixture, CONFIG), f"_source_id={SOURCE_ID}\nREPOS_ROOT={repositories.as_posix()}\n")


def two_commits(fixture: Fixture) -> None:
    """A repository whose second commit is a fix that removes a line HEAD still holds elsewhere."""
    construct = "    return compute_the_total(values)\n"
    fixture.repository(
        {"tools/sum.py": f"def total(values):\n{construct}", "tools/other.py": f"def other():\n{construct}"}
    )
    write(fixture.work / "tools" / "sum.py", "def total(values):\n    return sum(values)\n")
    fixture.git("commit", "-q", "-am", "fix(tools): sum the values")


def briefs_directory(fixture: Fixture) -> None:
    """A briefs directory a release run wrote, directly in the temporary directory the run is given."""
    directory = formatted(fixture, BRIEFS)
    write(directory / "area.md", "# Brief\n")
    marker = formatted(fixture, MARKER)
    marker.write_text(json.dumps({"directory": directory.resolve().as_posix()}), encoding="utf-8")


def snapshot(fixture: Fixture, documents: bool = True) -> None:
    """A review's snapshot holding a script, its suite, a document naming the script, and the manifest, and a diff
    changing the script."""
    root = formatted(fixture, SNAPSHOT)
    write(root / "tools" / "widget_tool.py", "def widget_total(values):\n    return sum(values)\n")
    write(root / "tests" / "tools" / "test_widget_tool.py", "import widget_tool\n")
    if documents:
        write(root / "docs" / "guide.md", "# Guide\n\nRun `python -B tools/widget_tool.py` daily.\n")
    write(root / "source-snapshot.json", json.dumps({"schema_version": 1, "source_hashes": {}, "excluded_paths": {}}))
    diff = (
        "diff --git a/tools/widget_tool.py b/tools/widget_tool.py\nindex 1111111..2222222 100644\n"
        "--- a/tools/widget_tool.py\n+++ b/tools/widget_tool.py\n@@ -1,2 +1,2 @@ def widget_total(values):\n"
        " def widget_total(values):\n-    return 0\n+    return sum(values)\n"
    )
    write(formatted(fixture, DIFF), diff)


def pushed_branch(fixture: Fixture) -> None:
    """A repository with a pull request template, its main on a local origin, and a branch past it pushed there, with
    a title and a body that pass every check."""
    origin = fixture.root / "origin.git"
    fixture.git("init", "-q", "--bare", "-b", "main", str(origin), folder=fixture.root)
    fixture.repository({".github/pull_request_template.md": TEMPLATE})
    fixture.git("remote", "add", "origin", str(origin))
    fixture.git("push", "-q", "origin", "main")
    fixture.git("switch", "-q", "-c", "fix/widget")
    write(fixture.work / "widget.txt", "widget\n")
    fixture.git("add", "widget.txt")
    fixture.git("commit", "-q", "-m", "fix: widget")
    fixture.git("push", "-q", "-u", "origin", "fix/widget")
    # A fetch the run repeats writes FETCH_HEAD as it is, so the work folder is compared as it was.
    fixture.git("fetch", "-q", "origin", "main")
    # Files no older than the index are racily clean, so each `git status` the run takes would write the index again;
    # aged, one refresh here leaves an index the run's status reads as it is.
    aged = time.time() - 60
    for path in (fixture.work / ".github" / "pull_request_template.md", fixture.work / "widget.txt"):
        os.utime(path, (aged, aged))
    fixture.git("status", "--porcelain")
    write(formatted(fixture, TITLE_FILE), "fix: widget\n")
    write(formatted(fixture, BODY_FILE), "## Summary\n\nThe widget.\n")


def template_repository(fixture: Fixture) -> None:
    fixture.repository({".github/pull_request_template.md": TEMPLATE})


def owed_issue(fixture: Fixture) -> None:
    """A known failure in the checkout's list, so the program asks git for the origin before it would ask GitHub; the
    same entry each run, so the checkout every run shares reads alike whichever ran first."""
    entry = {"reader": "fixture.reader", "case": "deep_nesting", "issue": 1}
    write(formatted(fixture, KNOWN_FAILURES), json.dumps([entry]))


def shepherd_hub(fixture: Fixture) -> None:
    fixture.repository()
    fixture.git("remote", "add", "origin", GITHUB_ORIGIN)


# Why a stat fault cannot apply to a file the command opens and reads without asking its kind or presence first: an
# open on Windows and elsewhere takes no stat the harness can fail, so the read succeeds and the command goes on.

PR_TEMPLATE = "{work}/.github/pull_request_template.md"
# A pull request template is looked for by listing its folders, as GitHub reads one, so one that is not there is never
# opened: the run refuses as its docstring says, with "no pull request template" naming the file it looked for.
NO_TEMPLATE = "pull_request.py lists the template's folders, so a template that is not there is its documented refusal"
SNAPSHOT_MANIFEST = f"{SNAPSHOT}/source-snapshot.json"
# A snapshot without a manifest leaves nothing out, as conditions.snapshot_exclusions documents.
NO_MANIFEST = "conditions.snapshot_exclusions reads a snapshot with no manifest as one that leaves nothing out"


def condition(entry: str, inputs: tuple[str, ...], **not_run: str) -> Driver:
    """A review condition run on the fixture's snapshot and diff, with the faults it is not run under by name."""
    return Driver(
        entry,
        "",
        ("--source-root", SNAPSHOT),
        snapshot,
        inputs,
        not_run=not_run,
        environment=CONDITION_ENVIRONMENT,
        outputs=(CONDITION_OUTPUT,),
    )


DRIVERS: list[Driver] = [
    # A dry run at a commit reads the configuration, then asks git for the commit before it renders anything.
    Driver(
        "deploy.py",
        "configure",
        ("configure",),
        configured_home,
        (CONFIG,),
        from_checkout=True,
        not_run={
            MISSING: "configure writes the configuration, so one that is not there is a first run, which asks for "
            "each value, not a file it fails to read; tests/deployer/test_configure.py drives its prompts and the "
            "end of input"
        },
    ),
    Driver(
        "deploy.py",
        "",
        ("--dry-run", "--all", "--at", "HEAD"),
        configured_home,
        (CONFIG,),
        git=True,
        from_checkout=True,
    ),
    Driver(
        AUDIT, "release", ("release", "--repository", "{work}", "--since", "HEAD~1"), two_commits, git=True, gh=True
    ),
    Driver(AUDIT, "twins", ("twins", "--repository", "{work}", "--since", "HEAD~1"), two_commits, git=True, gh=True),
    Driver(
        AUDIT,
        "rotation",
        ("rotation", "--repository", "{checkout}"),
        inputs=(ROTATION,),
        git=True,
        gh=True,
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        AUDIT,
        "guidance",
        ("guidance", "--repository", "{checkout}"),
        inputs=(GUIDANCE, CATALOGUE),
        git=True,
        gh=True,
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(AUDIT, "duplicates", ("duplicates", "--repository", "{work}", "audit finding"), gh=True),
    Driver(
        AUDIT,
        "remove-briefs",
        ("remove-briefs", BRIEFS),
        briefs_directory,
        (MARKER,),
        environment={"TMP": "{root}", "TEMP": "{root}", "TMPDIR": "{root}"},
        outputs=(BRIEFS,),
    ),
    Driver(
        CHANGE_SKILL,
        "section",
        ("section", "Files"),
        inputs=("{checkout}/docs/adding-a-skill.md",),
        from_checkout=True,
    ),
    Driver(CHANGE_SKILL, "gates", ("gates", "--base", "HEAD"), git=True, from_checkout=True),
    Driver(
        PULL_REQUEST,
        "draft",
        ("--repository", "{work}", "draft", "--closes", "1"),
        template_repository,
        (PR_TEMPLATE,),
        git=True,
        not_run={MISSING: NO_TEMPLATE},
    ),
    *(
        Driver(
            PULL_REQUEST,
            command,
            ("--repository", "{work}", command, "--title-file", TITLE_FILE, "--body-file", BODY_FILE, *BASE),
            pushed_branch,
            inputs,
            git=git,
            gh=git and command == "open",
            not_run=not_run,
        )
        for command in ("check", "open")
        for inputs, git, not_run in (
            ((TITLE_FILE, BODY_FILE), True, {STAT_RAISES: OPENED_ONLY}),
            ((PR_TEMPLATE,), False, {MISSING: NO_TEMPLATE}),
        )
    ),
    condition(DOC_PASSAGES, (DIFF,), stat_raises=OPENED_ONLY),
    condition(
        DOC_PASSAGES,
        (f"{SNAPSHOT}/docs/guide.md",),
        missing="a document the snapshot does not hold is one the route does not search; doc_passages.py fails only "
        "when the snapshot holds none of them, as its docstring says",
        directory="a folder is no document, so it is one the route does not search, as a document that is not there",
    ),
    condition(DOC_PASSAGES, (SNAPSHOT_MANIFEST,), missing=NO_MANIFEST, stat_raises=OPENED_ONLY),
    condition(UNTESTED, (DIFF,), stat_raises=OPENED_ONLY),
    condition(UNTESTED, (f"{SNAPSHOT}/tools/widget_tool.py",)),
    condition(UNTESTED, (SNAPSHOT_MANIFEST,), missing=NO_MANIFEST, stat_raises=OPENED_ONLY),
    Driver(SHEPHERD, "", ("--dry-run",), shepherd_hub, git=True, gh=True),
    Driver(
        "tests/known_failure_issues.py",
        "",
        (),
        owed_issue,
        (KNOWN_FAILURES, "{checkout}/tests/malformed_input/readers.py"),
        # gh is not run: the program asks GitHub only once `git remote get-url origin` names a GitHub repository,
        # which reads a remote only from the repository's own configuration, and the checkout copy every run shares
        # has none, which one run may not add for all. tests/validation/test_known_failures.py holds a gh that cannot
        # answer to a FAILED line.
        git=True,
        not_run={STAT_RAISES: OPENED_ONLY},
        from_checkout=True,
    ),
    Driver(
        "tests/run_validation.py",
        "",
        ("--pr-title", TITLE_FILE),
        lambda fixture: write(formatted(fixture, TITLE_FILE), "fix: widget\n"),
        (TITLE_FILE,),
        not_run={
            UNREADABLE: "run_validation.py starts each prerequisite tool to read its version before a policy check "
            "reads the title, and the harness stops a run that starts a program other than git"
        },
    ),
]
NOT_RUN: dict[str, str] = {
    "deploy.py check": "it starts each prerequisite program to report its version, which the harness stops, before "
    "it reads anything a fault could reach; tests/deployer/test_check.py drives its refusals",
    "deploy.py verify": "it starts Codex and Copilot CLI to list their skills, which the harness stops, before it "
    "reads the manifest; tests/deployer/test_verify.py drives its refusals",
}
