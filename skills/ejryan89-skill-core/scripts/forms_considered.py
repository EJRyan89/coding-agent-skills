r"""The one reader of a fix pull request's `## Forms considered` section, which validation's body check and the
`implement-change` repository skill's body check share.

A pull request titled `fix` fills the section its template holds with text of the author's own, not nothing and not the
template's guidance left as it was. This reads the section's shape, never its judgment, as GitHub renders it. The
heading is an ATX `## Forms considered`, indented up to three spaces; a setext `Forms considered` over `---`, an HTML
`<h2>`, and a heading inside a quote or a list item are read as missing. The section ends at the next first- or
second-level heading GitHub renders, ATX, setext, or an HTML `<h1>` or `<h2>`, indented up to three spaces; a
third-level heading, and a heading inside a quote, a list item, or a fence, are inside it. A list item holds each line
after its first indented at least to the column its text starts at, and each lazy line continuing its paragraph; one
that opens empty ends at a blank line right after it, since an item may begin with one blank line at most. Text that
renders as nothing, such as an empty tag, an entity for a space, or an invisible character, is no text.

A comment GitHub hides is not read, while a `<!--` inside a fenced or indented code block or an inline code span is text
GitHub shows. A body that holds no `<!--` is read as it is. Otherwise the blocks are read as CommonMark reads them,
before any comment goes: a `<!--` that starts a line, after its quote markers and up to three spaces from where they
end, opens an HTML block that runs to its `-->`, so a fence-like line it holds opens no fence, and the code blocks are
read once, with each such comment read as the thematic break that, like it, ends a paragraph and is no code. The
comments are then read left to right, and the code spans of each line that holds a `<!--`, so the reading takes time in
proportion to the body but for the code-span search on such a line. A comment that opens a block and is left unclosed
runs to the end of the body, as GitHub reads it.

The limits:
- an unclosed `<!--` inside a line also hides the rest of the body, where GitHub shows it as text;
- a `<!--` inside a line whose `-->` falls past its paragraph's end, after a blank line or a fence-like line, hides the
  text up to that `-->`, where GitHub shows the `<!--` as text;
- a `<!--` more than three spaces from the line's start after its quote markers, as at the content of a nested or wide
  list item, or after a list item's marker, is read inside the line, so a fence-like line it holds opens a fence that
  shows every later comment GitHub hides;
- a block comment whose `-->` falls outside the quote or list item that holds it, or a `<!--` after a block comment's
  `-->` on its closing line, ends at that `-->`, where GitHub hides the rest of the body;
- a quote marker indented four or more spaces is read as one, so `    > <!--` opens a comment block where GitHub shows
  the line as code;
- a code span is read by markdown_sections' `COMMONMARK_CODE_SPAN`, which may pair backtick runs of unequal length, so
  ``` ``a`<!-- x -->` ``` hides a comment GitHub shows in code, and whose search, on a line that holds a `<!--`, takes
  time that grows with the square of the line's backtick runs;
- a backslash-escaped backtick is read as opening a span, so ``\`<!-- x -->\` `` keeps a comment GitHub hides;
- a span is read within one line, so a span that crosses a line break shields no `<!--` on its second line, and its
  closing backtick opens a span on that line, so `` `a\nb` <!-- x --> `c` `` keeps a comment GitHub hides;
- `placeholder_forms` reads the template's guidance from every comment its section holds, with no code test, so a
  template whose section shows `<!--` in code yields guidance that is no comment.

A title is a fix's when it opens with the type `fix`, in any case, an optional scope, an optional `!`, a colon, and a
space, by the grammar `TITLE` reads every pull request title with.
"""

from __future__ import annotations

import html
import re
import unicodedata
from bisect import bisect_right
from enum import Enum

