"""Judging every run of every scenario against its expectations, and the lines that report it."""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))

from review_records import validate_record

from tools.skill_evals_records import (
    JUDGMENT,
    MODELS,
    RECORD,
    RUN,
    Index,
    Record,
    RecordError,
    names_model,
    record_name,
    unguarded,
    unreported_model,
)
from tools.skill_evals_runs import RECORD_VERSION, RecordSource, indexes, run_token
from tools.skill_evals_scenarios import TRIGGER, Scenario, quote


@dataclass(frozen=True)
class Outcome:
    scenario: str
    model: str
    label: str
    failure: str | None
    # JUDGMENT, RUN, or RECORD for a failure; None when the expectation passed.
    kind: str | None = None
    run: Index = None


def trigger_record_problem(record: object, scenario: Scenario, model: str) -> str | None:
    """Why a trigger record is not one of this scenario on this model, or None: an object of the session of each of
    the scenario's requests, once each, with the skills it started, and the model IDs the sessions ran on, every one
    the model's."""
    if not isinstance(record, dict) or set(record) != {"requests", "models"}:
        return "it is not a trigger record, an object of requests and models"
    entries, models = record["requests"], record["models"]
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict)
        or set(entry) != {"request", "started"}
        or not isinstance(entry["request"], str)
        or not isinstance(entry["started"], list)
        or any(not isinstance(name, str) for name in entry["started"])
        for entry in entries
    ):
        return "each of its requests must be an object of the request and the skills its session started"
    if sorted(entry["request"] for entry in entries) != sorted([*scenario.requests, *scenario.near_misses]):
        return "its requests are not the scenario's, each once"
    if not isinstance(models, list) or not models or any(not isinstance(name, str) for name in models):
        return "its models must be the model IDs its sessions ran on"
    other = sorted(name for name in models if not names_model(name, MODELS[model]))
    return f"its sessions ran on {', '.join(other)}, not {MODELS[model]}" if other else None


def records_in(directory: Path) -> RecordSource:
    """A record source reading DIR/<scenario>/<model>.json, or DIR/<scenario>/<model>-<run>.json for each of several
    runs, holding each record to what a run's is held to: for a trigger scenario, trigger_record_problem; for any
    other, the record contract, the scenario's review version, a guard that held every reviewer, and each reviewer's
    reported model."""

    def source(scenario: Scenario, model: str, index: Index) -> Record:
        path = directory / scenario.name / record_name(model, index)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RecordError(f"no record at {path}") from exc
        except (OSError, ValueError) as exc:
            raise RecordError(f"cannot read {path}: {exc}") from exc
        if scenario.mode == TRIGGER:
            problem = trigger_record_problem(record, scenario, model)
            if problem:
                raise RecordError(f"{path} counts for nothing: {problem}", RECORD)
            return record
        try:
            validate_record(record)
        except ValueError as exc:
            raise RecordError(f"{path} is not a review record: {exc}", RECORD) from exc
        version, wanted = record["review"]["version"], RECORD_VERSION[scenario.mode]
        reason = (
            f"it is review version {version}, not the {wanted} a {scenario.mode} run records"
            if version != wanted
            else unguarded(record) or unreported_model(record, model)
        )
        if reason:
            raise RecordError(f"{path} counts for nothing: {reason}", RECORD)
        return record

    return source


def counts(outcomes: Sequence[Outcome]) -> str:
    """The expectations passed of those checked, and over several runs, the runs that passed every expectation of
    theirs, of the runs judged: `3/3 (1/1 runs)` reads as every run passing, the pass^k of k runs."""
    passed = f"{sum(1 for outcome in outcomes if outcome.failure is None)}/{len(outcomes)}"
    numbered = {outcome.run for outcome in outcomes if outcome.run is not None}
    if not numbered:
        return passed
    failed = {outcome.run for outcome in outcomes if outcome.failure is not None}
    return f"{passed} ({len(numbered - failed)}/{len(numbered)} runs)"


def judge(scenarios: Sequence[Scenario], models: Sequence[str], source: RecordSource, runs: int = 1) -> list[Outcome]:
    """Every expectation of every run of every scenario on every model. A record the source could not give fails each
    of its expectations with the source's reason and kind; an expectation the record fails is a judgment."""
    outcomes = []
    for scenario in scenarios:
        for model in models:
            for index in indexes(runs):
                try:
                    record: Record | None = source(scenario, model, index)
                    missing, kind = None, None
                except RecordError as exc:
                    record, missing, kind = None, str(exc), exc.kind
                for item in scenario.expectations:
                    failure = missing
                    if record is not None:
                        try:
                            failure = item.check(record)
                        except RecordError as exc:
                            failure = str(exc)
                        kind = None if failure is None else JUDGMENT
                    outcomes.append(Outcome(scenario.name, model, item.label, failure, kind, index))
    return outcomes


def outcome_line(skill: str, outcome: Outcome) -> str:
    fact = f"{skill} {outcome.scenario} {outcome.model}{run_token(outcome.run)} {outcome.label}"
    return f"PASS {fact}" if outcome.failure is None else f"FAIL {fact} {outcome.kind} {quote(outcome.failure)}"


def not_counted_line(scenario: str, model: str, kind: str, reason: str, index: Index = None) -> str:
    return f"NOT_COUNTED {scenario} {model}{run_token(index)} {kind} {quote(reason)}"


def not_counted(outcomes: Sequence[Outcome]) -> list[str]:
    """One line per run of a scenario on a model whose record counts for nothing, with the reason each of its
    expectations failed with, in the order judged."""
    lines = {
        (outcome.scenario, outcome.model, outcome.run): not_counted_line(
            outcome.scenario, outcome.model, outcome.kind, failure, outcome.run
        )
        for outcome in outcomes
        if (failure := outcome.failure) is not None and outcome.kind in (RUN, RECORD)
    }
    return list(lines.values())


def runs_lines(outcomes: Sequence[Outcome]) -> list[str]:
    """Over several runs, one line per scenario and model, in the order judged: the runs that passed every
    expectation of theirs, of the runs, and whether that is all of them, some, or none."""
    passed: dict[tuple[str, str], dict[Index, bool]] = {}
    for outcome in outcomes:
        if outcome.run is not None:
            mine = passed.setdefault((outcome.scenario, outcome.model), {})
            mine[outcome.run] = mine.get(outcome.run, True) and outcome.failure is None
    lines = []
    for (scenario, model), by_run in passed.items():
        count = sum(by_run.values())
        every = "all" if count == len(by_run) else "none" if count == 0 else "some"
        lines.append(f"RUNS {scenario} {model} {count}/{len(by_run)} {every}")
    return lines


def table(scenarios: Sequence[Scenario], models: Sequence[str], outcomes: Sequence[Outcome]) -> list[str]:
    """A Markdown table: one row per scenario, one column per model, each cell its counts."""
    lines = [f"| Scenario | {' | '.join(models)} |", f"| --- |{' --- |' * len(models)}"]
    for scenario in scenarios:
        cells = [
            counts([outcome for outcome in outcomes if outcome.scenario == scenario.name and outcome.model == model])
            for model in models
        ]
        lines.append(f"| {scenario.name} | {' | '.join(cells)} |")
    return lines


def report(skill: str, outcomes: Sequence[Outcome]) -> bool:
    """Print each record that counts for nothing, then every outcome, then over several runs each scenario's runs
    passed on each model; return whether any expectation failed."""
    for line in [
        *not_counted(outcomes),
        *(outcome_line(skill, outcome) for outcome in outcomes),
        *runs_lines(outcomes),
    ]:
        print(line)
    return any(outcome.failure is not None for outcome in outcomes)
