"""The second-level Markdown section reader shared by repository tools and validation."""

from __future__ import annotations


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
