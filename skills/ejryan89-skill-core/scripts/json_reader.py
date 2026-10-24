"""The one parse of JSON a file or a command gave, refusing what JSON leaves undefined.

Python's reader keeps the last value of a key an object repeats, which RFC 8259 leaves to each reader, so another
reader of the same file may read the first; and it reads `NaN`, `Infinity`, and `-Infinity`, which JSON does not
define and other readers refuse, and which then reach code that compares numbers. `loads` refuses both as
`JsonRefused`, a `ValueError`, so a reader that already turns a `ValueError` from its parse into its own refusal
refuses these alike. It bounds nothing else: what `json.loads` raises, it raises.
"""

from __future__ import annotations

import json
from typing import Any


class JsonRefused(ValueError):
    """JSON the reader would not take; the message names the repeated key or the constant."""


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise JsonRefused(f"the JSON repeats the key {key!r} in one object")
        found[key] = value
    return found


def _constant(name: str) -> Any:
    raise JsonRefused(f"the JSON holds {name}, which JSON does not define")


def loads(text: str | bytes) -> Any:
    """The value `text` holds, as `json.loads` reads it, or `JsonRefused` when an object repeats a key or a number is
    `NaN`, `Infinity`, or `-Infinity`."""
    return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
