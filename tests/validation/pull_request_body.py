"""The pull request body guards: the body names each issue it closes in a `Closes #<n>` line of its own, and a pull
request titled `fix` fills its body's `## Forms considered` section.

The closing-line check reads every closing reference GitHub reads, through ejryan89-skill-core's `closing_references`,
on every line of the body, code spans, code blocks, and comments the author wrote included: the squash commit carries
the body into main as written, and GitHub closes an issue a commit message on the default branch names anywhere. A
comment copied from the pull request template, character for character as ejryan89-skill-core's `hidden_comments`
reads one, is the template's text, not the author's, so it is not read. It refuses only a reference GitHub closes an
issue by, so a placeholder such as `Closes #` or `Closes #abc` passes here; the `implement-change` repository skill's
body check refuses those as well.

The forms-considered check holds the section's shape, never its judgment, as ejryan89-skill-core's `forms_considered`
reads it, the reader the `implement-change` repository skill's body check shares: a fix body has the heading, and
under it text of the author's own, not nothing and not the template's guidance left as it was.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# forms_considered is imported before validation_support, which puts ejryan89-skill-core on sys.path too, so this
# module states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from closing_references import CLOSING_REFERENCE
from flat_text import flat_text
from forms_considered import FIX_TYPE, FORMS_HEADING, FormsFault, forms_fault, is_fix_title, placeholder_forms
from pull_request_title import head_branch
from hidden_comments import hidden_spans
from text_lines import text_lines
from upgrade_notes import PROFILE_DOC
from validation_support import REPOSITORY_ROOT, PullRequestChecks, markdown_section

from tools.release_notes import ReleaseNotesError

TEMPLATE = ".github/pull_request_template.md"
PROFILE_HEADING = "## Pull requests"
# The one line the body names an issue it closes on: the keyword, one space, and the number, alone between spaces.
CLOSES_LINE = re.compile(r"Closes #[1-9][0-9]*")


def is_fix(title: str | None, branch: str | None) -> bool:
    """Whether the pull request is a fix: by its title's type when there is a title, otherwise by its branch's kind,
    as in a local run given only --pr-body on a `fix/<name>` branch. Case is ignored, so `Fix: ...` is a fix here as
    well as a title the title check refuses."""
    if title is not None:
        return is_fix_title(title)
    return branch is not None and "/" in branch and branch.split("/", 1)[0].lower() == FIX_TYPE


def forms_problems(body: str, fix: bool, template: str) -> list[str]:
    """What is wrong with a fix body's `## Forms considered` section; a body that is not a fix's owes none."""
    if not fix:
        return []
    fault = forms_fault(body, template)
    guide = f'"Pull requests" in {PROFILE_DOC} says what it holds'
    if fault is FormsFault.MISSING:
        return [
            f"a pull request titled `{FIX_TYPE}` has no `{FORMS_HEADING}` section in its body; add one from "
            f"{TEMPLATE}, naming the construct, its catalogue with each entry's decision, the callers and skills "
            f"checked, and the regression test that fails with the fix reverted ({guide})"
        ]
    if fault is FormsFault.PLACEHOLDER:
        return [
            f"the `{FORMS_HEADING}` section of a pull request titled `{FIX_TYPE}` is still the placeholder from "
            f"{TEMPLATE}; replace it with this fix's own forms ({guide})"
        ]
    if fault is FormsFault.EMPTY:
        return [
            f"the `{FORMS_HEADING}` section of a pull request titled `{FIX_TYPE}` is empty; write the construct, its "
            f"catalogue with each entry's decision, the callers and skills checked, and the regression test that "
            f"fails with the fix reverted ({guide})"
        ]
    return []


def without_template_comments(body: str, template: str) -> str:
    """`body` with each comment GitHub hides that is, character for character, one GitHub hides in `template` made
    spaces, but its line breaks, CR and LF, so each line keeps its number; every comment the author wrote is kept."""
    copied = {template[first:past] for first, past in hidden_spans(template)}
    characters = list(body)
    for first, past in hidden_spans(body):
        if body[first:past] in copied:
            characters[first:past] = re.sub(r"[^\r\n]", " ", body[first:past])
    return "".join(characters)


def closing_line_problems(body: str, template: str) -> list[str]:
    """Each line of the body, as CommonMark ends one, that holds a closing reference GitHub reads and is not a
    `Closes #<n>` line of its own."""
    return [
        f"line {number} of the pull request body closes an issue outside a `Closes #<number>` line of its own: "
        f"{flat_text(line.strip())!r}; GitHub reads a closing reference anywhere in the body, code and comments "
        "included, so name each issue the change resolves on a line of its own and reword any other reference "
        f'without a closing keyword ("Pull requests" in {PROFILE_DOC})'
        for number, line in enumerate(text_lines(without_template_comments(body, template)), start=1)
        if CLOSING_REFERENCE.search(line) and not CLOSES_LINE.fullmatch(line.strip())
    ]


class PullRequestBodyPolicies(PullRequestChecks):
    def test_the_body_names_each_issue_it_closes_in_a_closes_line_of_its_own(self) -> None:
        # Without a body there is nothing to judge, so a local run passes, as the title check does without a title.
        body = self.pull_request.body_text()
        if body is None:
            return
        template = (REPOSITORY_ROOT / TEMPLATE).read_text(encoding="utf-8")
        self.assertEqual([], closing_line_problems(body, template))

    def test_a_fix_body_fills_its_forms_considered_section(self) -> None:
        # Without a body there is nothing to judge, so a local run passes, as the title check does without a title.
        body = self.pull_request.body_text()
        if body is None:
            return
        title = self.pull_request.title_text()
        branch = None
        if title is None:
            try:
                branch = head_branch()
            except ReleaseNotesError as error:
                self.fail(f"the forms-considered check cannot read the checkout's branch: {error}")
        template = (REPOSITORY_ROOT / TEMPLATE).read_text(encoding="utf-8")
        self.assertEqual([], forms_problems(body, is_fix(title, branch), template))

    def test_the_template_and_the_profile_name_the_section(self) -> None:
        problems = []
        template = (REPOSITORY_ROOT / TEMPLATE).read_text(encoding="utf-8")
        if markdown_section(template, FORMS_HEADING) is None:
            problems.append(f"{TEMPLATE} has no `{FORMS_HEADING}` section for a fix to fill")
        elif not all(placeholder_forms(template)):
            problems.append(
                f"{TEMPLATE}'s `{FORMS_HEADING}` section holds no placeholder, or no guidance in a comment, for the "
                "author"
            )
        profile = markdown_section((REPOSITORY_ROOT / PROFILE_DOC).read_text(encoding="utf-8"), PROFILE_HEADING)
        if profile is None or f"`{FORMS_HEADING}`" not in profile:
            problems.append(f"{PROFILE_DOC} does not name `{FORMS_HEADING}` under {PROFILE_HEADING}")
        self.assertEqual([], problems)
