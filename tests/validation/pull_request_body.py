"""The fix body guard: a pull request titled `fix` fills its body's `## Forms considered` section.

The check holds the section's shape, never its judgment: a fix body has the heading, and under it text of the author's
own, not nothing and not the template's guidance left as it was. It reads the section as GitHub renders it. The heading
is an ATX `## Forms considered`, indented up to three spaces; a setext `Forms considered` over `---`, an HTML `<h2>`,
and a heading inside a quote or a list item are read as missing. The section ends at the next first- or second-level
heading GitHub renders, ATX, setext, or an HTML `<h1>` or `<h2>`, indented up to three spaces; a third-level heading,
and a heading inside a quote, a list item, or a fence, are inside it. A comment GitHub hides is not read, one left
unclosed running to the end of the body, and text that renders as nothing, such as an empty tag, an entity for a space,
or an invisible character, is no text.
"""

from __future__ import annotations

import html
import re
import sys
import unicodedata
from pathlib import Path

# agent_skills and markdown_sections are imported before validation_support, which puts ejryan89-skill-core on sys.path
# too, so this module states the path itself.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from agent_skills import XML_TAG_PATTERN
from markdown_sections import INTERRUPTING_LIST_MARKER, ORDERED_MARKER, find_fences
from pull_request_title import TITLE, head_branch
from upgrade_notes import PROFILE_DOC
from validation_support import REPOSITORY_ROOT, PullRequestChecks, markdown_section

from tools.release_notes import ReleaseNotesError

FIX_TYPE = "fix"
FORMS_HEADING = "## Forms considered"
TEMPLATE = ".github/pull_request_template.md"
PROFILE_HEADING = "## Pull requests"
# The section's own heading: an ATX heading of the second level indented up to three spaces, with any closing hashes.
FORMS_LINE = re.compile(r" {0,3}## +Forms considered(?: +#*)?[ \t]*")
# A heading of the first or second level ends the section; a third-level one, such as a table's caption, is inside it.
# Each is a line indented up to three spaces: an ATX heading, an HTML h1 or h2 block, or, below a paragraph, a setext
# underline, which makes the whole paragraph the heading.
SECTION_END = re.compile(r" {0,3}(?:#{1,2}(?:[ \t]|$)|<h[12](?:[\s>/]|$))", re.IGNORECASE)
SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
# A line that is no paragraph's text: blank, a heading, a list item, a quote, a fence, or a rule. Markdown's whitespace
# is the space and the tab, and its list markers are read by markdown_sections' rules, so a digit outside ASCII or a
# line of no-break spaces is text.
NOT_PARAGRAPH = re.compile(
    rf"[ \t]*$| {{0,3}}(?:#{{1,6}}(?:[ \t]|$)|[-*+](?:[ \t]|$)|{ORDERED_MARKER}(?:[ \t]|$)|>|`{{3,}}|~{{3,}}"
    r"|([-*_])(?:[ \t]*\1){2,}[ \t]*$)"
)
# A line that ends the paragraph above it: as NOT_PARAGRAPH, but a list item only where CommonMark lets one interrupt a
# paragraph, a bullet or an item numbered 1 with text after it; any other ordered line continues the paragraph.
ENDS_PARAGRAPH = re.compile(
    rf"[ \t]*$| {{0,3}}(?:#{{1,6}}(?:[ \t]|$)|{INTERRUPTING_LIST_MARKER}[ \t]+[^ \t]|>|`{{3,}}|~{{3,}}"
    r"|([-*_])(?:[ \t]*\1){2,}[ \t]*$)"
)
# An HTML comment as GitHub hides it: the empty forms `<!-->` and `<!--->`, or up to `-->`, or, left unclosed, to the
# end of the body.
HIDDEN_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:-->|\Z))", re.DOTALL)
# Characters that render as nothing but are neither whitespace nor a format character, which are dropped by their
# category: the Hangul fillers and the blank braille pattern.
INVISIBLE = frozenset("ᅟᅠㅤﾠ⠀")
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
    level, with hidden comments removed; None when the body has no such heading. A heading inside a hidden comment or a
    fenced block is not the section's, and neither ends it."""
    lines = HIDDEN_COMMENT.sub("", body.replace("\r\n", "\n")).split("\n")
    fenced = {index for fence in find_fences(lines) for index in fence.lines()}
    start = next(
        (index + 1 for index, line in enumerate(lines) if index not in fenced and FORMS_LINE.fullmatch(line)), None
    )
    if start is None:
        return None
    end = next((index for index in range(start, len(lines)) if _ends_section(lines, index, start, fenced)), len(lines))
    return "\n".join(lines[start:end])


def _ends_section(lines: list[str], index: int, start: int, fenced: set[int]) -> bool:
    """Whether the line at index opens a first- or second-level heading: an ATX or HTML one, or the first line of a
    paragraph whose setext underline makes it one."""
    if index in fenced:
        return False
    if SECTION_END.match(lines[index]):
        return True
    opens_paragraph = index == start or NOT_PARAGRAPH.match(lines[index - 1]) is not None
    if not opens_paragraph or NOT_PARAGRAPH.match(lines[index]):
        return False
    for later in range(index + 1, len(lines)):
        if later in fenced:
            return False
        if SETEXT_UNDERLINE.fullmatch(lines[later]):
            return True
        if ENDS_PARAGRAPH.match(lines[later]):
            return False
    return False


def _words(text: str) -> str:
    """The text as it renders, with its markup dropped and its whitespace collapsed, lowercased, for comparison: tags
    removed, then entities read, so `&lt;br&gt;` stays the text it shows, then format and invisible characters
    removed."""
    # A tag renders as no text of its own.
    text = html.unescape(XML_TAG_PATTERN.sub(" ", text))
    text = "".join(
        character for character in text if unicodedata.category(character) != "Cf" and character not in INVISIBLE
    )
    return " ".join(MARKUP.sub(" ", text).split()).lower()


def placeholder_forms(template: str) -> tuple[str, set[str]]:
    """The template section's visible text, and the text of each of its comments, which an author may have kept with
    the comment markers dropped, each as _words gives it."""
    section = markdown_section(template.replace("\r\n", "\n"), FORMS_HEADING) or ""
    guidance = {_words(comment.removeprefix("<!--").removesuffix("-->")) for comment in HIDDEN_COMMENT.findall(section)}
    return _words(HIDDEN_COMMENT.sub("", section)), guidance - {""}


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
