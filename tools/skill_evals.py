"""Run a skill's fixed scenarios, model by model, and judge the review record each run wrote.

Usage:
  python -B tools/skill_evals.py SKILL [--write] [--timeout SECONDS] [--jobs N]
  python -B tools/skill_evals.py SKILL [--scenario NAME ...] [--model NAME ...] [--records DIR]

A scenario is a directory tests/fixtures/skill-evals/<skill>/<scenario>/, which never ships. It is a code-review
fixture, which `review_pipeline.py prepare --canary --fixture <scenario>` reviews in place of a pull request: the
change as two trees, base/ and head/, and the pull request in pull.json (see review_canary.py in code-review-core). A
re-review scenario's run adds `--re-review --prior <scenario>/<prior>`. Beside them is a scenario.json:

  {"skill": "<skill>", "mode": "initial" | "re-review",
   "prior": "<prior record file in the scenario>"  (re-review only),
   "expectations": [<expectation>, ...]}

Each expectation is a statement over the record, checked here by script and never by reading the report. Severities
rank SUGGESTION < SHOULD_FIX < MUST_FIX; `lines` lists the head lines a finding may be anchored at, any of them:

  {"kind": "finding", "path": P, "lines": [N, ...], "severity": S}
      a finding at P on one of the lines, at least as severe as S
  {"kind": "no_finding_above", "severity": S, "paths": [P, ...]}
      no finding more severe than S on the paths, or on any path when `paths` is left out
  {"kind": "verdict", "verdict": "APPROVED" | "CHANGES_REQUESTED" | "INCOMPLETE"}
  {"kind": "ledger", "addressed": N, "still_present": N}     re-review only
      exactly that many ledger entries this review judged addressed, and still present
  {"kind": "disposition", "entry": "v1:F001", "disposition": D}     re-review only
      this review's disposition of that ledger entry
  {"kind": "repeats", "path": P, "lines": [N, ...], "entry": "v1:F001"}     re-review only
      every finding this review raised at P on those lines at least as severe as that ledger entry repeats the entry,
      so the problem counts once; raising none there also passes, since the ledger then carries the entry alone, and
      so does a less severe finding there, which is about another defect

An unknown kind, or a field a kind does not take, fails the whole run before anything is judged.

Records come from a record source, a function of a scenario and a model that returns the record that run wrote.
--records DIR reads DIR/<scenario>/<model>.json, the record JSON a run finalized. A missing or unreadable record fails
every expectation of its scenario and model.

Without --records, it runs the scenarios. For each model it makes a fresh home under one throwaway directory,
deploys this checkout into it with `deploy.py --canary-home`, as tools/runtime_canary.py does, and sets the model
in that home's copy of the reviewer agent, whose `model: inherit` would otherwise run every reviewer on the
session's model: a fixture's generic reviewer has no MODEL line, and `inherit` outranks CLAUDE_CODE_SUBAGENT_MODEL.
The session gets that agent with `--agents`, which outranks the same agent loaded from the home's .claude/agents:
Claude Code runs a project agent's hooks only in a folder whose workspace trust was accepted, which a `-p` session
never is, so loaded from the home the reviewer guard would never run. The definition is the home's agent file, its
frontmatter and body, with one change: its hook runs the home's review_guard.py in place of the profile folder's,
so the guard checks the home's prompts against the home's copy of the pipeline that wrote them.
Then, for each scenario and model, up to --jobs at once, it starts Claude Code headless with the model's home as
its working directory, so the deployed skills and agent load as project ones, and the prompt
`/<skill> <prepare_arguments>`. The session, which orchestrates the skill, stays on SESSION_MODEL. It has no
Workflow tool, so reviewers start as native subagents, whose messages --forward-subagent-text puts in the
transcript with the model each ran on, and --include-hook-events puts each guard decision beside them. A run counts
only if every one of those messages names that model's family; a run with no subagent, or one on another model,
fails every expectation, since the model was not the one judged.
Edits are accepted, standing in for the user who approves each reviewer's result file, which the reviewers write
inside the home, and the reviewers' self-check and source commands are allowed; the skill's allowed-tools grant the
rest. Each
run's temporary directory is its own folder in the home, through TMPDIR, which Python reads first on every system,
so the pipeline's run folders and the canary root that finalize writes land there, and the record judged is the
highest review version in that canary root. The code-review configuration the runs read is written there too. A
record any of whose reviewers has a null `files_read` counts for nothing either: the guard's claim starts a role's
read log, so a reviewer the guard held has a count, if only 0, and null means the boundary was never exercised.

What still comes from the user's own setup: their sign-in, user CLAUDE.md and auto-memory, as for the runtime
canary.

Output, one fact per line:
  HOME "<dir>"                                    the throwaway directory, one home per model inside it
  DEPLOYED <model> <source id>                    or DEPLOY_FAILED <model> "<reason>", which stops the run, also
                                                  when the home's reviewer agent cannot be passed with --agents
  RUNTIME claude <version>
  TRANSCRIPT <scenario> <model> "<path>"          a run finished; its stream-json output, in the order runs end
  REVIEWER <model> <model ID>[,<model ID>]        the models the reviewers ran on, or `none`
  GUARDED <scenario> <model> <role> files_read=<n>
                                                  a reviewer of a run that counts, which the guard held, and the
                                                  snapshot files the guard counted it reading
  PASS <skill> <scenario> <model> <expectation>
  FAIL <skill> <scenario> <model> <expectation> "<reason>"
  FAILED "<reason>"                               nothing was run or judged: the scenarios could not be loaded,
                                                  or Claude Code is missing
then a Markdown table, one row per scenario and one column per model, each cell the expectations passed of those
checked, then `WROTE <file>` when --write replaced the skill's rows in docs/skill-evaluations.md, and `REMOVED
"<dir>"` when nothing failed and the home is gone; otherwise the home stays for its transcripts. It exits 1 on any
FAIL or FAILED line, 0 otherwise, and 2 on a usage error.

It records pass or fail, the model identifiers, the runtime version, and the date, and nothing else: no tokens,
cost, or timings. It calls models, so it is never part of validation; see .claude/skills/evaluate-skill/SKILL.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import itertools
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

import frontmatter
from console import use_utf8_output

from deployer import fsops, tools
from tools import runtime_canary
from tools.runtime_canary import Completed, Runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals"
RESULTS = REPOSITORY_ROOT / "docs" / "skill-evaluations.md"
# The reviewer subagent models a run is judged on, as Claude Code names them. The one place they are named.
MODELS = ("haiku", "sonnet", "opus")
# The session's own model, which orchestrates the skill: the strongest, so the orchestration never limits a result.
SESSION_MODEL = MODELS[-1]
DEFAULT_TIMEOUT = 1800
DEFAULT_JOBS = 3
# The reviewer agent the runs set the model of, in each model's home, and the guard its hook runs: the hook names
# the profile folder's copy, and the definition the session gets names the home's.
REVIEWER_AGENT = Path(".claude") / "agents" / "code-review-reviewer.md"
GUARD = Path(".claude") / "skills" / "code-review-core" / "scripts" / "review_guard.py"
PROFILE_GUARD = f"~/{GUARD.as_posix()}"
# The file in each home holding the reviewer definition the session gets with --agents, and the frontmatter keys
# that definition carries over; an agent with any other key is refused, since its definition would drop it.
AGENTS = "agents.json"
AGENT_KEYS = frozenset({"name", "description", "tools", "model", "omitClaudeMd", "hooks"})
# What the home's path may not hold: the hook command quotes it for Python inside a Bash double quote.
UNQUOTABLE = re.compile(r"[\\'\"`$!]")
# The commands a reviewer runs besides its file tools: its self-check, which review_pipeline.py writes as
# python -B "<script>" validate-result --run "<run>" --role "<role>", and a lazy snapshot's two source commands, which
# review_source.py writes as python -B "<script>" source-file --run "<run>" --role "<role>" --path="<path>", and the
# same with source-search and --pattern.
REVIEWER_COMMANDS = (
    'python -B "*review_pipeline.py" validate-result --run *',
    'python -B "*review_source.py" source-file --run *',
    'python -B "*review_source.py" source-search --run *',
)
STATE = ".skill-evals"
RESULTS_BEGIN = "<!-- skill-evals:begin -->"
RESULTS_END = "<!-- skill-evals:end -->"
RESULTS_HEADER = (
    "| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date |",
    "| --- | --- | --- | --- | --- | --- | --- | --- |",
)
SEVERITIES = ("SUGGESTION", "SHOULD_FIX", "MUST_FIX")
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
DISPOSITIONS = ("addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify")
MODES = ("initial", "re-review")
SCENARIO_FIELDS = frozenset({"skill", "mode", "expectations"})
ENTRY = re.compile(r"v([1-9][0-9]*):(F[0-9]{3,})")

Record = Mapping[str, Any]


class ScenarioError(Exception):
    """A scenario that cannot be loaded."""


class RecordError(Exception):
    """A record that is missing, unreadable, or lacks what an expectation reads."""


@dataclass(frozen=True)
class Expectation:
    label: str
    # The reason the record fails the expectation, or None when it passes.
    check: Callable[[Record], str | None]


@dataclass(frozen=True)
class Scenario:
    skill: str
    name: str
    directory: Path
    mode: str
    prior: Path | None
    expectations: tuple[Expectation, ...]


@dataclass(frozen=True)
class Outcome:
    scenario: str
    model: str
    label: str
    failure: str | None


RecordSource = Callable[[Scenario, str], Record]


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


# Loading


def _fields(spec: Mapping[str, Any], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    missing = sorted(required - set(spec))
    unknown = sorted(set(spec) - required - optional)
    if missing:
        raise ScenarioError(f"{spec.get('kind')} expectation lacks {', '.join(missing)}")
    if unknown:
        raise ScenarioError(f"{spec.get('kind')} expectation does not take {', '.join(unknown)}")


def _choice(value: Any, choices: Sequence[str], what: str) -> str:
    if value not in choices:
        raise ScenarioError(f"{what} must be one of {', '.join(choices)}, not {quote(str(value))}")
    return str(value)


def _path(value: Any) -> str:
    path = PurePosixPath(value) if isinstance(value, str) else None
    if path is None or not value or "\\" in value or path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ScenarioError(f"path must be a plain relative path, not {quote(str(value))}")
    return value


def _count(value: Any, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScenarioError(f"{what} must be a count, not {quote(str(value))}")
    return value


def _lines(value: Any) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(line, int) or isinstance(line, bool) or line < 1 for line in value)
        or len(set(value)) != len(value)
    ):
        raise ScenarioError(f"lines must be distinct positive line numbers, not {quote(str(value))}")
    return tuple(sorted(value))


def _entry(value: Any) -> tuple[int, str]:
    match = ENTRY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ScenarioError(f"entry must look like v1:F001, not {quote(str(value))}")
    return int(match[1]), match[2]


def _where(path: str, lines: tuple[int, ...]) -> str:
    return f"{path}:{','.join(str(line) for line in lines)}"


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def finding_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "severity"})
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    severity = _choice(spec["severity"], SEVERITIES, "severity")

    def check(record: Record) -> str | None:
        there = [finding for finding in findings(record) if finding["path"] == path]
        if any(finding["line"] in lines and _rank(finding["severity"]) >= _rank(severity) for finding in there):
            return None
        seen = ", ".join(f"{finding['severity']} at line {finding['line']}" for finding in there) or "none"
        return f"no finding at least {severity} on those lines; findings on {path}: {seen}"

    return Expectation(f"finding {_where(path, lines)} {severity}", check)


def no_finding_above_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "severity"}, frozenset({"paths"}))
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    paths: tuple[str, ...] | None = None
    if "paths" in spec:
        if not isinstance(spec["paths"], list) or not spec["paths"]:
            raise ScenarioError("paths must be a list of paths; leave it out for every path")
        paths = tuple(_path(path) for path in spec["paths"])

    def check(record: Record) -> str | None:
        above = [
            f"{finding['severity']} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if _rank(finding["severity"]) > _rank(severity) and (paths is None or finding["path"] in paths)
        ]
        return f"found {', '.join(above)}" if above else None

    return Expectation(f"no_finding_above {severity} {','.join(paths) if paths else '*'}", check)


def verdict_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "verdict"})
    verdict = _choice(spec["verdict"], VERDICTS, "verdict")

    def check(record: Record) -> str | None:
        actual = review(record)["verdict"]
        return None if actual == verdict else f"verdict is {actual}"

    return Expectation(f"verdict {verdict}", check)


def ledger_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "addressed", "still_present"})
    expected = {name: _count(spec[name], name) for name in ("addressed", "still_present")}

    def check(record: Record) -> str | None:
        judged = judgments(record)
        actual = {name: sum(1 for disposition in judged.values() if disposition == name) for name in expected}
        return None if actual == expected else " ".join(f"{name}={count}" for name, count in actual.items())

    return Expectation(" ".join(["ledger", *(f"{name}={count}" for name, count in expected.items())]), check)


def disposition_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "entry", "disposition"})
    entry = _entry(spec["entry"])
    disposition = _choice(spec["disposition"], DISPOSITIONS, "disposition")

    def check(record: Record) -> str | None:
        judged = judgments(record)
        if entry not in judged:
            return f"this review did not judge {spec['entry']}"
        return None if judged[entry] == disposition else f"{spec['entry']} is {judged[entry]}"

    return Expectation(f"disposition {spec['entry']} {disposition}", check)


def repeats_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "entry"})
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    version, identifier = _entry(spec["entry"])

    def check(record: Record) -> str | None:
        # Only a finding at least as severe as the entry can restate it; a less severe one there is another defect.
        floor = _rank(entry_severity(record, (version, identifier)))
        unlinked = [
            f"{finding.get('id', 'a finding')} at line {finding['line']}"
            for finding in findings(record)
            if finding["path"] == path
            and finding["line"] in lines
            and _rank(finding["severity"]) >= floor
            and finding.get("repeats") != {"version": version, "id": identifier}
        ]
        return f"{', '.join(unlinked)} not linked to {spec['entry']}" if unlinked else None

    return Expectation(f"repeats {_where(path, lines)} {spec['entry']}", check)


KINDS: dict[str, Callable[[Mapping[str, Any]], Expectation]] = {
    "finding": finding_expectation,
    "no_finding_above": no_finding_above_expectation,
    "verdict": verdict_expectation,
    "ledger": ledger_expectation,
    "disposition": disposition_expectation,
    "repeats": repeats_expectation,
}
RE_REVIEW_KINDS = frozenset({"ledger", "disposition", "repeats"})


def expectation(spec: Any, mode: str) -> Expectation:
    if not isinstance(spec, dict):
        raise ScenarioError("each expectation must be an object")
    kind = spec.get("kind")
    if kind not in KINDS:
        raise ScenarioError(f"unknown expectation kind {quote(str(kind))}")
    if kind in RE_REVIEW_KINDS and mode != "re-review":
        raise ScenarioError(f"a {kind} expectation needs a re-review")
    return KINDS[kind](spec)


def load_scenario(skill: str, directory: Path) -> Scenario:
    try:
        spec = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{directory.name}: cannot read scenario.json: {exc}") from exc
    try:
        if not isinstance(spec, dict):
            raise ScenarioError("scenario.json must hold an object")
        mode = _choice(spec.get("mode"), MODES, "mode")
        allowed = SCENARIO_FIELDS | ({"prior"} if mode == "re-review" else set())
        if set(spec) != allowed:
            raise ScenarioError(f"scenario.json must have exactly {', '.join(sorted(allowed))}")
        if spec["skill"] != skill:
            raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
        for tree in ("base", "head"):
            if not (directory / tree).is_dir():
                raise ScenarioError(f"has no {tree}/ tree")
        if not (directory / "pull.json").is_file():
            raise ScenarioError("has no pull.json")
        prior = None
        if mode == "re-review":
            prior = directory / _path(spec["prior"])
            if not prior.is_file():
                raise ScenarioError(f"prior record {spec['prior']} is missing")
        if not isinstance(spec["expectations"], list) or not spec["expectations"]:
            raise ScenarioError("expectations must be a non-empty list")
        expectations = tuple(expectation(item, mode) for item in spec["expectations"])
    except ScenarioError as exc:
        raise ScenarioError(f"{directory.name}: {exc}") from exc
    return Scenario(skill, directory.name, directory, mode, prior, expectations)


def prepare_arguments(scenario: Scenario) -> list[str]:
    """The arguments that prepare a run of the scenario, which `review_pipeline.py prepare` and review-prs both take:
    a fixture canary, and for a re-review its prior record."""
    arguments = ["--canary", "--fixture", str(scenario.directory)]
    if scenario.prior is not None:
        arguments += ["--re-review", "--prior", str(scenario.prior)]
    return arguments


def load_scenarios(skill: str, names: Sequence[str] = (), root: Path = SCENARIO_ROOT) -> list[Scenario]:
    """The skill's scenarios, each named one or every one, in name order."""
    folder = root / skill
    if not folder.is_dir():
        raise ScenarioError(f"no scenarios for {skill} under {folder}")
    found = sorted(path.parent.name for path in folder.glob("*/scenario.json"))
    unknown = [name for name in names if name not in found]
    if unknown:
        raise ScenarioError(f"no scenario {', '.join(unknown)} for {skill}")
    return [load_scenario(skill, folder / name) for name in (names or found)]


