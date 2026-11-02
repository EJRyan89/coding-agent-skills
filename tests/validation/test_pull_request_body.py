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
from markdown_sections import HTML_COMMENT
from pull_request_body import FILLERS, forms_problems, forms_section, is_fix, placeholder_forms
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
    ],
)
def test_a_fix_body_with_a_filled_section_passes(section: str) -> None:
    assert forms_problems(body(section), True, TEMPLATE) == []


def test_the_section_ends_at_the_next_heading_and_a_fenced_heading_does_not_end_it() -> None:
    text = body(f"```\n## Inside\n```\n{FILLED}", after="## Implications\n\nAfter.\n")
    section = forms_section(text)
    assert section is not None
    assert "After." not in section
    assert "## Inside" in section
    assert FILLED.strip() in section
    assert forms_problems(body("", after=f"## Implications\n\n{FILLED}"), True, TEMPLATE)


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
