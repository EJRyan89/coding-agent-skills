r"""The one reader of a fix pull request's `## Forms considered` section, which validation's body check and the
`implement-change` repository skill's body check share.

A pull request titled `fix` fills the section its template holds with text of the author's own, not nothing and not the
template's guidance left as it was. This reads the section's shape, never its judgment, as GitHub renders it. The
heading is an ATX `## Forms considered` no container holds, indented up to three spaces; a setext `Forms considered`
over `---`, an HTML `<h2>`, and a heading inside a quote or a list item are read as missing. The section ends at the
next first- or second-level heading GitHub renders that no container holds: an ATX heading, a setext heading, or an
HTML `<h1>` or `<h2>`, indented up to three spaces; a third-level heading, and a heading inside a quote, a list item, or
a code block, are inside it. Text that renders as nothing, such as an empty tag, an entity for a space, or an invisible
character, is no text.

The body's comments, its code blocks, and its headings are read by hidden_comments, the one reader of the comments
GitHub hides, whose walk of the body's blocks says which headings no container holds and which lines are code. A
comment GitHub hides is not read, and a `<!--` that code shows is text, within the limits hidden_comments states. Which
lines a quote or a list item holds, lazy lines included, is markdown_sections' `contained_lines`, read from the same
lines as that walk, so every heading that ends the section, ATX, setext, or HTML, is one no container holds by one
reading, within the limits markdown_sections' walk states.

The limit:
- an underline under an HTML block other than a comment's or an `<h1>` or `<h2>`, such as a `<div>` closed by
  `</div>`, makes the block's lines a setext heading that ends the section, since hidden_comments reads such a block
  as a paragraph, where GitHub reads the underline as the block's.

A title is a fix's when it opens with the type `fix`, in any case, an optional scope, an optional `!`, a colon, and a
space, by the grammar `TITLE` reads every pull request title with.
"""

from __future__ import annotations

import html
import re
import unicodedata
from enum import Enum

from agent_skills import XML_TAG_PATTERN
from hidden_comments import COMMENT_CLOSER, COMMENT_OPENER, Reading, read_comments
from markdown_sections import (
    ATX_HEADING,
    DELIMITER_ROW,
    ORDERED_MARKER,
    PARAGRAPH,
    TABLE_ROW,
    contained_lines,
    markdown_section,
)
from text_lines import LINE_BREAK

FIX_TYPE = "fix"
FORMS_HEADING = "## Forms considered"
# A pull request title: its type, an optional scope, an optional breaking-change mark, a colon, a space, and text.
TITLE = re.compile(r"(?P<type>[A-Za-z]+)(?P<scope>\([^()\s][^()]*\))?(?P<mark>!?): \S.*")
# The section's own heading's text, as markdown_sections' ATX_HEADING reads it, past its marks and any closing ones.
FORMS_TITLE = FORMS_HEADING.removeprefix("## ")
# The deepest heading that ends the section: one of the first or second level does, while a third-level one, such as a
# table's caption, is inside it.
SECTION_LEVEL = 2
# The blocks whose text a setext underline may make a heading: a paragraph, and a `|` line GitHub reads as one.
TEXT_BLOCKS = frozenset({PARAGRAPH, TABLE_ROW})
# A setext heading's underline, which makes the paragraph above it, in the same container, a heading.
SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*")
# Characters that render as nothing but are neither whitespace nor a format character, which are dropped by their
# category: the Hangul fillers and the blank braille pattern.
INVISIBLE = frozenset("ᅟᅠㅤﾠ⠀")
# What a line holds that is no text of the author's: list, quote, and table markers, rules, and punctuation. A list
# marker is read by markdown_sections' rule, followed by a space, a tab, or the line's end. The class's `\s` is not a
# block rule: every Unicode space renders blank, so it is no text of the author's either.
MARKUP = re.compile(rf"[\s\-*+>|#:.,;_=~`'\"()\[\]]+|{ORDERED_MARKER}(?=[ \t]|$)")
# A filler word that stands in for an answer rather than giving one; the comparison ignores case.
FILLERS = frozenset({"none", "n/a", "na", "tbd", "todo", "nothing", "same", "see above", "see below"})


