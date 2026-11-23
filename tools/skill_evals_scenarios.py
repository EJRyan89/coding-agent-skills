"""Loading a skill's evaluation scenarios: their fixtures, documents, reviewer files, trigger requests, and the
expectations each run is judged against."""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

import json_writer
from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-document")))
sys.path.insert(0, str(sibling_scripts("review-insights")))
sys.path.insert(0, str(sibling_scripts("audit-skill")))

import command_wrappers
import file_presence
import json_reader
import private_folder
import skill_inventory
import whole_number
from bash_syntax import BashSyntaxError
from git_client import NO_HOOKS, GitClient
from path_containment import resolved_path, within
from review_config import ConfigurationError, validate_repository_identity
from review_document import specialists_manifest
from review_flags import FlagError, load_store
from review_io import PersistenceError
from review_records import LEDGER_ID, source_parts
from review_runtime import (
    DOCUMENT_STAGES,
    GENERIC_SPECIALIST,
    RuntimeContractError,
    declared_reviewer_files,
    validate_adapter_manifest,
)
from review_synthesis import TYPES as RECOMMENDATION_TYPES
from review_vocabularies import DISPOSITIONS as RECORD_DISPOSITIONS
from review_vocabularies import SEVERITY_RANK
from suite_names import is_suite_name

from deployer import fsops
from tools import runtime_canary
from tools.skill_evals_records import (
    FAILED_CALL,
    MODELS,
    Record,
    RecordError,
    RoutePin,
    entry_severity,
    findings,
    judgments,
    review,
    session_calls,
    synthesized,
)

PLATFORM_ALLOWED = {
    "pathlib.PureWindowsPath": "refuses a path a scenario names that Windows reads as absolute, a drive's included, "
    "on every platform, since a scenario runs on each"
}


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals"
# The record's own vocabulary, from ejryan89-code-review-core's record module, in a stable order for messages:
# severities from the lowest rank, dispositions alphabetically.
SEVERITIES = tuple(sorted(SEVERITY_RANK, key=SEVERITY_RANK.__getitem__))
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
DISPOSITIONS = tuple(sorted(RECORD_DISPOSITIONS))
TRIGGER = "trigger"
SYNTHESIS = "synthesis"
MODES = ("initial", "re-review", SYNTHESIS, TRIGGER)
# A synthesis scenario: the one skill whose synthesis it judges, the folders of its fixture, which a run copies, and
# its flag store, and how a flag is named.
SYNTHESIS_SKILL = "review-insights"
SYNTHESIS_FOLDERS = ("archive", "checkout")
FLAGS = "flags.json"
FLAG_ID = re.compile(r"RF-[0-9]{6,}")
SCENARIO_FIELDS = frozenset({"skill", "mode", "expectations"})
# Where a trigger scenario's shipped skills are.
SHIPPED_SKILLS = "skills"
# The folder in a run's own folder where a document scenario's document is written for the skill to review.
DOCUMENT_FOLDER = "document"
# A by-name document scenario's folders in the model's home: the one the document is staged in, one folder down from
# the session's working directory, and the two decoys review-document's `resolve` passes over, a hidden folder and
# another checkout, each holding a copy under the same name.
BY_NAME_FOLDER = "drafts"
HIDDEN_DECOY = ".drafts-old"
CHECKOUT_DECOY = "old-checkout"
# What the `resolved` kind reads in the session's own tool calls: the search tools, the programs `_search_programs`
# names, review-document's script, and, since no reader here parses PowerShell as pwsh does, a PowerShell command's
# search program at a command's start, an assignment's right side included, and the script's `resolve` and `build`
# commands in its text. A Bash command is read through audit-skill's `bash_programs`, the one Bash grammar, which finds
# every program Bash runs, past assignments, wrappers, and shell scripts.
SEARCH_TOOLS = frozenset({"Glob", "Grep"})
DOCUMENT_SCRIPT_NAME = "review_document.py"
DOCUMENT_SCRIPT = r"review_document\.py[\"']?\s+"
# A repository reviewer's scenario: the skill that runs a reviewer manifest on a fixture, where this repository's own
# skills are, and the folders of a reviewer whose files are placed, besides its SKILL.md and what its manifest declares.
REVIEWER_SKILL = "review-prs"
REPOSITORY_SKILLS = PurePosixPath(".claude", "skills")
REVIEWER_FOLDERS = ("references", "scripts")
# The ending a repository reviewer's fixture holds a file under, which the run's copy drops.
HELD = ".fixture"
# The identity the throwaway checkout's one commit is made under, so no identity of the user's is needed or recorded,
# and signing is disabled.
COMMIT = ["-c", "user.name=skill-evals", "-c", "user.email=skill-evals@example.invalid", "-c", "commit.gpgsign=false"]


class ScenarioError(Exception):
    """A scenario that cannot be loaded."""


@dataclass(frozen=True)
class Expectation:
    label: str
    # The reason the record fails the expectation, or None when it passes.
    check: Callable[[Record], str | None]
    # Whether it reads only the session's own tool calls, SESSION_CALLS, so a by-name run that recorded no review is
    # still judged on them when its record source carries them.
    from_transcript: bool = False


@dataclass(frozen=True, kw_only=True)
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
    # Whether a document scenario starts the skill with its document's file name alone, staged in the model's home.
    by_name: bool = False
    # A repository reviewer's fixture's held files, each by its path in the fixture, which the copy restores.
    held: tuple[str, ...] = ()
    # A trigger scenario's requests that should start its skill, and its near misses, which should not: its
    # neighbours' requests, then its requests no skill serves.
    requests: tuple[str, ...] = ()
    near_misses: tuple[str, ...] = ()
    # Each route of the scenario's reviewer manifest that pins a model or an effort.
    pins: tuple[RoutePin, ...] = ()
    # A synthesis scenario's inclusive date range, as ISO dates, and the repository it analyzes.
    period: tuple[str, str] | None = None
    repository: str | None = None


def quote(value: str) -> str:
    return json_writer.dumps(value, ensure_ascii=False)


