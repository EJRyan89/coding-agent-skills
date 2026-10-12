"""The Markdown readers shared by skills, repository tools, and validation: the second-level section reader and the
inline code span pattern."""

from __future__ import annotations

import re

# An inline code span on one line, with its text as the first group, as a skill reads commands or cited paths from it.
CODE_SPAN = re.compile(r"`([^`\n]+)`")


def markdown_section(text: str, heading: str) -> str | None:
    """The lines under the first exact heading, ignoring its trailing whitespace, up to the next `## ` heading.

    CRLF becomes LF; body whitespace and comments are otherwise preserved. None means the heading is absent.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    start = next((index + 1 for index, line in enumerate(lines) if line.rstrip() == heading), None)
    if start is None:
        return None
    end = next((index for index in range(start, len(lines)) if lines[index].startswith("## ")), len(lines))
    return "\n".join(lines[start:end])
