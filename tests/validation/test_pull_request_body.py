"""Fixture tests for tests/validation/pull_request_body.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import pull_request_body
from forms_considered import FILLERS, forms_section, placeholder_forms
from markdown_sections import HTML_COMMENT
from pull_request_body import closing_line_problems, forms_problems, is_fix
from validation_support import REPOSITORY_ROOT, PullRequest

from tools.release_notes import ReleaseNotesError
from tools.worktrees import TITLE_TYPES

TEMPLATE = (REPOSITORY_ROOT / ".github" / "pull_request_template.md").read_text(encoding="utf-8")
FILLED = (
    "The construct is the quoting of a path in a Bash fence. Catalogue: single quotes, double quotes, and none; each\n"
    "is refused or read as the table says. Callers checked: `render.py` and `render_shell.py`. Lessons pattern:\n"
    '"A token is quoted for its context", whose **Search for** found only this caller. `test_x` fails reverted.\n'
)


def body(
    section: str | None, *, before: str = "Closes #1\n\nWhy.\n\n", after: str = "## Implications\n\nNone\n"
) -> str:
    """A pull request body holding the section as given, or none when section is None."""
    middle = "" if section is None else f"## Forms considered\n\n{section}\n"
    return f"{before}{middle}{after}"


def template_section() -> str:
    """The template's own section, comment and placeholder included, as an author who left it would submit it."""
    start = TEMPLATE.index("## Forms considered\n") + len("## Forms considered\n")
    return TEMPLATE[start : TEMPLATE.index("\n## ", start)]


