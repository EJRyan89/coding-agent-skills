"""Regression tests for forms_considered.py, the one reader of a fix pull request's `## Forms considered` section.

The section's reading, form by form, is shown in `tests/validation/test_pull_request_body.py`, the fixture suite of the
body check that reads it here; these show the reader's own answers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from forms_considered import FormsFault, forms_fault, forms_section, is_fix_title, without_hidden_comments

TEMPLATE = (
    "## Changes\n\n-\n\n## Forms considered\n\n"
    "<!-- For a fix, replace None with the forms the fix weighed. -->\n\nNone\n\n## Implications\n"
)
WEIGHED = "The construct is a path's quoting; single, double, and no quotes each read; `test_x` fails reverted."


def body(forms: str | None) -> str:
    """A pull request body with the section holding `forms`, or with no section when it is None."""
    section = "" if forms is None else f"## Forms considered\n\n{forms}\n\n"
    return f"Why.\n\n## Changes\n\n- `x.py`: y.\n\n{section}## Implications\n\nNone\n"


@pytest.mark.parametrize(
    ("forms", "fault"),
    [
        (None, FormsFault.MISSING),
        ("None", FormsFault.PLACEHOLDER),
        ("<!-- For a fix, replace None with the forms the fix weighed. -->\n\nNone", FormsFault.PLACEHOLDER),
        ("For a fix, replace None with the forms the fix weighed.", FormsFault.PLACEHOLDER),
        ("", FormsFault.EMPTY),
        ("TBD", FormsFault.EMPTY),
        ("- n/a", FormsFault.EMPTY),
        (WEIGHED, None),
    ],
)
def test_each_section_is_judged_against_the_template(forms: str | None, fault: FormsFault | None) -> None:
    assert forms_fault(body(forms), TEMPLATE) is fault


# A fix's own forms that show `<!--` in code, which GitHub renders as text, so no comment hides what follows it.
CODE_OPENERS = {
    "a fenced <!-- with no -->": f"```\n<!-- an opener the fix reads\n```\n\n{WEIGHED}",
    "an inline <!--": f"`<!--` with no closing marker is the construct. {WEIGHED}",
    "an inline <!-- in a double-backtick span": f"``<!-- `x` `` is the construct. {WEIGHED}",
    "an indented <!-- after a blank line": f"Read:\n\n    <!-- an opener the fix reads\n\n{WEIGHED}",
}


@pytest.mark.parametrize("forms", CODE_OPENERS.values(), ids=CODE_OPENERS.keys())
def test_a_comment_opener_code_shows_hides_nothing(forms: str) -> None:
    assert forms_fault(body(forms), TEMPLATE) is None
    section = forms_section(body(forms))
    assert section is not None
    assert section.strip().endswith(forms.splitlines()[-1])


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a <!-- b --> c", "a  c"),
        ("a <!-- b", "a "),
        ("`<!--` a <!-- b --> c", "`<!--` a  c"),
        ("```\n<!--\n```\nx <!-- y -->", "```\n<!--\n```\nx "),
        # A fence-like line a comment holds opens no block, so the comment ends at its own `-->`.
        ("<!--\n```\n-->\n## Forms considered", "\n## Forms considered"),
        # A backtick a comment holds pairs with none after the comment goes.
        ("<!-- ` --> a <!-- ` --> b", " a  b"),
    ],
)
def test_a_comment_is_hidden_only_where_it_opens_outside_code(text: str, expected: str) -> None:
    assert without_hidden_comments(text) == expected


def test_the_section_ends_at_the_next_second_level_heading() -> None:
    assert forms_section(body(WEIGHED)) == f"\n{WEIGHED}\n"


@pytest.mark.parametrize(
    "title", ["fix: read a path", "fix(deployer): read a path", "fix!: read a path", "Fix: read a path", " fix: x "]
)
def test_a_fix_title_is_read_as_a_fix(title: str) -> None:
    assert is_fix_title(title)


@pytest.mark.parametrize(
    "title", ["docs: say what a fix owes", "fixes: read a path", "a fix: read a path", "fix:read a path", "fix", ""]
)
def test_any_other_title_is_not_a_fix(title: str) -> None:
    assert not is_fix_title(title)
