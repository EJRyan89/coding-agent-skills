"""The tables of docs/skill-evaluations.md: each skill's result rows, and its row of the transcript sampled from its
last recorded run, and replacing them."""

from __future__ import annotations

import datetime
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "ejryan89-skill-core" / "scripts"))

from evaluation_results import NOT_RUN, RESULTS_HEADER, cells, results_table, samples_layout, samples_table
from text_lines import LINE_BREAK

from tools.skill_evals_judging import Outcome, counts
from tools.skill_evals_records import JUDGMENT, REVIEWER_EFFORT, Index, RoutePin
from tools.skill_evals_runs import Run, RunKey, run_keys
from tools.skill_evals_scenarios import TRIGGER, Scenario, ScenarioError

# The reviewer effort of a row whose scenarios start no reviewer, only trigger sessions.
NO_REVIEWER = "none"
# The route pins of a row whose scenarios' manifests pin no route's model or effort.
NO_PINS = "none"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def pins_cell(scenarios: Sequence[Scenario]) -> str:
    """The route pins the skill's scenarios measured, each once, by route: a pinned model, which its route's reviewers
    ran on, and a pinned effort, which they started at; or none."""
    pins: set[RoutePin] = {pin for scenario in scenarios for pin in scenario.pins}
    described = []
    for pin in sorted(pins, key=lambda item: (item.route, item.model or "", item.effort or "")):
        parts = [f"model {pin.model}"] if pin.model else []
        parts += [f"effort {pin.effort}"] if pin.effort else []
        described.append(f"{pin.route}: {', '.join(parts)}")
    return "; ".join(described) or NO_PINS


def result_rows(
    skill: str,
    scenarios: Sequence[Scenario],
    models: Sequence[str],
    outcomes: Sequence[Outcome],
    runs: Mapping[RunKey, Run],
    version: str,
    today: datetime.date,
) -> list[str]:
    """One row per model: the model IDs the part judged ran on, the effort its reviewers ran at, or none when every
    scenario is a trigger scenario, which starts no reviewer, the route pins its scenarios measured, its expectations
    passed, the runs of each scenario, each scenario's counts, and the run's versions. The runs of each scenario are
    those of each scenario not known to vary, or of every scenario when each is, and a scenario known to vary, which
    runs VARYING_RUNS times at least, says so in its counts; a model none of whose judged
    part ran is recorded as not run, with its first run's reason as the results file records it, which names no path
    of the machine. A run none of whose reviewers ran on a model, such as one the session refused before starting
    any, holds no judgment of that model, so its expectations are left out of the model's count, but one judged on the
    session's own tool calls alone, a by-name run's `resolved`, which counts; a scenario none of whose runs holds a
    counted expectation says `not run`, and a model that ran no reviewer but has one names `none` as its model IDs."""
    rows = []
    steady = {scenario.name for scenario in scenarios if not scenario.varies}
    numbers = {key[2] for key in runs if key[0] in steady} or {key[2] for key in runs}
    reviewed = any(scenario.mode != TRIGGER for scenario in scenarios)
    pins = pins_cell(scenarios)
    for model in models:
        mine = [(key, runs[key]) for key in run_keys(scenarios, [model], runs)]
        reviewers = sorted({name for _, run in mine for name in run.reviewers})
        session = ", ".join(sorted({name for _, run in mine for name in run.session})) or "unknown"
        theirs = [outcome for outcome in outcomes if outcome.model == model]
        unreviewed = {(key[0], key[2]) for key, run in mine if not run.reviewers}
        counted = [
            outcome
            for outcome in theirs
            if (outcome.scenario, outcome.run) not in unreviewed or outcome.from_transcript
        ]
        if reviewers or counted:
            theirs = counted
            identifier = ", ".join(reviewers) or "none"
            detail = ", ".join(
                f"{scenario.name} {counts(judged, scenario.varies)}" if judged else f"{scenario.name} {NOT_RUN}"
                for scenario in scenarios
                for judged in [[outcome for outcome in theirs if outcome.scenario == scenario.name]]
            )
        else:
            identifier = NOT_RUN
            detail = next((run.recorded for _, run in mine if run.recorded), "no run")
        passed = f"{sum(1 for outcome in theirs if outcome.failure is None)}/{len(theirs)}"
        effort = REVIEWER_EFFORT[model] if reviewed else NO_REVIEWER
        cells = [skill, model, identifier, effort, pins, passed, str(len(numbers)), detail, session, version]
        cells.append(today.isoformat())
        rows.append(f"| {' | '.join(_cell(cell) for cell in cells)} |")
    return rows


def _row_skill(row: str) -> str:
    """The skill a row of a table results_table read names."""
    return (cells(row) or [""])[0]