def _fields(spec: Mapping[str, object], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    missing = sorted(required - set(spec))
    unknown = sorted(set(spec) - required - optional)
    if missing:
        raise ScenarioError(f"{spec.get('kind')} expectation lacks {', '.join(missing)}")
    if unknown:
        raise ScenarioError(f"{spec.get('kind')} expectation does not take {', '.join(unknown)}")


def _choice(value: object, choices: Sequence[str], what: str) -> str:
    if value not in choices:
        raise ScenarioError(f"{what} must be one of {', '.join(choices)}, not {quote(str(value))}")
    return str(value)


def _path(value: object) -> str:
    """A plain relative path. One either platform reads as absolute, a drive's included, is refused on both, since a
    scenario runs on each: on Windows, a path joined to a drive leaves the scenario's folder."""
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).anchor
        or ".." in PurePosixPath(value).parts
        or str(PurePosixPath(value)) != value
    ):
        raise ScenarioError(f"path must be a plain relative path, not {quote(str(value))}")
    return value


def _count(value: object, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScenarioError(f"{what} must be a count, not {quote(str(value))}")
    return value


def _lines(value: object) -> tuple[int, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(line, int) or isinstance(line, bool) or line < 1 for line in value)
        or len(set(value)) != len(value)
    ):
        raise ScenarioError(f"lines must be distinct positive line numbers, not {quote(str(value))}")
    return tuple(sorted(value))


def _entry(value: object) -> tuple[int, str]:
    refused = ScenarioError(f"entry must look like v1:F001, not {quote(str(value))}")
    match = LEDGER_ID.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise refused
    return whole_number.read(match[1], refused), match[2]


def _where(path: str, lines: tuple[int, ...]) -> str:
    return f"{path}:{','.join(str(line) for line in lines)}"


def _rank(severity: str) -> int:
    return SEVERITIES.index(severity)


def _source(spec: Mapping[str, object]) -> str | None:
    """The reviewer an expectation names in `source`, or None when it names none and any reviewer counts."""
    if "source" not in spec:
        return None
    if not isinstance(spec["source"], str) or not spec["source"].strip():
        raise ScenarioError("source must be a reviewer's id; leave it out for any reviewer")
    return spec["source"].strip()


def _raised_by(finding: Mapping[str, Any], source: str | None) -> bool:
    """Whether the reviewer `source` is one of those the finding's `source` names, or any reviewer counts."""
    return source is None or source in source_parts(str(finding.get("source", "")))


def finding_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "path", "lines", "severity"}, frozenset({"category", "source"}))
    path, lines = _path(spec["path"]), _lines(spec["lines"])
    severity = _choice(spec["severity"], SEVERITIES, "severity")
    category: str | None = None
    if "category" in spec:
        if not isinstance(spec["category"], str) or not spec["category"].strip():
            raise ScenarioError("category must be a category name; leave it out for any category")
        category = spec["category"].strip()
    source = _source(spec)

    def matches(finding: Mapping[str, Any]) -> bool:
        if not _raised_by(finding, source):
            return False
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
        raised = f" by {finding.get('source')}" if source is not None else ""
        return f"{finding['severity']}{kind}{raised} at line {finding['line']}"

    wanted = severity if category is None else f"{severity} {category}"
    wanted = wanted if source is None else f"{wanted} by {source}"
    return Expectation(f"finding {_where(path, lines)} {wanted}", check)


def no_finding_above_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "severity"}, frozenset({"paths", "lines", "source"}))
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
    source = _source(spec)

    def check(record: Record) -> str | None:
        unseen = None if source is None else _unseen(record, source)
        if unseen is not None:
            return unseen
        raised = "" if source is None else f" by {source}"
        above = [
            f"{finding['severity']}{raised} at {finding['path']}:{finding['line']}"
            for finding in findings(record)
            if _rank(finding["severity"]) > _rank(severity)
            and (paths is None or finding["path"] in paths)
            and (lines is None or finding["line"] in lines)
            and _raised_by(finding, source)
        ]
        return f"found {', '.join(above)}" if above else None

    where = _where(paths[0], lines) if paths and lines else ",".join(paths) if paths else "*"
    by = "" if source is None else f" by {source}"
    return Expectation(f"no_finding_above {severity} {where}{by}", check)


def _unseen(record: Record, source: str) -> str | None:
    """Why the record cannot show what the reviewer `source` would have raised, or None: the review names no run of
    that reviewer, or it could not read every changed file (its coverage lists unavailable sources), whatever its
    verdict, since another reviewer's blocking finding records CHANGES_REQUESTED over INCOMPLETE. Either way the
    reviewer's silence is no evidence."""
    ran = [str(reviewer.get("id")) for reviewer in review(record).get("reviewers", [])]
    if source not in ran:
        return f"{source} did not run; the review ran {', '.join(ran) or 'no reviewer'}"
    unavailable = review(record).get("coverage", {}).get("unavailable_sources", [])
    if unavailable:
        return (
            f"{source} could not read every changed file: the review's coverage lists unavailable sources "
            f"{', '.join(unavailable)}"
        )
    return None


def only_at_expectation(spec: Mapping[str, object]) -> Expectation:
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


def verdict_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "verdict"})
    verdict = _choice(spec["verdict"], VERDICTS, "verdict")

    def check(record: Record) -> str | None:
        actual = review(record)["verdict"]
        return None if actual == verdict else f"verdict is {actual}"

    return Expectation(f"verdict {verdict}", check)


def ledger_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "addressed", "still_present"})
    expected = {name: _count(spec[name], name) for name in ("addressed", "still_present")}

    def check(record: Record) -> str | None:
        judged = judgments(record)
        actual = {name: sum(1 for disposition in judged.values() if disposition == name) for name in expected}
        return None if actual == expected else " ".join(f"{name}={count}" for name, count in actual.items())

    return Expectation(" ".join(["ledger", *(f"{name}={count}" for name, count in expected.items())]), check)


def disposition_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "entry", "disposition"})
    entry = _entry(spec["entry"])
    disposition = _choice(spec["disposition"], DISPOSITIONS, "disposition")

    def check(record: Record) -> str | None:
        judged = judgments(record)
        if entry not in judged:
            return f"this review did not judge {spec['entry']}"
        return None if judged[entry] == disposition else f"{spec['entry']} is {judged[entry]}"

    return Expectation(f"disposition {spec['entry']} {disposition}", check)


def repeats_expectation(spec: Mapping[str, object]) -> Expectation:
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


@dataclass(frozen=True, kw_only=True)
class Step:
    """One thing a call of the session's does, as the `resolved` kind reads it: the tool or program it searches with,
    whether it runs `resolve` with the document's name, whether it builds the staged copy (True), another file
    (False), or nothing (None), and whether it is a Bash command the grammar refuses, whose programs are unread."""

    search: str | None
    resolves: bool
    built: bool | None
    unread: bool