# Reading a record


def review(record: Record) -> Mapping[str, Any]:
    value = record.get("review")
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("version"), int)
        or not isinstance(value.get("verdict"), str)
    ):
        raise RecordError("the record has no review version and verdict")
    return value


def findings(record: Record) -> list[Mapping[str, Any]]:
    value = record.get("findings")
    if not isinstance(value, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("path"), str)
        or not isinstance(item.get("line"), int)
        or item.get("severity") not in SEVERITIES
        for item in value
    ):
        raise RecordError("the record's findings are malformed")
    return value


def ledger_entries(record: Record) -> list[Mapping[str, Any]]:
    ledger = record.get("ledger")
    if not isinstance(ledger, list):
        raise RecordError("the record has no ledger")
    for entry in ledger:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("version"), int)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("dispositions"), list)
            or any(not isinstance(item, dict) for item in entry["dispositions"])
        ):
            raise RecordError("the record's ledger is malformed")
    return ledger


def entry_severity(record: Record, key: tuple[int, str]) -> str:
    """The severity of the ledger entry with this version and ID."""
    entry = next((item for item in ledger_entries(record) if (item["version"], item["id"]) == key), None)
    if entry is None:
        raise RecordError(f"the record's ledger has no v{key[0]}:{key[1]}")
    if entry.get("severity") not in SEVERITIES:
        raise RecordError(f"the record's ledger gives v{key[0]}:{key[1]} no severity")
    return str(entry["severity"])