def _row_model(row: str) -> str:
    """The model a row of the results table names."""
    return (cells(row) or ["", ""])[1]


def kept_models(text: str, skill: str, models: Sequence[str], outcomes: Sequence[Outcome]) -> list[str]:
    """The models, in run order, whose committed row of the skill a write keeps: each no record of which counted,
    since every outcome of it failed with a run or record kind, and of which the skill has a row. Such a run
    measured nothing of the model, so its new row would replace a measured one with `not run` or a pass count of 0; a
    model the skill has no row of loses nothing, and its new row is written. Raise TableError when results_table
    cannot read its table. A judgment made on the session's calls alone is the session's, on its own model, so it
    measures nothing of the row's model either."""
    committed = {_row_model(row) for row in results_table(text)[2] if _row_skill(row) == skill}
    counted = {
        outcome.model for outcome in outcomes if outcome.kind in (None, JUDGMENT) and not outcome.from_transcript
    }
    return [model for model in models if model in committed and model not in counted]


def replace_results(text: str, skill: str, rows: Sequence[str], keep: Sequence[str] = ()) -> str:
    """The results file with the skill's rows replaced by these, except that each model in keep keeps its committed
    row in place of its new one, and every other skill's rows kept as written, sorted by skill; raise TableError
    when results_table cannot read its table."""
    lines = LINE_BREAK.split(text)
    begin, end, existing = results_table(text)
    kept = [row for row in existing if _row_skill(row) != skill]
    committed = {_row_model(row): row for row in existing if _row_skill(row) == skill}
    mine = [committed[_row_model(row)] if _row_model(row) in keep else row for row in rows]
    table = sorted([*kept, *mine], key=_row_skill)
    return "\n".join([*lines[: begin + 1], *RESULTS_HEADER, *table, *lines[end:]])


def pick_sample(
    skill: str, scenarios: Sequence[Scenario], models: Sequence[str], outcomes: Sequence[Outcome], text: str
) -> tuple[str, str, Index] | None:
    """The run whose transcript is read this time, as its scenario, model, and run: of the runs that passed every
    expectation, the first after the scenario and model the skill's samples row names, in scenario order and then
    model order, wrapping round, so successive recorded runs read each scenario on each model in turn. It is the first
    passing run when the skill has no row, and None when no run passed. Raise TableError when samples_table cannot
    read its table."""
    failed = {(outcome.scenario, outcome.model, outcome.run) for outcome in outcomes if outcome.failure is not None}
    passing: dict[tuple[str, str], Index] = {}
    for outcome in outcomes:
        if (outcome.scenario, outcome.model, outcome.run) not in failed:
            passing.setdefault((outcome.scenario, outcome.model), outcome.run)
    order = [(scenario.name, model) for scenario in scenarios for model in models]
    previous = [(found[1], found[2]) for row in samples_table(text)[2] if (found := cells(row) or [])[0] == skill]
    after = order.index(previous[0]) + 1 if previous and previous[0] in order else 0
    for key in order[after:] + order[:after]:
        if key in passing:
            return (*key, passing[key])
    return None


def replace_sample(text: str, skill: str, sample: tuple[str, str, Index], today: datetime.date) -> str:
    """The results file with the skill's samples row replaced by one naming this run, its transcript unread, and every
    other skill's row kept as written, sorted by skill; raise TableError when samples_table cannot read its
    table."""
    lines = LINE_BREAK.split(text)
    begin, end, existing = samples_table(text)
    scenario, model, index = sample
    written = [skill, scenario, model, str(index or 1), today.isoformat(), "unread"]
    kept = [row for row in existing if _row_skill(row) != skill]
    table = sorted([*kept, f"| {' | '.join(_cell(cell) for cell in written)} |"], key=_row_skill)
    return "\n".join([*lines[: begin + 1], *samples_layout()[1], *table, *lines[end:]])


def read_sample(text: str, skill: str, reading: str) -> str:
    """The results file with the skill's unread samples row marked with the reader's reading: `agrees` when the
    transcript shows what the script's verdict on each expectation says, and `disagrees` otherwise. Raise
    TableError when samples_table cannot read its table, and ScenarioError when the skill has no unread row."""
    lines = LINE_BREAK.split(text)
    begin, _, existing = samples_table(text)
    for number, row in enumerate(existing):
        found = cells(row) or []
        if found[0] != skill:
            continue
        if found[-1] != "unread":
            raise ScenarioError(f"{skill}'s sampled transcript is already read: {found[-1]}")
        lines[begin + 3 + number] = f"| {' | '.join(_cell(cell) for cell in [*found[:-1], reading])} |"
        return "\n".join(lines)
    raise ScenarioError(f"the samples table has no row of {skill}; a run with --write samples one")
