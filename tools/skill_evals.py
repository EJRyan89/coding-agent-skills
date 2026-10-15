"""Run a skill's fixed scenarios, model by model, and judge the review record each run wrote.

Usage:
  python -B tools/skill_evals.py SKILL [--write] [--timeout SECONDS] [--jobs N]
  python -B tools/skill_evals.py SKILL [--scenario NAME ...] [--model NAME ...] [--records DIR]

SKILL is one skill's name, a folder of scenarios under tests/fixtures/skill-evals/; a name that is not a skill's is a
usage error, and a skill with no scenario there fails the run. Repeated --scenario and --model names count once, in
first-occurrence order. Without --scenario, every scenario is selected in name order. --timeout is a positive number
of seconds.

A scenario is a directory tests/fixtures/skill-evals/<skill>/<scenario>/, which never ships, holding a scenario.json.
A fixture scenario is a code-review fixture, which `review_pipeline.py prepare --canary --fixture <scenario>` reviews
in place of a pull request: the change as two trees, base/ and head/, and the pull request in pull.json (see
review_canary.py in code-review-core). A re-review scenario's run adds `--re-review --prior <scenario>/<prior>`, its
prior a plain relative path inside the scenario's folder. A
document scenario names a document instead, for a skill started on one file's path, as review-document is: the run
writes it under its own file name into a new folder in the run's folder and starts the skill with that path alone.
With a base, that folder is a new git checkout whose one commit holds the base under the same name, and the document
is written over it uncommitted, so the skill reviews only the edit; without one, nothing encloses it and the skill
reviews it whole. The checkout's Git commands use a temporary empty hook directory, overriding global and
template-installed hooks, and the base commit disables signing. A finding's path is then the document's file name.
A repository reviewer's scenario judges one of this repository's own reviewers under .claude/skills/, such as
review-repository, which review-prs runs and no session starts: it sits under the reviewer's name, and is a fixture
scenario naming the reviewer and review-prs, whose pull.json manifest_path names the reviewer's manifest. Since
--canary-home ships no repository skill, each run copies the fixture into a throwaway folder outside the home and writes
there, at its path in both trees, each of the reviewer's files from this checkout: its SKILL.md, references/, scripts/
but its regression suites, and every file its manifest declares. A condition's `reads` add none, since they name what
its script reads of the reviewed tree. The copy is removed when the run ends, however it ends, and the fixture itself is
never written. The run starts review-prs on the copy, and its rows are the reviewer's. A reviewer the checkout lacks, a
file it declares that the checkout lacks, or a fixture already holding a file the run places, fails the load.
The scenario.json:

  {"skill": "<skill>", "mode": "initial" | "re-review",
   "prior": "<prior record file in the scenario>"  (re-review only),
   "document": "<path>",  "base": "<path>",  "stage": "<stage>"
      (a document scenario, initial only; base optional; stage optional, one of draft, review, and final, which the
      skill is started with as --stage),
   "reviewer": "<reviewer>"  (a repository reviewer's scenario, whose skill is review-prs),
   "expectations": [<expectation>, ...]}

A document or base path leads from the scenario's folder to a file, which may be another scenario's under the same
scenarios root, so a document serves several skills' scenarios without a copy.

Each expectation is a statement over the record, checked here by script and never by reading the report. Severities
rank SUGGESTION < SHOULD_FIX < MUST_FIX; `lines` lists the head lines a finding may be anchored at, any of them:

  {"kind": "finding", "path": P, "lines": [N, ...], "severity": S, "category": C}
      a finding at P on one of the lines, at least as severe as S, and of category C, matched ignoring case, when
      `category` is given; a scenario whose manifest declares finding_categories names one of them
  {"kind": "no_finding_above", "severity": S, "paths": [P, ...], "lines": [N, ...]}
      no finding more severe than S on the paths, or on any path when `paths` is left out; with `lines`, which takes
      exactly one path, only on those lines of it, so a document may hold a more severe finding elsewhere; S is
      below the highest severity, above which nothing ranks
  {"kind": "only_at", "path": P, "lines": [N, ...]}
      every finding, whatever its severity, is at P on one of the lines, as a review of one edited line should be
  {"kind": "verdict", "verdict": "APPROVED" | "CHANGES_REQUESTED" | "INCOMPLETE"}
  {"kind": "ledger", "addressed": N, "still_present": N}     re-review only
      exactly that many ledger entries this review judged addressed, and still present
  {"kind": "disposition", "entry": "v1:F001", "disposition": D}     re-review only
      this review's disposition of that ledger entry
  {"kind": "repeats", "path": P, "lines": [N, ...], "entry": "v1:F001"}     re-review only
      every finding this review raised at P on those lines at that ledger entry's severity repeats the entry, so the
      problem counts once; raising none there also passes, since the ledger then carries the entry alone, and so does
      a finding there of another severity: a less severe one is about another defect, and a more severe one is a new
      finding, since a repeat may not be more severe than what it repeats

An unknown kind, a field a kind does not take, or a `category` the scenario's manifest does not list, ignoring case,
fails the whole run before anything is judged. The manifest is the one the fixture's pull.json names in its base tree,
the checkout's for a repository reviewer's scenario, or review-document's design manifest for a document scenario.

Records come from a record source, a function of a scenario and a model that returns the record that run wrote.
--records DIR reads DIR/<scenario>/<model>.json, the record JSON a run finalized, and holds it to what a run's record
is held to: code-review-core's record contract (validate_record), the review version of the scenario's mode, a guard
that held every reviewer, and every reviewer's `model` naming that model's ID. The model is the one each reviewer's
result reports, which is weaker than a run's check of the transcript. A missing or unreadable record, or one any of
those refuses, fails every expectation of its scenario and model.

Without --records, it runs the scenarios. For each model it makes a fresh home under one throwaway directory,
deploys this checkout into it with `deploy.py --canary-home`, as tools/runtime_canary.py does, and sets the model's
ID in that home's copy of the reviewer agent, whose `model: inherit` would otherwise run every reviewer on the
session's model: a fixture's reviewers have no MODEL line, since neither the generic reviewer nor a scenario's
specialist names a model, and `inherit` outranks CLAUDE_CODE_SUBAGENT_MODEL.
The session gets that agent with `--agents`, which outranks the same agent loaded from the home's .claude/agents:
Claude Code runs a project agent's hooks only in a folder whose workspace trust was accepted, which a `-p` session
never is, so loaded from the home the reviewer guard would never run. The definition is the home's agent file, its
frontmatter and body, with one change: its hook runs the home's review_guard.py in place of the profile folder's,
so the guard checks the home's prompts against the home's copy of the pipeline that wrote them.
Then, for each scenario and model, up to --jobs at once, it starts Claude Code headless with the model's home as
its working directory, so the deployed skills and agent load as project ones, and the prompt
`/<skill> <prepare_arguments>`, on a repository reviewer's scenario's copy, or `/<skill> "<document>"` with the path
of a document scenario's document, staged in the run's folder; a document or a copy that cannot be staged fails the
run before any session starts. The session, which
orchestrates the skill, stays on SESSION_MODEL's ID. It has no Workflow tool, so reviewers start as native
subagents, whose messages --forward-subagent-text puts in the transcript with the model each ran on, and
--include-hook-events puts each guard decision beside them. A run counts only if every one of those messages names
that model's ID, a dated suffix allowed, and every message of the session names the session's; a run with no
subagent, or one on another model or another generation of the same family, fails every expectation, since the model
was not the one judged.
Edits are accepted, standing in for the user who approves each reviewer's result file, which the reviewers write
inside the home, and the reviewers' self-check and source commands are allowed, each by the quoted path of the home's
deployed script as the pipeline's prompts give it, in both shells, with no wildcard in the path; the skill's
allowed-tools grant the rest. Each run's temporary directory is its own folder in the home, through TMPDIR, which
Python reads first on every system, so the pipeline's run folders and the canary root that finalize writes land there.
The record judged is the latest record of the pull request recorded there, read and validated by code-review-core's
archive, and it must be version 2 for a re-review, whose canary root first holds the prior record as version 1, and
version 1 otherwise: a run whose latest record is another version, or cannot be read, recorded no review of its own,
and fails with that reason. The code-review configuration is written once per home at
`.skill-evals/config.json`, which all runs in that home read. A record any of whose reviewers has a null `files_read`
counts for nothing either: the guard's claim starts a role's read log, so a reviewer the guard held has a count, if
only 0, and null means the boundary was never exercised.

What still comes from the user's own setup: their sign-in, user CLAUDE.md and auto-memory, as for the runtime
canary.

Output, one fact per line:
  HOME "<dir>"                                    the throwaway directory, one home per model inside it
  DEPLOYED <model> <source id>                    or DEPLOY_FAILED <model> "<reason>", which stops the run, also
                                                  when the home's reviewer agent cannot be passed with --agents, or
                                                  the home's path holds a *, which a grant would read as any text
  RUNTIME claude <version>
  TRANSCRIPT <scenario> <model> "<path>"          a run finished; its stream-json output, in the order runs end
  REVIEWER <model> <model ID>[,<model ID>]        the models the reviewers ran on, or `none`
  GUARDED <scenario> <model> <role> files_read=<n>
                                                  a reviewer of a run that counts, which the guard held, and the
                                                  snapshot files the guard counted it reading
  PASS <skill> <scenario> <model> <expectation>
  FAIL <skill> <scenario> <model> <expectation> "<reason>"
  FAILED "<reason>"                               nothing was run or judged: the scenarios could not be loaded,
                                                  Claude Code is missing, or --write's existing results file
                                                  cannot be read or its table cannot be read
then a Markdown table, one row per scenario and one column per model, each cell the expectations passed of those
checked, then `WROTE <file>` when --write replaced the skill's rows in docs/skill-evaluations.md, and `REMOVED
"<dir>"` when nothing failed and the home is gone; otherwise the home stays for its transcripts. A model none of whose
reviewers ran is written there as not run with a fixed phrase for its first reason, such as `timed out after 1800
seconds` or `denied 2 commands`, which names no path of the machine; the FAIL lines and the transcript keep the whole
reason. --write reads the results table strictly before any run: the header, its separator, then rows of the header's
columns, each naming a skill, and nothing else between the markers. Every other skill's row is written back as it was.
It exits 1 on any FAIL, FAILED, or DEPLOY_FAILED line, 0 otherwise, and 2 on a usage error.

It records pass or fail, the model identifiers, the runtime version, and the date, and nothing else: no tokens,
cost, or timings. It calls models, so it is never part of validation; see .claude/skills/evaluate-skill/SKILL.md.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import itertools
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-document")))

import frontmatter
from bounded_process import without_repository_variables
from console import run_main
from git_client import GitClient, GitError
from review_archive import ArchiveError, latest_record, list_versions
from review_document import specialists_manifest
from review_io import PersistenceError
from review_records import DISPOSITIONS as RECORD_DISPOSITIONS
from review_records import LEDGER_ID, SEVERITY_RANK, validate_record
from review_runtime import DOCUMENT_STAGES, RuntimeContractError, declared_reviewer_files, validate_adapter_manifest

from deployer import fsops, platform_support, tools
from deployer.platform_support import PYTHON
from deployer.source import SKILL_NAME_PATTERN
from tools import runtime_canary
from tools.runtime_canary import Completed, Runner

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals"
RESULTS = REPOSITORY_ROOT / "docs" / "skill-evaluations.md"
# The reviewer subagent models a run is judged on, each by its name and the model ID a run sets and must find in its
# messages. The one place they are named. A run sets the ID, never Claude Code's alias of the same name, since an alias
# can resolve to another generation of the family, which would then be recorded under this name.
MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}
# The session's own model, which orchestrates the skill: the strongest, so the orchestration never limits a result.
SESSION_MODEL = list(MODELS)[-1]
# The dated suffix a model's messages may carry after its ID.
DATED = re.compile(r"-[0-9]{8}")
DEFAULT_TIMEOUT = 1800
DEFAULT_JOBS = 3
# The reviewer agent the runs set the model of, in each model's home, and the guard its hook runs: the hook names
# the profile folder's copy, and the definition the session gets names the home's.
REVIEWER_AGENT = Path(".claude") / "agents" / "code-review-reviewer.md"
# A home's deployed skills folder, and the core whose scripts a reviewer runs, by its folder's name there.
SKILLS = Path(".claude") / "skills"
CORE = "code-review-core"
GUARD = SKILLS / CORE / "scripts" / "review_guard.py"
PROFILE_GUARD = f"~/{GUARD.as_posix()}"
# The file in each home holding the reviewer definition the session gets with --agents, and the frontmatter keys
# that definition carries over; an agent with any other key is refused, since its definition would drop it.
AGENTS = "agents.json"
AGENT_KEYS = frozenset({"name", "description", "tools", "model", "omitClaudeMd", "hooks"})
# What the home's path may not hold: the hook command quotes it for Python inside a Bash double quote.
UNQUOTABLE = re.compile(r"[\\'\"`$!]")
# The review version a run's record must be, by the scenario's mode: a re-review's canary root first holds the prior
# record as version 1.
RECORD_VERSION = {"initial": 1, "re-review": 2}
STATE = ".skill-evals"
RESULTS_BEGIN = "<!-- skill-evals:begin -->"
RESULTS_END = "<!-- skill-evals:end -->"
RESULTS_HEADER = (
    "| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date |",
    "| --- | --- | --- | --- | --- | --- | --- | --- |",
)
# A results table's cell border, a pipe its cell does not escape, and a separator cell, aligned or not.
CELL_BORDER = re.compile(r"(?<!\\)\|")
SEPARATOR_CELL = re.compile(r":?-{3,}:?")
# A run that recorded no review, as the results file records it: why the record is missing may name the machine's
# paths, which the FAIL lines keep.
NO_REVIEW = "the run recorded no review"
# The record's own vocabulary, from code-review-core's record module, in a stable order for messages: severities from
# the lowest rank, dispositions alphabetically.
SEVERITIES = tuple(sorted(SEVERITY_RANK, key=SEVERITY_RANK.__getitem__))
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
DISPOSITIONS = tuple(sorted(RECORD_DISPOSITIONS))
MODES = ("initial", "re-review")
SCENARIO_FIELDS = frozenset({"skill", "mode", "expectations"})
# The folder in a run's own folder where a document scenario's document is written for the skill to review.
DOCUMENT_FOLDER = "document"
# A repository reviewer's scenario: the skill that runs a reviewer manifest on a fixture, where this repository's own
# skills are, and the folders of a reviewer whose files are placed, besides its SKILL.md and what its manifest declares.
REVIEWER_SKILL = "review-prs"
REPOSITORY_SKILLS = PurePosixPath(".claude", "skills")
REVIEWER_FOLDERS = ("references", "scripts")
# The identity the throwaway checkout's one commit is made under, so no identity of the user's is needed or recorded,
# and signing is disabled.
COMMIT = ["-c", "user.name=skill-evals", "-c", "user.email=skill-evals@example.invalid", "-c", "commit.gpgsign=false"]

Record = Mapping[str, Any]


class ScenarioError(Exception):
    """A scenario that cannot be loaded."""


class RecordError(Exception):
    """A record that is missing, unreadable, refused, or lacks what an expectation reads."""


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
    # A document scenario's document, and the version committed before it, if any; None for a fixture scenario.
    document: Path | None = None
    base: Path | None = None
    # A repository reviewer's scenario: the reviewer, the checkout its files come from, and those files, each by its
    # path in the checkout, which the run places at the same path in both trees of a throwaway copy of the fixture.
    reviewer: str | None = None
    checkout: Path | None = None
    placed: tuple[str, ...] = ()
    # The document stage a document scenario starts the skill at, or None for the skill's default.
    stage: str | None = None


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
    """A plain relative path. One either platform reads as absolute, a drive's included, is refused on both, since a
    scenario runs on each: on Windows, a path joined to a drive leaves the scenario's folder."""
    path = PurePosixPath(value) if isinstance(value, str) else None
    if (
        path is None
        or not value
        or "\\" in value
        or path.is_absolute()
        or PureWindowsPath(value).anchor
        or ".." in path.parts
        or str(path) != value
    ):
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
    match = LEDGER_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ScenarioError(f"entry must look like v1:F001, not {quote(str(value))}")
    return int(match[1]), match[2]