def judgments(record: Record) -> dict[tuple[int, str], str]:
    """This review's disposition of each ledger entry it judged, keyed by the entry's version and ID."""
    version = review(record)["version"]
    judged: dict[tuple[int, str], str] = {}
    for entry in ledger_entries(record):
        for item in entry["dispositions"]:
            if item.get("version") == version:
                judged[(entry["version"], entry["id"])] = str(item.get("disposition"))
    return judged


def records_in(directory: Path) -> RecordSource:
    """A record source reading DIR/<scenario>/<model>.json."""

    def source(scenario: Scenario, model: str) -> Record:
        path = directory / scenario.name / f"{model}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RecordError(f"no record at {path}") from exc
        except (OSError, ValueError) as exc:
            raise RecordError(f"cannot read {path}: {exc}") from exc
        if not isinstance(record, dict):
            raise RecordError(f"{path} does not hold a record")
        return record

    return source


# Running


@dataclass(frozen=True)
class Run:
    """One scenario run on one model: the record it wrote, or why it counts for nothing, and the models it used."""

    record: Record | None
    failure: str | None
    reviewers: frozenset[str]
    session: frozenset[str]


def review_config(directory: Path) -> dict[str, Any]:
    """The code-review configuration every run reads. A fixture is reviewed by the generic reviewer whatever is
    configured, so the one repository is a placeholder; only the runtime and the verdict policy matter."""
    generic = {"id": "generic", "protocol_version": 1, "trusted_ref": None, "scope": "generic", "manifest_path": None}
    return {
        "schema_version": 1,
        "default_repository_set": "evaluations",
        "repository_sets": {"evaluations": ["example/unrelated"]},
        "repositories": {"example/unrelated": {"reviewer": generic, "checkout_path": None}},
        "archive_root": str(directory / "archive"),
        "local_mirror_root": None,
        "summary_root": str(directory / "summaries"),
        "dashboard_file": str(directory / "dashboard.md"),
        "github_login": "reviewer",
        "runtime": "claude-code",
        "verdict_policy": {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
        "dashboard": {},
    }


def prompt(scenario: Scenario) -> str:
    """The skill started by name with the scenario's prepare arguments, each path quoted."""
    arguments = [item if item.startswith("--") else f'"{item}"' for item in prepare_arguments(scenario)]
    return " ".join([f"/{scenario.skill}", *arguments])


def claude_command(executable: str, scenario: Scenario, agents: Path) -> list[str]:
    # Project settings only, so the installed skills and agents do not load beside the home's; the reviewer agent from
    # the file of --agents, so its guard hook runs; and no Workflow tool, so reviewers start as native subagents whose
    # messages the transcript carries with their model.
    return [
        executable,
        "-p",
        prompt(scenario),
        "--model",
        SESSION_MODEL,
        "--output-format",
        "stream-json",
        "--verbose",
        "--forward-subagent-text",
        "--include-hook-events",
        "--setting-sources",
        "project,local",
        "--agents",
        str(agents),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disallowedTools",
        "Workflow",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *(f"{tool}({command})" for command in REVIEWER_COMMANDS for tool in ("Bash", "PowerShell")),
    ]


def run_environment(base: Mapping[str, str], run: Path, config: Path) -> dict[str, str]:
    """The user's environment, sign-in included, with the run's own temporary directory and code-review files."""
    return {
        **base,
        "TMPDIR": str(run / "tmp"),
        "CODE_REVIEW_CONFIG": str(config),
        "CODE_REVIEW_STATE": str(run / "state.json"),
        "CODE_REVIEW_FLAGS": str(run / "flags.json"),
    }


def set_reviewer_model(home: Path, model: str) -> str | None:
    """Put the model in the home's copy of the reviewer agent in place of `model: inherit`; return why it could
    not, or None."""
    path = home / REVIEWER_AGENT
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        at = lines.index("model: inherit", 1, lines.index("---", 1))
    except (OSError, ValueError):
        return f"{runtime_canary.forward(path)} has no frontmatter line `model: inherit` to set"
    lines[at] = f"model: {model}"
    path.write_text("\n".join(lines), encoding="utf-8")
    return None if frontmatter.read(path).string("model") == model else f"the reviewer agent does not read as {model}"


class AgentError(Exception):
    """The home's reviewer agent cannot be given to the session with --agents as it is."""


def _nested(lines: Sequence[str], key: str) -> str:
    """The scalar of the one `key:` among the hook lines, read by the frontmatter reader: its line and the lines
    indented below it, moved to the top level."""
    starts = [index for index, line in enumerate(lines) if line.lstrip(" -").startswith(f"{key}:")]
    if len(starts) != 1:
        raise AgentError(f"its hooks name {key} {len(starts)} times, not once")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip(" -"))
    block = [lines[start][indent:]]
    for line in lines[start + 1 :]:
        if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
            break
        block.append(line[indent:])
    try:
        value = frontmatter.parse("\n".join(["---", *block, "---"])).string(key)
    except frontmatter.FrontmatterError as exc:
        raise AgentError(f"its hook's {key} cannot be read: {exc}") from exc
    if not value:
        raise AgentError(f"its hook's {key} is empty")
    return value


