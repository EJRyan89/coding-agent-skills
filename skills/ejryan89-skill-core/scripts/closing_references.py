"""The one reader of the issue references a pull request closes when it merges.

GitHub closes an issue a pull request or a commit message on the default branch names after a closing keyword,
`close`, `fix`, or `resolve` in any of their forms and any case, with an optional colon. Its closing-keyword
documentation names two forms of the issue, `#<n>` in the same repository and `<owner>/<repo>#<n>`, and says a keyword
before a pull request's reference closes that pull request; its autolink documentation adds `GH-<n>` and the URL of an
issue or a pull request, `https://github.com/<owner>/<repo>/issues/<n>` or `.../pull/<n>`. This reads each of them, and
spellings of them GitHub's pages leave open: a keyword or a reference in Markdown emphasis (`**Closes** #<n>`), no
space after the keyword or its colon (`CLOSES:#<n>`), a URL in angle brackets, over `http`, through `www.`, or with no
scheme. Every caller refuses what it reads, so a spelling GitHub may not close by costs a refusal the author can
rewrite, while one left unread would let an issue close unseen. `KEYWORDS` holds the keywords, and the module's suite
holds them to GitHub's documented list and every documented keyword and reference form to being read, refused as a
keyword before the start of a reference, or named in `NOT_READ`.

What it does not see, each spelling with why in `NOT_READ`: a keyword and its reference on different lines, `_` or `~`
emphasis, a backslash-escaped `\\#<n>`, a keyword in another language, a reference GitHub resolves through a
repository's custom autolink prefix, a reference written as a Markdown link's text, and a URL through
`redirect.github.com`, which GitHub's autolink page offers so that a URL makes no backlink. It also does not see a
reference after the first in a list that names its keyword once (`Closes #<n>, #<m>`; GitHub's pages ask for the
keyword before each). `stray_closing_lines` also does not see a closing reference inside a comment copied character for
character from the pull request template, which the squash commit still carries.

A pull request is squash-merged with its title as the commit's title and its body, as written, HTML comments included,
as the message, so `pull_request_references` reads the title and the body together and reads inside comments. Every
reader of a closing reference reads it here, so a form one of them knows reaches all of them.

A pull request names each issue it closes in a `Closes #<n>` line of its own, so the closes it states are the closes it
makes. `title_holds_closing_reference` and `stray_closing_lines` hold that rule for every checker of a title or a body,
`implement-change`'s `pull_request.py check` and validation's title and body checks alike. A closing keyword before
the start of a reference counts there even where no issue follows, as in `Closes #abc`, so a placeholder or a misspelt
reference is refused rather than read as no reference. A body line is read with each comment copied, character for
character, from the pull request template blanked, since that text is the template's guidance, not the author's; every
comment the author wrote, and every code span and code block, is read, as GitHub reads them.

A `Closes #<n>` line of its own is refused too where the rendered body does not show it: inside a comment the author
wrote, which GitHub hides, or a fenced or indented code block, which GitHub shows as code. The squash commit still
carries it, so the issue closes on merge, but GitHub links no reference it does not render as one, so the project board,
which moves an issue by that link, never sees the close; this repository's history holds an issue a squash commit shut
from a code span, never linked to its pull request. GitHub's renderer links the line everywhere else it stands alone: in
a paragraph, under a setext underline, and inside an HTML `<details>` or `<div>` block. hidden_comments reads where a
comment hides it and where a code block holds it, and its stated limits are this reading's. It also does not see a line
inside an HTML `<pre>`, `<code>`, or `<a>` block, which GitHub links nothing in, and it refuses as hidden a line in a
`math` fence or in a footnote definition's continuation indented four spaces, both of which GitHub links.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

import whole_number
from github_remote import OWNER_AND_NAME
from hidden_comments import Reading, hidden_spans, read_comments
from text_lines import text_lines

# GitHub's closing keywords, each read in any case. The module's suite holds them to the list GitHub's closing-keyword
# page documents, and every keyword and reference form GitHub's pages document to being read, refused as a keyword
# before the start of a reference, or named in NOT_READ with why.
KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves", "resolved")
# The keywords as a pattern a caller may build on.
CLOSING_KEYWORDS = rf"(?:{'|'.join(KEYWORDS)})"
# Each spelling of a closing reference the reader neither reads nor refuses, with why; the docstring states them.
NOT_READ = {
    "Closes\n#1": "a keyword and its reference on different lines: GitHub's pages write them on one, and read across a "
    "line break, a keyword ending a line of prose would take the next line's number",
    "_Closes_ #1": "`_` emphasis around the keyword, which GitHub's pages never write; the reader reads `*` emphasis",
    "~~Closes~~ #1": "a struck-out keyword, which GitHub's pages never write and its author has struck out",
    "Closes \\#1": "a backslash-escaped number, which GitHub's pages never write",
    "Schließt #1": "a keyword in another language: GitHub's closing-keyword page lists English keywords only",
    "Closes JIRA-1": "a custom autolink prefix, which each repository sets for itself and points at another tracker",
    "Closes [#1](https://github.com/o/r/issues/1)": "a reference written as a Markdown link's text, which GitHub's "
    "pages never write",
    "Closes https://redirect.github.com/o/r/issues/1": "a `redirect.github.com` URL, which GitHub's autolink page "
    "offers so that a URL makes no backlink, the link a closing keyword makes, and through which Dependabot writes "
    "the issue links of the changelogs it quotes",
}
# A repository as GitHub names one in a reference: an owner and a name of github_remote's repository characters.
REPOSITORY = OWNER_AND_NAME
# What may come between a keyword and its reference: Markdown's `*` emphasis, spaces or tabs, one optional colon, and
# a URL's opening angle bracket. The colon splits the run into two classes that cannot both take one character, so a
# long run after a keyword is read once, never backtracked through.
SEPARATOR = r"[* \t]*(?::[* \t]*)?<?"
# A closing keyword standing as a word, then the separator. A letter or digit on either side makes it part of another
# word.
CLOSING_PREFIX = rf"(?<![A-Za-z0-9]){CLOSING_KEYWORDS}(?![A-Za-z0-9]){SEPARATOR}"
# The URL of a GitHub page, with or without its scheme and `www.`.
GITHUB_URL = r"(?:https?://)?(?:www\.)?github\.com/"
# How a reference starts, so a caller can refuse a line that starts one and names no issue this reads, such as
# `Closes #abc`; `GH-` starts one only before a digit, so prose such as `fixes GH-hosted runners` starts none.
REFERENCE_START = rf"(?:(?:{REPOSITORY})?#|GH-[0-9]|{GITHUB_URL})"
# A closing keyword and the issue it closes, in each form, a pull request's URL among them; the number is the digits
# GitHub reads, ending at a word end.
CLOSING_REFERENCE = re.compile(
    rf"{CLOSING_PREFIX}"
    rf"(?:(?P<repository>{REPOSITORY})?#(?P<number>[0-9]+)"
    rf"|GH-(?P<autolinked>[0-9]+)"
    rf"|{GITHUB_URL}(?P<linked>{REPOSITORY})/(?:issues|pull)/(?P<linked_number>[0-9]+))\b",
    re.IGNORECASE,
)
# A closing keyword and the start of an issue reference, which a title may not hold anywhere and a body line may not
# open with, even where what follows names no issue CLOSING_REFERENCE reads.
CLOSING_START = re.compile(rf"(?i){CLOSING_PREFIX}{REFERENCE_START}")
CLOSING_LINE = re.compile(rf"(?i)\s*{CLOSING_PREFIX}{REFERENCE_START}")
# The template's closing line with no issue yet, a placeholder, not a closing reference.
BARE_CLOSING_LINE = re.compile(rf"(?i){CLOSING_KEYWORDS}\b:?\s+#")
# The one line a body names an issue it closes on: the keyword, one space, and the number from 1, alone.
CLOSES_ONE = re.compile(r"Closes #([1-9][0-9]*)")


@dataclass(frozen=True)
class ClosingReference:
    """An issue, or a pull request, a body closes: its repository as the reference names it, None for one in the same
    repository, and its number."""

    repository: str | None
    number: int

    def in_repository(self, repository: str) -> bool:
        """Whether the reference closes an issue of `repository`, `owner/name`, as GitHub compares names, in any
        case; a reference that names no repository closes one of the repository the body is in."""
        return self.repository is None or self.repository.casefold() == repository.casefold()


def reference_digits(match: re.Match[str]) -> str:
    """The digits of the issue number a CLOSING_REFERENCE match names, in whichever form it names it."""
    return match["number"] or match["autolinked"] or match["linked_number"]


def closing_references(text: str) -> list[ClosingReference]:
    """Each issue `text` closes, in order; a number past the digit limit names no issue."""
    found: list[ClosingReference] = []
    for match in CLOSING_REFERENCE.finditer(text):
        number = whole_number.parse(reference_digits(match))
        if number is not None:
            found.append(ClosingReference(match["repository"] or match["linked"], number))
    return found


def pull_request_text(title: str | None, body: str | None) -> str:
    """The text the squash commit of a pull request carries, its title and its body as written, each on lines of its
    own, so a keyword ending one never reaches a reference starting the other."""
    return "\n".join(part for part in (title, body) if part is not None)


def pull_request_references(title: str | None, body: str | None) -> list[ClosingReference]:
    """Each issue a pull request closes when it is squash-merged, by its title or its body, HTML comments included."""
    return closing_references(pull_request_text(title, body))


def title_holds_closing_reference(title: str) -> bool:
    """Whether a pull request title holds a closing keyword before the start of an issue reference, which the squash
    commit's title would carry, so the issue belongs in a `Closes #<n>` line of the body instead."""
    return CLOSING_START.search(title) is not None


