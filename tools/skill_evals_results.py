"""The tables of docs/skill-evaluations.md: each skill's result rows, and its row of the transcript sampled from its
last recorded run, and replacing them."""

from __future__ import annotations

import datetime
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.skill_evals_judging import Outcome, counts
from tools.skill_evals_records import Index
from tools.skill_evals_runs import Run, RunKey, run_keys
from tools.skill_evals_scenarios import Scenario, ScenarioError, quote

RESULTS_BEGIN = "<!-- skill-evals:begin -->"
RESULTS_END = "<!-- skill-evals:end -->"
RESULTS_HEADER = (
    "| Skill | Model | Model ID | Passed | Runs | By scenario | Session model | Claude Code | Date |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
)


# A results table's cell border, a pipe its cell does not escape, and a separator cell, aligned or not.
CELL_BORDER = re.compile(r"(?<!\\)\|")
SEPARATOR_CELL = re.compile(r":?-{3,}:?")


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def result_rows(
    skill: str,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    outcomes: Sequence[Outcome],
    runs: Mapping[RunKey, Run],
    version: str,
    today: datetime.date,
) -> list[str]:
    """One row per model: the model IDs the part judged ran on, its expectations passed, the runs of each scenario,
    each scenario's counts, and the run's versions; a model none of whose judged part ran is recorded as not run, with
    its first run's reason as the results file records it, which names no path of the machine. A run none of whose
    reviewers ran on a model, such as one the session refused before starting any, holds no judgment of that model, so
    its expectations are left out of the model's count, and a scenario none of whose runs holds one says `not run`."""
    rows = []
    numbers = {key[2] for key in runs}
    for model in models:
        mine = [(key, runs[key]) for key in run_keys(scenarios, [model], runs)]
        reviewers = sorted({name for _, run in mine for name in run.reviewers})
        session = ", ".join(sorted({name for _, run in mine for name in run.session})) or "unknown"
        theirs = [outcome for outcome in outcomes if outcome.model == model]
        if reviewers:
            unreviewed = {(key[0], key[2]) for key, run in mine if not run.reviewers}
            theirs = [outcome for outcome in theirs if (outcome.scenario, outcome.run) not in unreviewed]
            identifier = ", ".join(reviewers)
            detail = ", ".join(
                f"{scenario.name} {counts(judged)}" if judged else f"{scenario.name} not run"
                for scenario in scenarios
                for judged in [[outcome for outcome in theirs if outcome.scenario == scenario.name]]
            )
        else:
            identifier = "not run"
            detail = next((run.recorded for _, run in mine if run.recorded), "no run")
        passed = f"{sum(1 for outcome in theirs if outcome.failure is None)}/{len(theirs)}"
        cells = [skill, model, identifier, passed, str(len(numbers)), detail, session, version, today.isoformat()]
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


def _table(text: str, markers: tuple[str, str], header_lines: tuple[str, str], what: str) -> tuple[int, int, list[str]]:
    """The line indexes of one table's markers in docs/skill-evaluations.md and its rows between them, each as
    written. Raise ScenarioError when the markers are not there once each and in order, or when anything between them
    is not the header, its separator, and then rows of the header's columns, each naming a skill."""
    lines = text.split("\n")
    opening, closing = markers
    if lines.count(opening) != 1 or lines.count(closing) != 1:
        raise ScenarioError(f"the results file needs one {opening} line and one {closing} line")
    begin, end = lines.index(opening), lines.index(closing)
    if end < begin:
        raise ScenarioError(f"{closing} comes before {opening} in the results file")
    header = _cells(header_lines[0]) or []
    table = lines[begin + 1 : end]
    if not table or _cells(table[0]) != header:
        raise ScenarioError(f"the {what} must start with the header {header_lines[0]}")
    separator = _cells(table[1]) if len(table) > 1 else None
    if separator is None or len(separator) != len(header) or not all(map(SEPARATOR_CELL.fullmatch, separator)):
        raise ScenarioError(f"the {what}'s header must be followed by its separator {header_lines[1]}")
    for number, row in enumerate(table[2:], begin + 4):
        cells = _cells(row)
        if cells is None or len(cells) != len(header) or not cells[0]:
            raise ScenarioError(
                f"line {number} of the results file is not a row of the table's {len(header)} columns naming a "
                f"skill: {quote(row)}"
            )
    return begin, end, table[2:]