def _search_programs() -> frozenset[str]:
    """The programs that list files or search for them or their text, which the `resolved` kind fails before
    `resolve`: each by its file name as Bash finds it, matched as written, and PowerShell's gci and Get-ChildItem, which
    the PowerShell reading matches in any case, as PowerShell does."""
    return frozenset(
        {
            "find",
            "ls",
            "dir",
            "tree",
            "fd",
            "fdfind",
            "rg",
            "grep",
            "egrep",
            "fgrep",
            "locate",
            "mdfind",
            "gci",
            "Get-ChildItem",
        }
    )


def _powershell_search(text: str) -> str | None:
    """The first program of `_search_programs` a PowerShell text runs at a command's start, an assignment's right side
    included, by a path or none and with or without .exe, as the text writes it, or None."""
    found = re.search(
        r"(?:^|[;|&{(\n]|\$\(|\$[\w:]+\s*=)\s*(?:[\w.~:-]*[/\\])*("
        + "|".join(_search_programs())
        + r")(?:\.exe)?(?=\s|$|[;|&(){}])",
        text,
        re.IGNORECASE,
    )
    return found[1] if found else None


def _document_command(words: Sequence[str], command: str) -> list[str] | None:
    """The words after review_document.py's `command` in one program's words, or None when it runs no such command.
    The script is named by its file name, after a path in either separator."""
    for index, (word, following) in enumerate(pairwise(words)):
        if following == command and command_wrappers.bash_program(word.rpartition("\\")[2]) == DOCUMENT_SCRIPT_NAME:
            return list(words[index + 2 :])
    return None


def _builds_staged(arguments: Sequence[str], name: str) -> bool:
    """Whether `build`'s arguments name the document's staged copy, BY_NAME_FOLDER/<name>, in either separator."""
    return any(PurePosixPath(word.replace("\\", "/")).parts[-2:] == (BY_NAME_FOLDER, name) for word in arguments)


def _bash_steps(command: str, name: str) -> list[Step]:
    """The steps of a Bash command, one per program it runs, in the order the grammar reads them."""
    try:
        programs = skill_inventory.bash_programs(command)
    except BashSyntaxError:
        return [Step(search=None, resolves=False, built=None, unread=True)]
    steps = []
    for _, words in programs:
        program = command_wrappers.bash_program(words[0])
        built = _document_command(words, "build")
        steps.append(
            Step(
                search=program if program in _search_programs() else None,
                resolves=_document_command(words, "resolve") == [name],
                built=None if built is None else _builds_staged(built, name),
                unread=False,
            )
        )
    return steps


def _powershell_steps(command: str, name: str) -> list[Step]:
    """The steps of a PowerShell command, read from its text: a search program at a command's start before `resolve`,
    then `resolve` with the name, quoted or not, then `build` and whether it names the staged copy."""
    end = r"(?=[\s\"';&|)]|$)"
    ran = re.search(DOCUMENT_SCRIPT + r"resolve\s+([\"']?)" + re.escape(name) + r"\1" + end, command)
    searched = _powershell_search(command[: ran.start()] if ran else command)
    built = re.search(DOCUMENT_SCRIPT + r"build\b", command)
    staged = re.compile(r"(?:^|[\s\"'/\\])" + re.escape(BY_NAME_FOLDER) + r"[/\\]" + re.escape(name) + end)
    steps = [Step(search=searched, resolves=False, built=None, unread=False)] if searched else []
    steps += [Step(search=None, resolves=True, built=None, unread=False)] if ran else []
    if built:
        steps.append(
            Step(search=None, resolves=False, built=staged.search(command, built.end()) is not None, unread=False)
        )
    return steps


def _steps(call: Mapping[str, Any], name: str) -> list[Step]:
    """What one of the session's calls does, as the `resolved` kind reads it."""
    command = call["input"].get("command")
    if call["name"] in SEARCH_TOOLS:
        return [Step(search=str(call["name"]), resolves=False, built=None, unread=False)]
    if not isinstance(command, str) or call["name"] not in skill_inventory.SHELL_TOOLS:
        return []
    return _bash_steps(command, name) if call["name"] == "Bash" else _powershell_steps(command, name)


# The `resolved` verdict on a session that ran `resolve` and then built nothing.
BUILT_NOTHING = "the session built nothing after it ran resolve"


def _resolved_verdict(calls: Sequence[Mapping[str, Any]], name: str) -> str | None:
    """Why the session's calls fail the `resolved` kind, or None when they pass it."""
    resolved = False
    for step in (step for call in calls for step in _steps(call, name)):
        if not resolved and step.unread:
            return "the session ran a Bash command before resolve that the Bash grammar refuses, so it is unread"
        if not resolved and step.search is not None:
            return f"the session searched with {step.search} before it ran resolve"
        resolved = resolved or step.resolves
        if resolved and step.built is not None:
            if step.built:
                return None
            return f"the session built a file other than {BY_NAME_FOLDER}/{name}, the one copy resolve finds"
    if resolved:
        return BUILT_NOTHING
    return f"the session never ran review_document.py resolve {quote(name)}"


def judged_calls(calls: Sequence[Mapping[str, Any]], name: str) -> list[Mapping[str, Any]] | None:
    """The session's calls the `resolved` kind judges in a run that recorded no review. When the session's last call
    that ran `resolve` with the document's name ended in an error, as its tool result reports, what the session did
    after it was no choice of its own, so only the calls through that one are judged, and None is given when all they
    show is BUILT_NOTHING: a search or an unread command before `resolve` stays the session's, and so does all that the
    failed call itself wrote, a build chained after `resolve` included, since the session chose every part of it before
    any part ran and its result does not say which part failed. Otherwise every call is judged."""
    ran = [index for index, call in enumerate(calls) if any(step.resolves for step in _steps(call, name))]
    if not ran or calls[ran[-1]].get(FAILED_CALL) is not True:
        return list(calls)
    through = list(calls[: ran[-1] + 1])
    return None if _resolved_verdict(through, name) == BUILT_NOTHING else through


