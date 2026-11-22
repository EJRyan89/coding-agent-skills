"""Regression tests for forms_considered.py, the one reader of a fix pull request's `## Forms considered` section.

The section's reading, form by form, is shown in `tests/validation/test_pull_request_body.py`, the fixture suite of the
body check that reads it here; these show the reader's own answers.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forms_considered
from forms_considered import (
    FormsFault,
    forms_fault,
    forms_section,
    is_fix_title,
    is_placeholder,
    placeholder_forms,
)
from markdown_sections import blocks
from test_hidden_comments import SCALING, WALKS_PER_LINE
from test_time_in_proportion import assert_time_in_proportion

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


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_the_walks_read_each_line_a_bounded_number_of_times(head: str, unit: str, tail: str) -> None:
    # Counting the lines the walks read, at two sizes, holds the reading linear without timing it.
    for repeats in (100, 400):
        text = head + unit * repeats + tail
        with mock.patch("hidden_comments.blocks", wraps=blocks) as walk:
            forms_considered.forms_section(text)
        read = sum(len(call.args[0]) for call in walk.call_args_list)
        assert read <= WALKS_PER_LINE * (text.count("\n") + 1)


# A body of SCALE times the units is read against the smaller one through assert_time_in_proportion. A linear reading
# takes about SCALE times as long and a quadratic one SCALE squared, so the bound sits halfway between in log space.
# The walk count above, which runs first, fails a reading that walks too far before this one times it.
SCALE = 8
SCALE_BOUND = SCALE**1.5
SMALL_BODY = 2_000


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_a_body_scale_times_as_long_is_read_in_about_scale_times_the_time(head: str, unit: str, tail: str) -> None:
    repeats = max(1, SMALL_BODY // len(unit))
    small, large = head + unit * repeats + tail, head + unit * repeats * SCALE + tail
    assert_time_in_proportion(forms_section, small, large, SCALE_BOUND)


def test_a_body_with_no_comment_is_read_as_it_is_whatever_its_backtick_runs() -> None:
    # A line of backtick runs once made every body's code-span search take time growing with their square.
    text = "`" * 32_768 + "a" * 32_728 + "\n\n## Forms considered\n\nText.\n"
    started = time.perf_counter()
    # The run opens a fence, as GitHub reads a line of backticks whose text after them holds none, so the fence holds
    # the heading.
    assert forms_section(text) is None
    assert time.perf_counter() - started < 1.0


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


# Each issue's body, with the section GitHub renders for it, by `gh api markdown`.
GITHUB_SECTIONS = {
    # A numbered line under a bullet's text opens an item of its own, so a heading indented less than its text is the
    # document's and ends the section.
    "a heading after a numbered item that left a bullet": (
        "## Forms considered\n\n- a\n2. b\n  ## Not the end\n\ntext",
        "\n- a\n2. b",
    ),
    # A `<!--` within a list item's text, with no `-->` in it, is text, so the section after it shows.
    "an opener prose shows in an earlier section": (
        "## Summary\n\n- `forms_considered.py`: a <!-- that prose shows no longer hides the rest.\n\n"
        "## Forms considered\n\nFilled.",
        "\nFilled.",
    ),
    "an unclosed opener inside a paragraph": (
        "## Forms considered\n\ntext <!-- a\n\nVISIBLE",
        "\ntext <!-- a\n\nVISIBLE",
    ),
    # A fence its list item stops holding ends there, so the comment under it opens a block that holds the fence-like
    # line, and the heading after its `-->` is the section's.
    "a comment after a fence its item ends": (
        "## Summary\n\n- a\n  ```\n<!--\n```\n-->\n## Forms considered\n\nOnly this one.\n\n```\n```",
        "\nOnly this one.\n\n```\n```",
    ),
    # An HTML heading opens a block that runs to the blank line under it, so the lines it holds are neither a fence nor
    # a heading.
    "a heading an HTML heading's block holds": ("<h2>Y</h2>\n- ```\n## Forms considered\n---\n\ntext", None),
    # A heading or an underline a comment hides, opened in a quote or a list item and closed past it, ends nothing.
    "a heading inside a comment a quote opens": (
        "## Forms considered\n\nw1\n\n> <!-- c\n\n## w5\n> <!-- d -->\n\nw9\n",
        "\nw1\n\n> \n\nw9\n",
    ),
    "an underline inside a comment a list item opens": (
        "## Forms considered\n\nw1\n\n- <!-- c\n\nw5\n---\n<!-- d -->\n\nw9\n",
        "\nw1\n\n- \n\nw9\n",
    ),
    # An item that holds only a comment is empty once GitHub hides it, so the text under it is no lazy line of the item
    # and the underline makes it a heading.
    "an underline under an item that holds only a comment": (
        "## Forms considered\n\nw1\n\n- <!-- w2 -->\nw3\n---\nw4\n",
        "\nw1\n\n- ",
    ),
    # A line an HTML heading's block holds is passed on as HTML, where no backtick opens a code span, so its `<!--`
    # opens a comment that hides the rest of the body.
    "a backticked opener an HTML heading's block holds": ("<h2>x</h2>\n`<!--` y\n\n## Forms considered\n\nw1", None),
    # The lines an HTML heading's block holds are no fence, so the paragraph after the list is no item's text and the
    # underline under it makes it a heading.
    "an underline after a fence-like line an HTML heading's block holds": (
        "<h2>x</h2>\n```\n\n## Forms considered\n\nw1\n\n- w2\n\nw3\n---\n\nw4",
        "\nw1\n\n- w2\n",
    ),
    "an underline after a tilde line an HTML heading's block holds": (
        "<h2>x</h2>\n~~~\n\n## Forms considered\n\nw1\n\n- w2\n\nw3\n===\n\nw4",
        "\nw1\n\n- w2\n",
    ),
    # A numbered line under a bullet's text opens an item of its own, whose content starts a column further in, so a
    # paragraph indented two columns after a blank line is the document's, and the underline under it makes it a
    # heading that ends the section.
    "an underline after a numbered item that left a bullet": (
        "## Forms considered\n\n- a\n2. b\n\n  Text\n  ---\nafter",
        "\n- a\n2. b\n",
    ),
    # An item that holds only a comment, right under a paragraph, interrupts it, as an item with text does, so the
    # line under it is the item's, and an underline the item does not hold is a rule, which ends nothing.
    "a rule under an item that holds only a comment and interrupts a paragraph": (
        "## Forms considered\n\nw1\n- <!-- w2 -->\n  w3\n---\n\nw4",
        "\nw1\n- \n  w3\n---\n\nw4",
    ),
    # A bare marker opens an item that holds the lines indented to its content, so the comment and the heading under
    # it are the item's.
    "a comment under an item that opens empty": (
        "## Forms considered\n\n-\n  <!-- a\n  -->\n  ## not a section\n\nafter",
        "\n-\n  \n  ## not a section\n\nafter",
    ),
}


@pytest.mark.parametrize(("text", "section"), GITHUB_SECTIONS.values(), ids=GITHUB_SECTIONS.keys())
def test_the_section_is_the_one_github_renders(text: str, section: str | None) -> None:
    assert forms_section(text) == section


# Generated bodies, each read as GitHub renders it (`gh api markdown`), that the mutation gate's surviving mutants
# read otherwise: each changes the section of one of them or fails on it.
GENERATED_SECTIONS = [
    (
        "## Forms considered\n\n- a\n  ##\n\nafter\n\n## End\n",
        "\n- a\n  ##\n\nafter\n",
    ),
    (
        "   w1 lazy\n\n<h1>w2\nw3 -->\n<h1>w4\n## Forms considered\n",
        None,
    ),
    (
        "w1 <!-- w2\n<h2>\nw3\n~~~\n\n- w4\n  ```\n## Forms considered\n\n",
        "\n",
    ),
    (
        "- w1\n  ```\n\n## Forms considered\n\n<!-- w2 -->\n<h2>w3</h2>",
        "\n",
    ),
    (
        "-\n  <h2>Y</h2>\n> q\n## Forms considered\n  - b\n    code\n\n## End\ntail",
        "  - b\n    code\n",
    ),
    (
        "-->\n\nw1\n===\nw2 -->\n## Forms considered\n\n> ## w3\n\nw4 -->\n    <!-- w5\n",
        "\n> ## w3\n\nw4 -->\n    <!-- w5\n",
    ),
    (
        "  - b\n   - deep\n> ---\n## Forms considered\n| - |\n---\nlazy\n\n## End\ntail",
        "",
    ),
    (
        "> - w1\n\n## Forms considered\n\n> w2 <!-- w3\n> <!-- w4\n  ## w5\n- <!-- w6\n",
        "\n> w2 <!-- w3\n> ",
    ),
    (
        "  text\n\t- t\n- the <!-- no closer\n## Forms considered\n- <!-- x\n\n-\n- \n\n## End\ntail",
        "- ",
    ),
    (
        "  <!-- w1\n\n# w2\n<h2>\nw3\n\n  w4 indented\n2. w5\n\nw6 -->\n<h1>w7\n## Forms considered\n\n",
        None,
    ),
    (
        (
            "-->\n<!--\n1. c\n## Forms considered\na -->\n- a\n  <h2>Y</h2>\n2. b\n      text\n<!-- c -->\n"
            "\n## End\ntail"
        ),
        None,
    ),
    (
        (
            "w1\n===\n## Forms considered\n\n- w2\n  ```\n\n> <!-- w3\nw4 -->\n- \n\n- w5 <!-- w6\n\n~~~\n"
            "> - w7\n\n- <!-- w8\n\n"
        ),
        "\n- w2\n  ```\n\n> ",
    ),
    (
        (
            "> - w1\n> w2 <!-- w3\n> <!-- w4 -->\n## Forms considered\n\nw5 <!-- w6 --> w7\n  w8 indented\n"
            "- w9\n  ```\n`<!--` w10\n- "
        ),
        "\nw5  w7\n  w8 indented\n- w9\n  ```",
    ),
    (
        (
            "---\n> - w1\n## Forms considered\n\n- a\n2. w2\n- a\n2. w3\n\n> <!-- w4\n- <!-- w5\n\n"
            "> ## Forms considered\n>\n\nw6\n---\nw7 -->\n---\n> w8\n\n"
        ),
        "\n- a\n2. w2\n- a\n2. w3\n\n> ",
    ),
    (
        (
            "- w1\n  ```\n  w2\n#\n2. w3\n\n- ## Forms considered\n\n- w4\n<!-- w5 -->\n\n#\n\n> w6\n"
            "    <!-- w7\n\n   w8 lazy\n## Forms considered\n\nw9\n---\n#\n\n"
        ),
        "",
    ),
    (
        (
            "<h2>w1</h2>\nw2 <!-- w3\nw4 <!-- w5 --> w6\n`<!--` w7\n~~~\n\nw8 <!-- w9\n\n"
            "## Forms considered\n\n>\n\n```\n<!--\n```\n-->\n- w10\n  ---\n\n### w11\n\n"
        ),
        None,
    ),
    (
        (
            "<!--\n```\n-->\n## Forms considered\n-\n- w1\n  ```\n\nw2\n---\n\n-->\n### w3\n"
            "> ## Forms considered\nw4 text\n\n- w5\n  ---\n- a\n2. w6\n<h2>w7</h2>\n\n# w8\n\n"
            "- w9 <!-- w10\n\n"
        ),
        "-\n- w1\n  ```\n",
    ),
    (
        (
            "- w1\n  ```\n<h1>w2\n> - w3\n\nw4\n---\n- w5\n  ---\n\n<!--\n```\n-->\nw6 -->\n\n#\n\n"
            "  ## w7\n\n## Forms considered\n\n<!--\n```\n-->\n- <h2>w8</h2>\n\n- w9\n  ```\n  w10\n  ```\n"
            "    <!-- w11\n  w12 indented\n"
        ),
        "\n\n- <h2>w8</h2>\n\n- w9\n  ```\n  w10\n  ```\n    ",
    ),
    (
        (
            "- w1\n  ```\n  - w2\n<!--\nw3\n-->\n\n<!--\n```\n-->\n\n- w4 <!-- w5\n\nw6\n===\n<!--\n#\n"
            "| w7 |\n| --- |\n| w8 |\nw9 -->\n\n## Forms considered\n\nw10\n===\nw11 <!-- w12 --> w13\n"
            "  w14 indented\n\n- ## w15\n\n"
        ),
        "",
    ),
    (
        (
            "w1 <!-- w2 --> w3\n\n## Forms considered\n\n| w4 |\n| --- |\n| w5 |\n- w6\n\n"
            "w7 <!-- w8 --> w9\n\nw10 text\nw11 <!-- w12 --> w13\n- <!-- w14\n<!--\nw15\n-->\n-\n"
            "- w16 <!-- w17\n\n> <h1>w18</h1>\n\n<!-- w19\n"
        ),
        (
            "\n| w4 |\n| --- |\n| w5 |\n- w6\n\nw7  w9\n\nw10 text\nw11  w13\n- \n-\n- w16 <!-- w17\n\n"
            "> <h1>w18</h1>\n\n"
        ),
    ),
]


@pytest.mark.parametrize(("text", "section"), GENERATED_SECTIONS)
def test_a_generated_body_is_read_as_github_renders_it(text: str, section: str | None) -> None:
    assert forms_section(text) == section


# Where a setext or HTML heading ends the section, read through markdown_sections' contained_lines, each pinned to
# GitHub's rendering: an underline or an HTML heading a list item or a quote holds is inside the section, and one no
# container holds ends it.
SETEXT_AND_HTML_ENDS = {
    "underline in an item's later paragraph": ("- a\n\n  text\n  ---", True),
    "underline in a quote": ("> text\n> ---", True),
    "underline below a lazy line of an item": ("- a\ntext\n---", True),
    "equals underline in a numbered item": ("1. a\n   b\n   ===", True),
    "equals line below a lazy line of a quote": ("> a\ntext\n===", True),
    "underline in an item that holds only a comment, under a paragraph": ("w1\n- <!-- w2 -->\n  w3\n  ---", True),
    "underline in a nested item": ("- a\n  - b\n\n    text\n    ---", True),
    "underline in a quote in an item": ("- a\n  > text\n  > ---", True),
    "HTML heading in an item": ("- a\n  <h2>x</h2>", True),
    "HTML heading in a quote": ("> <h2>x</h2>", True),
    "underline after a list": ("- a\n\ntext\n===", False),
    "underline under a paragraph": ("text\n---", False),
    "HTML heading": ("<h2>x</h2>", False),
    # A table's rows run on to the underline, which is then a rule, so no heading ends the section there, while a
    # paragraph after the table is a heading's text.
    "underline under a table's rows": ("| a |\n| - |\n| b |\n---", True),
    "underline under a paragraph after a table": ("| a |\n| - |\n| b |\n\ntext\n---", False),
    # An ATX heading with no text, a run of `#` alone, is a heading too.
    "empty heading": ("text\n\n##", False),
    "empty heading with trailing spaces": ("text\n#  ", False),
    "empty heading in a quote": ("> ##", True),
    "empty heading in an item": ("- ##", True),
}


@pytest.mark.parametrize(("forms", "inside"), SETEXT_AND_HTML_ENDS.values(), ids=SETEXT_AND_HTML_ENDS.keys())
def test_a_setext_or_html_heading_ends_the_section_only_outside_every_container(forms: str, inside: bool) -> None:
    section = forms_section(f"## Forms considered\n\n{forms}\n\nafter\n\n## End\n")
    assert section is not None
    assert ("after" in section) is inside, section


@pytest.mark.parametrize("text", ["## Forms considered", "## Forms considered\n## Next\n\ntext"])
def test_a_heading_with_no_line_before_the_next_holds_an_empty_section(text: str) -> None:
    assert forms_section(text) == ""
    assert forms_fault(text, TEMPLATE) is FormsFault.EMPTY


def test_the_template_s_guidance_is_the_comments_github_hides_never_one_code_shows() -> None:
    # A template that shows a comment in code, in a fence or an inline span, holds no guidance there, as GitHub shows.
    template = (
        "## Forms considered\n\n```\n<!-- shown in a fence -->\n```\n\n`<!-- shown in a span -->` text.\n\n"
        "<!-- The guidance. -->\n\nNone\n"
    )
    visible, guidance = placeholder_forms(template)
    assert guidance == {"the guidance"}
    assert "shown in a fence" in visible
    assert "shown in a span" in visible


def test_an_empty_comment_in_the_template_is_no_guidance() -> None:
    # An empty comment's words are none, so it is no guidance that would match any section.
    template = "## Forms considered\n\n<!-- -->\n<!-- Weigh the forms. -->\n\nNone\n"
    assert placeholder_forms(template)[1] == {"weigh the forms"}
    assert is_placeholder("weigh the forms", template)


def test_the_template_s_visible_text_is_read_from_its_first_character() -> None:
    assert placeholder_forms("## Forms considered\nNone\n")[0] == "none"
