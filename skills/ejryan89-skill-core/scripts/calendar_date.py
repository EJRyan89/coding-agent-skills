"""The one reader of a calendar date written YYYY-MM-DD in text a script did not write, such as a record's date.

`date.fromisoformat` alone reads more than that form: the basic form `20261109`, a week date such as `2026-W45-1`, and,
on some interpreters, digits of other scripts, and a regular expression on `\\d` passes every script's digits and
never asks whether the day exists. `parse` takes four, two, and two ASCII digits joined by hyphens that name a day
the calendar has, and gives None for anything else, so `2026-13-45` and the same day in Arabic-Indic digits are no
date.
"""

from __future__ import annotations

import re
from datetime import date

# The written form alone: ASCII digits, which `\d` is not, in the extended form with its hyphens.
DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def parse(text: str) -> date | None:
    """The date `text` writes as YYYY-MM-DD in ASCII digits, or None when it writes another form or a day the calendar
    lacks."""
    if not DATE_FORMAT.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
