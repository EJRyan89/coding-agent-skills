"""Regression tests for the one resolver of a Markdown link's destination to a repository path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_destinations import link_destinations
from link_targets import link_span, link_target

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
    ],
)
def test_a_link_s_span_runs_from_its_text_to_its_end(line: str, span: str) -> None:
    (destination,) = link_destinations([line])
    start, end = link_span(line, destination)
    assert line[start:end] == span
