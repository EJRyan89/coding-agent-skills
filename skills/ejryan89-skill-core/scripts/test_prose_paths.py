"""Regression tests for the one class of the characters a path written in prose or a code span holds."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from prose_paths import PATH_CHARACTER, PATH_START, PROSE_PATH

PATH = re.compile(rf"{PATH_START}{PATH_CHARACTER}*\.md")


@pytest.mark.parametrize(
    "path",
    [
        "a.md",
        "docs/a.md",
        "./a.md",
        "../shared/a.md",
        ".venv/a.md",
        "-a.md",
        "%20a.md",
        "docs/c++.md",
        "docs/über.md",
        "docs/日本.md",
        "docs/x@y.md",
        "PROGRA~1\\a.md",
        "references/Q&A.md",
        "bin/x%20y.md",
        "2026_notes.md",
    ],
)
def test_a_path_is_read_whole_with_every_character_a_path_holds(path: str) -> None:
    assert PATH.fullmatch(path)


@pytest.mark.parametrize(
    "text",
    [
        "~/a.md",
        "@docs/a.md",
        "+a.md",
        "&a.md",
        "$HOME/a.md",
        "/a.md",
        "\\a.md",
        "my notes.md",
        "it's.md",
        "a=b.md",
        "#a.md",
        "a;b.md",
        "a,b.md",
        "C:a.md",
        "a!.md",
        "**/*.md",
        "a\tb.md",
        # The stated limit: a name in Unicode's decomposed form, NFD, holds a combining mark, no word character.
        "docs/u\u0308ber.md",
    ],
)
def test_a_text_starting_or_holding_what_no_path_does_is_no_path(text: str) -> None:
    # A root, `/` or `\`, is no first character: a reader that reads an absolute path names the root before
    # PATH_START.
    assert not PATH.fullmatch(text)


@pytest.mark.parametrize(
    ("line", "paths"),
    [
        ("Read docs%20x/guide.md now.", ["docs%20x/guide.md"]),
        ("Read a\\b/c.md now.", ["a\\b/c.md"]),
        ("See docs/c++.md, then src/A.cs.", ["docs/c++.md", "src/A.cs"]),
        ("Open docs/rules.md#style here.", ["docs/rules.md"]),
        # `@path` is Claude Code's file mention, and the `@` starts no path of its own.
        ("Follow @docs/review-guide.md for every change.", ["docs/review-guide.md"]),
        # A path needs a separator and an extension, and nothing that holds no path character starts one.
        ("README.md and docs/ and ~/x/y.md", []),
    ],
)
def test_a_prose_path_is_read_whole_from_where_no_path_character_stands_before_it(line: str, paths: list[str]) -> None:
    assert re.findall(PROSE_PATH, line) == paths
