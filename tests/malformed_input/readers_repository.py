"""The repository's own readers: its tools' records and catalogues, its repository skills' scripts, and validation's
tables.

Each refusal is the exception its module names; a reader that answers a file it cannot read with a value, a skipped
line, or a problem line is called through an adapter that raises that answer as Refused.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import cast
from unittest import mock

from reader_table import REPOSITORY, NotText, Reader, Refused, module_at, text_of

sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests" / "validation"))

import json_reader
from github_client import CommandResult, GitHubClient, Listing
from guidance_record import GUIDANCE_PATH, PYTHON_SUBJECT, Changelog
from job_selection import job_seconds
from known_failures import KnownFailuresUnreadable, read_known_failures
from runtime_support import RUNTIMES

from deployer.errors import DeployError
from tools import (
    branch_protection,
    captured_samples,
    deployable_report,
    hub_guard,
    mutation_gate,
    plugin_build,
    plugin_canary,
    plugin_release,
    runtime_canary,
    runtime_changelogs,
    runtime_prompts,
    skill_evals_judging,
    skill_evals_runs,
    skill_evals_scenarios,
    skill_reference,
    skill_shims,
)
from tools.skill_evals_records import MODELS, RecordError

REPOSITORY_SKILLS = REPOSITORY / ".claude" / "skills"
audit_repository = module_at(REPOSITORY_SKILLS / "audit-repository" / "scripts" / "audit_repository.py")
change_skill = module_at(REPOSITORY_SKILLS / "change-skill" / "scripts" / "change_skill.py")
conditions = module_at(REPOSITORY_SKILLS / "review-repository" / "scripts" / "conditions.py")
pull_request = module_at(REPOSITORY_SKILLS / "implement-change" / "scripts" / "pull_request.py")
untested_functions = module_at(REPOSITORY_SKILLS / "review-repository" / "scripts" / "untested_functions.py")
shepherd = module_at(REPOSITORY_SKILLS / "shepherd-pull-requests" / "scripts" / "shepherd_pull_requests.py")

GUIDANCE_SUBJECTS = [*RUNTIMES, PYTHON_SUBJECT]
GUIDANCE = (REPOSITORY / GUIDANCE_PATH).read_bytes()
FRONTMATTER = b"---\nname: example\ndescription: An example. Use it when an example is wanted.\n---\n\nBody.\n"
ONE_LINE_TITLE = (
    "the title check refuses only a title file with no title or more than one line, so one line is its title"
)

# A trigger scenario of a shipped, model-invocable skill, and the trigger record of its requests on one model.
TRIGGER_SCENARIO = json.dumps(
    {
        "skill": "review-prs",
        "mode": "trigger",
        "requests": ["Review my pull requests."],
        "near_misses": ["Review the design document at docs/design/export.md."],
        "unserved": ["Write a poem."],
    }
).encode()
TRIGGER_FIXTURE = skill_evals_scenarios.load_scenario(
    "review-prs", REPOSITORY / "tests" / "fixtures" / "skill-evals" / "review-prs" / "description"
)
TRIGGER_MODEL = "opus"
TRIGGER_RECORD = json.dumps(
    {
        "requests": [
            {"request": request, "started": []} for request in [*TRIGGER_FIXTURE.requests, *TRIGGER_FIXTURE.near_misses]
        ],
        "models": [MODELS[TRIGGER_MODEL]],
    }
).encode()


# A selection of one module that both legs run, and the Windows leg's run of its mutant.
MUTATION_MUTANT = {"line": 4, "before": "a", "after": "b", "source": "b\n", "at_import": False, "readers": [5]}
MUTATION_SELECTION = {
    "base": "origin/main",
    "mutmut": "3.8.0",
    "head": "cd" * 20,
    "suites": ["tests/c.py"],
    "carried": None,
    "modules": [
        {
            "module": "tools/paths.py",
            "lines": 1,
            "tables": [["X_ALLOWED", "tests/validation/x.py"]],
            "windows": True,
            "digest": "ab" * 32,
            "mutants": [MUTATION_MUTANT],
        }
    ],
}
MUTATION_LEG = {
    "leg": mutation_gate.WINDOWS_LEG,
    "shard": 0,
    "shards": 1,
    "minutes": 40,
    "seconds": 1.5,
    "workers": 2,
    "failed_suites": {"tests/a.py": "failed unmutated with exit code 1"},
    "unmeasured_suites": ["tests/b.py"],
    "needed": {"tools/paths.py": 12.5},
    "available": 30.0,
    # The CI leg timed out on the mutant, which the local run then killed.
    "outcomes": {"tools/paths.py": [{"status": mutation_gate.TIMED_OUT, "suites": ["tests/c.py"], "tests": [1]}]},
}
MUTATION_LOCAL = {
    "leg": mutation_gate.WINDOWS_LEG,
    "minutes": 60,
    "seconds": 12.0,
    "workers": 2,
    "mutants": {"tools/paths.py": [0]},
    "failed_suites": {"tests/d.py": "failed unmutated with exit code 1"},
    "unmeasured_suites": ["tests/e.py"],
    "outcomes": {"tools/paths.py": [{"status": mutation_gate.KILLED, "suites": ["tests/c.py"], "tests": [1]}]},
}
# The verdict a finished run of the selection left, with the object name of its one tested file.
MUTATION_VERDICT = {
    "run": "https://github.com/example/repository/actions/runs/1/attempts/1",
    "selection": MUTATION_SELECTION,
    "files": {"tools/paths.py": "ef" * 20},
    "summary": "## Mutation gate\n\nEvery mutant on a changed line was killed.\n",
}


def _mutation_leg(path: Path) -> object:
    selected = mutation_gate.read_selection(path.with_name(mutation_gate.MUTANTS_FILE))
    return mutation_gate.read_leg(path, selected, mutation_gate.WINDOWS_LEG)


def _mutation_selection(path: Path) -> None:
    path.with_name(mutation_gate.MUTANTS_FILE).write_text(json.dumps(MUTATION_SELECTION), encoding="utf-8")


def _mutation_local(path: Path) -> object:
    selected = mutation_gate.read_selection(path.with_name(mutation_gate.MUTANTS_FILE))
    leg = mutation_gate.read_leg(
        path.with_name(mutation_gate.outcomes_name(mutation_gate.WINDOWS_LEG)), selected, mutation_gate.WINDOWS_LEG
    )
    return mutation_gate.read_local(mutation_gate.read_record(path), path.name, leg)


def _mutation_leg_record(path: Path) -> None:
    _mutation_selection(path)
    path.with_name(mutation_gate.outcomes_name(mutation_gate.WINDOWS_LEG)).write_text(
        json.dumps(MUTATION_LEG), encoding="utf-8"
    )


def _catalogue(path: Path) -> object:
    return plugin_build.load_catalogue(path.parents[1])


def _marketplace(path: Path) -> object:
    return plugin_release.read_marketplace(path.parents[1])


def _canary_records(path: Path) -> object:
    """The records, with each line the reader set aside, as a crashed writer's incomplete line is, refused."""
    records = runtime_canary.read_records(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    if len(records) < len(lines):
        raise Refused(f"{len(lines) - len(records)} of {len(lines)} lines set aside")
    return records


def _scenario(path: Path) -> object:
    return skill_evals_scenarios.load_scenario("review-prs", path.parent)


def _trigger_record(path: Path) -> object:
    return skill_evals_judging.records_in(path.parents[1])(TRIGGER_FIXTURE, TRIGGER_MODEL, None)


def _guidance_watch(path: Path) -> object:
    return runtime_changelogs.read_record(path.parents[4])


def _shims(path: Path) -> object:
    """The shim files of the one repository skill: none for no closed frontmatter, a reason for one unreadable."""
    expected = skill_shims._expected(path.parents[3])
    found = expected["example"]
    if not isinstance(found, dict):
        raise Refused(f"no shim: {found}")
    return found


def _rotation(path: Path) -> object:
    return audit_repository.read_record(path)


def _runtime_catalogue(path: Path) -> object:
    return audit_repository.supported_runtimes(path.parents[3])


def _guidance_audit(path: Path) -> object:
    return audit_repository.read_guidance_record(path, GUIDANCE_SUBJECTS)


def _skill_metadata(path: Path) -> object:
    found = change_skill.metadata(text_of(path, "utf-8-sig"))
    if found is None:
        raise Refused("not a JSON object")
    return found


def _closed_issues(path: Path) -> object:
    return pull_request.closed_issues(text_of(path))


def _python_definitions(path: Path) -> object:
    return untested_functions.python_definitions(path.name, text_of(path))


# What the shepherd reads from gh, with the fields PULL_FIELDS and RUN_FIELDS request: one open pull request, its
# Validation run with a failed leg, and the runs on its head. GitHub's JSON for each is an object or a list of them.
SHEPHERD_HEAD = "a" * 40
SHEPHERD_PULL = {
    "number": 12,
    "headRefName": "fix/example",
    "headRefOid": SHEPHERD_HEAD,
    "isCrossRepository": False,
    "isDraft": False,
    "mergeStateStatus": "CLEAN",
    "autoMergeRequest": {"enabledAt": "2026-10-25T12:00:00Z", "mergeMethod": "SQUASH"},
    "files": [{"path": "tools/example.py", "additions": 1, "deletions": 0}],
    "changedFiles": 1,
    "statusCheckRollup": [
        {
            "__typename": "CheckRun",
            "name": "Validation",
            "workflowName": "Validation",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-10-25T12:00:00Z",
            "detailsUrl": "https://github.com/owner/repo/actions/runs/7/job/8",
        }
    ],
    "body": "Closes #1",
    "baseRefName": "main",
}
SHEPHERD_RUN = {
    "headSha": SHEPHERD_HEAD,
    "attempt": 1,
    "conclusion": "failure",
    "status": "completed",
    "jobs": [
        {
            "databaseId": 8,
            "name": "Linux shard 1/2",
            "conclusion": "failure",
            "steps": [
                {"name": "Set up job", "conclusion": "success"},
                {"name": "Run repository validation", "conclusion": "failure"},
            ],
        }
    ],
}


SHEPHERD_UNREAD = (
    "the shepherd reads a file's path, a check's kind, name, workflow, state, start, and run, and whether auto-merge "
    "is requested, never this field"
)


class _ShepherdGh:
    """A gh client that answers the shepherd's run view, run listing, and failed-leg log from the case's file."""

    def __init__(self, path: Path, answer: str) -> None:
        self.path, self.answer = path, answer

    def document(self) -> object:
        try:
            return json_reader.read_file(self.path)
        except (OSError, ValueError) as exc:
            raise shepherd.GitHubError(f"gh printed no JSON: {exc}", kind="malformed") from exc

    def json(self, arguments: list[str]) -> object:
        return self.document() if self.answer == "run" else {"headSha": SHEPHERD_HEAD, "attempt": 1, "jobs": []}

    def capped_list(self, arguments: list[str], limit: int) -> object:
        items = self.document() if self.answer == "runs" else [{"databaseId": 7}]
        if not isinstance(items, list):
            raise shepherd.GitHubError("gh printed a listing that is not a JSON list", kind="malformed")
        return Listing(items, False)

    def run(self, arguments: list[str]) -> CommandResult:
        return CommandResult(0, "", "")


def _shepherd_pull(path: Path) -> object:
    try:
        item = json_reader.read_file(path)
    except (OSError, ValueError) as exc:
        raise shepherd.GitHubError(f"gh printed no JSON: {exc}", kind="malformed") from exc
    return shepherd.pull_of(item)


def _shepherd_pull_on(head: str) -> object:
    return shepherd.Pull(
        12, "fix/example", head, "CLEAN", False, shepherd.Check("FAILURE", 7), (), False, False, None, "main", 0
    )


def _shepherd_judge(path: Path) -> object:
    return shepherd.judge(_ShepherdGh(path, "run"), "owner/repo", _shepherd_pull_on(SHEPHERD_HEAD))


def _shepherd_runs(path: Path) -> object:
    return shepherd.newest_run(_ShepherdGh(path, "runs"), "owner/repo", SHEPHERD_HEAD)


def _shepherd_run_of(path: Path) -> object:
    return shepherd.run_of({"detailsUrl": text_of(path)})


def _title(path: Path) -> object:
    title, problems = pull_request.read_title(pull_request.read_file(path))
    if problems:
        raise Refused(" ".join(problems))
    return title


# A provenance record of one captured sample, as tools/captured_samples.py reads it.
PROVENANCE = json.dumps(
    {"samples": {"changelogs/example.md": {"versions": {RUNTIMES[0]: "1.2.3"}, "captured": "2026-01-02", "how": "x"}}}
).encode()
# A coverage.py JSON report of one module, one statement run by one test of the suite.
COVERAGE_MODULE = "module.py"
COVERAGE_SUITE = "test_module.py"
COVERAGE_REPORT = json.dumps(
    {
        "files": {
            COVERAGE_MODULE: {
                "executed_lines": [1],
                "missing_lines": [2],
                "excluded_lines": [],
                "contexts": {"1": [f"{COVERAGE_SUITE}::test_one|run"]},
            }
        }
    }
).encode()
# main's protection as gh api answers it.
PROTECTION = json.dumps({"required_status_checks": {"strict": True, "contexts": ["Validation"]}}).encode()


def _provenance(path: Path) -> object:
    return captured_samples.read(path.parents[len(captured_samples.PROVENANCE.parts) - 1], RUNTIMES)


def _coverage_module(path: Path) -> None:
    (path.parent / COVERAGE_MODULE).write_text("first = 1\nsecond = 2\n", encoding="utf-8")


def _coverage_report(path: Path) -> object:
    """The lines and tests a report says ran, its refusal raised as Refused."""
    report = mutation_gate.read_report(path)
    if isinstance(report, str):
        raise Refused(report)
    modules = [COVERAGE_MODULE]
    collected = [f"{COVERAGE_SUITE}::test_one"]
    return (
        mutation_gate.executed_lines(report, path.parent, modules),
        mutation_gate.executed_tests(report, path.parent, modules, COVERAGE_SUITE, collected),
    )


class _ProtectionGh:
    """gh answering every api call with the case's file as its output, decoded as GitHubClient decodes gh's output:
    UTF-8 with each byte that is not kept as a lone surrogate, never refused."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def run(self, arguments: list[str]) -> CommandResult:
        # gh's output is never a directory or a file it cannot open, so the case's own read failing is no answer.
        try:
            output = self.path.read_bytes()
        except OSError as error:
            raise NotText(f"{type(error).__name__}: {error}") from error
        return CommandResult(0, output.decode("utf-8", "surrogateescape"), "")


def _protection(path: Path) -> object:
    document = branch_protection._read(cast(GitHubClient, _ProtectionGh(path)), branch_protection.PROTECTION_ENDPOINT)
    return branch_protection.protection_problems(document, document, document, document, document)


# A PreToolUse event of an edit to a file outside every checkout, which the guard reads and lets through.
HUB_EVENT = json.dumps(
    {"tool_name": "Write", "tool_input": {"file_path": "example.txt"}, "cwd": tempfile.gettempdir()}
).encode()


def _hub_guard(path: Path) -> object:
    """The guard on the event as its hook gives it on stdin: an event it skips, or a refusal it prints, is Refused."""
    printed, noted = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(noted):
        code = hub_guard.guard(text_of(path))
    if printed.getvalue() or "hub guard skipped" in noted.getvalue():
        raise Refused(f"{printed.getvalue()}{noted.getvalue()}")
    return code


# Each Claude Code stream reader passes over a line that is not an event it knows, so a stream from which it takes
# nothing is its answer to one it cannot read: its caller reports that stream as a session that showed nothing.
STREAM_PASSES_OVER = {
    "torn_line": "a line cut off part way is no event, and the events before it are read",
    "blank_lines": "a blank line holds no event and is passed over",
}


def _claude_stream(path: Path) -> object:
    skills, denials = runtime_canary.claude_stream(text_of(path))
    if skills is None and not denials:
        raise Refused("the stream holds no init event and no denial")
    return skills, denials


def _skills_in_stream(path: Path) -> object:
    found = skill_evals_runs.skills_in_stream(text_of(path))
    if not found:
        raise Refused("the stream starts no skill")
    return found


def _stream_models(path: Path) -> object:
    session, subagents = skill_evals_runs.stream_models(text_of(path))
    if not session | subagents:
        raise Refused("the stream names no model")
    return session, subagents


def _plugin_events(path: Path) -> object:
    events = list(plugin_canary.stream_events(text_of(path)))
    if not events:
        raise Refused("the stream holds no event")
    return events


PLUGIN_ID = f"example@{plugin_canary.MARKETPLACE}"


def _plugin_listing(path: Path) -> object:
    entry = plugin_canary.listed_entry(text_of(path), PLUGIN_ID)
    if entry is None:
        raise Refused("the listing does not list the plugin")
    return entry


# The home a canary marker names, which stands for the case's own folder, since no fixed document can name that.
CANARY_HOME = "<the canary's home>"


def _canary_claim(path: Path) -> object:
    home = path.parents[1]
    lexical = runtime_canary.lexical
    with mock.patch.object(
        runtime_canary, "lexical", lambda value: lexical(home) if value == CANARY_HOME else lexical(value)
    ):
        if not runtime_canary.claimed(home):
            raise Refused("the marker does not claim its home")
    return True


CHANGELOG = Changelog("codex", "https://example.com/releases", "github-releases", "1.1.0")
# One page of GitHub's releases API: a release after the read_through, and the read_through itself.
RELEASES = json.dumps([{"tag_name": "v1.2.0", "body": "- Changed a flag"}, {"tag_name": "v1.1.0", "body": ""}]).encode()


def _github_releases(path: Path) -> object:
    try:
        body = path.read_bytes()
    except OSError as error:
        raise NotText(f"{type(error).__name__}: {error}") from error
    return runtime_changelogs.changelog_releases(
        CHANGELOG, lambda url, timeout: runtime_changelogs.Fetched(200, url, body), 1.0
    )


def _briefs_marker(path: Path) -> object:
    if not audit_repository.names_directory(path, Path("briefs")):
        raise Refused("the marker does not name the briefs directory")
    return True


def _frontmatter_value(path: Path) -> object:
    found = change_skill.frontmatter_value(text_of(path), "name")
    if found is change_skill.UNREADABLE:
        raise Refused("the frontmatter cannot be read")
    return found


def _user_only(path: Path) -> object:
    if not change_skill.user_only(text_of(path)):
        raise Refused("the frontmatter does not read as user-only")
    return True


def _snapshot_exclusions(path: Path) -> object:
    return conditions.snapshot_exclusions(path.parent)


READERS = [
    Reader(
        "audit_repository.read_guidance_record",
        "json",
        GUIDANCE,
        _guidance_audit,
        (audit_repository.AuditError,),
    ),
    Reader(
        "audit_repository.read_record",
        "json",
        (REPOSITORY / audit_repository.RECORD_PATH).read_bytes(),
        _rotation,
        (audit_repository.AuditError,),
    ),
    Reader(
        "audit_repository.supported_runtimes",
        "text",
        b'RUNTIMES = ("claude-code",)\nRUNTIME_TITLES = {"claude-code": "Claude Code"}\n',
        _runtime_catalogue,
        (audit_repository.AuditError,),
        filename=audit_repository.RUNTIME_CATALOGUE,
        # A unary chain long enough to overflow the parser's own stack, which nests far deeper than a Python walk.
        nesting=lambda levels: b"X = " + b"-" * (levels * 50) + b"1\n",
    ),
    Reader(
        "change_skill.metadata",
        "json",
        b'{"skill_deps": ["ejryan89-skill-core"]}',
        _skill_metadata,
        (Refused,),
        accepts={"bom": "head_metadata decodes each metadata file as UTF-8 with an optional byte-order mark"},
    ),
    Reader(
        "job_selection.job_seconds",
        "json",
        b'{"runs": [1], "seconds": {"Linux": {"job": 1.5}, "Windows": {"job": 2.5}}}',
        job_seconds,
        (ValueError,),
    ),
    Reader(
        "known_failures.read_known_failures",
        "json",
        b'[{"reader": "module.function", "case": "empty", "issue": 1}]',
        read_known_failures,
        (KnownFailuresUnreadable,),
        accepts={"top_level_list": "an empty list names no known failure, as the list holds once every reader passes"},
    ),
    Reader(
        "pull_request.closed_issues",
        "text",
        b"Body.\n\nCloses #12\n",
        _closed_issues,
        (pull_request.PullRequestError,),
        accepts={
            "empty": "a body with no Closes line closes no issue",
            "truncated": "a body with no Closes line closes no issue",
            "long_line": "a body with no Closes line closes no issue",
            "bom": "a byte-order mark before the body's first line leaves its Closes lines as they are",
            "nul_bytes": "NUL bytes after the body's last line leave its Closes lines as they are",
        },
        number=lambda digits: b"Closes #" + digits + b"\n",
    ),
    Reader(
        "pull_request.read_title",
        "text",
        b"feat: an example title\n",
        _title,
        (pull_request.PullRequestError, Refused),
        accepts={
            "truncated": ONE_LINE_TITLE,
            "long_line": ONE_LINE_TITLE,
            "bom": "read_file drops the byte-order mark an editor may save, so the title is judged as typed",
        },
    ),
    Reader(
        "untested_functions.python_definitions",
        "text",
        b"def example():\n    return 1\n",
        _python_definitions,
        # The condition reports a file it cannot parse as an UNPARSED block, from either error.
        (SyntaxError, ValueError),
        filename="example.py",
        accepts={
            "empty": "an empty module defines nothing",
            "bom": "the parser takes a byte-order mark before a module's first line",
            "long_line": "the case is a valid Python module, which defines nothing",
        },
        # A unary chain long enough to overflow the parser's own stack, which nests far deeper than a Python walk.
        nesting=lambda levels: b"x = " + b"-" * (levels * 50) + b"1\n",
    ),
    Reader(
        "shepherd_pull_requests.judge",
        "json",
        json.dumps(SHEPHERD_RUN).encode(),
        _shepherd_judge,
        (shepherd.GitHubError,),
        accepts={"wrong_type:status": "the judgment reads a run's conclusion and jobs, never its status"},
    ),
    Reader(
        "shepherd_pull_requests.newest_run",
        "json",
        json.dumps([{"databaseId": 7}, {"databaseId": 5}]).encode(),
        _shepherd_runs,
        (shepherd.GitHubError,),
        accepts={"top_level_list": "an empty listing is a head with no run, whose newest is none"},
    ),
    Reader(
        "shepherd_pull_requests.pull_of",
        "json",
        json.dumps(SHEPHERD_PULL).encode(),
        _shepherd_pull,
        (shepherd.GitHubError,),
        accepts={
            f"wrong_type:{field}": SHEPHERD_UNREAD
            for field in (
                "files.0.additions",
                "files.0.deletions",
                "autoMergeRequest.enabledAt",
                "autoMergeRequest.mergeMethod",
            )
        },
    ),
    Reader(
        "shepherd_pull_requests.run_of",
        "text",
        b"https://github.com/owner/repo/actions/runs/7/job/8",
        _shepherd_run_of,
        (shepherd.GitHubError,),
        accepts=dict.fromkeys(
            ("empty", "bom", "nul_bytes", "truncated", "long_line"),
            "a check whose URL names no run is one the shepherd reads as naming none",
        ),
        number=lambda digits: b"https://github.com/owner/repo/actions/runs/" + digits + b"/job/8",
    ),
    Reader(
        "tools.deployable_report._read_results",
        "json",
        b'[{"label": "first", "results": []}]',
        deployable_report._read_results,
        (DeployError,),
        accepts={"top_level_list": "an empty array is the results file of no passes"},
    ),
    Reader(
        "tools.mutation_gate.read_leg",
        "json",
        json.dumps(MUTATION_LEG).encode(),
        _mutation_leg,
        (mutation_gate.RecordError,),
        filename="outcomes-windows.json",
        prepare=_mutation_selection,
    ),
    Reader(
        "tools.mutation_gate.read_local",
        "json",
        json.dumps(MUTATION_LOCAL).encode(),
        _mutation_local,
        (mutation_gate.RecordError,),
        filename=mutation_gate.local_name(mutation_gate.WINDOWS_LEG),
        prepare=_mutation_leg_record,
    ),
    Reader(
        "tools.mutation_gate.read_verdict",
        "json",
        json.dumps(MUTATION_VERDICT).encode(),
        mutation_gate.read_verdict,
        (mutation_gate.RecordError,),
        filename=mutation_gate.VERDICT_FILE,
    ),
    Reader(
        "tools.mutation_gate.read_selection",
        "json",
        json.dumps(MUTATION_SELECTION).encode(),
        mutation_gate.read_selection,
        (mutation_gate.RecordError,),
        filename=mutation_gate.MUTANTS_FILE,
    ),
    Reader(
        "tools.plugin_build.load_catalogue",
        "json",
        b'{"plugins": [{"name": "example"}]}',
        _catalogue,
        (plugin_build.BuildError,),
        filename=plugin_build.MARKETPLACE.as_posix(),
    ),
    Reader(
        "tools.plugin_release.read_marketplace",
        "json",
        b'{"plugins": [{"name": "example"}]}',
        _marketplace,
        (plugin_release.ReleaseError,),
        filename=plugin_build.MARKETPLACE.as_posix(),
        accepts={
            "wrong_type:plugins.0.name": "the release compares each entry's name with each plugin it builds, so a "
            "name of another type fails it as a plugin the marketplace does not list",
        },
    ),
    Reader(
        "tools.runtime_canary.read_records",
        "json-lines",
        b'{"runtime": "claude", "status": "PASS"}\n',
        _canary_records,
        # A file it cannot read raises OSError, NotAFile for a directory in its place, which the run reports.
        (Refused, OSError, runtime_canary.RecordRefused),
        accepts={
            "empty": "the canary creates each records file empty, so an empty one is a run that recorded nothing",
            **{
                f"wrong_type:{field}": "each verdict compares the field with the value it expects, so a field of "
                "another type matches none and the run fails as one that recorded nothing"
                for field in ("runtime", "status")
            },
        },
    ),
    Reader(
        "tools.runtime_changelogs.read_record",
        "json",
        GUIDANCE,
        _guidance_watch,
        (runtime_changelogs.WatchError,),
        filename=GUIDANCE_PATH,
    ),
    Reader(
        "tools.skill_evals_judging.records_in",
        "json",
        TRIGGER_RECORD,
        _trigger_record,
        (RecordError,),
        filename=f"{TRIGGER_FIXTURE.name}/{TRIGGER_MODEL}.json",
    ),
    Reader(
        "tools.skill_evals_scenarios.load_scenario",
        "json",
        TRIGGER_SCENARIO,
        _scenario,
        (skill_evals_scenarios.ScenarioError,),
        filename="scenario/scenario.json",
    ),
    Reader(
        "tools.skill_reference.read_frontmatter",
        "frontmatter",
        FRONTMATTER,
        skill_reference.read_frontmatter,
        (skill_reference.SkillReferenceError,),
        accepts={
            "bom": "frontmatter.read decodes UTF-8 with an optional byte-order mark, as editors on Windows save it",
            "nul_bytes": "the body after the frontmatter's closing line is never read",
        },
    ),
    Reader(
        "tools.skill_shims._expected",
        "frontmatter",
        FRONTMATTER,
        _shims,
        # A SKILL.md it cannot read raises OSError, NotAFile for a directory in its place, ending the run as FAILED.
        (Refused, OSError, skill_shims.ShimError),
        filename=".claude/skills/example/SKILL.md",
        accepts={
            "bom": "each SKILL.md is decoded as UTF-8 with an optional byte-order mark",
            "nul_bytes": "the body after the frontmatter's closing line is never read",
        },
    ),
    Reader(
        "tools.branch_protection._read",
        "json",
        PROTECTION,
        _protection,
        (Refused, ValueError),
        open_fields="protection_problems reads each field through _get and compares it with the value it expects, so "
        "a field of another type is a drifted setting it reports",
    ),
    Reader(
        "tools.captured_samples.read",
        "json",
        PROVENANCE,
        _provenance,
        (captured_samples.SampleError,),
        filename=captured_samples.PROVENANCE.as_posix(),
    ),
    Reader(
        "tools.mutation_gate.read_report",
        "json",
        COVERAGE_REPORT,
        _coverage_report,
        (Refused,),
        filename="report.json",
        prepare=_coverage_module,
    ),
    Reader(
        "tools.runtime_prompts.read_pull_record",
        "json",
        (REPOSITORY / "tests" / "fixtures" / "skill-evals" / "review-prs" / "clean-change" / "pull.json").read_bytes(),
        runtime_prompts.read_pull_record,
        (runtime_prompts.RenderError,),
        filename="pull.json",
        open_fields="the record goes on to the review pipeline with its manifest path and a thread set, and the "
        "pipeline's own reader of a pull request record, held in its table, reads every other field by kind",
    ),
    Reader(
        "tools.hub_guard.guard",
        "json",
        HUB_EVENT,
        _hub_guard,
        (Refused,),
    ),
    Reader(
        "tools.runtime_canary.claude_stream",
        "json-lines",
        b'{"type": "system", "subtype": "init", "skills": ["example"]}\n',
        _claude_stream,
        (Refused,),
        accepts=STREAM_PASSES_OVER,
    ),
    Reader(
        "tools.skill_evals_runs.skills_in_stream",
        "json-lines",
        json.dumps(
            {
                "type": "assistant",
                "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "example"}}]},
            }
        ).encode()
        + b"\n",
        _skills_in_stream,
        (Refused, skill_evals_runs.StreamShapeError),
        accepts=STREAM_PASSES_OVER,
    ),
    Reader(
        "tools.skill_evals_runs.stream_models",
        "json-lines",
        b'{"type": "assistant", "message": {"model": "claude-example"}}\n',
        _stream_models,
        (Refused,),
        accepts=STREAM_PASSES_OVER,
    ),
    Reader(
        "tools.plugin_canary.stream_events",
        "json-lines",
        b'{"type": "result", "result": "done"}\n',
        _plugin_events,
        (Refused,),
        accepts=STREAM_PASSES_OVER,
        open_fields="it yields each event object whole, and each check reads the fields it needs by kind",
    ),
    Reader(
        "tools.plugin_canary.listed_entry",
        "json",
        json.dumps([{"id": PLUGIN_ID, "version": "1.0.0", "installPath": "/home/cache/example"}]).encode(),
        _plugin_listing,
        (Refused,),
        open_fields="it returns the plugin's entry whole, and install_path reads its installPath by kind",
    ),
    Reader(
        "tools.runtime_canary.claimed",
        "json",
        json.dumps({"made_by": "tools/runtime_canary.py", "home": CANARY_HOME}).encode(),
        _canary_claim,
        (Refused,),
        filename=f"home/{runtime_canary.STATE}/{runtime_canary.HOME_MARKER}",
        refuses_links=True,
        accepts={"wrong_type:made_by": "the claim is the home the marker names; made_by is for a person reading it"},
    ),
    Reader(
        "tools.runtime_changelogs.changelog_releases",
        "json",
        RELEASES,
        _github_releases,
        (runtime_changelogs.WatchError,),
        accepts={
            "bom": "GitHub's body is decoded as UTF-8 with an optional byte-order mark",
            "wrong_type:0": "an item that is not a release object is passed over, as a draft is, and the read_through "
            "is still found on the page",
            "wrong_type:0.tag_name": "a tag naming no release of numbers is passed over, as a tag of another product "
            "is, and the read_through is still found on the page",
            "wrong_type:0.body": "a body is read as its text, which holds no bullet, so the watch reports the release "
            "as unread with that text",
        },
    ),
    Reader(
        "audit_repository.names_directory",
        "json",
        b'{"directory": "briefs"}',
        _briefs_marker,
        (Refused,),
        refuses_links=True,
    ),
    Reader(
        "change_skill.frontmatter_value",
        "frontmatter",
        FRONTMATTER,
        _frontmatter_value,
        (Refused, NotText),
        accepts={
            **dict.fromkeys(
                ("empty", "no_frontmatter", "bom"),
                "a document whose first line is not the opening rule has no frontmatter, so each key compares as "
                "absent",
            ),
            "nul_bytes": "the body after the frontmatter's closing line is never read",
        },
    ),
    Reader(
        "change_skill.frontmatter_block",
        "frontmatter",
        FRONTMATTER,
        lambda path: change_skill.frontmatter_block(text_of(path)),
        (NotText,),
        accepts={
            **dict.fromkeys(
                ("empty", "no_frontmatter", "bom"),
                "a document whose first line is not the opening rule has no frontmatter, an empty block",
            ),
            "nul_bytes": "the body after the frontmatter's closing line is never read",
            **dict.fromkeys(
                ("truncated", "top_level_list", "deep_nesting", "duplicate_keys"),
                "a block it cannot read is compared as all the document's lines, so a change to it is still seen",
            ),
        },
    ),
    Reader(
        "change_skill.user_only",
        "frontmatter",
        b"---\nname: example\ndisable-model-invocation: true\n---\n",
        _user_only,
        (Refused, NotText),
        accepts={"nul_bytes": "the body after the frontmatter's closing line is never read"},
    ),
    Reader(
        "conditions.snapshot_exclusions",
        "json",
        b'{"excluded_paths": {".claude/settings.json": "agent-instruction"}}',
        _snapshot_exclusions,
        (conditions.MissingEvidence,),
        filename=conditions.SNAPSHOT_MANIFEST,
    ),
]
