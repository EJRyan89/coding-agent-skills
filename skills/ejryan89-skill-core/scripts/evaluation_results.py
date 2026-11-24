"""The one reader of the tables in docs/skill-evaluations.md: a table between its two marker lines, the results table
whose rows record each skill's evaluation on each model, the transcript samples table, and the scenarios a skill's
rows record as measured.

It reads text and starts nothing: the evaluation runner under tools/, which calls models, writes the tables, and the
lessons ledger in validation, which must never start one, reads them through this module alone.
"""

from __future__ import annotations

import re

import json_writer
from text_lines import LINE_BREAK

RESULTS_BEGIN = "<!-- skill-evals:begin -->"
RESULTS_END = "<!-- skill-evals:end -->"
RESULTS_HEADER = (
    "| Skill | Model | Model ID | Reviewer effort | Route pins | Passed | Runs | By scenario | Session model "
    "| Claude Code | Date |",
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
)
# What a row's Model ID records for a model none of whose judged part ran, and By scenario for a scenario not run.
NOT_RUN = "not run"

# A table's cell border, a pipe its cell does not escape, and a separator cell, aligned or not.
CELL_BORDER = re.compile(r"(?<!\\)\|")
SEPARATOR_CELL = re.compile(r":?-{3,}:?")


class TableError(Exception):
    """A table of docs/skill-evaluations.md that is not as written: its message says how."""


def cells(line: str) -> list[str] | None:
    """A table line's cells, stripped, or None when the line is not one: it starts and ends with a cell border."""
    stripped = line.strip()
    borders = [match.start() for match in CELL_BORDER.finditer(stripped)]
    if len(borders) < 2 or borders[0] != 0 or borders[-1] != len(stripped) - 1:
        return None
    return [cell.strip() for cell in CELL_BORDER.split(stripped[1:-1])]


def read_table(
    text: str, markers: tuple[str, str], header_lines: tuple[str, str], what: str
) -> tuple[int, int, list[str]]:
    """The line indexes of one table's markers in docs/skill-evaluations.md and its rows between them, each as
    written. Raise TableError when the markers are not there once each and in order, or when anything between them is
    not the header, its separator, and then rows of the header's columns, each naming a skill."""
    lines = LINE_BREAK.split(text)
    opening, closing = markers
    if lines.count(opening) != 1 or lines.count(closing) != 1:
        raise TableError(f"the results file needs one {opening} line and one {closing} line")
    begin, end = lines.index(opening), lines.index(closing)
    if end < begin:
        raise TableError(f"{closing} comes before {opening} in the results file")
    header = cells(header_lines[0]) or []
    table = lines[begin + 1 : end]
    if not table or cells(table[0]) != header:
        raise TableError(f"the {what} must start with the header {header_lines[0]}")
    separator = cells(table[1]) if len(table) > 1 else None
    if separator is None or len(separator) != len(header) or not all(map(SEPARATOR_CELL.fullmatch, separator)):
        raise TableError(f"the {what}'s header must be followed by its separator {header_lines[1]}")
    for number, row in enumerate(table[2:], begin + 4):
        found = cells(row)
        if found is None or len(found) != len(header) or not found[0]:
            raise TableError(
                f"line {number} of the results file is not a row of the table's {len(header)} columns naming a "
                f"skill: {json_writer.dumps(row, ensure_ascii=False)}"
            )
    return begin, end, table[2:]


def results_table(text: str) -> tuple[int, int, list[str]]:
    """The line indexes of the results markers in docs/skill-evaluations.md and the table's rows between them, each
    as written. Raise TableError when the markers are not there once each and in order, or when anything between them
    is not the header, its separator, and then rows of the header's columns, each naming a skill."""
    return read_table(text, (RESULTS_BEGIN, RESULTS_END), RESULTS_HEADER, "results table")


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
    return read_table(text, markers, header, "samples table")


def measured_scenarios(text: str, skill: str) -> set[str]:
    """The scenarios of `skill` that a row of the results table in docs/skill-evaluations.md, whose text is `text`,
    records as measured on its model: each By scenario names with counts, never one it records as not run, and none
    of a row whose model ran no judged part, whose By scenario holds the reason instead. Raise TableError when
    results_table cannot read its table, which it refuses for a row that is not one of the header's columns."""
    header = cells(RESULTS_HEADER[0]) or []
    model_id, by_scenario = header.index("Model ID"), header.index("By scenario")
    measured: set[str] = set()
    for row in results_table(text)[2]:
        found = cells(row) or []
        if found[0] != skill or found[model_id] == NOT_RUN:
            continue
        for item in found[by_scenario].split(", "):
            name, _, count = item.partition(" ")
            if count and count != NOT_RUN:
                measured.add(name)
    return measured
