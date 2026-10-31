"""The one reader of the issue references a pull request closes when it merges.

GitHub closes an issue a pull request or a commit message on the default branch names after a closing keyword,
`close`, `fix`, or `resolve` in any of their forms and any case, with an optional colon. Its closing-keyword
documentation names two forms of the issue, `#<n>` in the same repository and `<owner>/<repo>#<n>`, and its autolink
documentation two more it links an issue by, `GH-<n>` and the issue's URL, `https://github.com/<owner>/<repo>/issues/<n>`.
This reads all four, and spellings of them GitHub's pages leave open: a keyword or a reference in Markdown emphasis
(`**Closes** #1`), no space after the keyword or its colon (`CLOSES:#1`), a URL in angle brackets, over `http`, through
`www.`, or with no scheme. Every caller refuses what it reads, so a spelling GitHub may not close by costs a refusal
the author can rewrite, while one left unread would let an issue close unseen.

What it does not see: a keyword and its reference on different lines, a reference after the first in a list that
names its keyword once (`Closes #1, #2`; GitHub's pages ask for the keyword before each), `_` or `~` emphasis, a
backslash-escaped `\\#1`, a keyword in another language, and a reference GitHub resolves through a repository's custom
autolink prefix.

A pull request is squash-merged with its title as the commit's title and its body, as written, HTML comments included,
as the message, so `pull_request_references` reads the title and the body together and reads inside comments. Every
reader of a closing reference reads it here, so a form one of them knows reaches all of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import whole_number

# GitHub's closing keywords, as a pattern a caller may build on.
CLOSING_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A repository as GitHub names one in a reference: an owner and a name of letters, digits, `_`, `.`, and `-`.
REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
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
# A closing keyword and the issue it closes, in each form; the number is the digits GitHub reads, ending at a word end.
CLOSING_REFERENCE = re.compile(
    rf"{CLOSING_PREFIX}"
    rf"(?:(?P<repository>{REPOSITORY})?#(?P<number>[0-9]+)"
    rf"|GH-(?P<autolinked>[0-9]+)"
    rf"|{GITHUB_URL}(?P<linked>{REPOSITORY})/issues/(?P<linked_number>[0-9]+))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ClosingReference:
    """An issue a body closes: its repository as the reference names it, None for one in the same repository, and its
    number."""

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
