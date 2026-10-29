"""The one reader of the issue references a pull request body closes when it merges.

GitHub closes an issue a body names after a closing keyword, `close`, `fix`, or `resolve` in any of their forms and
any case, with an optional colon, in each of the forms it links an issue by: `#<n>` in the same repository,
`<owner>/<repo>#<n>`, and the issue's URL, `https://github.com/<owner>/<repo>/issues/<n>`. Every reader of a closing
reference reads it here, so a form one of them knows reaches all of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import whole_number

# GitHub's closing keywords, as a pattern a caller may build on.
CLOSING_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
# A repository as GitHub names one in a reference: an owner and a name of letters, digits, `_`, `.`, and `-`.
REPOSITORY = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
# A closing keyword and the issue it closes, in each form; the number is the digits GitHub reads, ending at a word end.
CLOSING_REFERENCE = re.compile(
    rf"\b{CLOSING_KEYWORDS}\b:?[ \t]+"
    rf"(?:(?P<repository>{REPOSITORY})?#(?P<number>[0-9]+)"
    rf"|https://github\.com/(?P<linked>{REPOSITORY})/issues/(?P<linked_number>[0-9]+))\b",
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


def closing_references(text: str) -> list[ClosingReference]:
    """Each issue `text` closes, in order; a number past the digit limit names no issue. The caller removes what
    GitHub does not read, such as an HTML comment."""
    found: list[ClosingReference] = []
    for match in CLOSING_REFERENCE.finditer(text):
        number = whole_number.parse(match["number"] or match["linked_number"])
        if number is not None:
            found.append(ClosingReference(match["repository"] or match["linked"], number))
    return found
