"""Text a fact line or a table row prints on one line.

A record's titles, bodies, and rationales are free text a reviewer or a person wrote, and Git's names and messages
are text a repository holds; either may hold newlines, runs of whitespace, and control characters. A script that
prints one on a line, or in a Markdown cell or bullet, flattens it here first, so every script flattens the same
characters: those `str.split()` treats as whitespace, which include every line separator `str.splitlines()` breaks
on, and every control character, C0, DEL, and C1, so no terminal escape such as ESC reaches the reader's terminal.
"""

from __future__ import annotations

import re

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def flat_text(text: str) -> str:
    """`text` with each control character made a space, then each run of whitespace, line breaks included, made one
    space, and none at either end."""
    return " ".join(CONTROL.sub(" ", text).split())
