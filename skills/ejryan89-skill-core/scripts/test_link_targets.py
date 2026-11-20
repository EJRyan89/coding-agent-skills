"""Regression tests for the one resolver of a Markdown link's destination to a repository path."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from link_targets import link_target

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
