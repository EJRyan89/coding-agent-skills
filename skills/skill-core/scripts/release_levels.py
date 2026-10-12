"""The release tag pattern and the ordered level words read from the release procedure."""

from __future__ import annotations

import re
from collections.abc import Iterable

from markdown_sections import markdown_section

RELEASING_DOC = "docs/releasing.md"
VERSIONING_HEADING = "## Versioning"
VERSION = re.compile(r"v\d+\.\d+\.\d+")
LEVEL_ITEM = re.compile(r"- \*\*([A-Za-z]+)\.\*\*")
LEADING_LEVEL = re.compile(r"[A-Za-z]+\b|\S+")


def versioning_levels(releasing: str) -> list[str]:
    """The **Levels** list's words under Versioning, lowercased, in the order written."""
    section = markdown_section(releasing, VERSIONING_HEADING) or ""
    _, found, rest = section.partition("**Levels**")
    levels: list[str] = []
    for line in rest.split("\n")[1:] if found else []:
        if match := LEVEL_ITEM.match(line):
            levels.append(match.group(1).lower())
        elif line.strip() and not line.startswith(("- ", "  ")):
            break
    return levels


def level_word(value: str) -> str | None:
    """An entry's leading Level word, or its first invalid token; None means the field is empty."""
    match = LEADING_LEVEL.match(value)
    return match.group(0) if match else None


def largest_level(values: Iterable[str], levels: list[str]) -> str | None:
    """The largest leading Level in the documented order; no entries means no level, not patch."""
    if not levels or len(set(levels)) != len(levels):
        raise ValueError(f"{RELEASING_DOC} must have a nonempty **Levels** list of distinct words")
    ranks = {level: rank for rank, level in enumerate(levels)}
    largest: str | None = None
    for value in values:
        word = level_word(value)
        if word is None:
            raise ValueError("an upgrade-note entry is missing '- Level:' or leaves it empty")
        level = word.lower()
        if level not in ranks:
            raise ValueError(
                f"an upgrade-note entry starts its Level with {word!r}, not one of the Levels in "
                f"{RELEASING_DOC}: {', '.join(levels)}"
            )
        if largest is None or ranks[level] > ranks[largest]:
            largest = level
    return largest