class FormsFault(Enum):
    """What is wrong with a fix body's section: no section, the template's placeholder, or no text of the author's."""

    MISSING = "missing"
    PLACEHOLDER = "placeholder"
    EMPTY = "empty"


def is_fix_title(title: str) -> bool:
    """Whether a pull request title's type is `fix`, in any case, so `Fix: ...` is a fix here as well as a title the
    title check refuses."""
    match = TITLE.fullmatch(title.strip())
    return match is not None and match.group("type").lower() == FIX_TYPE


def forms_section(body: str) -> str | None:
    """The text under the body's first `## Forms considered` heading, up to the next heading of the first or second
    level, with hidden comments removed; None when the body has no such heading. A heading inside a hidden comment, a
    code block, a list item, or a quote is not the section's, and none ends it."""
    reading = read_comments(body)
    # A heading a hidden comment holds, as one opened in a quote or a list item whose `-->` falls past it, is hidden.
    headings = {
        index: level
        for index, level in reading.walked.headings.items()
        if index not in reading.stand_ins and not reading.hides(index)
    }
    start = next(
        (
            index + 1
            for index in sorted(headings)
            if headings[index] == SECTION_LEVEL
            and (heading := ATX_HEADING.fullmatch(_visible_line(reading, index))) is not None
            and heading.group("text") == FORMS_TITLE
        ),
        None,
    )
    if start is None:
        return None
    ends = [index for index, level in headings.items() if index >= start and level <= SECTION_LEVEL]
    end = min([*ends, *_setext_headings(reading, start), len(reading.lines)])
    if end == start:
        return ""
    stop = reading.starts[end - 1] + len(reading.lines[end - 1])
    return "\n".join(LINE_BREAK.split(reading.visible(reading.starts[start], stop)))


def _visible_line(reading: Reading, index: int) -> str:
    """The line at `index` without the hidden comments in it."""
    start = reading.starts[index]
    return reading.visible(start, start + len(reading.lines[index]))


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
    """The template section's visible text, and the text of each comment GitHub hides in it, read as a body's are,
    which an author may have kept with the comment markers dropped, each as _words gives it."""
    section = markdown_section(template.replace("\r\n", "\n"), FORMS_HEADING) or ""
    reading = read_comments(section)
    comments = (section[first:past] for first, past in reading.hidden)
    guidance = {_words(comment.removeprefix(COMMENT_OPENER).removesuffix(COMMENT_CLOSER)) for comment in comments}
    return _words(reading.visible(0, len(section))), guidance - {""}


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


def forms_fault(body: str, template: str) -> FormsFault | None:
    """What is wrong with the body's `## Forms considered` section as a fix's, judged against `template`, the pull
    request template the body was written from; None when the section holds text of the author's own."""
    section = forms_section(body)
    if section is None:
        return FormsFault.MISSING
    words = _words(section)
    if is_placeholder(words, template):
        return FormsFault.PLACEHOLDER
    if not words or words in FILLERS:
        return FormsFault.EMPTY
    return None


def _setext_headings(reading: Reading, start: int) -> list[int]:
    """The first line of the first setext heading of the first or second level at or after `start` that no container
    holds: a paragraph, or a `|` line GitHub reads as one, with an underline right under it, both outside every
    container and code block. A table's rows are no paragraph."""
    walked = reading.walked
    # Which lines a quote or a list item holds, read from the lines the walk read, where each comment and HTML heading
    # that opens a block stands in as one, so a fence-like line such a block holds opens none.
    contained = contained_lines(reading.read)
    for underline in range(start + 1, len(reading.lines)):
        if underline in walked.code or underline in contained or reading.hides(underline):
            continue
        if not SETEXT_UNDERLINE.fullmatch(reading.lines[underline]):
            continue
        first = underline - 1
        while first >= start and first not in contained and walked.held.get(first, ("", True))[0] in TEXT_BLOCKS:
            if DELIMITER_ROW.fullmatch(reading.read[first]) and walked.held.get(first - 1, ("", True))[0] == TABLE_ROW:
                first = underline
                break
            first -= 1
        if first + 1 < underline:
            return [first + 1]
    return []
