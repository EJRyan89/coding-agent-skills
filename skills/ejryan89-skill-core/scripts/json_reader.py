"""The one parse of JSON a file or a command gave, refusing what JSON leaves undefined.

Python's reader keeps the last value of a key an object repeats, which RFC 8259 leaves to each reader, so another
reader of the same file may read the first; and it reads `NaN`, `Infinity`, and `-Infinity`, which JSON does not
define and other readers refuse, and which then reach code that compares numbers. `loads` refuses both as
`JsonRefused`, a `ValueError`, so a reader that already turns a `ValueError` from its parse into its own refusal
refuses these alike. Nesting past the interpreter's recursion limit, which `json.loads` raises as a `RecursionError`
that such a reader lets escape, is refused the same way, and so is an integer of more digits than `whole_number`'s
`DIGIT_LIMIT`, the one cap on a number read from text, whatever limit the interpreter is configured with. It bounds
nothing else: whatever else `json.loads` raises, such as its `JSONDecodeError`, it raises. `CONTENT_ERRORS` is
every error it raises on bad content, the family a caller's handler names whole.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import whole_number


class JsonRefused(ValueError):
    """JSON the reader would not take; the message names the repeated key or the constant."""


# What `loads`, `raw_decode`, and `read_file` raise on bad content, each a ValueError: the reader's own refusal, the
# json module's for text that is not JSON, and the codec's for bytes that are not UTF-8.
CONTENT_ERRORS: tuple[type[Exception], ...] = (JsonRefused, json.JSONDecodeError, UnicodeDecodeError)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise JsonRefused(f"the JSON repeats the key {key!r} in one object")
        found[key] = value
    return found


def _constant(name: str) -> Any:
    raise JsonRefused(f"the JSON holds {name}, which JSON does not define")


def _integer(text: str) -> int:
    digits = text.removeprefix("-")
    refused = JsonRefused(f"the JSON holds an integer of more than {whole_number.DIGIT_LIMIT} digits")
    value = whole_number.read(digits, refused)
    return -value if digits != text else value


def loads(text: str | bytes) -> Any:
    """The value `text` holds, as `json.loads` reads it, or `JsonRefused` when an object repeats a key, a number is
    `NaN`, `Infinity`, or `-Infinity`, an integer has more digits than `DIGIT_LIMIT`, or the value nests past the
    recursion limit."""
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=_constant, parse_int=_integer)
    except RecursionError as error:
        raise JsonRefused("the JSON nests deeper than the parser can read") from error


def raw_decode(text: str, index: int = 0) -> tuple[Any, int]:
    """The value `text` holds from `index` and the index just past it, as `json.JSONDecoder.raw_decode` reads it,
    refusing what `loads` refuses, so a value followed by other text, such as a line's next field, is read alike."""
    decoder = json.JSONDecoder(object_pairs_hook=_object, parse_constant=_constant, parse_int=_integer)
    try:
        return decoder.raw_decode(text, index)
    except RecursionError as error:
        raise JsonRefused("the JSON nests deeper than the parser can read") from error


def read_file(path: Path) -> Any:
    """The value the UTF-8 file at `path` holds, as `loads` reads it, whatever the platform's own codec: an `OSError`
    when the file cannot be read, a `UnicodeDecodeError`, a `ValueError` too, when it is not UTF-8, which includes a
    byte-order mark that the caller does not expect."""
    return loads(path.read_bytes().decode("utf-8"))
