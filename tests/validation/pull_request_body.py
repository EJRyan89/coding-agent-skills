"""The fix body guard: a pull request titled `fix` fills its body's `## Forms considered` section.

The check holds the section's shape, never its judgment: a fix body has the heading, and under it text of the author's
own, not nothing and not the template's guidance left as it was.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# markdown_sections is imported before validation_support, which puts ejryan89-skill-core on sys.path too, so this
# module states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from markdown_sections import HTML_COMMENT, find_fences
from pull_request_title import TITLE, head_branch
from upgrade_notes import PROFILE_DOC
from validation_support import REPOSITORY_ROOT, PullRequestChecks, markdown_section

from tools.release_notes import ReleaseNotesError

FIX_TYPE = "fix"
FORMS_HEADING = "## Forms considered"
TEMPLATE = ".github/pull_request_template.md"
PROFILE_HEADING = "## Pull requests"
# A heading of the first or second level ends the section; a third-level one, such as a table's caption, is inside it.
SECTION_END = re.compile(r"#{1,2}(?:\s|$)")
# What a line holds that is no text of the author's: list, quote, and table markers, rules, and punctuation.
MARKUP = re.compile(r"[\s\-*+>|#:.,;_=~`'\"()\[\]]+|\d+[.)](?=\s|$)")
# A filler word that stands in for an answer rather than giving one; the comparison ignores case.
FILLERS = frozenset({"none", "n/a", "na", "tbd", "todo", "nothing", "same", "see above", "see below"})


def is_fix(title: str | None, branch: str | None) -> bool:
    """Whether the pull request is a fix: by its title's type when there is a title, otherwise by its branch's kind,
    as in a local run given only --pr-body on a `fix/<name>` branch. Case is ignored, so `Fix: ...` is a fix here as
    well as a title the title check refuses."""
    if title is not None:
        match = TITLE.fullmatch(title.strip())
        return match is not None and match.group("type").lower() == FIX_TYPE
    return branch is not None and "/" in branch and branch.split("/", 1)[0].lower() == FIX_TYPE


def forms_section(body: str) -> str | None:
    """The text under the body's first `## Forms considered` heading, up to the next heading of the first or second
    level, with HTML comments removed; None when the body has no such heading. A heading inside an HTML comment or a
    fenced block is not the section's, and neither ends it."""
    lines = HTML_COMMENT.sub("", body.replace("\r\n", "\n")).split("\n")
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    start = next(
        (index + 1 for index, line in enumerate(lines) if index not in fenced and line.rstrip() == FORMS_HEADING), None
    )
    if start is None:
        return None
    end = next(
        (index for index in range(start, len(lines)) if index not in fenced and SECTION_END.match(lines[index])),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _words(text: str) -> str:
    """The text with its markup dropped and its whitespace collapsed, lowercased, for comparison."""
    return " ".join(MARKUP.sub(" ", text).split()).lower()


def placeholder_forms(template: str) -> tuple[str, set[str]]:
    """The template section's visible text, and the text of each of its comments, which an author may have kept with
    the comment markers dropped, each as _words gives it."""
    section = markdown_section(template.replace("\r\n", "\n"), FORMS_HEADING) or ""
    guidance = {_words(comment[len("<!--") : -len("-->")]) for comment in HTML_COMMENT.findall(section)}
    return _words(HTML_COMMENT.sub("", section)), guidance - {""}


def is_placeholder(words: str, template: str) -> bool:
    """Whether a section's words are still the template's: its visible placeholder, or its guidance standing alone or
    beside that placeholder or a filler."""
    visible, guidance = placeholder_forms(template)
    if visible and words == visible:
        return True
    rest = words
    for comment in guidance:
        rest = rest.replace(comment, " ")
    rest = " ".join(rest.split())
    return rest != words and (not rest or rest == visible or rest in FILLERS)


def forms_problems(body: str, fix: bool, template: str) -> list[str]:
    """What is wrong with a fix body's `## Forms considered` section; a body that is not a fix's owes none."""
    if not fix:
        return []
    section = forms_section(body)
    guide = f'"Pull requests" in {PROFILE_DOC} says what it holds'
    if section is None:
        return [
            f"a pull request titled `{FIX_TYPE}` has no `{FORMS_HEADING}` section in its body; add one from "
            f"{TEMPLATE}, naming the construct, its catalogue with each entry's decision, the callers and skills "
            f"checked, and the regression test that fails with the fix reverted ({guide})"
        ]
    words = _words(section)
    if is_placeholder(words, template):
        return [
            f"the `{FORMS_HEADING}` section of a pull request titled `{FIX_TYPE}` is still the placeholder from "
            f"{TEMPLATE}; replace it with this fix's own forms ({guide})"
        ]
    if not words or words in FILLERS:
        return [
            f"the `{FORMS_HEADING}` section of a pull request titled `{FIX_TYPE}` is empty; write the construct, its "
            f"catalogue with each entry's decision, the callers and skills checked, and the regression test that "
            f"fails with the fix reverted ({guide})"
        ]
    return []


class PullRequestBodyPolicies(PullRequestChecks):
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
