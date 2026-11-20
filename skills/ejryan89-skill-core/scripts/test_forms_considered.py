"""Regression tests for forms_considered.py, the one reader of a fix pull request's `## Forms considered` section.

The section's reading, form by form, is shown in `tests/validation/test_pull_request_body.py`, the fixture suite of the
body check that reads it here; these show the reader's own answers.
"""

from __future__ import annotations

import gc
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forms_considered
from forms_considered import WALK_WINDOW, FormsFault, forms_fault, forms_section, is_fix_title, without_hidden_comments
from markdown_sections import blocks

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
        # A comment that opens a block ends the paragraph, so an indented line after it is code that shows its `<!--`.
        ("<!-- x -->\n    <!-- y", "\n    <!-- y"),
        ("Text.\n<!-- x -->\n    <!-- y -->", "Text.\n\n    <!-- y -->"),
        # A comment block inside a quote holds its fence-like lines too.
        ("> <!-- x\n> ```\n> -->\n<!-- y -->", "> \n"),
        # A fence-like line inside a comment within a line is a fence, as GitHub reads the blocks before the comments,
        # so the fence shows the `<!--` after it, and the comment inside the line, whose paragraph the fence ends before
        # any `-->`, is text.
        ("a <!-- b\n```\n--> <!-- c -->", "a <!-- b\n```\n--> <!-- c -->"),
        # A `<!--` inside a paragraph, a list item's text, or a heading that its block ends before any `-->` is text,
        # while one that opens an HTML block, at a line's start or a list item's or a quote's content, hides the rest.
        ("a <!-- b\n\nc", "a <!-- b\n\nc"),
        ("- the construct <!-- with no closer\n- VISIBLE", "- the construct <!-- with no closer\n- VISIBLE"),
        ("text <!-- a\n## H\n-->", "text <!-- a\n## H\n-->"),
        ("text <!-- a\nb --> c", "text  c"),
        ("<!-- a\n\nb", ""),
        ("- <!-- a\n- b", "- "),
        ("> <!-- a\n\nb", "> "),
        ("- a\n      <!-- b\n\nc", "- a\n      <!-- b\n\nc"),
        # A comment on a line GitHub passes on as HTML, after a comment block's `-->` on its line, runs to the end.
        ("<!-- a --> b <!-- c\n\nd", " b "),
        ("<!-->x<!--->y", "xy"),
    ],
)
def test_a_comment_is_hidden_only_where_it_opens_outside_code(text: str, expected: str) -> None:
    assert without_hidden_comments(text) == expected


# What each stated limit of the module's docstring reads, so a change to one is seen.
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("> a <!-- b\n>\n> c -->\nd", "> a \nd", id="a comment in a quote ends anywhere in the quote"),
        pytest.param(
            "q\n> a\n> 2. <!-- b\n\nc", "q\n> a\n> 2. ", id="a quoted marker that cannot interrupt opens a block"
        ),
        pytest.param("- <!-- a\nb\n-->\nc", "- \nc", id="a block comment its item stops holding ends at its closer"),
        pytest.param("<div>\n```\n</div>\n\n<!-- a -->", "<div>\n```\n</div>\n\n<!-- a -->", id="a div is a paragraph"),
        pytest.param("``a`<!-- x -->` z", "``a`` z", id="backtick runs of unequal length pair as a span"),
        pytest.param("\\`<!-- x -->\\` z", "\\`<!-- x -->\\` z", id="an escaped backtick opens a span"),
        pytest.param(
            "`foo\nbar <!-- x --> baz` VISIBLE", "`foo\nbar  baz` VISIBLE", id="a span is read within one line"
        ),
    ],
)
def test_a_comment_is_read_within_the_module_s_stated_limits(text: str, expected: str) -> None:
    assert without_hidden_comments(text) == expected


# Bodies of one form repeated to GitHub's limit, each of which once took seconds to minutes to read, or would read in
# time growing with the square of the body if a comment block's walk started again from the body's start.
SCALING = {
    "closed comments": ("", "<!-- c -->\n", ""),
    "empty comments": ("", "<!-->\n", ""),
    "openers a fence shows": ("```\n", "<!--\n", "```\n"),
    "comments inside lines": ("", "x <!-- c --> y\n", ""),
    "unclosed comments inside lines": ("", "x <!-- y\n", ""),
    "comments holding a fence-like line": ("", "<!--\n```\n-->\n", ""),
    "fences showing an opener closed after them": ("", "```\n<!--\n```\n-->\n", ""),
    "quoted comments holding a fence-like line": ("", "> <!--\n> ```\n> -->\n", ""),
    "comments in a list item": ("- a\n", "  <!--\n  x\n  -->\n", ""),
    "comments in a list item holding a fence-like line": ("- a\n", "  <!--\n  ```\n  -->\n", ""),
    "comments in an item in a quote": ("> - a\n", ">   <!--\n>   ```\n>   -->\n", ""),
    "comments their item stops holding": ("", "- <!--\nx\n-->\n", ""),
    "HTML headings holding a fence-like line": ("", "<h2>x</h2>\n```\ntext\n\n", ""),
    "nested list items": ("", "".join("  " * depth + "- <!--\n" + "  " * depth + "  -->\n" for depth in range(60)), ""),
    # A run of quote markers a comment block's start once backtracked through in time exponential in its length.
    "nested quote markers": ("<!-- c -->\n", "> " * 40 + "x\n", ""),
}
# How many times over the block walks may read a body's lines, whatever its comments.
WALKS_PER_LINE = 8


