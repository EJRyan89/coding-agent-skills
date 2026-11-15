"""Text a fact line or a table row prints on one line.

A record's titles, bodies, and rationales are free text a reviewer or a person wrote, and Git's names and messages
are text a repository holds; either may hold newlines, runs of whitespace, and control characters. A script that
prints one on a line, or in a Markdown cell or bullet, flattens it here first, so every script flattens the same
characters: each control character, C0, DEL, and C1, so no terminal escape such as ESC reaches the reader's
terminal, and each line separator `str.splitlines()` breaks on, so the text stays on its line.

`flat_text` also makes each run of whitespace one space and trims both ends, for free text. `single_line` keeps the
text's spaces as they are and trims only its end, for a line whose layout is its own, such as an aligned summary row.
`fact_line` builds a tab-separated fact line whose every field goes through `flat_text`, so no field adds a line or a
field of its own. `reason_line` gives what a failed command printed as the reason it failed, on one bounded line.
"""

from __future__ import annotations

import re

LINE_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# The most of a failed command's output a one-line reason keeps; past it, the reason is cut and marked with "...".
REASON_CHARACTERS = 500


def flat_text(text: str) -> str:
    """`text` with each control character and line separator made a space, then each run of whitespace made one
    space, and none at either end."""
    return " ".join(LINE_UNSAFE.sub(" ", text).split())


def fact_line(key: str, *fields: object) -> str:
    """`key`, then each field as `flat_text` gives its text, joined by tabs: one fact on one line, whose fields a
    reader splits on tabs."""
    return "\t".join([key, *(flat_text(str(field)) for field in fields)])


def single_line(text: str) -> str:
    """`text` with each run of control characters and line separators made one space, its other whitespace kept, and
    none at its end."""
    return LINE_UNSAFE.sub(" ", text).rstrip()


def reason_line(output: str, *, end: bool = False) -> str:
    """What a failed command printed, as the reason it gives on one line: `flat_text` of it, and, past
    REASON_CHARACTERS characters, its start followed by "...", or, with `end`, "..." followed by its end, for a command
    that states its error last; "" when it printed nothing but whitespace, which leaves the caller to name the
    command's exit code instead."""
    reason = flat_text(output)
    if len(reason) <= REASON_CHARACTERS:
        return reason
    if end:
        return "..." + reason[-REASON_CHARACTERS:].lstrip()
    return reason[:REASON_CHARACTERS].rstrip() + "..."