def _guard_hook(lines: Sequence[str], home: Path) -> dict[str, Any]:
    """The agent's one PreToolUse command hook, running the home's guard in place of the profile folder's."""
    events = [line.strip() for line in lines if line.strip() and len(line) - len(line.lstrip(" ")) == 2]
    if events != ["PreToolUse:"] or _nested(lines, "type") != "command":
        raise AgentError("its hooks are not one PreToolUse command hook")
    command = _nested(lines, "command")
    if command.count(PROFILE_GUARD) != 1:
        raise AgentError(f"its hook does not name {PROFILE_GUARD} once")
    guard = home / GUARD
    if not guard.is_file():
        raise AgentError(f"the home has no {GUARD.as_posix()} for its hook to run")
    if UNQUOTABLE.search(runtime_canary.forward(home)):
        raise AgentError(f"its hook cannot quote the home's path {runtime_canary.forward(home)}")
    timeout = _nested(lines, "timeout")
    if not timeout.isdigit():
        raise AgentError(f"its hook's timeout {timeout} is not a whole number of seconds")
    hook = {"type": "command", "command": command.replace(PROFILE_GUARD, runtime_canary.forward(guard))}
    return {"matcher": _nested(lines, "matcher"), "hooks": [{**hook, "timeout": int(timeout)}]}