def resolved_expectation(spec: Mapping[str, object], name: str) -> Expectation:
    """That the session found the document named by `name` alone through review-document's `resolve`, searching for
    nothing before it, and then built the copy `resolve` finds, the one staged in BY_NAME_FOLDER."""
    _fields(spec, {"kind"})

    def check(record: Record) -> str | None:
        return _resolved_verdict(session_calls(record), name)

    return Expectation(f"resolved {quote(name)}", check, from_transcript=True)


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
# The kind judged from the session's own tool calls, which only a by-name document scenario's record carries.
RESOLVED = "resolved"


def _distinct(value: object, what: str, read: Callable[[object], str]) -> tuple[str, ...]:
    """A non-empty list of distinct values, each read by `read`."""
    if not isinstance(value, list) or not value:
        raise ScenarioError(f"{what} must be a non-empty list")
    items = tuple(read(item) for item in value)
    if len(set(items)) != len(items):
        raise ScenarioError(f"{what} name a value twice")
    return items


def _repository(value: object) -> str:
    try:
        return validate_repository_identity(value)
    except (ConfigurationError, ValueError) as exc:
        raise ScenarioError(f"repository must be owner/name, not {quote(str(value))}") from exc


def _type(value: object) -> str:
    return _choice(value, RECOMMENDATION_TYPES, "each of types")


def _flag(value: object) -> str:
    if not isinstance(value, str) or not FLAG_ID.fullmatch(value):
        raise ScenarioError(f"each of flags must be a flag ID such as RF-000001, not {quote(str(value))}")
    return value


def target_paths(item: Mapping[str, Any]) -> list[str]:
    """The files a synthesized recommendation targets: its target's path and, for a consolidation, the files it
    merges. An analyzer recommendation, or one that names no file, targets none."""
    target = item.get("target")
    if not isinstance(target, dict):
        return []
    named = [target.get("path"), *(target.get("merge") or [])]
    return [path for path in named if isinstance(path, str)]


def path_key(path: str) -> str:
    """A target path as the synthesis kinds compare it: its POSIX form, so `./`, `//`, and `/./` spell the same file,
    ignoring case."""
    return PurePosixPath(path).as_posix().casefold()


def describe_recommendation(item: Mapping[str, Any]) -> str:
    target = item.get("target") or {}
    where = (
        f"{target.get('repository')}:{'+'.join(target_paths(item)) or '(no file)'}"
        if "repository" in target
        else "an analyzer"
    )
    flags = ",".join(item.get("linked_flags") or []) or "none"
    return f"{item.get('id')} {item.get('type')} {where} flags={flags}"


@dataclass(frozen=True)
class RecommendationMatch:
    """Which synthesized recommendations a recommends or never_recommends expectation is about: of one of the types,
    when given, on the repository, when given, targeting one of the files, ignoring case, and linking every flag."""

    paths: tuple[str, ...]
    types: tuple[str, ...] | None
    repository: str | None
    flags: tuple[str, ...] = ()

    @classmethod
    def read(cls, spec: Mapping[str, object]) -> RecommendationMatch:
        return cls(
            paths=_distinct(spec["paths"], "paths", _path),
            types=_distinct(spec["types"], "types", _type) if "types" in spec else None,
            repository=_repository(spec["repository"]) if "repository" in spec else None,
            flags=_distinct(spec["flags"], "flags", _flag) if "flags" in spec else (),
        )

    def matches(self, item: Mapping[str, Any]) -> bool:
        target = item.get("target") or {}
        wanted = {path_key(path) for path in self.paths}
        return (
            (self.types is None or item.get("type") in self.types)
            and (self.repository is None or str(target.get("repository", "")).casefold() == self.repository)
            and any(path_key(path) in wanted for path in target_paths(item))
            and set(self.flags) <= set(item.get("linked_flags") or [])
        )

    def label(self) -> str:
        types = "|".join(self.types) if self.types else "any"
        flags = f" flags={','.join(self.flags)}" if self.flags else ""
        return f"{types} {self.repository or '*'}:{'|'.join(self.paths)}{flags}"


def recommends_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "repository", "paths", "types"}, frozenset({"flags"}))
    wanted = RecommendationMatch.read(spec)

    def check(record: Record) -> str | None:
        found = synthesized(record)
        if any(wanted.matches(item) for item in found):
            return None
        recorded = "; ".join(map(describe_recommendation, found)) or "none"
        return f"no synthesized recommendation does; it recorded {recorded}"

    return Expectation(f"recommends {wanted.label()}", check)


def never_recommends_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "paths"}, frozenset({"repository", "types"}))
    unwanted = RecommendationMatch.read(spec)

    def check(record: Record) -> str | None:
        found = [describe_recommendation(item) for item in synthesized(record) if unwanted.matches(item)]
        return f"recorded {'; '.join(found)}" if found else None

    return Expectation(f"never_recommends {unwanted.label()}", check)


def never_titled_expectation(spec: Mapping[str, object]) -> Expectation:
    _fields(spec, {"kind", "text"})
    text = spec["text"]
    if not isinstance(text, str) or not text.strip() or text != text.strip() or not text.isprintable():
        raise ScenarioError(f"text must be one line of text, not {quote(str(text))}")

    def check(record: Record) -> str | None:
        titled = [
            f"{item.get('id')} is titled {quote(str(item.get('title')))}"
            for item in synthesized(record)
            if text.casefold() in str(item.get("title", "")).casefold()
        ]
        return "; ".join(titled) or None

    return Expectation(f"never_titled {quote(text)}", check)


SYNTHESIS_KINDS: dict[str, Callable[[Mapping[str, Any]], Expectation]] = {
    "recommends": recommends_expectation,
    "never_recommends": never_recommends_expectation,
    "never_titled": never_titled_expectation,
}


def expectation(spec: object, mode: str, by_name: str | None = None) -> Expectation:
    """An expectation of a scenario of this mode; `by_name` is a by-name document scenario's document's file name."""
    if not isinstance(spec, dict):
        raise ScenarioError("each expectation must be an object")
    kind = spec.get("kind")
    if not isinstance(kind, str) or (kind not in KINDS and kind not in SYNTHESIS_KINDS and kind != RESOLVED):
        raise ScenarioError(f"unknown expectation kind {quote(str(kind))}")
    if kind == RESOLVED:
        if by_name is None:
            raise ScenarioError(f"a {RESOLVED} expectation needs a by_name document scenario")
        return resolved_expectation(spec, by_name)
    if (kind in SYNTHESIS_KINDS) != (mode == SYNTHESIS):
        raise ScenarioError(
            f"a {kind} expectation needs a {SYNTHESIS if kind in SYNTHESIS_KINDS else 'review'} scenario"
        )
    if kind in RE_REVIEW_KINDS and mode != "re-review":
        raise ScenarioError(f"a {kind} expectation needs a re-review")
    return SYNTHESIS_KINDS[kind](spec) if mode == SYNTHESIS else KINDS[kind](spec)