from agent_skills import XML_TAG_PATTERN
from markdown_sections import (
    COMMONMARK_CODE_SPAN,
    INTERRUPTING_LIST_MARKER,
    LIST_MARKER,
    ORDERED_MARKER,
    THEMATIC_BREAK,
    code_block_lines,
    find_fences,
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
# A heading of the first or second level ends the section; a third-level one, such as a table's caption, is inside it.
# Each is a line indented up to three spaces: an ATX heading, an HTML h1 or h2 block, or, below a paragraph, a setext
# underline, which makes the whole paragraph the heading. CommonMark starts an HTML block only where the tag name is
# followed by a space, a tab, the line's end, `>`, or `/>`, so any other Unicode space leaves the line text.
SECTION_END = re.compile(r" {0,3}(?:#{1,2}(?:[ \t]|$)|<h[12](?:[ \t>]|/>|$))", re.IGNORECASE)
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
# A line that opens a list item, indented up to three spaces: its marker, and the spaces or tabs after it, or none
# where the item opens empty.
LIST_OPENER = re.compile(rf"(?P<indent> {{0,3}})(?P<marker>{LIST_MARKER})(?P<gap>[ \t]*)(?P<text>.*)")
# Past this many columns after its marker, an item's text is an indented code block, and its content starts one
# column after the marker.
MAX_ITEM_GAP = 4
# An HTML comment as GitHub hides it: the empty forms `<!-->` and `<!--->`, or up to `-->`, or, left unclosed, to the
# end of the body.
HIDDEN_COMMENT = re.compile(r"<!--(?:-?>|.*?(?:-->|\Z))", re.DOTALL)
COMMENT_OPENER = "<!--"
# The blockquote markers a line opens with, matched greedily once and never backtracked into, which a comment block's
# later lines keep so the quote holds them.
QUOTE_PREFIX = re.compile(r"(?:[ \t]*>[ \t]?)*")
# What follows a line's quote markers when its `<!--` opens an HTML block, as CommonMark starts one: up to three spaces.
COMMENT_BLOCK_START = re.compile(r"[ ]{0,3}<!--")
# What a comment block's first line is read as for the code reading: a thematic break, which, as the HTML block does,
# ends a paragraph and is no code.
COMMENT_BLOCK_STAND_IN = "***"
# The lines a fence's closer is first looked for in, doubled until it is found, so a long fence is read once over.
FENCE_WINDOW = 32
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
    level, with hidden comments removed; None when the body has no such heading. A heading inside a hidden comment or a
    fenced block is not the section's, and neither ends it."""
    lines = LINE_BREAK.split(without_hidden_comments(body))
    fences = find_fences(lines)
    fenced = {index for fence in fences for index in fence.lines()}
    # A line inside a list item is the item's, so a heading there neither opens the section nor ends it.
    held = fenced | in_list_items(lines, {index for fence in fences for index in fence.lines()[1:]})
    start = next(
        (index + 1 for index, line in enumerate(lines) if index not in held and FORMS_LINE.fullmatch(line)), None
    )
    if start is None:
        return None
    end = next((index for index in range(start, len(lines)) if _ends_section(lines, index, start, held)), len(lines))
    return "\n".join(lines[start:end])


def without_hidden_comments(text: str) -> str:
    """`text` without each comment GitHub hides, read left to right: one whose `<!--` opens outside every code block
    and code span, up to its `-->` wherever that falls, or to the end of the text. A `<!--` that code shows is text."""
    if COMMENT_OPENER not in text:
        return text
    lines = LINE_BREAK.split(text)
    starts = [0, *(match.end() for match in LINE_BREAK.finditer(text))]
    code = code_block_lines(_comment_blocks_read_as_breaks(text, lines, starts))
    kept: list[str] = []
    position = 0
    # The next code span on the line the last `<!--` was on, read up to that line's end, which `searched` holds.
    span: re.Match[str] | None = None
    searched = -1
    while (opener := text.find(COMMENT_OPENER, position)) != -1:
        line = bisect_right(starts, opener) - 1
        line_end = starts[line + 1] if line + 1 < len(starts) else len(text)
        if searched != line_end or (span is not None and span.start() < position):
            span = COMMONMARK_CODE_SPAN.search(text, max(position, starts[line]), line_end)
            searched = line_end
        while span is not None and span.end() <= opener:
            span = COMMONMARK_CODE_SPAN.search(text, span.end(), line_end)
        if span is not None and span.start() < opener:
            # A code span that holds the `<!--` shows it, and every character up to the span's end.
            kept.append(text[position : span.end()])
            position = span.end()
            continue
        if line in code:
            kept.append(text[position : opener + 1])
            position = opener + 1
            continue
        kept.append(text[position:opener])
        position = _comment_end(text, opener)
    kept.append(text[position:])
    return "".join(kept)


def _comment_blocks_read_as_breaks(text: str, lines: list[str], starts: list[int]) -> list[str]:
    """The lines with each comment that opens an HTML block outside a fence read as a thematic break on its first
    line and blank lines after it, inside the blockquotes that hold each line, so the code reading meets no fence-like
    line the comment holds."""
    read = list(lines)
    index = 0
    while index < len(lines):
        if find_fences([lines[index]]):
            index = _fence_stop(lines, index)
            continue
        quotes = QUOTE_PREFIX.match(lines[index])
        block = COMMENT_BLOCK_START.match(lines[index], quotes.end() if quotes is not None else 0)
        if block is None:
            index += 1
            continue
        opener = starts[index] + block.end() - len(COMMENT_OPENER)
        last = bisect_right(starts, max(_comment_end(text, opener) - 1, opener)) - 1
        read[index] = lines[index][: block.end() - len(COMMENT_OPENER)] + COMMENT_BLOCK_STAND_IN
        for later in range(index + 1, last + 1):
            held = QUOTE_PREFIX.match(lines[later])
            read[later] = held.group() if held is not None else ""
        index = last + 1
    return read


def _comment_end(text: str, opener: int) -> int:
    """The index just past the comment whose `<!--` is at `opener`: past its `-->`, or the end of the text, where the
    pattern ends every comment left unclosed."""
    comment = HIDDEN_COMMENT.match(text, opener)
    return len(text) if comment is None else comment.end()


def _fence_stop(lines: list[str], opener: int) -> int:
    """The index just past the fence that opens at `opener`, read by find_fences over a window from the opener that
    doubles until the fence ends inside it or the window reaches the last line."""
    window = FENCE_WINDOW
    while True:
        fence = find_fences(lines[opener : opener + window])[0]
        stop = fence.lines().stop
        if fence.closer is not None or stop < window or opener + window >= len(lines):
            return opener + stop
        window *= 2


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


def _ends_section(lines: list[str], index: int, start: int, held: set[int]) -> bool:
    """Whether the line at index opens a first- or second-level heading: an ATX or HTML one, or the first line of a
    paragraph whose setext underline makes it one. A line a fence or a list item holds opens none."""
    if index in held:
        return False
    if SECTION_END.match(lines[index]):
        return True
    opens_paragraph = index == start or NOT_PARAGRAPH.match(lines[index - 1]) is not None
    if not opens_paragraph or NOT_PARAGRAPH.match(lines[index]):
        return False
    for later in range(index + 1, len(lines)):
        if later in held:
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
    return _words(without_hidden_comments(section)), guidance - {""}


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
