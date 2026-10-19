"""Text a fact line or a table row prints on one line.

A record's titles, bodies, and rationales are free text a reviewer or a person wrote, and Git's names and messages
are text a repository holds; either may hold newlines, runs of whitespace, and control characters. A script that
prints one on a line, or in a Markdown cell or bullet, flattens it here first, so every script flattens the same
characters: each control character, C0, DEL, and C1, so no terminal escape such as ESC reaches the reader's
terminal, and each line separator `str.splitlines()` breaks on, so the text stays on its line.

`flat_text` also makes each run of whitespace one space and trims both ends, for free text. `single_line` keeps the
text's spaces as they are and trims only its end, for a line whose layout is its own, such as an aligned summary row.
"""

from __future__ import annotations

import re

LINE_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")


def flat_text(text: str) -> str:
    """`text` with each control character and line separator made a space, then each run of whitespace made one
    space, and none at either end."""
    return " ".join(LINE_UNSAFE.sub(" ", text).split())


def single_line(text: str) -> str:
    """`text` with each run of control characters and line separators made one space, its other whitespace kept, and
    none at its end."""
    return LINE_UNSAFE.sub(" ", text).rstrip()