# Each form of a fix body whose section is missing: no heading, or a heading this check does not take for it.
MISSING = {
    "no heading": body(None),
    "heading only inside a comment": body(None, after=f"<!--\n## Forms considered\n\n{FILLED}-->\n"),
    "heading only inside a backtick fence": body(None, after=f"```\n## Forms considered\n\n{FILLED}```\n"),
    "heading only inside a tilde fence": body(None, after=f"~~~markdown\n## Forms considered\n\n{FILLED}~~~\n"),
    "third-level heading": body(None, after=f"### Forms considered\n\n{FILLED}"),
    "first-level heading": body(None, after=f"# Forms considered\n\n{FILLED}"),
    "other case": body(None, after=f"## Forms Considered\n\n{FILLED}"),
    "trailing colon": body(None, after=f"## Forms considered:\n\n{FILLED}"),
    "no space after the hashes": body(None, after=f"##Forms considered\n\n{FILLED}"),
    "bold text, not a heading": body(None, after=f"**Forms considered**\n\n{FILLED}"),
    **{
        f"setext heading underlined with {underline!r}": body(None, after=f"Forms considered\n{underline}\n\n{FILLED}")
        for underline in ("---", "===", "-", "=")
    },
    "HTML heading": body(None, after=f"<h2>Forms considered</h2>\n\n{FILLED}"),
    "heading inside a quote": body(None, after=f"> ## Forms considered\n\n{FILLED}"),
    "heading inside a list item": body(None, after=f"- ## Forms considered\n\n{FILLED}"),
    "heading inside a list item's continuation": body(None, after=f"- item\n\n  ## Forms considered\n\n{FILLED}"),
    "heading inside a numbered item's continuation": body(None, after=f"1. item\n\n   ## Forms considered\n\n{FILLED}"),
    "heading indented four spaces, a code block": body(None, after=f"    ## Forms considered\n\n{FILLED}"),
    "heading inside a comment left unclosed": body(None, after=f"<!-- later\n## Forms considered\n\n{FILLED}"),
}
# Each heading GitHub renders at the first or second level, which ends the section, so the text under it is not the
# section's: ATX at each level and indentation up to three spaces, setext under each underline, and HTML.
NEXT_HEADINGS = {
    **{
        f"{hashes} heading indented {indent}": f"{' ' * indent}{hashes} Implications"
        for hashes in ("#", "##")
        for indent in range(4)
    },
    **{
        f"setext heading underlined with {underline!r}, indented {indent}": f"Implications\n{' ' * indent}{underline}"
        for underline in ("---", "===", "-", "=", "------   ")
        for indent in range(4)
    },
    "setext heading over two lines": "The\nimplications\n---",
    # Below a paragraph's text, an ordered line numbered other than 1, a digit outside ASCII, and a line of no-break
    # spaces continue the paragraph, so the underline makes all of it the heading.
    "setext heading over a line numbered 2": "The\n2. implications\n---",
    "setext heading over a line numbered with an Arabic-Indic digit": f"The\n{chr(0x663)}. implications\n---",
    "setext heading over a line of no-break spaces": f"The\n{chr(0xA0)}\nimplications\n---",
    # An empty list item cannot interrupt a paragraph, so it continues it too.
    "setext heading over an empty bullet": "The\n*\nimplications\n---",
    "setext heading over an empty item numbered 1": "The\n1.\nimplications\n---",
    "closed ATX heading": "## Implications ##",
    # A heading indented less than the column a list item's text starts at is no longer the item's.
    "heading indented less than the numbered item above it": "1. TBD\n\n  ## Implications",
    "heading after a list item": "- TBD\n## Implications",
    # An item whose text sits more than four columns after its marker opens with indented code, so its content starts
    # one column after the marker, and a heading indented less than that is no longer the item's.
    "heading indented less than an item that opens with indented code": "-      TBD\n\n ## Implications",
    # Four columns after the marker are still the gap, so the item's content starts at column 5 and holds no heading.
    "heading indented less than an item whose text is four columns after its marker": "-    TBD\n\n  ## Implications",
    **{
        f"HTML heading {tag!r}": f"{tag}Implications</h2>"
        for tag in ("<h1>", "<h2>", "<H2 id='i'>", "  <h2>", "<h2/>", "<h2\tid='i'>")
    },
}
# Each form of a section that renders as nothing: invisible characters, entities for a space or for nothing, and tags
# with no text, alone and around a filler.
RENDERS_EMPTY = {
    **{f"the invisible character U+{ord(character):04X}": character for character in "​‌‍⁠﻿­ᅟᅠㅤﾠ⠀"},
    **{
        f"the entity {entity}": entity
        for entity in ("&nbsp;", "&#160;", "&#xA0;", "&#8203;", "&zwnj;", "&ensp;", "&emsp;", "&thinsp;", "&#x200B;")
    },
    **{
        f"the markup {markup}": markup
        for markup in (
            "<br>",
            "<br/>",
            "<BR />",
            "<p></p>",
            "<details><summary></summary></details>",
            "<div>&nbsp;</div>",
            "<sub>​</sub>",
        )
    },
    "a filler between empty tags": "<p>TBD</p>",
    "a filler behind a zero-width space": "​None​",
    "a comment left unclosed": "<!-- forms later",
    "a comment left unclosed over the rest of the body": f"<!-- forms later\n\n## Implications\n\n{FILLED}",
    # A paragraph a setext underline follows is a heading, which ends the section before it.
    "text a setext underline turns into the next heading": f"{FILLED}---",
}
# Each form of a section that holds no text of the author's.
EMPTY = {
    "nothing": "",
    "blank lines": "\n\n   \n\t\n",
    "a comment only": "<!-- to do -->",
    "an empty list item": "-",
    "empty list items of each marker": "- \n* \n+ \n1. \n2)",
    "an empty quote": ">",
    "a rule": "---",
    "an empty table": "| | |\n| --- | --- |\n| | |",
    "an empty fence": "```\n```",
    **{f"the filler {word!r}": word for word in sorted(FILLERS)},
    **{f"the filler {word.upper()!r} in a list item": f"- {word.upper()}." for word in sorted(FILLERS)},
    "a filler beside a comment": "<!-- later -->\nTBD",
    # The next second-level heading ends the section, so text below it is not the section's.
    "text only under the next heading": "",
}


class FixDetection(unittest.TestCase):
    def test_a_title_of_each_type_is_a_fix_only_when_the_type_is_fix(self) -> None:
        for kind in TITLE_TYPES:
            for title in (f"{kind}: a change", f"{kind}(deployer): a change", f"  {kind}: a change\n"):
                with self.subTest(title=title):
                    self.assertEqual(kind == "fix", is_fix(title, "fix/other"))

    def test_the_type_is_read_in_any_case(self) -> None:
        for title in ("Fix: a change", "FIX(scope): a change"):
            with self.subTest(title=title):
                self.assertTrue(is_fix(title, None))

    def test_a_title_that_does_not_parse_is_not_a_fix_here_and_the_title_check_refuses_it(self) -> None:
        for title in ("fix a change", "fixed: a change", "fix!: a change", "fixup: a change", ""):
            with self.subTest(title=title):
                self.assertEqual(title == "fix!: a change", is_fix(title, "fix/other"))

    def test_without_a_title_the_branch_kind_decides(self) -> None:
        self.assertTrue(is_fix(None, "fix/forms"))
        self.assertTrue(is_fix(None, "Fix/forms"))
        for branch in ("feat/forms", "fixes/forms", "claude/fix", "fix", "main", None):
            with self.subTest(branch=branch):
                self.assertFalse(is_fix(None, branch))