def blank_spans(text: str, spans: Sequence[tuple[int, int]]) -> str:
    """`text` with the characters of each span, in order, but its line breaks, CR and LF, made spaces, so each line
    keeps its number."""
    kept: list[str] = []
    position = 0
    for first, past in spans:
        kept.extend((text[position:first], re.sub(r"[^\r\n]", " ", text[first:past])))
        position = past
    kept.append(text[position:])
    return "".join(kept)


def template_comment_spans(body: str, template: str) -> list[tuple[int, int]]:
    """The span of each comment GitHub hides in `body` that is, character for character, one GitHub hides in
    `template`: a placeholder the template left."""
    comments = {template[first:past] for first, past in hidden_spans(template)}
    return [(first, past) for first, past in hidden_spans(body) if body[first:past] in comments]


class StrayLine(NamedTuple):
    """A body line that names an issue it closes where a pull request may not: its number from 1 where CommonMark ends
    a line, its text without the spaces around it, and whether it is a `Closes #<n>` line of its own that the rendered
    body does not show, which closes its issue on merge without GitHub linking it."""

    number: int
    text: str
    hidden: bool


def hidden_from_render(reading: Reading, index: int, column: int) -> bool:
    """Whether the rendered body does not show the character at `column` of the line at `index`: a comment GitHub
    hides holds it, or a code block holds its line."""
    if index in reading.walked.code:
        return True
    position = reading.starts[index] + column
    span = bisect_right(reading.hidden, (position, len(reading.text) + 1)) - 1
    return span >= 0 and reading.hidden[span][0] <= position < reading.hidden[span][1]


def stray_closing_lines(body: str, template: str) -> list[StrayLine]:
    """Each line of `body` that holds a closing reference, or opens with a closing keyword before the start of one,
    and is neither a `Closes #<n>` line of its own nor the template's closing line with no issue, and each `Closes
    #<n>` line of its own that the rendered body does not show, marked hidden; comments copied from `template` are not
    read."""
    read = text_lines(blank_spans(body, template_comment_spans(body, template)))
    found: list[StrayLine] = []
    reading: Reading | None = None
    for index, line in enumerate(read):
        text = line.strip()
        if CLOSES_ONE.fullmatch(text):
            # The body is read for its comments and blocks only once a `Closes #<n>` line asks where it stands.
            if reading is None:
                reading = read_comments(body)
            if hidden_from_render(reading, index, len(line) - len(line.lstrip())):
                found.append(StrayLine(index + 1, text, hidden=True))
        elif not BARE_CLOSING_LINE.fullmatch(text) and (CLOSING_REFERENCE.search(line) or CLOSING_LINE.match(line)):
            found.append(StrayLine(index + 1, text, hidden=False))
    return found