def _document(directory: Path, value: object, what: str) -> Path:
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
    root = resolved_path(directory.parents[1])
    path = resolved_path(directory / value)
    if not within(path, root):
        raise ScenarioError(f"{what} {value} is outside the scenarios under {root}")
    _require(path, f"{what} {value} is missing", f"{what} {value}")
    return path


def _require(path: Path, missing: str, what: str, *, folder: bool = False) -> None:
    """Raise ScenarioError `missing` when nothing is at `path`, and `<what> <reason>` when something other than a
    file, or with `folder` a folder, is there, or a stat cannot examine it, so neither is reported as missing."""
    file_presence.require(path, ScenarioError, missing, what, folder=folder)


def _fixture(directory: Path) -> None:
    for tree in ("base", "head"):
        _require(directory / tree, f"has no {tree}/ tree", f"its {tree}/ tree", folder=True)
    _require(directory / "pull.json", "has no pull.json", "its pull.json")


def _fixture_in_place(directory: Path) -> None:
    """Check a fixture reviewed in place, which holds no held file, since only a reviewer's copy restores one."""
    _fixture(directory)
    for tree, relative in held_files(directory):
        raise ScenarioError(f"its {tree}/ tree holds {relative}, which only a repository reviewer's copy restores")


def _raise(error: OSError) -> None:
    raise error


def _files_below(folder: Path) -> list[Path]:
    """Every entry below `folder` that is not a folder, sorted, or none when nothing is at `folder`. Raise ScenarioError
    for a folder it cannot examine or list, or a file in its place: os.walk raises there, where rglob would match
    nothing."""
    try:
        if not file_presence.is_directory(folder):
            return []
        return sorted(Path(base, name) for base, _, names in os.walk(folder, onerror=_raise) for name in names)
    except OSError as exc:
        raise ScenarioError(f"cannot read {folder}: {exc}") from exc


def _occupied(path: Path) -> bool:
    """Whether anything is at `path`; one it cannot examine raises ScenarioError, never reads as nothing there."""
    try:
        return file_presence.is_occupied(path)
    except OSError as exc:
        raise ScenarioError(f"cannot examine {path}: {exc}") from exc


def _reviewer_folder(checkout: Path, folder: PurePosixPath) -> list[str]:
    """Every file a run places from the reviewer's folders, by its path in the checkout: all of references/, and all
    of scripts/ but its regression suites and Python's caches. The suites test the reviewer, which no review runs,
    and they name the scenarios' own files, which would show a condition script that looks for a change's tests one
    that is not there."""
    files = []
    for name in REVIEWER_FOLDERS:
        for path in _files_below(checkout / folder / name):
            relative = PurePosixPath(path.relative_to(checkout).as_posix())
            if "__pycache__" not in relative.parts and not is_suite_name(relative.name):
                files.append(str(relative))
    return files


def _manifest_path(directory: Path) -> str | None:
    """The reviewer manifest a fixture's pull.json names, or None for the generic reviewer."""
    try:
        manifest_path = json_reader.read_file(directory / "pull.json").get("manifest_path")
    except (OSError, ValueError, AttributeError) as exc:
        raise ScenarioError(f"cannot read pull.json's manifest_path: {exc}") from exc
    return None if manifest_path is None else _path(manifest_path)


def _manifest(tree: Path, manifest_path: str, what: str) -> dict[str, Any]:
    try:
        return validate_adapter_manifest(json_reader.read_file(tree / manifest_path))
    except (OSError, ValueError, RuntimeContractError) as exc:
        raise ScenarioError(f"{what} {manifest_path} cannot be read: {exc}") from exc


def scenario_manifest(spec: Mapping[str, object], directory: Path, checkout: Path) -> dict[str, Any] | None:
    """The manifest the scenario's reviewers follow, or None for the generic reviewer alone: a document scenario's is
    review-document's design manifest, a repository reviewer's the checkout's manifest, and any other fixture's the one
    its pull.json names, which prepare reads from the base tree."""
    if "document" in spec:
        return specialists_manifest(PurePosixPath(str(spec["document"])).name)
    manifest_path = _manifest_path(directory)
    if manifest_path is None:
        return None
    tree = checkout if "reviewer" in spec else directory / "base"
    return _manifest(tree, manifest_path, "the manifest")


def finding_categories(spec: Mapping[str, object], directory: Path, checkout: Path) -> list[str] | None:
    """The finding categories of the manifest the scenario's reviewers follow, or None when it lists none."""
    manifest = scenario_manifest(spec, directory, checkout)
    categories = None if manifest is None else manifest.get("finding_categories")
    return None if categories is None else list(categories)


def finding_sources(spec: Mapping[str, object], directory: Path, checkout: Path) -> list[str]:
    """The reviewers that may raise a finding in the scenario: the generic reviewer and each specialist of the manifest
    the scenario's reviewers follow."""
    manifest = scenario_manifest(spec, directory, checkout) or {}
    return [GENERIC_SPECIALIST, *(specialist["id"] for specialist in manifest.get("specialists", []))]


def route_pins(spec: Mapping[str, object], directory: Path, checkout: Path) -> tuple[RoutePin, ...]:
    """Each route of the manifest the scenario's reviewers follow that pins a model or an effort, in the manifest's
    order. A pinned `inherit` asks for the model the run judges, so it pins no model. Raise ScenarioError for a pinned
    model MODELS names no ID for, since no run could show its route's reviewer ran on it."""
    pins = []
    for specialist in (scenario_manifest(spec, directory, checkout) or {}).get("specialists", []):
        model = specialist.get("model")
        model = None if model == "inherit" else model
        effort = specialist.get("effort")
        if model is not None and model not in MODELS:
            raise ScenarioError(
                f"route {specialist['id']} pins model {model}, which the evaluation names no model ID for: "
                f"{', '.join(MODELS)}"
            )
        if model is not None or effort is not None:
            pins.append(RoutePin(str(specialist["id"]), model, effort))
    return tuple(pins)