def reviewer_agents(home: Path) -> dict[str, Any]:
    """The --agents definition of the home's reviewer agent: its frontmatter and body, its hook running the home's
    guard. Raise AgentError when the agent has anything the definition would drop or cannot carry."""
    path = home / REVIEWER_AGENT
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        found = frontmatter.split(lines)
        if found is None:
            raise AgentError("it has no frontmatter")
        head, body = found
        values = frontmatter.Frontmatter(head)
        extra = sorted(set(values.keys()) - AGENT_KEYS)
        if extra:
            raise AgentError(f"the evaluation does not pass {', '.join(extra)} with --agents")
        name = values.string("name")
        if name != path.stem:
            raise AgentError(f"it is named {name}, not {path.stem}")
        if "hooks:" not in head:
            raise AgentError("it has no hooks")
        after = head[head.index("hooks:") + 1 :]
        hook_lines = list(itertools.takewhile(lambda line: not line or line[0] == " ", after))
        definition: dict[str, Any] = {
            "description": values.string("description"),
            "prompt": "\n".join(lines[body:]).strip("\n"),
            "tools": [tool.strip() for tool in (values.string("tools") or "").split(",")],
            "model": values.string("model"),
            "hooks": {"PreToolUse": [_guard_hook(hook_lines, home)]},
        }
        if values.string("omitClaudeMd") == "true":
            definition["omitClaudeMd"] = True
    except (OSError, ValueError) as exc:
        raise AgentError(str(exc)) from exc
    return {path.stem: definition}