@pytest.mark.parametrize("form", sorted(MISSING))
def test_a_fix_body_without_the_section_fails(form: str) -> None:
    problems = forms_problems(MISSING[form], True, TEMPLATE)
    assert len(problems) == 1
    assert "has no `## Forms considered` section" in problems[0]


@pytest.mark.parametrize("form", sorted(EMPTY))
def test_a_fix_body_whose_section_is_empty_fails(form: str) -> None:
    text = body(EMPTY[form], after=f"## Implications\n\n{FILLED}")
    problems = forms_problems(text, True, TEMPLATE)
    assert len(problems) == 1
    assert "is empty" in problems[0] or "still the placeholder" in problems[0]


@pytest.mark.parametrize(
    "heading", [f"{' ' * indent}## Forms considered{closing}" for indent in range(4) for closing in ("", " ##", "  ")]
)
def test_the_heading_is_read_indented_up_to_three_spaces_and_closed(heading: str) -> None:
    assert forms_problems(body(None, after=f"{heading}\n\n{FILLED}"), True, TEMPLATE) == []
    assert forms_problems(body(None, after=f"{heading}\n\nTBD\n\n## Implications\n\n{FILLED}"), True, TEMPLATE)


@pytest.mark.parametrize("form", sorted(NEXT_HEADINGS))
def test_an_empty_section_never_takes_the_text_under_the_next_heading(form: str) -> None:
    text = body("", after=f"{NEXT_HEADINGS[form]}\n\n{FILLED}")
    problems = forms_problems(text, True, TEMPLATE)
    assert len(problems) == 1
    assert "is empty" in problems[0]


@pytest.mark.parametrize("form", sorted(RENDERS_EMPTY))
def test_a_section_that_renders_as_nothing_fails(form: str) -> None:
    problems = forms_problems(body(RENDERS_EMPTY[form], after=f"## Implications\n\n{FILLED}"), True, TEMPLATE)
    assert len(problems) == 1
    assert "is empty" in problems[0] or "still the placeholder" in problems[0]


def test_the_templates_section_with_its_comment_left_unclosed_fails_as_the_placeholder() -> None:
    section = template_section()
    # GitHub hides everything after an unclosed comment, so a body whose later sections hold no comment of their own,
    # one closing it by chance, renders this section, and the rest, as nothing.
    unclosed = section.replace("-->", "", 1)
    for text in (
        body(unclosed, after=f"## Implications\n\n{FILLED}"),
        body(unclosed.replace("\n\nNone", "", 1), after=f"## Implications\n\n{FILLED}"),
        TEMPLATE.replace(section, unclosed, 1),
    ):
        problems = forms_problems(text, True, TEMPLATE)
        assert len(problems) == 1, text
        assert "is empty" in problems[0] or "still the placeholder" in problems[0]


def test_the_templates_section_left_as_it_was_fails_as_the_placeholder() -> None:
    for text in (body(template_section()), TEMPLATE, TEMPLATE.replace("\n", "\r\n")):
        problems = forms_problems(text, True, TEMPLATE)
        assert len(problems) == 1
        assert "still the placeholder from .github/pull_request_template.md" in problems[0]


def test_the_templates_guidance_with_its_comment_markers_dropped_fails_as_the_placeholder() -> None:
    (comment,) = HTML_COMMENT.findall(template_section())
    guidance = comment[len("<!--") : -len("-->")]
    for section in (guidance, f"{guidance}\n\nNone", "\n".join(f"> {line}" for line in guidance.splitlines())):
        problems = forms_problems(body(section), True, TEMPLATE)
        assert len(problems) == 1, section
        assert "still the placeholder" in problems[0]


def test_the_template_holds_a_placeholder_and_guidance() -> None:
    visible, guidance = placeholder_forms(TEMPLATE)
    assert visible == "none"
    assert any("regression test that fails with the fix reverted" in form for form in guidance)