def _listed_sources(expectations: Sequence[Mapping[str, Any]], sources: Sequence[str]) -> None:
    """Refuse an expectation's source no reviewer of the scenario is, which no run could ever pass."""
    for item in expectations:
        if "source" in item and item["source"].strip() not in sources:
            raise ScenarioError(
                f"source {quote(item['source'])} is not a reviewer of the scenario: {', '.join(sources)}"
            )


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


def _clean_verdict(expectations: Sequence[Mapping[str, Any]], mode: str) -> None:
    """Refuse an initial review's scenario that expects no finding and names no verdict, which an INCOMPLETE review
    that raised nothing would pass. A `no_finding_above` that names its source fails such a review itself."""
    kinds = {item["kind"] for item in expectations}
    scoped = any(item["kind"] == "no_finding_above" and "source" in item for item in expectations)
    if mode == "initial" and "finding" not in kinds and "verdict" not in kinds and not scoped:
        raise ScenarioError(
            "a scenario that expects no finding names its verdict, "
            "or an INCOMPLETE review that raised nothing passes it"
        )


def reviewer_files(reviewer: str, directory: Path, checkout: Path) -> tuple[str, ...]:
    """The files a run of a repository reviewer's scenario places from the checkout: the reviewer's SKILL.md, its
    folders, its manifest, at the fixture's pull.json manifest_path, and every file that manifest declares (its
    resources, its specialists' profiles and resources, and its condition scripts). A condition's `reads` are not
    among them: they name what the script reads of the reviewed tree, the fixture's own. Raise ScenarioError when the
    reviewer or one of its files is missing from the checkout, or the fixture already holds a file the run places."""
    folder = REPOSITORY_SKILLS / reviewer
    _require(
        checkout / folder / "SKILL.md",
        f"no repository skill {reviewer}: the checkout has no {folder}/SKILL.md",
        f"the checkout's {folder}/SKILL.md",
    )
    manifest_path = _manifest_path(directory)
    if manifest_path is None or not within(PurePosixPath(manifest_path), folder):
        raise ScenarioError(f"pull.json's manifest_path must name {reviewer}'s manifest under {folder}/")
    manifest = _manifest(checkout, manifest_path, f"{reviewer}'s manifest")
    own = [str(folder / "SKILL.md"), *_reviewer_folder(checkout, folder), manifest_path]
    files = tuple(dict.fromkeys([*own, *declared_reviewer_files(manifest)]))
    for relative in files:
        _require(
            checkout / relative,
            f"{reviewer} declares {relative}, which the checkout lacks",
            f"{reviewer} declares {relative}, and the checkout's {relative}",
        )
        for tree in ("base", "head"):
            if _occupied(directory / tree / relative):
                raise ScenarioError(f"its {tree}/ tree holds {relative}, which the run places from the checkout")
    return files


def held_files(directory: Path) -> list[tuple[str, str]]:
    """Each file of a fixture's trees held under its name with HELD appended, as its tree and its path there."""
    return [
        (tree, path.relative_to(directory / tree).as_posix())
        for tree in ("base", "head")
        for path in _files_below(directory / tree)
        if fnmatch.fnmatch(path.name, f"*{HELD}")
    ]


def _held(directory: Path, placed: Sequence[str]) -> tuple[str, ...]:
    """A repository reviewer's fixture's held files, each by its path in the fixture. Raise ScenarioError when one is
    named HELD alone, sits beside the file it is restored to, or is restored to a file the run places."""
    found = []
    for tree, relative in held_files(directory):
        restored = relative.removesuffix(HELD)
        if PurePosixPath(relative).name == HELD:
            raise ScenarioError(f"its {tree}/ tree holds {relative}, which names no file to restore")
        if _occupied(directory / tree / restored):
            raise ScenarioError(f"its {tree}/ tree holds both {restored} and {relative}")
        if restored in placed:
            raise ScenarioError(f"its {tree}/ tree holds {relative}, which the run places from the checkout")
        found.append(f"{tree}/{relative}")
    return tuple(found)


