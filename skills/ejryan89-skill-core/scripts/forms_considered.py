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
comment GitHub hides is not read, and a `<!--` that code shows is text, within the limits hidden_comments states.

The limit:
- a setext heading is held by a list item as this module's own item reading holds it, which tracks the outermost item
  alone and reads a numbered line under an item's text as a lazy line of it, so after `- a\n2. b` and a blank line, an
  underlined line indented two columns is read as the first item's, where GitHub ends the section at it, and a list
  item that holds only a comment, right under a paragraph, is read as an empty item that cannot interrupt it, so an
  underline under the item's next line is read as the paragraph's, where GitHub reads the line as the item's, since
  markdown_sections offers no reader of the lines its containers hold.

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
    DELIMITER_ROW,
    INTERRUPTING_LIST_MARKER,
    LIST_MARKER,
    ORDERED_MARKER,
    PARAGRAPH,
    QUOTE,
    TABLE_ROW,
    THEMATIC_BREAK,
    indentation,
    is_blank,
    list_item,
    markdown_section,
)
from text_lines import LINE_BREAK

FIX_TYPE = "fix"
FORMS_HEADING = "## Forms considered"
# A pull request title: its type, an optional scope, an optional breaking-change mark, a colon, a space, and text.
TITLE = re.compile(r"(?P<type>[A-Za-z]+)(?P<scope>\([^()\s][^()]*\))?(?P<mark>!?): \S.*")
# The section's own heading: an ATX heading of the second level indented up to three spaces, with any closing hashes.
FORMS_LINE = re.compile(r" {0,3}## +Forms considered(?: +#*)?[ \t]*")
# The deepest heading that ends the section: one of the first or second level does, while a third-level one, such as a
# table's caption, is inside it.
SECTION_LEVEL = 2
# The blocks whose text a setext underline may make a heading: a paragraph, and a `|` line GitHub reads as one.
TEXT_BLOCKS = frozenset({PARAGRAPH, TABLE_ROW})
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
# A line that opens a list item, indented up to three spaces: its marker, and the spaces or tabs after it, or none
# where the item opens empty.
LIST_OPENER = re.compile(rf"(?P<indent> {{0,3}})(?P<marker>{LIST_MARKER})(?P<gap>[ \t]*)(?P<text>.*)")
# Past this many columns after its marker, an item's text is an indented code block, and its content starts one
# column after the marker.
MAX_ITEM_GAP = 4
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
            if headings[index] == SECTION_LEVEL and FORMS_LINE.fullmatch(_visible_line(reading, index))
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


def in_list_items(lines: list[str], inside_fences: set[int]) -> set[int]:
    """The index of each line after a list item's first that the item holds, as CommonMark reads it: a blank line, a
    line indented at least to the column its text starts at, a fence's body, or a lazy line continuing its paragraph.
    An item that opens empty holds no blank line right after its marker's line, since CommonMark lets an item begin
    with one blank line at most, so that line ends it. Only the outermost item is tracked, since a line a nested item
    holds is held by the outer one too."""
    held: set[int] = set()
    content: int | None = None
    # Whether the item opened empty on the line before, so a blank line now ends it.
    opened_empty = False
    # Whether the line before is paragraph text, which a lazy line continues and only some markers interrupt.
    paragraph = False
    for index, line in enumerate(lines):
        if content is not None and opened_empty and is_blank(line) and index not in inside_fences:
            content = None
        opened_empty = False
        if content is not None:
            if index in inside_fences or is_blank(line) or indentation(line) >= content:
                held.add(index)
                nested = " " * (indentation(line) - content) + line.lstrip(" \t")
                paragraph = not is_blank(line) and index not in inside_fences and not NOT_PARAGRAPH.match(nested)
                continue
            if paragraph and not ENDS_PARAGRAPH.match(line):
                held.add(index)
                continue
            content = None
        opener = LIST_OPENER.fullmatch(line)
        if (
            opener is not None
            and not THEMATIC_BREAK.fullmatch(line)
            and list_item(line, interrupting=paragraph) is not None
        ):
            content = _content_column(opener)
            paragraph = not is_blank(opener.group("text"))
            opened_empty = not paragraph
        elif opener is not None and is_blank(opener.group("gap") + opener.group("text")) and not paragraph:
            # An item that opens empty: its content starts one column after the marker.
            content = len(opener.group("indent")) + len(opener.group("marker")) + 1
            paragraph = False
            opened_empty = True
        else:
            paragraph = index not in inside_fences and not NOT_PARAGRAPH.match(line)
    return held


def _content_column(opener: re.Match[str]) -> int:
    """The column a list item's text starts at, which a line must be indented to for the item to hold it."""
    marker_end = len(opener.group("indent")) + len(opener.group("marker"))
    gap = indentation(opener.group("gap") + "x")
    return marker_end + (1 if gap > MAX_ITEM_GAP or is_blank(opener.group("text")) else gap)


def _setext_headings(reading: Reading, start: int) -> list[int]:
    """The first line of the first setext heading of the first or second level at or after `start` that no container
    holds: a paragraph, or a `|` line GitHub reads as one, with an underline right under it, both outside every
    container and code block. A table's rows are no paragraph."""
    walked = reading.walked
    # Which lines a container holds: a quote's, as the walk reads them, and a list item's, by this module's own item
    # reading over the body with its hidden comments gone, as `main` read it, with the walk's code blocks, so a
    # fence-like line an HTML heading's block holds opens none, until markdown_sections offers a reader of the lines
    # its containers hold.
    visible, owners = reading.visible_lines()
    in_items = in_list_items(visible, {owners[line] for line in walked.code})
    contained = {line for line, owner in enumerate(owners) if owner in in_items} | {
        line for line, (kind, _) in walked.held.items() if kind == QUOTE
    }
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
