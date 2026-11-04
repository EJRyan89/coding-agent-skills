"""The one reader of a whole number from text a script did not write, such as an argument, a label, or a line.

`int()` reads more than a whole number's ASCII digits: a sign, surrounding whitespace, underscores, and every Unicode
decimal digit, and `str.isdigit()`, the test that usually guards it, also passes a digit such as `²` that `int()`
then refuses. Past the interpreter's integer-string limit `int()` raises a plain `ValueError`, which names no input,
and below it converting a long run of digits still takes time that grows faster than its length. `read` takes ASCII
digits alone, at most `DIGIT_LIMIT` of them or the lower limit its caller gives, and raises the caller's own error,
which names the field, for anything else; `parse` gives None instead, for a caller to whom such text is no number.
`argument` is the argparse `type` of every option that takes a whole number, which `type=int` would read as `int()`
does: it reads through `read` within the bounds its caller gives and refuses anything else with a usage error naming
the value. `json_reader` refuses an integer past the same limit, so a number read from JSON and one read from text
are bounded alike.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable

# The interpreter's default integer-string limit, fixed here so neither reader depends on how it is configured.
DIGIT_LIMIT = 4300


def parse(text: str, *, limit: int = DIGIT_LIMIT) -> int | None:
    """The whole number `text` holds, as one or more ASCII digits and nothing else, or None when it holds anything
    else or more digits than `limit`, which `DIGIT_LIMIT` caps."""
    if not text.isascii() or not text.isdigit() or len(text) > min(limit, DIGIT_LIMIT):
        return None
    try:
        return int(text)
    except ValueError:  # the interpreter's own integer-string limit, configured lower than DIGIT_LIMIT
        return None


def read(text: str, refused: Exception, *, limit: int = DIGIT_LIMIT) -> int:
    """The whole number `parse` reads in `text`, or `refused` raised where it reads none."""
    number = parse(text, limit=limit)
    if number is None:
        raise refused
    return number


def argument(minimum: int = 0, maximum: int | None = None) -> Callable[[str], int]:
    """The argparse `type` of an option that takes a whole number from `minimum` to `maximum`, or from `minimum` up
    when `maximum` is None. It reads the value through `read`, with a leading `-` only where `minimum` is below zero,
    at most as many digits as the larger bound has, and refuses anything else, or a number outside the bounds, with an
    `argparse.ArgumentTypeError` naming the value and the bounds, which argparse ends in a usage error."""
    if maximum is not None and maximum < minimum:
        raise ValueError(f"a whole number argument's maximum {maximum} is below its minimum {minimum}")
    noun = "an integer" if minimum < 0 else "a whole number"
    bounds = f"from {minimum} to {maximum}" if maximum is not None else f"of at least {minimum}"
    limit = DIGIT_LIMIT if maximum is None else len(str(max(abs(minimum), abs(maximum))))

    def whole_number_argument(value: str) -> int:
        refused = argparse.ArgumentTypeError(f"must be {noun} {bounds} in ASCII digits, not {value!r}")
        negative = minimum < 0 and value.startswith("-")
        number = read(value[1:] if negative else value, refused, limit=limit)
        if negative:
            number = -number
        if number < minimum or (maximum is not None and number > maximum):
            raise refused
        return number

    return whole_number_argument
