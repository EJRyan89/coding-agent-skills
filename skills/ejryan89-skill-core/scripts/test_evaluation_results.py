"""Regression tests for evaluation_results.py, the one reader of the tables in docs/skill-evaluations.md. Every
test reads text it builds, so it holds on every platform."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evaluation_results
from evaluation_results import TableError, measured_scenarios


def table(*rows: str) -> str:
    return "\n".join(
        [
            "# Skill evaluations",
            evaluation_results.RESULTS_BEGIN,
            *evaluation_results.RESULTS_HEADER,
            *rows,
            evaluation_results.RESULTS_END,
        ]
    )


def test_a_scenario_is_measured_where_a_row_of_its_skill_whose_model_ran_gives_it_counts() -> None:
    # A scenario not run is left out, and so is every scenario of a row whose model ran no judged part, whose By
    # scenario cell holds the reason, here one that opens with a scenario's name; another skill's row counts for it
    # alone.
    text = table(
        "| a | haiku | id | high | none | 3/4 | 1 | one 1/1, two not run, three 2/3 (1/2 runs) | id | 1.0 "
        "| 2026-01-01 |",
        "| a | sonnet | not run | high | none | 0/1 | 1 | two 1/1 was refused | unknown | 1.0 | 2026-01-01 |",
        "| b | haiku | id | high | none | 1/1 | 1 | four 1/1 | id | 1.0 | 2026-01-01 |",
    )
    assert measured_scenarios(text, "a") == {"one", "three"}
    assert measured_scenarios(text, "b") == {"four"}
    assert measured_scenarios(text, "c") == set()


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (
            table("| a | haiku | id | one 1/1 |"),
            "line 5 of the results file is not a row of the table's 11 columns naming a skill: \"| a | haiku | id "
            '| one 1/1 |"',
        ),
        (
            "# Skill evaluations\n",
            "the results file needs one <!-- skill-evals:begin --> line and one <!-- skill-evals:end --> line",
        ),
        (
            "\n".join([evaluation_results.RESULTS_END, evaluation_results.RESULTS_BEGIN]),
            "<!-- skill-evals:end --> comes before <!-- skill-evals:begin --> in the results file",
        ),
        (
            "\n".join([evaluation_results.RESULTS_BEGIN, "| Skill |", evaluation_results.RESULTS_END]),
            f"the results table must start with the header {evaluation_results.RESULTS_HEADER[0]}",
        ),
        (
            "\n".join(
                [evaluation_results.RESULTS_BEGIN, evaluation_results.RESULTS_HEADER[0], evaluation_results.RESULTS_END]
            ),
            f"the results table's header must be followed by its separator {evaluation_results.RESULTS_HEADER[1]}",
        ),
    ],
)
def test_a_table_not_as_written_is_refused_never_read_as_nothing_measured(text: str, reason: str) -> None:
    with pytest.raises(TableError, match=f"^{re.escape(reason)}$"):
        measured_scenarios(text, "a")


def test_a_cell_holding_an_escaped_pipe_is_one_cell_and_a_line_without_borders_is_no_row() -> None:
    assert evaluation_results.cells("| a \\| b | c |") == ["a \\| b", "c"]
    assert evaluation_results.cells("a | b") is None
