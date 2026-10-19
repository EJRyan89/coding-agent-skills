"""The results table of docs/skill-evaluations.md: each skill's rows, and replacing them."""

from __future__ import annotations

import datetime
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.skill_evals_judging import Outcome, counts
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
