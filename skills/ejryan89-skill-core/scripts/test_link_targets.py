"""Regression tests for the one resolver of a Markdown link's destination to a repository path."""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_destinations import link_destinations
from link_targets import link_spans, link_target

SKILL = ".claude/skills/review/SKILL.md"


@pytest.mark.parametrize(
    ("linking_file", "destination", "expected"),
    [
        # A relative destination is read from the linking file's folder, as GitHub and Claude Code read it.
        (SKILL, "references/rules.md", ".claude/skills/review/references/rules.md"),
        # A leading `./` is that same folder.
        (SKILL, "./references/rules.md", ".claude/skills/review/references/rules.md"),
        (SKILL, "../shared/a.md", ".claude/skills/shared/a.md"),
        (SKILL, "../../../docs/rules.md", "docs/rules.md"),
        (SKILL, "references/./x/../rules.md", ".claude/skills/review/references/rules.md"),
        # A destination that starts with `/` is read from the repository root, never kept as `dir///x.md`.
        ("dir/g.md", "/x.md", "x.md"),
        (SKILL, "/docs/rules.md", "docs/rules.md"),
        (SKILL, "/docs/../x.md", "x.md"),
        # A file at the root links from the root.
        ("README.md", "docs/a.md", "docs/a.md"),
        ("", "docs/a.md", "docs/a.md"),
        # Only a fragment: the linking file itself.
        (SKILL, "", SKILL),
    ],
)
def test_a_destination_resolves_from_the_linking_files_folder_or_the_root(
    linking_file: str, destination: str, expected: str
) -> None:
    assert link_target(linking_file, destination) == expected


@pytest.mark.parametrize(
    ("linking_file", "destination"),
    [
        ("docs/a.md", "../../x.md"),
        ("a.md", "../x.md"),
        ("a.md", "/../x.md"),
        ("docs/a.md", ".."),
        ("docs/a.md", "/"),
        ("", ""),
    ],
)
def test_a_destination_out_of_the_repository_or_at_its_root_leads_to_no_file(
    linking_file: str, destination: str
) -> None:
    assert link_target(linking_file, destination) is None


@pytest.mark.parametrize(
    ("line", "span"),
    [
        # An inline link, from the `[` of its text to its `)`.
        ("See [references/rules.md](references/rules.md) now.", "[references/rules.md](references/rules.md)"),
        ("[a](x.md 'title (b.md)') and c.md", "[a](x.md 'title (b.md)')"),
        ('[a]( <x y.md> "t") then', '[a]( <x y.md> "t")'),
        ("[a](x.md (t)) then", "[a](x.md (t))"),
        # Brackets in the text balance, and an escaped or code-span bracket is no bracket.
        ("x [a [b] c](d.md) y", "[a [b] c](d.md)"),
        ("x [a \\] b](d.md) y", "[a \\] b](d.md)"),
        ("x [a `]` b](d.md) y", "[a `]` b](d.md)"),
        ("x [a [b](d.md) y", "[b](d.md)"),
        # A definition takes its whole line, from its label.
        ("  [r]: docs/a.md 'title'", "[r]: docs/a.md 'title'"),
        # No `[` opens the text: from the `]`.
        ("a](d.md) b", "](d.md)"),
        # A title that never closes, or no `)` after the title: to the end of the line.
        ('[a](x.md "t and docs/b.md', '[a](x.md "t and docs/b.md'),
        ('[a](x.md "t" and docs/b.md', '[a](x.md "t" and docs/b.md'),
    ],
)
def test_a_link_s_span_runs_from_its_text_to_its_end(line: str, span: str) -> None:
    (destination,) = link_destinations([line])
    ((start, end),) = link_spans(line, [destination])
    assert line[start:end] == span


def test_each_link_on_a_line_has_its_own_span() -> None:
    line = "[a](x.md) then [b [c]](y.md 'z') and z](w.md)"
    spans = link_spans(line, link_destinations([line]))
    assert [line[start:end] for start, end in spans] == ["[a](x.md)", "[b [c]](y.md 'z')", "](w.md)"]
    assert link_spans(line, []) == []


# The scaling test reads the same characters as eight lines and as one line eight times as long. A reader whose time
# grows with the line's length takes about as long either way, a ratio near 1; one that reads the line again for each
# link takes eight times as long on the long line. The bound is the two ratios' geometric midpoint, and each side is
# timed for the same work, about ten milliseconds and more, with garbage collection off, so a loaded runner slows both.
LINES = 8
LINKS = 1000
RUNS = 7
SCALING_BOUND = LINES**0.5
# A long line this many times past the bound is past any runner's noise, so the test fails without timing it again.
FAR_PAST = 3


def _seconds(lines: list[str]) -> float:
    found = [(line, link_destinations([line])) for line in lines]
    start = time.perf_counter()
    for line, destinations in found:
        link_spans(line, destinations)
    return time.perf_counter() - start


@pytest.mark.parametrize("unit", ["[a](d/b.md) ", "a](d/b.md) "], ids=["opened", "no-opening-bracket"])
def test_a_line_of_links_takes_time_in_proportion_to_its_length(unit: str) -> None:
    # link_span read the whole line again for each link: it masked its code spans, sliced it, and walked back to its
    # start, so a line of 8000 links took 16 times as long as eight lines of 1000, and 70 times without a `[`.
    short = [unit * LINKS] * LINES
    long = [unit * LINKS * LINES]
    gc.disable()
    try:
        eight = min(_seconds(short) for _ in range(RUNS))
        one = []
        for _ in range(RUNS):
            one.append(_seconds(long))
            if one[-1] < SCALING_BOUND * eight or one[-1] > FAR_PAST * SCALING_BOUND * eight:
                break
    finally:
        gc.enable()
    assert min(one) < SCALING_BOUND * eight