@pytest.mark.parametrize(
    "section",
    [
        FILLED,
        f"<!-- the template's comment -->\n{FILLED}",
        f"### Construct\n\n{FILLED}\n### Decisions\n\n| Entry | Decision |\n| --- | --- |\n| `-e` | refused |",
        f"```text\n## not a heading\n```\n{FILLED}",
        "See the table: `--no-index` is refused, `--no-i` refused; `test_spellings` fails reverted.",
        f"<!-->\n{FILLED}",
        f"<!--->\n{FILLED}",
        f"{FILLED}\n---\n\nA rule above, not an underline, since a blank line stands before it.",
        f"{FILLED}***",
        f"> ## A heading inside a quote\n\n{FILLED}",
        f"- ## A heading inside a list item\n\n{FILLED}",
        "&lt;br&gt; is refused as text it shows.",
        f"<details><summary>Catalogue</summary>\n\n{FILLED}</details>",
        # A bullet or an item numbered 1 interrupts the paragraph, so the rule below it is a rule, not an underline
        # that would make the whole paragraph a heading.
        f"{FILLED}1. item\n---",
        f"{FILLED}- item\n---",
        f"{FILLED}01) item\n---",
        # A digit outside ASCII or ten digits is text, not list markup, and a tag name followed by a no-break space
        # starts no HTML block.
        f"{chr(0x663)}.",
        "1234567890.",
        f"<h2{chr(0xA0)}id='x'>Not a heading</h2>\n\n{FILLED}",
        # A `<!--` that code shows is text, never a comment opener left unclosed that hides the rest of the body.
        f"```\n<!-- an opener the fix reads\n```\n\n{FILLED}",
        f"`<!--` with no closing marker is the construct.\n{FILLED}",
        f"    <!-- an opener the fix reads\n\n{FILLED}",
    ],
)
def test_a_fix_body_with_a_filled_section_passes(section: str) -> None:
    assert forms_problems(body(section), True, TEMPLATE) == []


def test_the_section_is_read_where_commonmark_ends_a_line() -> None:
    section = forms_section("## Changes\r\r- x\r\r## Forms considered\r\rRead: a.\r\r## Implications\r\rAfter.\r")
    assert section == "\nRead: a.\n"


def test_the_section_ends_at_the_next_heading_and_a_fenced_heading_does_not_end_it() -> None:
    text = body(f"```\n## Inside\n```\n{FILLED}", after="## Implications\n\nAfter.\n")
    section = forms_section(text)
    assert section is not None
    assert "After." not in section
    assert "## Inside" in section
    assert FILLED.strip() in section
    assert forms_problems(body("", after=f"## Implications\n\n{FILLED}"), True, TEMPLATE)


@pytest.mark.parametrize(
    "item",
    [
        "- an item\n\n  ## not a section",
        "- an item\n\n   # not a section",
        "* an item\n  ## not a section",
        "1. an item\n\n   ## not a section",
        "10) an item\n\n    ## not a section",
        "-   an item\n\n    ## not a section",
        # Text five or more columns after the marker is indented code, so the item's content starts at column 2.
        "-      indented code\n\n  ## not a section",
        "-     indented code\n\n  ## not a section",
        "- an item\n  - a nested item\n\n    ## not a section",
        "- an item\n\n  <h2>not a section</h2>",
        "- an item\n  continued\n  ---",
        "- an item\nlazily continued\n\n  ## not a section",
        "- an item\n\n  ```\n  ## not a section\n  ```\n\n  ## still not a section",
        "-\n  ## not a section",
    ],
)
def test_a_heading_a_list_item_holds_does_not_end_the_section(item: str) -> None:
    section = forms_section(body(f"{item}\n\n{FILLED}", after="## Implications\n\nAfter.\n"))
    assert section is not None
    assert FILLED.strip() in section, section
    assert "After." not in section


@pytest.mark.parametrize("marker", ["-", "*", "1.", "2)", "- "])
def test_an_item_that_opens_empty_ends_at_a_blank_line_after_it(marker: str) -> None:
    # CommonMark lets an item begin with one blank line at most, so the heading below is the document's, not the
    # item's, and ends the section; the section is the empty item alone.
    section = forms_section(f"## Forms considered\n\n{marker}\n\n  ## Implications\n\nOther text here.\n")
    assert section is not None
    assert "Implications" not in section, section
    assert "Other text here." not in section, section


def test_the_first_section_is_read_when_the_heading_repeats() -> None:
    text = body("", after=f"## Forms considered\n\n{FILLED}")
    assert forms_problems(text, True, TEMPLATE)
    assert forms_problems(body(FILLED, after="## Forms considered\n\n"), True, TEMPLATE) == []


def test_a_body_that_is_not_a_fixs_owes_no_section() -> None:
    for text in (body(None), body("None"), TEMPLATE):
        assert forms_problems(text, False, TEMPLATE) == []


