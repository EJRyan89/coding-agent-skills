"""The one parse of JSON a file or a command gave, refusing what JSON leaves undefined.

Python's reader keeps the last value of a key an object repeats, which RFC 8259 leaves to each reader, so another
reader of the same file may read the first; and it reads `NaN`, `Infinity`, and `-Infinity`, which JSON does not
define and other readers refuse, and which then reach code that compares numbers, as does a number such as `1e400`
that is too large for a float and which Python's reader turns into an infinity. `loads` refuses each as
`JsonRefused`, a `ValueError`, so a reader that already turns a `ValueError` from its parse into its own refusal
refuses these alike. Nesting past `nesting_depth`'s `DEPTH_LIMIT` is refused the same way, whether `json.loads`
raises it as a `RecursionError` that such a reader lets escape or reads a value deeper than a Python walk over it can
recurse, each array or object one level and a scalar none, and so is an integer of more digits than `whole_number`'s
`DIGIT_LIMIT`, the one cap on a number read from text, whatever limit the interpreter is configured with. A string or
key holding a lone surrogate, which a `\\ud800` escape gives and UTF-8 cannot encode, is refused too, so the reader
takes nothing `json_writer` refuses to write. It bounds nothing else: whatever else `json.loads` raises, such as its
`JSONDecodeError`, it raises. `CONTENT_ERRORS` is every error it raises on bad content, the family a caller's handler
names whole.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import nesting_depth
import whole_number

# The refusal of a value nested deeper than the reader descends, by the scanner's recursion or the shared limit.
NESTED = "the JSON nests deeper than the parser can read"


class JsonRefused(ValueError):
    """JSON the reader would not take; the message names the repeated key or the constant."""


# What `loads`, `raw_decode`, and `read_file` raise on bad content, each a ValueError: the reader's own refusal, the
# json module's for text that is not JSON, and the codec's for bytes that are not UTF-8.
CONTENT_ERRORS: tuple[type[Exception], ...] = (JsonRefused, json.JSONDecodeError, UnicodeDecodeError)
# The functions that raise CONTENT_ERRORS on content.
CONTENT_READERS = ("loads", "raw_decode", "read_file")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise JsonRefused(f"the JSON repeats the key {key!r} in one object")
        found[key] = value
    return found


def _constant(name: str) -> Any:
    raise JsonRefused(f"the JSON holds {name}, which JSON does not define")


def _float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise JsonRefused("the JSON holds a number too large for a float")
    return value


def _integer(text: str) -> int:
    digits = text.removeprefix("-")
    refused = JsonRefused(f"the JSON holds an integer of more than {whole_number.DIGIT_LIMIT} digits")
    value = whole_number.read(digits, refused)
    return -value if digits != text else value


def _items(value: Any) -> Any:
    return value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()


def _containers(value: Any) -> list[Any]:
    """The arrays and objects `value` holds directly, each one level below it; a scalar is no level."""
    return [item for item in _items(value) if isinstance(item, (dict, list))]


def _strings(value: Any) -> list[str]:
    """Every string and key `value` holds, at any depth, gathered without recursing."""
    found: list[str] = []
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            found.append(node)
        elif isinstance(node, dict):
            found.extend(node)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def _bounded(value: Any, *, surrogates: bool = False) -> Any:
    """`value`, or `JsonRefused` when it nests past `DEPTH_LIMIT` or, unless `surrogates` keeps them, a string or key
    holds a lone surrogate."""
    if nesting_depth.nests_past(value, _containers):
        raise JsonRefused(NESTED)
    for text in () if surrogates else _strings(value):
        try:
            text.encode("utf-8")
        except UnicodeEncodeError as error:
            character = ord(text[error.start])
            raise JsonRefused(f"the JSON holds a lone surrogate U+{character:04X}, which UTF-8 cannot encode") from (
                error
            )
    return value


def loads(text: str | bytes, *, surrogates: bool = False) -> Any:
    """The value `text` holds, as `json.loads` reads it, or `JsonRefused` when an object repeats a key, a number is
    `NaN`, `Infinity`, `-Infinity`, or too large for a float, an integer has more digits than `DIGIT_LIMIT`, the
    value nests past `DEPTH_LIMIT`, or a string or key holds a lone surrogate. With `surrogates`, a lone surrogate is
    kept, for a record whose own writer keeps one, such as a name another program gave as bytes that are not UTF-8,
    held as its surrogate escapes; a module that writes such a record states why in its `JSON_WRITE_ALLOWED`."""
    try:
        value = json.loads(
            text, object_pairs_hook=_object, parse_constant=_constant, parse_float=_float, parse_int=_integer
        )
    except RecursionError as error:
        raise JsonRefused(NESTED) from error
    return _bounded(value, surrogates=surrogates)


def raw_decode(text: str, index: int = 0) -> tuple[Any, int]:
    """The value `text` holds from `index` and the index just past it, as `json.JSONDecoder.raw_decode` reads it,
    refusing what `loads` refuses, so a value followed by other text, such as a line's next field, is read alike."""
    decoder = json.JSONDecoder(
        object_pairs_hook=_object, parse_constant=_constant, parse_float=_float, parse_int=_integer
    )
    try:
        value, end = decoder.raw_decode(text, index)
    except RecursionError as error:
        raise JsonRefused(NESTED) from error
    return _bounded(value), end


def read_file(path: Path) -> Any:
    """The value the UTF-8 file at `path` holds, as `loads` reads it, whatever the platform's own codec: an `OSError`
    when the file cannot be read, a `UnicodeDecodeError`, a `ValueError` too, when it is not UTF-8, which includes a
    byte-order mark that the caller does not expect."""
    return loads(path.read_bytes().decode("utf-8"))
