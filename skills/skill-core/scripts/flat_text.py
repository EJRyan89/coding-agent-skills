"""Text a fact line or a table row prints on one line.

A record's titles, bodies, and rationales are free text a reviewer or a person wrote, and may hold newlines and runs
of whitespace. A script that prints one on a line, or in a Markdown cell or bullet, flattens it here first, so every
script flattens the same characters: those `str.split()` treats as whitespace.
"""

from __future__ import annotations


def flat_text(text: str) -> str:
    """`text` with each run of whitespace, line breaks included, made one space, and none at either end."""
    return " ".join(text.split())