def _where(path: str, lines: tuple[int, ...]) -> str:
    return f"{path}:{','.join(str(line) for line in lines)}"


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def finding_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "severity"}, frozenset({"category"}))
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    category: str | None = None
    if "category" in spec:
        if not isinstance(spec["category"], str) or not spec["category"].strip():
            raise ScenarioError("category must be a category name; leave it out for any category")
        category = spec["category"].strip()

    def matches(finding: Mapping[str, Any]) -> bool:
        return category is None or str(finding.get("category", "")).casefold() == category.casefold()

    def check(record: Record) -> str | None:
        there = [finding for finding in findings(record) if finding["path"] == path]
        if any(
            finding["line"] in lines and _rank(finding["severity"]) >= _rank(severity) and matches(finding)
            for finding in there
        ):
            return None
        seen = ", ".join(describe(finding) for finding in there) or "none"
        return f"no finding at least {wanted} on those lines; findings on {path}: {seen}"

    def describe(finding: Mapping[str, Any]) -> str:
        kind = f" {finding.get('category')}" if category is not None else ""
        return f"{finding['severity']}{kind} at line {finding['line']}"

    wanted = severity if category is None else f"{severity} {category}"
    return Expectation(f"finding {_where(path, lines)} {wanted}", check)


def no_finding_above_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "severity"}, frozenset({"paths", "lines"}))
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    if severity == SEVERITIES[-1]:
        raise ScenarioError(f"no_finding_above {severity} holds for every record: no severity ranks above it")
    paths: tuple[str, ...] | None = None
    if "paths" in spec:
        if not isinstance(spec["paths"], list) or not spec["paths"]:
            raise ScenarioError("paths must be a list of paths; leave it out for every path")
        paths = tuple(_path(path) for path in spec["paths"])
    lines: tuple[int, ...] | None = None
    if "lines" in spec:
        if paths is None or len(paths) != 1:
            raise ScenarioError("lines are lines of one path: give exactly one path in paths")
        lines = _lines(spec["lines"])

    def check(record: Record) -> str | None:
        above = [
            f"{finding['severity']} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if _rank(finding["severity"]) > _rank(severity)
            and (paths is None or finding["path"] in paths)
            and (lines is None or finding["line"] in lines)
        ]
        return f"found {', '.join(above)}" if above else None

    where = _where(paths[0], lines) if paths and lines else ",".join(paths) if paths else "*"
    return Expectation(f"no_finding_above {severity} {where}", check)