# A body of SCALE times the units is read against the smaller one, each the fastest of RUNS readings with the garbage
# collector off, so a slow or loaded runner slows both alike. A linear reading takes about SCALE times as long and a
# quadratic one about SCALE squared, so the ratio of the larger's time to SCALE times the smaller's is bounded between.
SCALE = 8
RUNS = 5
SCALE_BOUND = 3.0
SMALL_BODY = 2_000


def fastest_pair(small: str, large: str) -> tuple[float, float]:
    """The fastest of RUNS readings of each body, taken in turn so a change in the runner's load meets both."""
    times: tuple[list[float], list[float]] = ([], [])
    collecting = gc.isenabled()
    gc.disable()
    try:
        for _ in range(RUNS):
            for text, kept in zip((small, large), times, strict=True):
                started = time.perf_counter()
                forms_section(text)
                kept.append(time.perf_counter() - started)
    finally:
        if collecting:
            gc.enable()
    return min(times[0]), min(times[1])


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_a_body_scale_times_as_long_is_read_in_about_scale_times_the_time(head: str, unit: str, tail: str) -> None:
    repeats = max(1, SMALL_BODY // len(unit))
    small, large = fastest_pair(head + unit * repeats + tail, head + unit * repeats * SCALE + tail)
    assert large / (SCALE * small) < SCALE_BOUND


def test_a_body_with_no_comment_is_read_as_it_is_whatever_its_backtick_runs() -> None:
    # A line of backtick runs once made every body's code-span search take time growing with their square.
    text = "`" * 32_768 + "a" * 32_728 + "\n\n## Forms considered\n\nText.\n"
    started = time.perf_counter()
    # The run opens a fence, as GitHub reads a line of backticks whose text after them holds none, so the fence holds
    # the heading.
    assert forms_section(text) is None
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(("head", "unit", "tail"), SCALING.values(), ids=SCALING.keys())
def test_the_walks_read_each_line_a_bounded_number_of_times(head: str, unit: str, tail: str) -> None:
    # Counting the lines the walks read, at two sizes, holds the reading linear without timing it.
    for repeats in (100, 400):
        text = head + unit * repeats + tail
        with mock.patch("forms_considered.blocks", wraps=blocks) as walk:
            forms_considered.forms_section(text)
        read = sum(len(call.args[0]) for call in walk.call_args_list)
        assert read <= WALKS_PER_LINE * (text.count("\n") + 1)


# Fences whose closer, or whose end, falls before, at, and past each width the window doubles through, so every
# window the fence's reading takes reads it alike.
FENCE_LENGTHS = sorted(
    {length for width in (WALK_WINDOW, WALK_WINDOW * 2, WALK_WINDOW * 4) for length in (width - 2, width - 1, width)}
)


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_a_fence_longer_than_a_window_shows_its_openers_and_ends_where_it_closes(length: int) -> None:
    fence = "```\n" + "<!-- shown\n" * length + "```\n"
    assert without_hidden_comments(f"{fence}a <!-- b --> c") == f"{fence}a  c"


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_an_unclosed_fence_longer_than_a_window_shows_every_opener_to_the_end(length: int) -> None:
    text = "```\n" + "<!-- shown\n" * length + "<!-- also shown -->"
    assert without_hidden_comments(text) == text


@pytest.mark.parametrize("length", FENCE_LENGTHS)
def test_a_quoted_fence_longer_than_a_window_ends_with_its_quote(length: int) -> None:
    fence = "> ```\n" + "> <!-- shown\n" * length
    assert without_hidden_comments(f"{fence}\n<!-- hidden -->after") == f"{fence}\nafter"


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


# Where a setext or HTML heading ends the section, read here by the module's own item reading and the walk's quotes as
# it was before the walk read the rest of the body, each pinned to GitHub's rendering: an underline or an HTML heading
# a list item or a quote holds is inside the section, and one no container holds ends it.
SETEXT_AND_HTML_ENDS = {
    "underline in an item's later paragraph": ("- a\n\n  text\n  ---", True),
    "underline in a quote": ("> text\n> ---", True),
    "underline below a lazy line of an item": ("- a\ntext\n---", True),
    "equals underline in a numbered item": ("1. a\n   b\n   ===", True),
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


def test_a_setext_heading_is_held_within_the_module_s_stated_limit() -> None:
    # The module's own item reading holds a numbered line under a bullet's text as lazy, so the underlined line after
    # the blank line is read as the bullet's, where GitHub ends the section at it.
    section = forms_section("## Forms considered\n\n- a\n2. b\n\n  Text\n  ---\nafter")
    assert section == "\n- a\n2. b\n\n  Text\n  ---\nafter"


@pytest.mark.parametrize("text", ["## Forms considered", "## Forms considered\n## Next\n\ntext"])
def test_a_heading_with_no_line_before_the_next_holds_an_empty_section(text: str) -> None:
    assert forms_section(text) == ""
    assert forms_fault(text, TEMPLATE) is FormsFault.EMPTY