def stream_models(output: str) -> tuple[frozenset[str], frozenset[str]]:
    """The models the session's own messages named, and those its subagents' messages named."""
    session: set[str] = set()
    subagents: set[str] = set()
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (
            not isinstance(event, dict)
            or event.get("type") != "assistant"
            or not isinstance(event.get("message"), dict)
        ):
            continue
        model = event["message"].get("model")
        # A message Claude Code writes itself, such as an API error, names no model of its own.
        if isinstance(model, str) and model.startswith("claude-"):
            (subagents if event.get("parent_tool_use_id") else session).add(model)
    return frozenset(session), frozenset(subagents)


def find_record(directory: Path) -> Record:
    """The highest review version under the canary roots in a run's temporary directory."""
    records = []
    for path in sorted(directory.glob("code-review-canary-*/**/*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        review = value.get("review") if isinstance(value, dict) else None
        if isinstance(review, dict) and isinstance(review.get("version"), int):
            records.append(value)
    if not records:
        raise RecordError("the run recorded no review")
    return max(records, key=lambda record: record["review"]["version"])


def run_failure(model: str, completed: Completed, timeout: float, skill: str, missing: str | None) -> str | None:
    """Why a run counts for nothing, or None when it recorded a review on the model it was given."""
    listed, denials = runtime_canary.claude_stream(completed.stdout)
    reviewers = stream_models(completed.stdout)[1]
    if missing is not None:
        reasons = [missing]
        if completed.timed_out:
            reasons.append(f"timed out after {timeout:g} seconds")
        elif completed.returncode != 0:
            reasons.append(f"Claude Code exited with code {completed.returncode}")
        if listed is not None and skill not in listed:
            reasons.append(f"Claude Code did not list {skill}")
        if denials:
            reasons.append(f"denied: {'; '.join(denials)}")
        return "; ".join(reasons)
    if not reviewers:
        return "no reviewer subagent ran, so the record is not the model's"
    other = sorted(name for name in reviewers if f"claude-{model}-" not in name)
    if other:
        return f"reviewers ran on {', '.join(other)}, not {model}"
    return None


def unguarded(record: Record) -> str | None:
    """Why the record shows a reviewer the reviewer guard did not hold, or None. The guard's claim starts the role's
    read log, so a held reviewer's files_read is a count, if only 0; null means no guard ran."""
    try:
        reviewers = review(record).get("reviewers")
    except RecordError as exc:
        return str(exc)
    if not isinstance(reviewers, list) or not reviewers:
        return "the record names no reviewer"
    loose = [
        str(reviewer.get("id")) if isinstance(reviewer, dict) else "a reviewer"
        for reviewer in reviewers
        if not isinstance(reviewer, dict) or reviewer.get("files_read") is None
    ]
    return f"no reviewer guard held {', '.join(loose)}: its files_read is null" if loose else None


def run_scenario(
    scenario: Scenario, model: str, home: Path, executable: str, base: Mapping[str, str], runner: Runner, timeout: float
) -> tuple[Run, Path]:
    """Run the scenario once in the model's home; return what it gave and its transcript."""
    run = home / STATE / scenario.name
    (run / "tmp").mkdir(parents=True)
    environment = run_environment(base, run, home / STATE / "config.json")
    completed = runner(claude_command(executable, scenario, home / STATE / AGENTS), home, environment, timeout)
    transcript = run / "transcript.jsonl"
    transcript.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
    record: Record | None = None
    missing = None
    try:
        record = find_record(run / "tmp")
    except RecordError as exc:
        missing = str(exc)
    failure = run_failure(model, completed, timeout, scenario.skill, missing)
    if failure is None and record is not None:
        failure = unguarded(record)
    session, reviewers = stream_models(completed.stdout)
    return Run(None if failure else record, failure, reviewers, session), transcript


def guarded_lines(scenario: str, model: str, run: Run) -> list[str]:
    """One line per reviewer of a run that counts, which `unguarded` found the guard held, with its count."""
    if run.record is None:
        return []
    return [
        f"GUARDED {scenario} {model} {reviewer['id']} files_read={reviewer['files_read']}"
        for reviewer in review(run.record)["reviewers"]
    ]


def prepare_home(home: Path, model: str, deploy: Callable[[Path, Path], tuple[int, str]]) -> str | None:
    """Deploy the checkout into the model's home, set its reviewer model, and write the reviewer definition the
    session gets with --agents; return why that failed, or None."""
    home.mkdir(parents=True)
    code, log = deploy(home, REPOSITORY_ROOT)
    if code != 0:
        (home / STATE).mkdir(exist_ok=True)
        log_file = home / STATE / "deploy.log"
        log_file.write_text(log, encoding="utf-8")
        return f"the deployment failed; its log is {runtime_canary.forward(log_file)}"
    reason = set_reviewer_model(home, model)
    if reason:
        return reason
    try:
        agents = reviewer_agents(home)
    except AgentError as exc:
        return f"{runtime_canary.forward(home / REVIEWER_AGENT)} cannot be passed with --agents: {exc}"
    (home / STATE).mkdir(exist_ok=True)
    (home / STATE / AGENTS).write_text(json.dumps(agents, indent=2), encoding="utf-8")
    (home / STATE / "config.json").write_text(json.dumps(review_config(home / STATE), indent=2), encoding="utf-8")
    return None


def run_all(
    scenarios: Sequence[Scenario],
    homes: Mapping[str, Path],
    executable: str,
    base: Mapping[str, str],
    runner: Runner,
    timeout: float,
    jobs: int,
) -> dict[tuple[str, str], Run]:
    """Every scenario on every model with a home, up to `jobs` at once, printing each transcript as its run ends. One
    job runs in this thread, in order."""
    runs: dict[tuple[str, str], Run] = {}
    pairs = [(scenario, model, home) for scenario in scenarios for model, home in homes.items()]

    def ended(scenario: Scenario, model: str, result: tuple[Run, Path]) -> None:
        runs[(scenario.name, model)] = result[0]
        print(f"TRANSCRIPT {scenario.name} {model} {quote(runtime_canary.forward(result[1]))}", flush=True)

    if jobs == 1:
        for scenario, model, home in pairs:
            ended(scenario, model, run_scenario(scenario, model, home, executable, base, runner, timeout))
        return runs
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {
            pool.submit(run_scenario, scenario, model, home, executable, base, runner, timeout): (scenario, model)
            for scenario, model, home in pairs
        }
        for future in concurrent.futures.as_completed(pending):
            ended(*pending[future], future.result())
    return runs


def runs_source(runs: Mapping[tuple[str, str], Run]) -> RecordSource:
    """A record source over finished runs."""

    def source(scenario: Scenario, model: str) -> Record:
        run = runs[(scenario.name, model)]
        if run.record is None:
            raise RecordError(run.failure or "the run recorded no review")
        return run.record

    return source


def claude_version(executable: str, runner: Runner, base: Mapping[str, str], directory: Path) -> str:
    completed = runner([executable, "--version"], directory, dict(base), 60)
    version = tools.parse_version(completed.stdout) if completed.returncode == 0 else None
    return tools.format_version(version) if version else "unknown"


# Recording the result


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def result_rows(
    skill: str,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    outcomes: Sequence[Outcome],
    runs: Mapping[tuple[str, str], Run],
    version: str,
    today: datetime.date,
) -> list[str]:
    """One row per model: the model IDs its reviewers ran on, its expectations passed, and the run's versions; a
    model none of whose reviewers ran is recorded as not run, with the first reason."""
    rows = []
    for model in models:
        mine = [runs[(scenario.name, model)] for scenario in scenarios if (scenario.name, model) in runs]
        reviewers = sorted({name for run in mine for name in run.reviewers})
        session = ", ".join(sorted({name for run in mine for name in run.session})) or "unknown"
        theirs = [outcome for outcome in outcomes if outcome.model == model]
        passed = f"{sum(1 for outcome in theirs if outcome.failure is None)}/{len(theirs)}"
        if reviewers:
            identifier = ", ".join(reviewers)
            detail = ", ".join(
                f"{scenario.name} {sum(1 for o in theirs if o.scenario == scenario.name and o.failure is None)}"
                f"/{sum(1 for o in theirs if o.scenario == scenario.name)}"
                for scenario in scenarios
            )
        else:
            identifier = "not run"
            detail = next((outcome.failure for outcome in theirs if outcome.failure), "no run")
        cells = [skill, model, identifier, passed, detail, session, version, today.isoformat()]
        rows.append(f"| {' | '.join(_cell(cell) for cell in cells)} |")
    return rows


def results_table(text: str) -> tuple[int, int]:
    """The line indexes of the results markers in docs/skill-evaluations.md, or a ScenarioError."""
    lines = text.split("\n")
    if lines.count(RESULTS_BEGIN) != 1 or lines.count(RESULTS_END) != 1:
        raise ScenarioError(f"the results file needs one {RESULTS_BEGIN} line and one {RESULTS_END} line")
    begin, end = lines.index(RESULTS_BEGIN), lines.index(RESULTS_END)
    if end < begin:
        raise ScenarioError(f"{RESULTS_END} comes before {RESULTS_BEGIN} in the results file")
    return begin, end


def replace_results(text: str, skill: str, rows: Sequence[str]) -> str:
    """The results file with the skill's rows replaced by these, every other skill's rows kept, sorted by skill."""
    lines = text.split("\n")
    begin, end = results_table(text)
    kept = [
        line
        for line in lines[begin + 1 : end]
        if line.startswith("| ") and line not in RESULTS_HEADER and line.split("|")[1].strip() != skill
    ]
    table = sorted([*kept, *rows], key=lambda line: line.split("|")[1].strip())
    return "\n".join([*lines[: begin + 1], *RESULTS_HEADER, *table, *lines[end:]])


# Judging and reporting


def judge(scenarios: Sequence[Scenario], models: Sequence[str], source: RecordSource) -> list[Outcome]:
    outcomes = []
    for scenario in scenarios:
        for model in models:
            try:
                record: Record | None = source(scenario, model)
                missing = None
            except RecordError as exc:
                record, missing = None, str(exc)
            for item in scenario.expectations:
                failure = missing
                if record is not None:
                    try:
                        failure = item.check(record)
                    except RecordError as exc:
                        failure = str(exc)
                outcomes.append(Outcome(scenario.name, model, item.label, failure))
    return outcomes


def outcome_line(skill: str, outcome: Outcome) -> str:
    fact = f"{skill} {outcome.scenario} {outcome.model} {outcome.label}"
    return f"PASS {fact}" if outcome.failure is None else f"FAIL {fact} {quote(outcome.failure)}"


def table(scenarios: Sequence[Scenario], models: Sequence[str], outcomes: Sequence[Outcome]) -> list[str]:
    """A Markdown table: one row per scenario, one column per model, each cell passed of checked."""
    lines = [f"| Scenario | {' | '.join(models)} |", f"| --- |{' --- |' * len(models)}"]
    for scenario in scenarios:
        cells = []
        for model in models:
            mine = [outcome for outcome in outcomes if outcome.scenario == scenario.name and outcome.model == model]
            cells.append(f"{sum(1 for outcome in mine if outcome.failure is None)}/{len(mine)}")
        lines.append(f"| {scenario.name} | {' | '.join(cells)} |")
    return lines


def report(skill: str, scenarios: Sequence[Scenario], models: Sequence[str], outcomes: Sequence[Outcome]) -> bool:
    """Print every outcome and the table; return whether any expectation failed."""
    for outcome in outcomes:
        print(outcome_line(skill, outcome))
    for line in table(scenarios, models, outcomes):
        print(line)
    return any(outcome.failure is not None for outcome in outcomes)


def new_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="skill-evals-")).resolve()


