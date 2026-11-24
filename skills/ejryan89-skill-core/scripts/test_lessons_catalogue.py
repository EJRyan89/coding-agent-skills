"""Regression tests for lessons_catalogue.py, the one reader of the lessons catalogue.

Validation's ledger check, in `tests/validation/test_markdown_links.py`, shows the problems it reports from what this
reads; these show the reader's own answers. Every test holds on every platform.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lessons_catalogue import MECHANISM, CatalogueError, Pattern, mechanism_bases, patterns, read_patterns

CATALOGUE = """# Lessons

```markdown
## Patterns
```

## Patterns

### First

- **Rule:** A rule
  that continues.
- **Use:** `json_reader:read_json` and `deployer.config:Config.load`.
- **Colour:** an unknown field.
- **Rule:** again.

```markdown
### Shown in a fence
```

### Second

- **Search for:** `os.walk(`

## Using the catalogue

### After the section
"""


def test_each_pattern_is_read_with_its_fields_continuations_and_problems() -> None:
    assert patterns(CATALOGUE) == [
        Pattern(
            9,
            "First",
            {
                "Rule": [(11, "A rule"), (12, "that continues."), (15, "again.")],
                "Use": [(13, "`json_reader:read_json` and `deployer.config:Config.load`.")],
                "Colour": [(14, "an unknown field.")],
            },
            [
                (14, "names the field 'Colour', which a pattern does not have"),
                (15, "names the field 'Rule' more than once"),
            ],
        ),
        Pattern(21, "Second", {"Search for": [(23, "`os.walk(`")]}, []),
    ]


def test_a_field_ends_at_a_line_it_does_not_indent_and_a_heading_keeps_its_first_field() -> None:
    # The section's heading may trail spaces; a field's first line follows its heading directly; one space indents a
    # continuation; a plain line or a blank ends the field, so an indented line after either belongs to none, as an
    # indented line before any field does.
    text = (
        "## Patterns  \n\n### Tight\n- **Rule:** a\n plain one-space continuation\nplain text\n- **Why:** w\n\n"
        "  indented after a blank\n\n### Lead\n  indented before any field\n- **Rule:** r\n"
    )
    assert patterns(text) == [
        Pattern(3, "Tight", {"Rule": [(4, "a"), (5, "plain one-space continuation")], "Why": [(7, "w")]}, []),
        Pattern(11, "Lead", {"Rule": [(13, "r")]}, []),
    ]


def test_a_fields_clauses_split_at_each_semicolon_outside_a_code_span() -> None:
    # Two spans after a `;` share its clause; a `;` that ends a line splits from the next, as one that opens it does;
    # and a clause that a line's own `;` opens runs on into the next line.
    text = (
        "## Patterns\n\n### P\n\n- **Search for:** `a` and `b`; `c { x; }` and `c2`;\n  `d`; `d2` and\n  `d3`\n"
        "  ; `e`.\n"
    )
    (pattern,) = patterns(text)
    assert pattern.clauses("Search for") == [
        [(5, "a"), (5, "b")],
        [(5, "c { x; }"), (5, "c2")],
        [(6, "d")],
        [(6, "d2"), (7, "d3")],
        [(8, "e")],
    ]
    assert pattern.clauses("Guard") == []


def test_a_fields_spans_are_its_code_spans_by_line() -> None:
    first = patterns(CATALOGUE)[0]
    assert first.spans("Use") == [(13, ["json_reader:read_json", "deployer.config:Config.load"])]
    assert first.spans("Guard") == []


@pytest.mark.parametrize(
    ("span", "groups"),
    [
        ("json_reader:read_json", ("json_reader", "read_json", None)),
        ("deployer.config:Config.load", ("deployer.config", "Config", "load")),
    ],
)
def test_a_mechanism_is_a_module_a_name_and_a_member(span: str, groups: tuple[str, str, str | None]) -> None:
    match = MECHANISM.fullmatch(span)
    assert match is not None
    assert match.groups() == groups


@pytest.mark.parametrize("span", ["json_reader", "json_reader:", "tests/test_a.py::Tests"])
def test_anything_else_is_no_mechanism(span: str) -> None:
    assert MECHANISM.fullmatch(span) is None


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("# Lessons\n", "docs/lessons.md has no '## Patterns' section"),
        (
            "## Patterns\n\nNothing yet.\n\n## Next\n\n### Elsewhere\n",
            "docs/lessons.md has no pattern under '## Patterns'",
        ),
    ],
)
def test_a_catalogue_with_no_pattern_is_refused(text: str, reason: str) -> None:
    with pytest.raises(CatalogueError, match=f"^{re.escape(reason)}$"):
        patterns(text)


def test_the_catalogue_is_read_from_its_place_in_the_repository(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "lessons.md").write_bytes(CATALOGUE.encode("utf-8"))
    assert [pattern.name for pattern in read_patterns(tmp_path)] == ["First", "Second"]


def test_a_missing_catalogue_is_named_missing(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError, match=f"^{re.escape('docs/lessons.md does not exist')}$"):
        read_patterns(tmp_path)


def test_a_catalogue_that_is_a_folder_is_named_so(tmp_path: Path) -> None:
    (tmp_path / "docs" / "lessons.md").mkdir(parents=True)
    with pytest.raises(CatalogueError, match=f"^{re.escape('docs/lessons.md is not a file')}$"):
        read_patterns(tmp_path)


def test_a_catalogue_that_is_not_utf8_is_named_unreadable_with_the_decoders_words(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "lessons.md").write_bytes(b"## Patterns\n\xff\n")
    with pytest.raises(
        CatalogueError, match=re.escape("docs/lessons.md cannot be read: 'utf-8' codec can't decode byte 0xff")
    ):
        read_patterns(tmp_path)


def test_mechanisms_are_imported_from_the_root_then_each_skills_scripts(tmp_path: Path) -> None:
    for folder in ("skills/b/scripts", "skills/a/scripts", ".claude/skills/c/scripts", "skills/d"):
        (tmp_path / folder).mkdir(parents=True)
    assert mechanism_bases(tmp_path) == [
        tmp_path,
        tmp_path / ".claude/skills/c/scripts",
        tmp_path / "skills/a/scripts",
        tmp_path / "skills/b/scripts",
    ]