def write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_bytes(text.encode("utf-8"))
    return path


class PolicyCheck(unittest.TestCase):
    def check(self, pull_request: PullRequest) -> None:
        pull_request_body.PullRequestBodyPolicies(
            "test_a_fix_body_fills_its_forms_considered_section", pull_request
        ).test_a_fix_body_fills_its_forms_considered_section()

    def test_a_fix_title_with_the_templates_section_fails_and_a_filled_one_passes(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            title = write(Path(folder), "title.txt", "fix(deployer): refuse a bad name\n")
            with self.assertRaises(AssertionError) as raised:
                self.check(PullRequest(write(Path(folder), "body.md", TEMPLATE), title))
            self.assertIn("still the placeholder", str(raised.exception))
            self.check(PullRequest(write(Path(folder), "filled.md", body(FILLED)), title))

    def test_another_title_passes_with_the_template_left_as_it_was(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            title = write(Path(folder), "title.txt", "feat(deployer): add a name\n")
            self.check(PullRequest(write(Path(folder), "body.md", TEMPLATE), title))

    def test_without_a_title_the_branch_decides_and_without_a_body_nothing_is_judged(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            template = write(Path(folder), "body.md", TEMPLATE)
            with mock.patch.object(pull_request_body, "head_branch", return_value="fix/forms"):
                with self.assertRaises(AssertionError):
                    self.check(PullRequest(template, None))
                self.check(PullRequest(None, None))
            with mock.patch.object(pull_request_body, "head_branch", return_value="docs/forms"):
                self.check(PullRequest(template, None))

    def test_a_branch_git_cannot_read_fails_the_check_never_reads_as_no_fix(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            template = write(Path(folder), "body.md", TEMPLATE)
            failing = mock.patch.object(pull_request_body, "head_branch", side_effect=ReleaseNotesError("no git"))
            with failing, self.assertRaises(AssertionError) as raised:
                self.check(PullRequest(template, None))
            self.assertIn("cannot read the checkout's branch: no git", str(raised.exception))

    def test_the_template_and_the_profile_name_the_section(self) -> None:
        pull_request_body.PullRequestBodyPolicies(
            "test_the_template_and_the_profile_name_the_section"
        ).test_the_template_and_the_profile_name_the_section()


# Each body that closes an issue it names in no `Closes #<n>` line of its own: GitHub reads a closing reference anywhere
# in the body, code spans, code blocks, and comments included, and the squash commit carries the body into main.
STRAY_CLOSING_LINES = {
    "a backticked reference in prose": "Closes #1\n\nTwo examples, such as `Closes #12`, read wrong.\n",
    "a reference in prose": "Closes #1\n\nThis also fixes #12 in passing.\n",
    "a reference in a code block": "Closes #1\n\n```text\nCloses #12 when it merges\n```\n",
    "a reference in an indented code block": "Closes #1\n\nWhy.\n\n    resolves #12\n",
    "a reference in a comment the author wrote": "Closes #1\n\n<!-- Closes #12 -->\n",
    "a reference in a comment the author wrote over lines": "Closes #1\n\n<!-- later:\nfixes #12\n-->\n",
    "a second reference on a closing line": "Closes #1, #12\n",
    "a second keyword on a closing line": "Closes #1 and closes #12\n",
    "text after a closing line's reference": "Closes #1 for now\n",
    "a closing line as a list item": "- Closes #12\n",
    "a closing line in a quote": "> Closes #12\n",
    "a closing line in emphasis": "**Closes** #12\n",
    "a closing line in another case": "closes #12\n",
    "a closing line with a colon": "Closes: #12\n",
    "another keyword alone": "Fixes #12\n",
    "another repository's issue": "Closes owner/repo#12\n",
    "an autolinked issue": "Resolves GH-12\n",
    "an issue's URL": "Fixed https://github.com/owner/repo/issues/12\n",
    "a closing line naming issue zero": "Closes #0\n",
    "a closing line with a leading zero": "Closes #012\n",
    "a line that opens a reference naming no issue": "Closes #abc\n",
    "a reference after a CRLF line break": "Closes #1\r\n\r\nSee `fixes #12`.\r\n",
    "a reference after a lone CR line break": "Closes #1\r\rSee `fixes #12`.\r",
}


@pytest.mark.parametrize("form", sorted(STRAY_CLOSING_LINES))
def test_a_closing_reference_outside_a_closes_line_of_its_own_fails(form: str) -> None:
    problems = closing_line_problems(STRAY_CLOSING_LINES[form], TEMPLATE)
    assert len(problems) == 1, problems
    assert "Closes #<number>" in problems[0]


# Each body whose closing references are all `Closes #<n>` lines of their own, or that holds none.
CLOSES_LINES_ONLY = {
    "one closing line": "Closes #12\n\nWhy.\n",
    "a closing line per issue": "Closes #12\nCloses #13\n\nWhy.\n",
    "a closing line between spaces": "  Closes #12 \t\n\nWhy.\n",
    "no closing line": "Why, with no issue to close.\n",
    "a keyword before no reference": "This fixes the crash and closes the file.\n",
    "GH- before no digit": "This fixes GH-hosted runners.\n",
    "a keyword inside another word": "Disclosed #12 earlier; prefixes #12 too.\n",
    "the template's closing line left with no issue": "Closes #\n\nWhy.\n",
    "a closing line in an HTML block, which GitHub renders and links": "<details>\nCloses #12\n</details>\n",
}


@pytest.mark.parametrize("form", sorted(CLOSES_LINES_ONLY))
def test_a_body_naming_issues_only_in_closes_lines_passes(form: str) -> None:
    assert closing_line_problems(CLOSES_LINES_ONLY[form], TEMPLATE) == []


# Each `Closes #<n>` line the rendered body does not show, which closes its issue on merge without GitHub linking it.
HIDDEN_CLOSING_LINES = {
    "in a comment the author wrote": ("Closes #1\n<!--\nCloses #12\n-->\n", 3),
    "in a fenced code block": ("Closes #1\n\n```\nCloses #12\n```\n", 4),
    "in an indented code block": ("Closes #1\n\n    Closes #12\n", 3),
}


@pytest.mark.parametrize("form", sorted(HIDDEN_CLOSING_LINES))
def test_a_closes_line_the_rendered_body_does_not_show_fails_as_hidden(form: str) -> None:
    body, line = HIDDEN_CLOSING_LINES[form]
    [problem] = closing_line_problems(body, TEMPLATE)
    assert problem.startswith(f"line {line} of the pull request body is a `Closes #<number>` line hidden from the ")
    assert "'Closes #12'" in problem
    assert "close its issue on merge without GitHub linking it" in problem


def test_the_template_left_as_it_was_passes_its_comments_unread() -> None:
    # The template's first comment names "a Closes #N line of its own": the template's text, not the author's.
    assert "Closes #N" in TEMPLATE
    assert closing_line_problems(TEMPLATE, TEMPLATE) == []
    assert closing_line_problems(TEMPLATE.replace("Closes #\n", "Closes #12\n", 1), TEMPLATE) == []


def test_a_template_comment_the_author_changed_is_theirs_and_read() -> None:
    changed = TEMPLATE.replace("Closes #N line of its own", "Closes #12 line of its own", 1)
    assert changed != TEMPLATE
    assert len(closing_line_problems(changed, TEMPLATE)) == 1


def test_each_problem_names_its_line_and_text() -> None:
    problems = closing_line_problems("Closes #1\n\nSee `fixes #12`.\n\nAnd\tfixes   #13 too.\n", TEMPLATE)
    assert len(problems) == 2
    assert "line 3 " in problems[0]
    assert "'See `fixes #12`.'" in problems[0]
    assert "line 5 " in problems[1]
    assert "'And fixes #13 too.'" in problems[1]


class ClosingLinePolicyCheck(unittest.TestCase):
    def check(self, pull_request: PullRequest) -> None:
        pull_request_body.PullRequestBodyPolicies(
            "test_the_body_names_each_issue_it_closes_in_a_closes_line_of_its_own", pull_request
        ).test_the_body_names_each_issue_it_closes_in_a_closes_line_of_its_own()

    def test_a_stray_reference_fails_and_closes_lines_alone_pass_whatever_the_title(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            stray = write(Path(folder), "stray.md", STRAY_CLOSING_LINES["a backticked reference in prose"])
            for title in (None, write(Path(folder), "title.txt", "docs: say why\n")):
                with self.subTest(title=title), self.assertRaises(AssertionError) as raised:
                    self.check(PullRequest(stray, title))
                self.assertIn("line 3", str(raised.exception))
            self.check(PullRequest(write(Path(folder), "body.md", TEMPLATE), None))

    def test_without_a_body_nothing_is_judged(self) -> None:
        self.check(PullRequest(None, None))
