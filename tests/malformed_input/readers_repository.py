"""The repository's own readers: its tools' records and catalogues, its repository skills' scripts, and validation's
tables.

Each refusal is the exception its module names; a reader that answers a file it cannot read with a value, a skipped
line, or a problem line is called through an adapter that raises that answer as Refused.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from reader_table import REPOSITORY, Reader, Refused, module_at, text_of

sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests" / "validation"))

import json_reader
from github_client import CommandResult, Listing
from guidance_record import GUIDANCE_PATH, PYTHON_SUBJECT
from job_selection import job_seconds
from known_failures import KnownFailuresUnreadable, read_known_failures
from runtime_support import RUNTIMES

from deployer.errors import DeployError
from tools import (
    deployable_report,
    mutation_gate,
    plugin_build,
    plugin_release,
    runtime_canary,
    runtime_changelogs,
    skill_evals_judging,
    skill_evals_scenarios,
    skill_reference,
    skill_shims,
)
from tools.skill_evals_records import MODELS, RecordError

REPOSITORY_SKILLS = REPOSITORY / ".claude" / "skills"
audit_repository = module_at(REPOSITORY_SKILLS / "audit-repository" / "scripts" / "audit_repository.py")
change_skill = module_at(REPOSITORY_SKILLS / "change-skill" / "scripts" / "change_skill.py")
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
    "modules": [
        {
            "module": "tools/paths.py",
            "lines": 1,
            "tables": [["X_ALLOWED", "tests/validation/x.py"]],
            "windows": True,
            "mutants": [MUTATION_MUTANT],
        }
    ],
}
MUTATION_LEG = {
    "leg": mutation_gate.WINDOWS_LEG,
    "minutes": 40,
    "seconds": 1.5,
    "workers": 2,
    "failed_suites": {"tests/a.py": "failed unmutated with exit code 1"},
    "unmeasured_suites": ["tests/b.py"],
    "outcomes": {"tools/paths.py": [{"status": mutation_gate.KILLED, "suites": ["tests/c.py"], "tests": [1]}]},
}


def _mutation_leg(path: Path) -> object:
    selected = mutation_gate.read_selection(path.with_name(mutation_gate.MUTANTS_FILE))
    return mutation_gate.read_leg(path, selected, mutation_gate.WINDOWS_LEG)


def _mutation_selection(path: Path) -> None:
    path.with_name(mutation_gate.MUTANTS_FILE).write_text(json.dumps(MUTATION_SELECTION), encoding="utf-8")


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
    "the shepherd reads a file's path, a check's name, workflow, state, start, and run, and whether auto-merge is "
    "requested, never this field"
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
    return shepherd.Pull(12, "fix/example", head, "CLEAN", False, shepherd.Check("FAILURE", 7), (), False, False, None)


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
                "statusCheckRollup.0.__typename",
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
]
