"""Judge a skill's runs of fixed scenarios, model by model, against the review record each run wrote.

Usage:
  python -B tools/skill_evals.py SKILL --records DIR [--scenario NAME ...] [--model NAME ...]

A scenario is a directory tests/fixtures/skill-evals/<skill>/<scenario>/, which never ships. It holds the change as
two trees, base/ and head/, and a scenario.json:

  {"skill": "<skill>", "mode": "initial" | "re-review", "title": "<pull request title>", "body": "<its body>",
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
      every finding this review raised at P on those lines repeats that entry, so the problem counts once; raising
      none there also passes, since the ledger then carries the entry alone

An unknown kind, or a field a kind does not take, fails the whole run before anything is judged.

Records come from a record source, a function of a scenario and a model that returns the record that run wrote.
--records DIR reads DIR/<scenario>/<model>.json, the record JSON a run finalized. A missing or unreadable record fails
every expectation of its scenario and model.

Output, one fact per line:
  PASS <skill> <scenario> <model> <expectation>
  FAIL <skill> <scenario> <model> <expectation> "<reason>"
  FAILED "<reason>"                               the scenarios could not be loaded; nothing was judged
then a Markdown table, one row per scenario and one column per model, each cell the expectations passed of those
checked. It exits 1 on any FAIL or FAILED line, 0 otherwise, and 2 on a usage error.

It records pass or fail and nothing else: no tokens, cost, or timings.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from deployer import platform_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "skill-evals"
# The reviewer subagent models a run is judged on, as the Agent tool names them. The one place they are named.
MODELS = ("haiku", "sonnet", "opus")
SEVERITIES = ("SUGGESTION", "SHOULD_FIX", "MUST_FIX")
VERDICTS = ("APPROVED", "CHANGES_REQUESTED", "INCOMPLETE")
DISPOSITIONS = ("addressed", "partially_addressed", "still_present", "superseded", "unable_to_verify")
MODES = ("initial", "re-review")
SCENARIO_FIELDS = frozenset({"skill", "mode", "title", "body", "expectations"})
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
    title: str
    body: str
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
        unlinked = [
            f"{finding.get('id', 'a finding')} at line {finding['line']}"
            for finding in findings(record)
            if finding["path"] == path
            and finding["line"] in lines
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
        if any(not isinstance(spec[name], str) or not spec[name].strip() for name in ("title", "body")):
            raise ScenarioError("title and body must be text")
        for tree in ("base", "head"):
            if not (directory / tree).is_dir():
                raise ScenarioError(f"has no {tree}/ tree")
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
    return Scenario(skill, directory.name, directory, mode, spec["title"], spec["body"], prior, expectations)


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


def judgments(record: Record) -> dict[tuple[int, str], str]:
    """This review's disposition of each ledger entry it judged, keyed by the entry's version and ID."""
    version = review(record)["version"]
    ledger = record.get("ledger")
    if not isinstance(ledger, list):
        raise RecordError("the record has no ledger")
    judged: dict[tuple[int, str], str] = {}
    for entry in ledger:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("version"), int)
            or not isinstance(entry.get("id"), str)
            or not isinstance(entry.get("dispositions"), list)
            or any(not isinstance(item, dict) for item in entry["dispositions"])
        ):
            raise RecordError("the record's ledger is malformed")
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


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Judge a skill's scenario runs against their review records.")
    result.add_argument("skill", help="the skill whose scenarios to judge, a folder under tests/fixtures/skill-evals")
    result.add_argument("--records", type=Path, required=True, help="a folder of <scenario>/<model>.json records")
    result.add_argument("--scenario", action="append", default=[], help="judge only this scenario (repeatable)")
    result.add_argument("--model", action="append", choices=MODELS, help="judge only this model (repeatable)")
    return result


def main(argv: Sequence[str], root: Path = SCENARIO_ROOT) -> int:
    options = parser().parse_args(argv)
    try:
        scenarios = load_scenarios(options.skill, options.scenario, root)
    except ScenarioError as exc:
        print(f"FAILED {quote(str(exc))}")
        return 1
    models = list(dict.fromkeys(options.model or MODELS))
    outcomes = judge(scenarios, models, records_in(options.records))
    for outcome in outcomes:
        print(outcome_line(options.skill, outcome))
    for line in table(scenarios, models, outcomes):
        print(line)
    return 1 if any(outcome.failure is not None for outcome in outcomes) else 0


if __name__ == "__main__":
    platform_support.use_utf8_output()
    sys.exit(main(sys.argv[1:]))