def results_table(text: str) -> tuple[int, int, list[str]]:
    """The line indexes of the results markers in docs/skill-evaluations.md and the table's rows between them, each
    as written. Raise ScenarioError when the markers are not there once each and in order, or when anything between
    them is not the header, its separator, and then rows of the header's columns, each naming a skill."""
    return _table(text, (RESULTS_BEGIN, RESULTS_END), RESULTS_HEADER, "results table")


def replace_results(text: str, skill: str, rows: Sequence[str]) -> str:
    """The results file with the skill's rows replaced by these and every other skill's rows kept as written, sorted
    by skill; raise ScenarioError when results_table cannot read its table."""
    lines = text.split("\n")
    begin, end, existing = results_table(text)
    kept = [row for row in existing if _row_skill(row) != skill]
    table = sorted([*kept, *rows], key=_row_skill)
    return "\n".join([*lines[: begin + 1], *RESULTS_HEADER, *table, *lines[end:]])


def samples_layout() -> tuple[tuple[str, str], tuple[str, str]]:
    """The transcript samples table's markers, and its header with its separator."""
    return (
        ("<!-- skill-evals-samples:begin -->", "<!-- skill-evals-samples:end -->"),
        ("| Skill | Scenario | Model | Run | Date | Transcript |", "| --- | --- | --- | --- | --- | --- |"),
    )


def samples_table(text: str) -> tuple[int, int, list[str]]:
    """The line indexes of the samples markers in docs/skill-evaluations.md and the table's rows between them, read
    as results_table reads its own, and refused as it refuses its own."""
    markers, header = samples_layout()
    return _table(text, markers, header, "samples table")


def pick_sample(
    skill: str, scenarios: Sequence[Scenario], models: Sequence[str], outcomes: Sequence[Outcome], text: str
) -> tuple[str, str, Index] | None:
    """The run whose transcript is read this time, as its scenario, model, and run: of the runs that passed every
    expectation, the first after the scenario and model the skill's samples row names, in scenario order and then
    model order, wrapping round, so successive recorded runs read each scenario on each model in turn. It is the first
    passing run when the skill has no row, and None when no run passed. Raise ScenarioError when samples_table cannot
    read its table."""
    failed = {(outcome.scenario, outcome.model, outcome.run) for outcome in outcomes if outcome.failure is not None}
    passing: dict[tuple[str, str], Index] = {}
    for outcome in outcomes:
        if (outcome.scenario, outcome.model, outcome.run) not in failed:
            passing.setdefault((outcome.scenario, outcome.model), outcome.run)
    order = [(scenario.name, model) for scenario in scenarios for model in models]
    previous = [(cells[1], cells[2]) for row in samples_table(text)[2] if (cells := _cells(row) or [])[0] == skill]
    after = order.index(previous[0]) + 1 if previous and previous[0] in order else 0
    for key in order[after:] + order[:after]:
        if key in passing:
            return (*key, passing[key])
    return None


def replace_sample(text: str, skill: str, sample: tuple[str, str, Index], today: datetime.date) -> str:
    """The results file with the skill's samples row replaced by one naming this run, its transcript unread, and every
    other skill's row kept as written, sorted by skill; raise ScenarioError when samples_table cannot read its
    table."""
    lines = text.split("\n")
    begin, end, existing = samples_table(text)
    scenario, model, index = sample
    cells = [skill, scenario, model, str(index or 1), today.isoformat(), "unread"]
    kept = [row for row in existing if _row_skill(row) != skill]
    table = sorted([*kept, f"| {' | '.join(_cell(cell) for cell in cells)} |"], key=_row_skill)
    return "\n".join([*lines[: begin + 1], *samples_layout()[1], *table, *lines[end:]])


def read_sample(text: str, skill: str, reading: str) -> str:
    """The results file with the skill's unread samples row marked with the reader's reading: `agrees` when the
    transcript shows what the script's verdict on each expectation says, and `disagrees` otherwise. Raise
    ScenarioError when samples_table cannot read its table, or the skill has no unread row."""
    lines = text.split("\n")
    begin, _, existing = samples_table(text)
    for number, row in enumerate(existing):
        cells = _cells(row) or []
        if cells[0] != skill:
            continue
        if cells[-1] != "unread":
            raise ScenarioError(f"{skill}'s sampled transcript is already read: {cells[-1]}")
        lines[begin + 3 + number] = f"| {' | '.join(_cell(cell) for cell in [*cells[:-1], reading])} |"
        return "\n".join(lines)
    raise ScenarioError(f"the samples table has no row of {skill}; a run with --write samples one")