@dataclass(frozen=True)
class Seams:
    """What a run reaches outside this module, so tests can stand in for each."""

    runner: Runner = runtime_canary.run_process
    which: Callable[[str], str | None] = shutil.which
    deploy: Callable[[Path, Path], tuple[int, str]] = runtime_canary.deploy
    environment: Mapping[str, str] | None = None
    make_home: Callable[[], Path] = new_home
    today: Callable[[], datetime.date] = datetime.date.today
    results: Path = RESULTS


def preflight(write: bool, seams: Seams) -> tuple[str, str | None]:
    """Claude Code's path and, with --write, the results file's text; raise ScenarioError before any run when
    something a run needs is missing."""
    executable = seams.which("claude")
    if executable is None:
        raise ScenarioError("Claude Code (claude) is not on PATH")
    if not write:
        return executable, None
    try:
        text = seams.results.read_text(encoding="utf-8")
    except OSError as exc:
        raise ScenarioError(f"cannot read {seams.results}: {exc}") from exc
    results_table(text)
    return executable, text


def evaluate(skill: str, scenarios: Sequence[Scenario], options: argparse.Namespace, seams: Seams) -> int:
    """Run every scenario on every model from throwaway homes, judge the records, and print the result."""
    models = list(dict.fromkeys(options.model or MODELS))
    base = dict(os.environ if seams.environment is None else seams.environment)
    try:
        executable, results_text = preflight(options.write, seams)
    except ScenarioError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    root = seams.make_home()
    print(f"HOME {quote(runtime_canary.forward(root))}", flush=True)
    homes = {model: root / model for model in models}
    for model, home in homes.items():
        reason = prepare_home(home, model, seams.deploy)
        if reason:
            print(f"DEPLOY_FAILED {model} {quote(reason)}")
            return 1
        print(f"DEPLOYED {model} {runtime_canary.source_id(REPOSITORY_ROOT)}", flush=True)
    version = claude_version(executable, seams.runner, base, root)
    print(f"RUNTIME claude {version}", flush=True)
    runs = run_all(scenarios, homes, executable, base, seams.runner, options.timeout, options.jobs)
    for model in models:
        used = sorted({name for scenario in scenarios for name in runs[(scenario.name, model)].reviewers})
        print(f"REVIEWER {model} {','.join(used) or 'none'}")
    for scenario in scenarios:
        for model in models:
            for line in guarded_lines(scenario.name, model, runs[(scenario.name, model)]):
                print(line)
    outcomes = judge(scenarios, models, runs_source(runs))
    failed = report(skill, scenarios, models, outcomes)
    if results_text is not None:
        rows = result_rows(skill, scenarios, models, outcomes, runs, version, seams.today())
        seams.results.write_text(replace_results(results_text, skill, rows), encoding="utf-8", newline="\n")
        print(f"WROTE {runtime_canary.forward(seams.results)}")
    if not failed:
        fsops.remove(root)
        print(f"REMOVED {quote(runtime_canary.forward(root))}")
    return 1 if failed else 0


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {value}")
    return number


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run a skill's scenarios on each model and judge their records.")
    result.add_argument("skill", help="the skill whose scenarios to judge, a folder under tests/fixtures/skill-evals")
    result.add_argument("--records", type=Path, help="judge a folder of <scenario>/<model>.json records; run nothing")
    result.add_argument("--scenario", action="append", default=[], help="judge only this scenario (repeatable)")
    result.add_argument("--model", action="append", choices=MODELS, help="judge only this model (repeatable)")
    result.add_argument("--write", action="store_true", help="replace the skill's rows in docs/skill-evaluations.md")
    result.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"seconds for each run (default {DEFAULT_TIMEOUT})"
    )
    result.add_argument("--jobs", type=positive, default=DEFAULT_JOBS, help=f"runs at once (default {DEFAULT_JOBS})")
    return result


def main(argv: Sequence[str], root: Path = SCENARIO_ROOT, seams: Seams | None = None) -> int:
    command = parser()
    options = command.parse_args(argv)
    if options.write and (options.records or options.scenario or options.model):
        command.error("--write records a full run: every scenario on every model, with no --records")
    try:
        scenarios = load_scenarios(options.skill, options.scenario, root)
    except ScenarioError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    if options.records is None:
        return evaluate(options.skill, scenarios, options, seams or Seams())
    models = list(dict.fromkeys(options.model or MODELS))
    return 1 if report(options.skill, scenarios, models, judge(scenarios, models, records_in(options.records))) else 0


if __name__ == "__main__":
    use_utf8_output(errors="backslashreplace")
    sys.exit(main(sys.argv[1:]))