def _reviewer(
    spec: Mapping[str, object], skill: str, directory: Path, checkout: Path
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """A repository reviewer's scenario's reviewer, the files a run places, and the fixture's held files: the scenario
    sits under the reviewer's name, and the skill it names is the one that runs the reviewer on a fixture."""
    if spec["reviewer"] != skill:
        raise ScenarioError(f"scenario.json names reviewer {quote(str(spec['reviewer']))}, not {skill}")
    if spec["skill"] != REVIEWER_SKILL:
        raise ScenarioError(f"a reviewer's scenario is started by {REVIEWER_SKILL}, not {quote(str(spec['skill']))}")
    if "document" in spec:
        raise ScenarioError("a reviewer's scenario is a fixture scenario, with no document")
    _fixture(directory)
    placed = reviewer_files(skill, directory, checkout)
    return skill, placed, _held(directory, placed)


def _requests(value: object, what: str) -> tuple[str, ...]:
    """A trigger scenario's list of requests, each one line of text a user might type, given once."""
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ScenarioError(f"{what} must be a non-empty list of requests")
    for item in value:
        if not item.strip() or item != item.strip() or not item.isprintable():
            raise ScenarioError(f"each of {what} must be one line of text, not {quote(item)}")
        if item.startswith(("-", "/")):
            raise ScenarioError(
                f"{quote(item)} starts with {item[0]}, which Claude Code reads as an option or a command"
            )
    if len(set(value)) != len(value):
        raise ScenarioError(f"{what} name a request twice")
    return tuple(value)


def skills_started(record: Record, request: str) -> list[str]:
    """The skills a trigger record shows the session of this request starting."""
    entry = next((item for item in record["requests"] if item["request"] == request), None)
    if entry is None:
        raise RecordError(f"the record has no session of {quote(request)}")
    return list(entry["started"])


def trigger_expectation(skill: str, request: str, starts: bool) -> Expectation:
    """That the session given only this request starts the skill, or, for a near miss, does not."""

    def check(record: Record) -> str | None:
        found = skills_started(record, request)
        return None if (skill in found) == starts else f"the session started {', '.join(found) or 'no skill'}"

    return Expectation(f"{'starts' if starts else 'does_not_start'} {quote(request)}", check)


def load_trigger(skill: str, directory: Path, spec: Mapping[str, object], checkout: Path) -> Scenario:
    """A trigger scenario: requests that should start a shipped, model-invocable skill and near misses that should
    not, its neighbours' requests and then requests no skill serves, one expectation each."""
    fields = ("mode", "near_misses", "requests", "skill", "unserved")
    if sorted(spec) != list(fields):
        raise ScenarioError(f"a trigger scenario.json must have exactly {', '.join(fields)}")
    if spec["skill"] != skill:
        raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
    skill_file = checkout / SHIPPED_SKILLS / skill / "SKILL.md"
    _require(
        skill_file,
        f"a trigger scenario's skill is a shipped one, and the checkout has no {SHIPPED_SKILLS}/{skill}",
        f"the checkout's {SHIPPED_SKILLS}/{skill}/SKILL.md",
    )
    if runtime_canary.starts_only_by_user(skill_file):
        raise ScenarioError(f"{skill} sets disable-model-invocation, so no request can start it")
    requests, neighbours = _requests(spec["requests"], "requests"), _requests(spec["near_misses"], "near_misses")
    near_misses = (*neighbours, *_requests(spec["unserved"], "unserved"))
    both = [item for item in requests if item in near_misses]
    if both:
        raise ScenarioError(f"{quote(both[0])} is both a request and a near miss")
    if len(set(near_misses)) != len(near_misses):
        raise ScenarioError("near_misses and unserved name a request twice")
    expectations = [trigger_expectation(skill, item, True) for item in requests]
    expectations += [trigger_expectation(skill, item, False) for item in near_misses]
    return Scenario(
        skill=skill,
        name=directory.name,
        directory=directory,
        mode=TRIGGER,
        prior=None,
        expectations=tuple(expectations),
        requests=requests,
        near_misses=near_misses,
    )


def _date(value: object, what: str) -> date:
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.isoformat() != value:
        raise ScenarioError(f"{what} must be an ISO date such as 2026-01-31, not {quote(str(value))}")
    return parsed


def open_flags(store: Path) -> set[str]:
    """The IDs of the open flags in a synthesis fixture's flag store, read by ejryan89-code-review-core's reader."""
    try:
        return {flag["id"] for flag in load_store(store)["flags"] if flag["status"] == "open"}
    except (OSError, ValueError, FlagError, PersistenceError) as exc:
        raise ScenarioError(f"its {FLAGS} cannot be read: {exc}") from exc


def load_synthesis(skill: str, directory: Path, spec: Mapping[str, object]) -> Scenario:
    """A synthesis scenario: review-insights started on a date range and one repository over the fixture's archive,
    flag store, and checkout, and the expectations its recorded synthesis is judged against. A scenario that expects
    no recommendation is refused, since a synthesis that recommends nothing would pass it, and so is one whose
    recommends expectation names a flag the fixture's store holds no open flag of, which no run could pass."""
    fields = ("end", "expectations", "mode", "repository", "skill", "start")
    if sorted(spec) != list(fields):
        raise ScenarioError(f"a synthesis scenario.json must have exactly {', '.join(fields)}")
    if spec["skill"] != skill:
        raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
    if skill != SYNTHESIS_SKILL:
        raise ScenarioError(f"a synthesis scenario judges {SYNTHESIS_SKILL}, not {skill}")
    start, end = _date(spec["start"], "start"), _date(spec["end"], "end")
    if end < start:
        raise ScenarioError(f"end {end} is before start {start}")
    repository = _repository(spec["repository"])
    for folder in SYNTHESIS_FOLDERS:
        _require(directory / folder, f"has no {folder}/", f"its {folder}/", folder=True)
    _require(directory / FLAGS, f"has no {FLAGS}", f"its {FLAGS}")
    items = spec["expectations"]
    if not isinstance(items, list) or not items:
        raise ScenarioError("expectations must be a non-empty list")
    expectations = tuple(expectation(item, SYNTHESIS) for item in items)
    if not any(item["kind"] == "recommends" for item in items):
        raise ScenarioError(
            "a synthesis scenario expects a recommendation, or a synthesis that recommends nothing passes"
        )
    held = open_flags(directory / FLAGS)
    for item in items:
        for flag in item.get("flags", []) if item["kind"] == "recommends" else []:
            if flag not in held:
                raise ScenarioError(f"flag {flag} is no open flag of its {FLAGS}")
    return Scenario(
        skill=skill,
        name=directory.name,
        directory=directory,
        mode=SYNTHESIS,
        prior=None,
        expectations=expectations,
        period=(start.isoformat(), end.isoformat()),
        repository=repository,
    )


def load_scenario(skill: str, directory: Path, checkout: Path = REPOSITORY_ROOT) -> Scenario:
    try:
        spec = json_reader.read_file(directory / "scenario.json")
    except (OSError, ValueError) as exc:
        raise ScenarioError(f"{directory.name}: cannot read scenario.json: {exc}") from exc
    try:
        if not isinstance(spec, dict):
            raise ScenarioError("scenario.json must hold an object")
        mode = _choice(spec.get("mode"), MODES, "mode")
        if mode == TRIGGER:
            return load_trigger(skill, directory, spec, checkout)
        if mode == SYNTHESIS:
            return load_synthesis(skill, directory, spec)
        return review_scenario(skill, directory, spec, mode, checkout)
    except ScenarioError as exc:
        raise ScenarioError(f"{directory.name}: {exc}") from exc


def _by_name(spec: Mapping[str, object]) -> bool:
    """Whether a document scenario starts the skill with its document's file name alone: `by_name` is true or left
    out, and a by-name document stands alone, since a base needs a checkout of its own, which `resolve` passes over."""
    if "by_name" not in spec:
        return False
    if spec["by_name"] is not True:
        raise ScenarioError(f"by_name must be true; leave it out for a path, not {quote(str(spec['by_name']))}")
    if "base" in spec:
        raise ScenarioError("a by_name document has no base: resolve passes over the checkout a base is committed in")
    return True


def review_scenario(skill: str, directory: Path, spec: Mapping[str, object], mode: str, checkout: Path) -> Scenario:
    """A scenario of a review: a fixture, a document, or a repository reviewer's fixture, and its expectations."""
    allowed = SCENARIO_FIELDS | ({"prior"} if mode == "re-review" else set())
    if "document" in spec:
        allowed |= {"document"} | ({"base"} if "base" in spec else set()) | ({"stage"} if "stage" in spec else set())
        allowed |= {"by_name"} if "by_name" in spec else set()
    if "reviewer" in spec:
        allowed |= {"reviewer"}
    if set(spec) != allowed:
        raise ScenarioError(f"scenario.json must have exactly {', '.join(sorted(allowed))}")
    reviewer: str | None = None
    placed: tuple[str, ...] = ()
    held: tuple[str, ...] = ()
    if "reviewer" in spec:
        reviewer, placed, held = _reviewer(spec, skill, directory, checkout)
    elif spec["skill"] != skill:
        raise ScenarioError(f"scenario.json names skill {quote(str(spec['skill']))}, not {skill}")
    document: Path | None = None
    base: Path | None = None
    stage: str | None = None
    by_name = False
    if "document" in spec:
        if mode != "initial":
            raise ScenarioError("a document scenario is an initial review: its mode is initial")
        document = _document(directory, spec["document"], "document")
        base = _document(directory, spec["base"], "base") if "base" in spec else None
        stage = _choice(spec["stage"], tuple(DOCUMENT_STAGES), "stage") if "stage" in spec else None
        by_name = _by_name(spec)
    elif reviewer is None:
        _fixture_in_place(directory)  # a reviewer's scenario had its fixture checked in _reviewer
    prior = None
    if mode == "re-review":
        prior = directory / _path(spec["prior"])
        _require(prior, f"prior record {spec['prior']} is missing", f"prior record {spec['prior']}")
    if not isinstance(spec["expectations"], list) or not spec["expectations"]:
        raise ScenarioError("expectations must be a non-empty list")
    named = document.name if by_name and document is not None else None
    expectations = tuple(expectation(item, mode, named) for item in spec["expectations"])
    _clean_verdict(spec["expectations"], mode)
    _listed_categories(spec["expectations"], finding_categories(spec, directory, checkout))
    _listed_sources(spec["expectations"], finding_sources(spec, directory, checkout))
    return Scenario(
        skill=str(spec["skill"]),
        name=directory.name,
        directory=directory,
        mode=mode,
        prior=prior,
        expectations=expectations,
        document=document,
        base=base,
        reviewer=reviewer,
        checkout=checkout if reviewer else None,
        placed=placed,
        stage=stage,
        by_name=by_name,
        held=held,
        pins=route_pins(spec, directory, checkout),
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
        # Hooks read from the null device, which holds none, so neither the user's nor a template's run.
        isolated = ["-c", f"core.hooksPath={NO_HOOKS}"]
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
    so the session never finds the copied skill as one of its own, with each held file renamed without HELD and each
    placed file written from the checkout at its path in both trees; the copy is removed when the block ends, however
    it ends. Any other scenario's is its own folder, untouched."""
    if scenario.reviewer is None or scenario.checkout is None:
        yield scenario.directory
        return
    copy = resolved_path(tempfile.mkdtemp(prefix="skill-evals-fixture-", dir=private_folder.temporary_directory()))
    try:
        fixture = copy / scenario.name
        shutil.copytree(scenario.directory, fixture)
        for relative in scenario.held:
            held = fixture / relative
            held.rename(held.with_name(held.name.removesuffix(HELD)))
        for relative in scenario.placed:
            content = (scenario.checkout / relative).read_bytes()
            for tree in ("base", "head"):
                target = fixture / tree / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        yield fixture
    finally:
        fsops.remove(copy)


@contextlib.contextmanager
def document_place(scenario: Scenario, run: Path) -> Iterator[Path]:
    """The folder a run stages its document scenario's document in, under DOCUMENT_FOLDER. A document with a base is
    staged in the run's folder, since its folder is a git checkout of its own. One without stands outside any
    checkout, and the run's folder is inside the model's home, a git repository of its own, so it is staged in a
    throwaway folder outside the home, removed when the block ends, however it ends. A fixture scenario stages
    nothing, so it gets the run's folder."""
    if scenario.document is None or scenario.base is not None:
        yield run
        return
    place = resolved_path(tempfile.mkdtemp(prefix="skill-evals-document-", dir=private_folder.temporary_directory()))
    try:
        yield place
    finally:
        fsops.remove(place)


def skill_arguments(scenario: Scenario, place: Path, git: GitClient, fixture: Path | None = None) -> list[str]:
    """What the skill is started with: a fixture scenario's prepare arguments, on the fixture given when it is a
    copy, or the path of a document scenario's document, staged in a new folder in `place` (`document_place`), and its
    stage, if it names one."""
    if scenario.document is None:
        return prepare_arguments(scenario, fixture)
    if scenario.by_name:
        raise ScenarioError(f"{scenario.name} starts the skill by its document's name, staged in the model's home")
    return document_arguments(scenario, str(stage_document(scenario, place / DOCUMENT_FOLDER, git)))


def document_arguments(scenario: Scenario, document: str) -> list[str]:
    """What a document scenario starts the skill with: the document, by its staged path or, for a by-name scenario,
    its file name alone, and its stage, if it names one."""
    return [document] if scenario.stage is None else [document, "--stage", scenario.stage]


def load_scenarios(
    skill: str, names: Sequence[str] = (), root: Path = SCENARIO_ROOT, checkout: Path = REPOSITORY_ROOT
) -> list[Scenario]:
    """Each named scenario once in first-occurrence order, or every scenario in name order; a repository reviewer's
    files come from the checkout."""
    folder = root / skill
    try:
        there = file_presence.is_directory(folder)
        # Listed with iterdir, which raises for a folder it cannot list, where glob would match nothing.
        entries = sorted(folder.iterdir()) if there else []
        found = [entry.name for entry in entries if file_presence.is_present(entry / "scenario.json")]
    except OSError as exc:
        raise ScenarioError(f"cannot read the scenarios for {skill} at {folder}: {exc}") from exc
    if not found:
        raise ScenarioError(f"no scenarios for {skill}: each is a <scenario>/scenario.json under {folder}")
    unknown = [name for name in names if name not in found]
    if unknown:
        # A folder whose scenario.json is gone is no scenario, so the refusal says what a scenario holds.
        raise ScenarioError(
            f"no scenario {', '.join(unknown)} for {skill}: each is a <scenario>/scenario.json under {folder}"
        )
    return [load_scenario(skill, folder / name, checkout) for name in dict.fromkeys(names or found)]