def only_at_expectation(spec: Mapping[str, Any]) -> Expectation:
    _fields(spec, {"kind", "path", "lines"})
    path, lines = _path(spec["path"]), _lines(spec["lines"])

    def check(record: Record) -> str | None:
        elsewhere = [
            f"{finding['severity']} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if finding["path"] != path or finding["line"] not in lines
        ]
        return f"found {', '.join(elsewhere)}" if elsewhere else None

    return Expectation(f"only_at {_where(path, lines)}", check)


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
        # Only a finding at the entry's severity must restate it. A less severe one there is another defect, and the
        # record contract refuses a link from a more severe one, which a review must record as a new finding.
        severity = _rank(entry_severity(record, (version, identifier)))
        unlinked = [
            f"{finding.get('id', 'a finding')} at line {finding['line']}"
            for finding in findings(record)
            if finding["path"] == path
            and finding["line"] in lines
            and _rank(finding["severity"]) == severity
            and finding.get("repeats") != {"version": version, "id": identifier}
        ]
        return f"{', '.join(unlinked)} not linked to {spec['entry']}" if unlinked else None

    return Expectation(f"repeats {_where(path, lines)} {spec['entry']}", check)


KINDS: dict[str, Callable[[Mapping[str, Any]], Expectation]] = {
    "finding": finding_expectation,
    "no_finding_above": no_finding_above_expectation,
    "only_at": only_at_expectation,
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


def _document(directory: Path, value: Any, what: str) -> Path:
    """A file a document scenario names by its path from the scenario's folder. The path may lead to another
    scenario's file under the same scenarios root, so one document serves several skills' scenarios uncopied. A path
    either platform reads as absolute, a drive's included, is refused on both, since a scenario runs on each."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).anchor
        or PureWindowsPath(value).anchor
    ):
        raise ScenarioError(f"{what} must be a relative path, not {quote(str(value))}")
    root = directory.parents[1].resolve()
    path = (directory / value).resolve()
    if not path.is_relative_to(root):
        raise ScenarioError(f"{what} {value} is outside the scenarios under {root}")
    if not path.is_file():
        raise ScenarioError(f"{what} {value} is missing")
    return path


def _fixture(directory: Path) -> None:
    for tree in ("base", "head"):
        if not (directory / tree).is_dir():
            raise ScenarioError(f"has no {tree}/ tree")
    if not (directory / "pull.json").is_file():
        raise ScenarioError("has no pull.json")


def _regression_suite(name: str) -> bool:
    """Whether a file is named as validation names a regression suite: test_*, test-*, *_test, *-test, or *.test.*."""
    stem = PurePosixPath(name).stem
    return stem.startswith(("test_", "test-")) or stem.endswith(("_test", "-test")) or ".test." in name


def _reviewer_folder(checkout: Path, folder: PurePosixPath) -> list[str]:
    """Every file a run places from the reviewer's folders, by its path in the checkout: all of references/, and all
    of scripts/ but its regression suites and Python's caches. The suites test the reviewer, which no review runs,
    and they name the scenarios' own files, which would show a condition script that looks for a change's tests one
    that is not there."""
    files = []
    for name in REVIEWER_FOLDERS:
        for path in sorted((checkout / folder / name).rglob("*")):
            relative = PurePosixPath(path.relative_to(checkout).as_posix())
            if path.is_file() and "__pycache__" not in relative.parts and not _regression_suite(relative.name):
                files.append(str(relative))
    return files


def _manifest_path(directory: Path) -> str | None:
    """The reviewer manifest a fixture's pull.json names, or None for the generic reviewer."""
    try:
        manifest_path = json.loads((directory / "pull.json").read_text(encoding="utf-8")).get("manifest_path")
    except (OSError, ValueError, AttributeError) as exc:
        raise ScenarioError(f"cannot read pull.json's manifest_path: {exc}") from exc
    return None if manifest_path is None else _path(manifest_path)


def _manifest(tree: Path, manifest_path: str, what: str) -> dict[str, Any]:
    try:
        return validate_adapter_manifest(json.loads((tree / manifest_path).read_text(encoding="utf-8")))
    except (OSError, ValueError, RuntimeContractError) as exc:
        raise ScenarioError(f"{what} {manifest_path} cannot be read: {exc}") from exc


def finding_categories(spec: Mapping[str, Any], directory: Path, checkout: Path) -> list[str] | None:
    """The finding categories of the manifest the scenario's reviewers follow, or None when it lists none: a document
    scenario's is review-document's design manifest, a repository reviewer's the checkout's manifest, and any other
    fixture's the one its pull.json names, which prepare reads from the base tree."""
    if "document" in spec:
        return list(specialists_manifest(PurePosixPath(str(spec["document"])).name)["finding_categories"])
    manifest_path = _manifest_path(directory)
    if manifest_path is None:
        return None
    tree = checkout if "reviewer" in spec else directory / "base"
    categories = _manifest(tree, manifest_path, "the manifest").get("finding_categories")
    return None if categories is None else list(categories)


def _listed_categories(expectations: Sequence[Mapping[str, Any]], categories: Sequence[str] | None) -> None:
    """Refuse a finding expectation's category its manifest does not list, ignoring case, as the judge matches it."""
    if categories is None:
        return
    listed = {category.casefold() for category in categories}
    for item in expectations:
        if item["kind"] == "finding" and "category" in item and item["category"].strip().casefold() not in listed:
            raise ScenarioError(
                f"category {quote(item['category'])} is not one of the manifest's finding_categories: "
                f"{', '.join(categories)}"
            )


def reviewer_files(reviewer: str, directory: Path, checkout: Path) -> tuple[str, ...]:
    """The files a run of a repository reviewer's scenario places from the checkout: the reviewer's SKILL.md, its
    folders, its manifest, at the fixture's pull.json manifest_path, and every file that manifest declares (its
    resources, its specialists' profiles and resources, and its condition scripts). A condition's `reads` are not
    among them: they name what the script reads of the reviewed tree, the fixture's own. Raise ScenarioError when the
    reviewer or one of its files is missing from the checkout, or the fixture already holds a file the run places."""
    folder = REPOSITORY_SKILLS / reviewer
    if not (checkout / folder / "SKILL.md").is_file():
        raise ScenarioError(f"no repository skill {reviewer}: the checkout has no {folder}/SKILL.md")
    manifest_path = _manifest_path(directory)
    if manifest_path is None or not PurePosixPath(manifest_path).is_relative_to(folder):
        raise ScenarioError(f"pull.json's manifest_path must name {reviewer}'s manifest under {folder}/")
    manifest = _manifest(checkout, manifest_path, f"{reviewer}'s manifest")
    own = [str(folder / "SKILL.md"), *_reviewer_folder(checkout, folder), manifest_path]
    files = tuple(dict.fromkeys([*own, *declared_reviewer_files(manifest)]))
    for relative in files:
        if not (checkout / relative).is_file():
            raise ScenarioError(f"{reviewer} declares {relative}, which the checkout lacks")
        for tree in ("base", "head"):
            if (directory / tree / relative).exists():
                raise ScenarioError(f"its {tree}/ tree holds {relative}, which the run places from the checkout")
    return files


def _reviewer(spec: Mapping[str, Any], skill: str, directory: Path, checkout: Path) -> tuple[str, tuple[str, ...]]:
    """A repository reviewer's scenario's reviewer and the files a run places: the scenario sits under the
    reviewer's name, and the skill it names is the one that runs the reviewer on a fixture."""
    if spec["reviewer"] != skill:
        raise ScenarioError(f"scenario.json names reviewer {quote(str(spec['reviewer']))}, not {skill}")
    if spec["skill"] != REVIEWER_SKILL:
        raise ScenarioError(f"a reviewer's scenario is started by {REVIEWER_SKILL}, not {quote(str(spec['skill']))}")
    if "document" in spec:
        raise ScenarioError("a reviewer's scenario is a fixture scenario, with no document")
    _fixture(directory)
    return skill, reviewer_files(skill, directory, checkout)


def load_scenario(skill: str, directory: Path, checkout: Path = REPOSITORY_ROOT) -> Scenario:
    try:
        spec = json.loads((directory / "scenario.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{directory.name}: cannot read scenario.json: {exc}") from exc
    try:
        if not isinstance(spec, dict):
            raise ScenarioError("scenario.json must hold an object")
        mode = _choice(spec.get("mode"), MODES, "mode")
        allowed = SCENARIO_FIELDS | ({"prior"} if mode == "re-review" else set())
        if "document" in spec:
            allowed |= (
                {"document"} | ({"base"} if "base" in spec else set()) | ({"stage"} if "stage" in spec else set())
            )
        if "reviewer" in spec:
            allowed |= {"reviewer"}
        if set(spec) != allowed:
            raise ScenarioError(f"scenario.json must have exactly {', '.join(sorted(allowed))}")
        reviewer: str | None = None
        placed: tuple[str, ...] = ()
        if "reviewer" in spec:
            reviewer, placed = _reviewer(spec, skill, directory, checkout)
        elif spec["skill"] != skill:
            raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
        document: Path | None = None
        base: Path | None = None
        stage: str | None = None
        if "document" in spec:
            if mode != "initial":
                raise ScenarioError("a document scenario is an initial review: its mode is initial")
            document = _document(directory, spec["document"], "document")
            base = _document(directory, spec["base"], "base") if "base" in spec else None
            stage = _choice(spec["stage"], tuple(DOCUMENT_STAGES), "stage") if "stage" in spec else None
        elif reviewer is None:
            _fixture(directory)  # a reviewer's scenario had its fixture checked in _reviewer
        prior = None
        if mode == "re-review":
            prior = directory / _path(spec["prior"])
            if not prior.is_file():
                raise ScenarioError(f"prior record {spec['prior']} is missing")
        if not isinstance(spec["expectations"], list) or not spec["expectations"]:
            raise ScenarioError("expectations must be a non-empty list")
        expectations = tuple(expectation(item, mode) for item in spec["expectations"])
        _listed_categories(spec["expectations"], finding_categories(spec, directory, checkout))
    except ScenarioError as exc:
        raise ScenarioError(f"{directory.name}: {exc}") from exc
    return Scenario(
        str(spec["skill"]),
        directory.name,
        directory,
        mode,
        prior,
        expectations,
        document,
        base,
        reviewer,
        checkout if reviewer else None,
        placed,
        stage,
    )


def prepare_arguments(scenario: Scenario, fixture: Path | None = None) -> list[str]:
    """The arguments that prepare a run of a fixture scenario, which `review_pipeline.py prepare` and review-prs both
    take: a fixture canary of the scenario, or of the copy of it given, and for a re-review its prior record."""
    arguments = ["--canary", "--fixture", str(scenario.directory if fixture is None else fixture)]
    if scenario.prior is not None:
        arguments += ["--re-review", "--prior", str(scenario.prior)]
    return arguments


def stage_document(scenario: Scenario, folder: Path, git: GitClient) -> Path:
    """Write a document scenario's document into a new folder under its own file name, and return its path there.
    Without a base it stands alone, so the skill reviews it whole; with one, the folder is a new git checkout whose
    one commit holds the base under that name, and the document is written over it as an uncommitted edit."""
    if scenario.document is None:
        raise ScenarioError(f"{scenario.name} is a fixture scenario, with no document")
    folder.mkdir(parents=True)
    staged = folder / scenario.document.name
    if scenario.base is not None:
        with tempfile.TemporaryDirectory(prefix="skill-evals-hooks.") as hooks:
            isolated = ["-c", f"core.hooksPath={hooks}"]
            git.output([*isolated, "init", "--quiet"], directory=folder)
            staged.write_bytes(scenario.base.read_bytes())
            git.output([*isolated, "add", "--", staged.name], directory=folder)
            git.output(
                [*isolated, *COMMIT, "commit", "--quiet", "--no-verify", "--message", "The committed base"],
                directory=folder,
            )
    staged.write_bytes(scenario.document.read_bytes())
    return staged


@contextlib.contextmanager
def reviewed_fixture(scenario: Scenario) -> Iterator[Path]:
    """The fixture a run reviews. A repository reviewer's scenario's is a throwaway copy of it, outside the run's home
    so the session never finds the copied skill as one of its own, with each placed file written from the checkout at
    its path in both trees; the copy is removed when the block ends, however it ends. Any other scenario's is its own
    folder, untouched."""
    if scenario.reviewer is None or scenario.checkout is None:
        yield scenario.directory
        return
    copy = Path(tempfile.mkdtemp(prefix="skill-evals-fixture-")).resolve()
    try:
        fixture = copy / scenario.name
        shutil.copytree(scenario.directory, fixture)
        for relative in scenario.placed:
            content = (scenario.checkout / relative).read_bytes()
            for tree in ("base", "head"):
                target = fixture / tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        yield fixture
    finally:
        fsops.remove(copy)


def skill_arguments(scenario: Scenario, run: Path, git: GitClient, fixture: Path | None = None) -> list[str]:
    """What the skill is started with: a fixture scenario's prepare arguments, on the fixture given when it is a
    copy, or the path of a document scenario's document, staged in the run's folder, and its stage, if it names one."""
    if scenario.document is None:
        return prepare_arguments(scenario, fixture)
    staged = str(stage_document(scenario, run / DOCUMENT_FOLDER, git))
    return [staged] if scenario.stage is None else [staged, "--stage", scenario.stage]


def load_scenarios(
    skill: str, names: Sequence[str] = (), root: Path = SCENARIO_ROOT, checkout: Path = REPOSITORY_ROOT
) -> list[Scenario]:
    """Each named scenario once in first-occurrence order, or every scenario in name order; a repository reviewer's
    files come from the checkout."""
    folder = root / skill
    found = sorted(path.parent.name for path in folder.glob("*/scenario.json")) if folder.is_dir() else []
    if not found:
        raise ScenarioError(f"no scenarios for {skill}: each is a <scenario>/scenario.json under {folder}")
    unknown = [name for name in names if name not in found]
    if unknown:
        raise ScenarioError(f"no scenario {', '.join(unknown)} for {skill}")
    return [load_scenario(skill, folder / name, checkout) for name in dict.fromkeys(names or found)]


# Reading a record


# A record source gives only a record code-review-core's record contract accepts, so these read its parts unchecked.


def review(record: Record) -> Mapping[str, Any]:
    return record["review"]


def findings(record: Record) -> list[Mapping[str, Any]]:
    return record["findings"]


def ledger_entries(record: Record) -> list[Mapping[str, Any]]:
    ledger = record.get("ledger")
    if ledger is None:
        raise RecordError("the record has no ledger")
    return ledger


def entry_severity(record: Record, key: tuple[int, str]) -> str:
    """The severity of the ledger entry with this version and ID."""
    entry = next((item for item in ledger_entries(record) if (item["version"], item["id"]) == key), None)
    if entry is None:
        raise RecordError(f"the record's ledger has no v{key[0]}:{key[1]}")
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
    """A record source reading DIR/<scenario>/<model>.json, holding each record to what a run's is held to: the
    record contract, the scenario's review version, a guard that held every reviewer, and each reviewer's reported
    model."""

    def source(scenario: Scenario, model: str) -> Record:
        path = directory / scenario.name / f"{model}.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RecordError(f"no record at {path}") from exc
        except (OSError, ValueError) as exc:
            raise RecordError(f"cannot read {path}: {exc}") from exc
        try:
            validate_record(record)
        except ValueError as exc:
            raise RecordError(f"{path} is not a review record: {exc}") from exc
        version, wanted = record["review"]["version"], RECORD_VERSION[scenario.mode]
        reason = (
            f"it is review version {version}, not the {wanted} a {scenario.mode} run records"
            if version != wanted
            else unguarded(record) or unreported_model(record, model)
        )
        if reason:
            raise RecordError(f"{path} counts for nothing: {reason}")
        return record

    return source


# Running


@dataclass(frozen=True)
class Run:
    """One scenario run on one model: the record it wrote, or why it counts for nothing, and the models it used.
    `recorded` is that reason as the results file records it, in fixed phrases that name no path of the machine."""

    record: Record | None
    failure: str | None
    reviewers: frozenset[str]
    session: frozenset[str]
    recorded: str | None = None


def review_config(directory: Path) -> dict[str, Any]:
    """The code-review configuration every run reads. A fixture is reviewed by the generic reviewer, or by the
    manifest its pull.json names, whatever is configured, so the one repository is a placeholder; only the runtime and
    the verdict policy matter."""
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


def prompt(skill: str, arguments: Sequence[str]) -> str:
    """The skill started by name with its arguments, each path quoted."""
    return " ".join([f"/{skill}", *(item if item.startswith("--") else f'"{item}"' for item in arguments)])


def reviewer_script_folders(home: Path, skill: str) -> list[str]:
    """Each spelling of the home's deployed core scripts folder a role's prompt may give, ending in its separator: the
    started skill's ${CLAUDE_SKILL_DIR}/../code-review-core/scripts/ as Claude Code fills it in, which the pipeline
    repeats from its start, and the folder's resolved path, which it gives when it cannot repeat the start."""
    skills = home / SKILLS
    started = f"{runtime_canary.forward(skills / skill)}/../{CORE}/scripts/"
    return list(dict.fromkeys([started, f"{skills / CORE / 'scripts'}{os.sep}"]))


def reviewer_grants(home: Path, skill: str) -> list[str]:
    """The commands a reviewer runs besides its file tools, granted in both shells at each spelling of the home's
    scripts: its self-check, which review_pipeline.py writes as python -B "<script>" validate-result --run "<run>"
    --role "<role>", and a lazy snapshot's two source commands, which review_source.py writes as python -B "<script>"
    source-file --run "<run>" --role "<role>" --path="<path>", and the same with source-search and --pattern. Each
    names its script by its quoted path, so no other file matches; validation holds them to the skill grant rules."""
    grants: list[str] = []
    for folder in reviewer_script_folders(home, skill):
        grants += [
            f'Bash({PYTHON} -B "{folder}review_pipeline.py" validate-result --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_pipeline.py" validate-result --run *)',
            f'Bash({PYTHON} -B "{folder}review_source.py" source-file --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_source.py" source-file --run *)',
            f'Bash({PYTHON} -B "{folder}review_source.py" source-search --run *)',
            f'PowerShell({PYTHON} -B "{folder}review_source.py" source-search --run *)',
        ]
    return grants


def claude_command(executable: str, request: str, home: Path, skill: str) -> list[str]:
    # Project settings only, so the installed skills and agents do not load beside the home's; the reviewer agent from
    # the home's file for --agents, so its guard hook runs; and no Workflow tool, so reviewers start as native
    # subagents whose messages the transcript carries with their model.
    return [
        executable,
        "-p",
        request,
        "--model",
        MODELS[SESSION_MODEL],
        "--output-format",
        "stream-json",
        "--verbose",
        "--forward-subagent-text",
        "--include-hook-events",
        "--setting-sources",
        "project,local",
        "--agents",
        str(home / STATE / AGENTS),
        "--strict-mcp-config",
        "--no-session-persistence",
        "--disallowedTools",
        "Workflow",
        "--permission-mode",
        "acceptEdits",
        "--allowedTools",
        *reviewer_grants(home, skill),
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
    """Put the model's ID in the home's copy of the reviewer agent in place of `model: inherit`; return why it
    could not, or None."""
    path = home / REVIEWER_AGENT
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
        at = lines.index("model: inherit", 1, lines.index("---", 1))
    except (OSError, ValueError):
        return f"{runtime_canary.forward(path)} has no frontmatter line `model: inherit` to set"
    lines[at] = f"model: {MODELS[model]}"
    path.write_text("\n".join(lines), encoding="utf-8")
    if frontmatter.read(path).string("model") == MODELS[model]:
        return None
    return f"the reviewer agent does not read as {MODELS[model]}"


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


def names_model(name: str, model_id: str) -> bool:
    """Whether a message's model is that model ID, with or without a dated suffix."""
    return name == model_id or (name.startswith(model_id) and DATED.fullmatch(name[len(model_id) :]) is not None)


def latest_review(pull: Path, version: int) -> Record | str:
    """A pull request folder's latest record, read and validated by code-review-core's archive, when it is this
    review version, or else what its latest record is. The folder is <canary root>/<owner>/<name>/pulls/<number>."""
    latest = max(list_versions(pull), default=None)
    try:
        record = latest_record(pull.parents[3], f"{pull.parents[2].name}/{pull.parents[1].name}", int(pull.name))
    except (ArchiveError, PersistenceError, OSError, ValueError) as exc:
        return f"its latest record cannot be read: {exc}"
    if record is None:
        return "it holds no record"
    found = record["review"]["version"] if latest == version else latest
    return record if found == version else f"its latest record is version {found}"


def find_record(directory: Path, version: int) -> Record:
    """The record a run wrote: the latest record of the pull request under the canary roots in its temporary
    directory, which must be this review version. Raise RecordError when none is, saying what each pull request's
    latest record is instead, or when more than one is, since which is the run's own is then unknown."""
    pulls = sorted(path for path in directory.glob("code-review-canary-*/*/*/pulls/*") if path.is_dir())
    if not pulls:
        raise RecordError("the run recorded no review")
    latest = [latest_review(pull, version) for pull in pulls]
    found = [record for record in latest if not isinstance(record, str)]
    if len(found) > 1:
        raise RecordError(f"the run recorded {len(found)} reviews at version {version}, so which is its own is unknown")
    if not found:
        reasons = "; ".join(reason for reason in latest if isinstance(reason, str))
        raise RecordError(f"the run recorded no review version {version}; {reasons}")
    return found[0]


def run_failure(
    model: str, completed: Completed, timeout: float, skill: str, missing: str | None, recorded: bool = False
) -> str | None:
    """Why a run counts for nothing, or None when it recorded a review on the model it was given. With `recorded`, the
    reason as the results file records it: why the record is missing, why Claude Code could not start, and each
    denied command, which may name the machine's paths, are each a fixed phrase."""
    listed, denials = runtime_canary.claude_stream(completed.stdout)
    session, reviewers = stream_models(completed.stdout)
    if missing is not None:
        reasons = [NO_REVIEW if recorded else missing]
        if completed.error:
            reasons.append("Claude Code could not start" if recorded else completed.error)
        elif completed.timed_out:
            reasons.append(f"timed out after {timeout:g} seconds")
        elif completed.returncode != 0:
            reasons.append(f"Claude Code exited with code {completed.returncode}")
        if listed is not None and skill not in listed:
            reasons.append(f"Claude Code did not list {skill}")
        if denials and recorded:
            reasons.append(f"denied {len(denials)} command{'' if len(denials) == 1 else 's'}")
        elif denials:
            reasons.append(f"denied: {'; '.join(denials)}")
        return "; ".join(reasons)
    if not reviewers:
        return "no reviewer subagent ran, so the record is not the model's"
    other = sorted(name for name in reviewers if not names_model(name, MODELS[model]))
    if other:
        return f"reviewers ran on {', '.join(other)}, not {MODELS[model]}"
    elsewhere = sorted(name for name in session if not names_model(name, MODELS[SESSION_MODEL]))
    if elsewhere:
        return f"the session ran on {', '.join(elsewhere)}, not {MODELS[SESSION_MODEL]}"
    return None


def unguarded(record: Record) -> str | None:
    """Why the record shows a reviewer the reviewer guard did not hold, or None. The guard's claim starts the role's
    read log, so a held reviewer's files_read is a count, if only 0; null means no guard ran."""
    reviewers = review(record).get("reviewers")
    if not reviewers:
        return "the record names no reviewer"
    loose = [str(reviewer["id"]) for reviewer in reviewers if reviewer.get("files_read") is None]
    return f"no reviewer guard held {', '.join(loose)}: its files_read is null" if loose else None


def unreported_model(record: Record, model: str) -> str | None:
    """Why the record does not show every reviewer on the model's ID, by the model each reviewer's result reports, or
    None. A reviewer reports its own model, so this is weaker than a run's check of the transcript."""
    others = [
        f"{reviewer['id']} on {reviewer.get('model', 'no model it reports')}"
        for reviewer in review(record).get("reviewers", [])
        if not names_model(str(reviewer.get("model", "")), MODELS[model])
    ]
    return f"its reviewers did not all run on {MODELS[model]}: {', '.join(others)}" if others else None


@dataclass(frozen=True)
class Session:
    """What every run starts its session with: Claude Code, the user's environment without git's repository-selecting
    variables, the runner, its time limit, and the git client a document scenario's checkout is made with."""

    executable: str
    base: Mapping[str, str]
    runner: Runner
    timeout: float
    git: GitClient


def run_scenario(scenario: Scenario, model: str, home: Path, session: Session) -> tuple[Run, Path]:
    """Run the scenario once in the model's home; return what it gave and its transcript."""
    run = home / STATE / scenario.name
    (run / "tmp").mkdir(parents=True)
    transcript = run / "transcript.jsonl"
    with contextlib.ExitStack() as copies:
        try:
            fixture = copies.enter_context(reviewed_fixture(scenario))
            arguments = skill_arguments(scenario, run, session.git, fixture)
        except (GitError, OSError) as exc:
            transcript.write_text("", encoding="utf-8")
            staged = "fixture" if scenario.document is None else "document"
            failure = f"the {staged} could not be staged"
            return Run(None, f"{failure}: {exc}", frozenset(), frozenset(), failure), transcript
        return _session_run(scenario, model, home, session, arguments, transcript)


def _session_run(
    scenario: Scenario, model: str, home: Path, session: Session, arguments: Sequence[str], transcript: Path
) -> tuple[Run, Path]:
    """Start the session on the skill with its arguments in the model's home, keep its transcript, and find the
    record the run wrote."""
    run = transcript.parent
    environment = run_environment(session.base, run, home / STATE / "config.json")
    command = claude_command(session.executable, prompt(scenario.skill, arguments), home, scenario.skill)
    completed = session.runner(command, home, environment, session.timeout)
    transcript.write_text(completed.stdout, encoding="utf-8")
    if completed.stderr:
        transcript.with_suffix(".stderr.txt").write_text(completed.stderr, encoding="utf-8")
    record: Record | None = None
    missing = None
    try:
        record = find_record(run / "tmp", RECORD_VERSION[scenario.mode])
    except RecordError as exc:
        missing = str(exc)
    failure = run_failure(model, completed, session.timeout, scenario.skill, missing)
    recorded = run_failure(model, completed, session.timeout, scenario.skill, missing, recorded=True)
    if failure is None and record is not None:
        failure = recorded = unguarded(record)
    orchestrators, reviewers = stream_models(completed.stdout)
    return Run(None if failure else record, failure, reviewers, orchestrators, recorded), transcript


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
    if "*" in str(home):
        return (
            f"the home's path {runtime_canary.forward(home)} holds a *, which a reviewer's grant would read as any text"
        )
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
    scenarios: Sequence[Scenario], homes: Mapping[str, Path], session: Session, jobs: int
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
            ended(scenario, model, run_scenario(scenario, model, home, session))
        return runs
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        pending = {
            pool.submit(run_scenario, scenario, model, home, session): (scenario, model)
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
    model none of whose reviewers ran is recorded as not run, with its first run's reason as the results file records
    it, which names no path of the machine. A scenario none of whose
    reviewers ran on a model, such as one the session refused before starting any, holds no judgment of that model,
    so its cell says `not run` and its expectations are left out of the model's count."""
    rows = []
    for model in models:
        mine = [runs[(scenario.name, model)] for scenario in scenarios if (scenario.name, model) in runs]
        reviewers = sorted({name for run in mine for name in run.reviewers})
        session = ", ".join(sorted({name for run in mine for name in run.session})) or "unknown"
        theirs = [outcome for outcome in outcomes if outcome.model == model]
        if reviewers:
            unreviewed = {
                scenario.name
                for scenario in scenarios
                if (scenario.name, model) in runs and not runs[(scenario.name, model)].reviewers
            }
            theirs = [outcome for outcome in theirs if outcome.scenario not in unreviewed]
            identifier = ", ".join(reviewers)
            detail = ", ".join(
                f"{scenario.name} not run"
                if scenario.name in unreviewed
                else f"{scenario.name} {sum(1 for o in theirs if o.scenario == scenario.name and o.failure is None)}"
                f"/{sum(1 for o in theirs if o.scenario == scenario.name)}"
                for scenario in scenarios
            )
        else:
            identifier = "not run"
            detail = next((run.recorded for run in mine if run.recorded), "no run")
        passed = f"{sum(1 for outcome in theirs if outcome.failure is None)}/{len(theirs)}"
        cells = [skill, model, identifier, passed, detail, session, version, today.isoformat()]
        rows.append(f"| {' | '.join(_cell(cell) for cell in cells)} |")
    return rows


def _cells(line: str) -> list[str] | None:
    """A table line's cells, stripped, or None when the line is not one: it starts and ends with a cell border."""
    stripped = line.strip()
    borders = [match.start() for match in CELL_BORDER.finditer(stripped)]
    if len(borders) < 2 or borders[0] != 0 or borders[-1] != len(stripped) - 1:
        return None
    return [cell.strip() for cell in CELL_BORDER.split(stripped[1:-1])]


def _row_skill(row: str) -> str:
    """The skill a row of a table results_table read names."""
    return (_cells(row) or [""])[0]


def results_table(text: str) -> tuple[int, int, list[str]]:
    """The line indexes of the results markers in docs/skill-evaluations.md and the table's rows between them, each
    as written. Raise ScenarioError when the markers are not there once each and in order, or when anything between
    them is not the header, its separator, and then rows of the header's columns, each naming a skill."""
    lines = text.split("\n")
    if lines.count(RESULTS_BEGIN) != 1 or lines.count(RESULTS_END) != 1:
        raise ScenarioError(f"the results file needs one {RESULTS_BEGIN} line and one {RESULTS_END} line")
    begin, end = lines.index(RESULTS_BEGIN), lines.index(RESULTS_END)
    if end < begin:
        raise ScenarioError(f"{RESULTS_END} comes before {RESULTS_BEGIN} in the results file")
    header = _cells(RESULTS_HEADER[0]) or []
    table = lines[begin + 1 : end]
    if not table or _cells(table[0]) != header:
        raise ScenarioError(f"the results table must start with the header {RESULTS_HEADER[0]}")
    separator = _cells(table[1]) if len(table) > 1 else None
    if separator is None or len(separator) != len(header) or not all(map(SEPARATOR_CELL.fullmatch, separator)):
        raise ScenarioError(f"the results table's header must be followed by its separator {RESULTS_HEADER[1]}")
    for number, row in enumerate(table[2:], begin + 4):
        cells = _cells(row)
        if cells is None or len(cells) != len(header) or not cells[0]:
            raise ScenarioError(
                f"line {number} of the results file is not a row of the table's {len(header)} columns naming a "
                f"skill: {quote(row)}"
            )
    return begin, end, table[2:]


def replace_results(text: str, skill: str, rows: Sequence[str]) -> str:
    """The results file with the skill's rows replaced by these and every other skill's rows kept as written, sorted
    by skill; raise ScenarioError when results_table cannot read its table."""
    lines = text.split("\n")
    begin, end, existing = results_table(text)
    kept = [row for row in existing if _row_skill(row) != skill]
    table = sorted([*kept, *rows], key=_row_skill)
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
    which: Callable[[str], str | None] = platform_support.find_executable
    deploy: Callable[[Path, Path], tuple[int, str]] = runtime_canary.deploy
    environment: Mapping[str, str] | None = None
    make_home: Callable[[], Path] = new_home
    today: Callable[[], datetime.date] = datetime.date.today
    results: Path = RESULTS
    git: GitClient = field(default_factory=GitClient)


def preflight(write: bool, seams: Seams) -> tuple[str, str | None]:
    """Claude Code's path and, with --write, the results file's text; raise ScenarioError before any run when
    something a run needs is missing."""
    executable = seams.which("claude")
    if executable is None:
        raise ScenarioError("Claude Code (claude) is not installed")
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
    # Without git's repository-selecting variables, so neither the version probe nor a session, nor anything the
    # session starts, reads the repository a shell's `GIT_DIR` names rather than the throwaway home.
    base = without_repository_variables(os.environ if seams.environment is None else seams.environment)
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
    session = Session(executable, base, seams.runner, options.timeout, seams.git)
    runs = run_all(scenarios, homes, session, options.jobs)
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


def seconds(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds, not {value}")
    return number


def skill_name(value: str) -> str:
    if not SKILL_NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"must be a skill's name, a folder under {SCENARIO_ROOT}, not {value}")
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run a skill's scenarios on each model and judge their records.")
    result.add_argument(
        "skill", type=skill_name, help="the skill whose scenarios to judge, a folder under tests/fixtures/skill-evals"
    )
    result.add_argument("--records", type=Path, help="judge a folder of <scenario>/<model>.json records; run nothing")
    result.add_argument("--scenario", action="append", default=[], help="judge only this scenario (repeatable)")
    result.add_argument("--model", action="append", choices=list(MODELS), help="judge only this model (repeatable)")
    result.add_argument("--write", action="store_true", help="replace the skill's rows in docs/skill-evaluations.md")
    result.add_argument(
        "--timeout", type=seconds, default=DEFAULT_TIMEOUT, help=f"seconds for each run (default {DEFAULT_TIMEOUT})"
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
    run_main(lambda: main(sys.argv[1:]), errors="backslashreplace")
