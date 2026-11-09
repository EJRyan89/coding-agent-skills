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
names whole. `answers_request` tells whether text `loads` refused is the response to a pending JSON-RPC request, so a
reader waiting for one reports the refusal rather than waiting out its time limit, and `is_request_id` is the one rule
by which a response's `id`, taken or refused, is matched to the request.
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


# The characters JSON allows between tokens.
_WHITESPACE = " \t\n\r"


def is_request_id(value: Any, request_id: int) -> bool:
    """Whether `value`, an `id` that `loads` read, is the integer `request_id` a JSON-RPC request was sent with: a JSON
    number equal to it, spelled `2`, `2.0`, or `2e0`, since JSON gives a number one value whatever its spelling. A
    string, null, and a boolean, which Python compares equal to 1 or 0, are never a number's id. It is the one rule by
    which a reader waiting for a response matches it, whether `loads` takes the response or `answers_request` reads
    a refused one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == request_id


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in _WHITESPACE:
        index += 1
    return index


def _string_end(text: str, index: int) -> int:
    """The index just past the string that opens at `index`, or -1 when it never closes."""
    index += 1
    while index < len(text):
        if text[index] == "\\":
            index += 2
        elif text[index] == '"':
            return index + 1
        else:
            index += 1
    return -1


def _value_end(text: str, index: int) -> int:
    """The index just past the value that starts at `index`, or -1: a string to its closing quote, an array or an
    object to the bracket that closes it, counted rather than parsed so that no depth recurses, and any other value
    to the next comma, bracket, or whitespace."""
    if index >= len(text):
        return -1
    if text[index] == '"':
        return _string_end(text, index)
    if text[index] in "[{":
        depth = 0
        while index < len(text):
            character = text[index]
            if character == '"':
                index = _string_end(text, index)
                if index < 0:
                    return -1
                continue
            if character in "[{":
                depth += 1
            elif character in "]}":
                depth -= 1
                if depth == 0:
                    return index + 1
            index += 1
        return -1
    end = index
    while end < len(text) and text[end] not in ",]}" + _WHITESPACE:
        end += 1
    return end if end > index else -1


def _is_id_key(spelled: str) -> bool:
    try:
        return loads(spelled, surrogates=True) == "id"
    except CONTENT_ERRORS:
        return False


def _top_level_ids(text: str) -> list[str]:
    """The text of each `id` member's value in the object `text` holds, a repeated one each time, scanned member by
    member at the top level alone, so that no value is parsed and no nesting recurses: none when `text` holds no
    object, and only those before the first member that does not scan."""
    ids: list[str] = []
    index = _skip_whitespace(text, 0)
    if index >= len(text) or text[index] != "{":
        return ids
    index = _skip_whitespace(text, index + 1)
    while index < len(text) and text[index] == '"':
        key_end = _string_end(text, index)
        if key_end < 0:
            return ids
        key = text[index:key_end]
        index = _skip_whitespace(text, key_end)
        if index >= len(text) or text[index] != ":":
            return ids
        start = _skip_whitespace(text, index + 1)
        end = _value_end(text, start)
        if end < 0:
            return ids
        if _is_id_key(key):
            ids.append(text[start:end])
        index = _skip_whitespace(text, end)
        if index >= len(text) or text[index] != ",":
            return ids
        index = _skip_whitespace(text, index + 1)
    return ids


def answers_request(text: str | bytes, request_id: int) -> bool:
    """Whether `text`, which `loads` refused, is a JSON object any of whose `id` members `is_request_id` takes for
    `request_id`: the response to a pending JSON-RPC request. It tells only that, so a caller reports the refusal as
    the response's rather than passing over the line as one that answers nothing and waiting out its time limit; the
    response's other values are read by `loads` alone, so none is ever acted on. It scans the top-level members without
    parsing their values, so a response nested past the interpreter's recursion limit is told too, and reads each `id`
    value with `loads`, so a refused response is matched by the rule a taken one is. Text holding no object answers no
    request."""
    if isinstance(text, bytes):
        try:
            text = text.decode(json.detect_encoding(text))
        except UnicodeDecodeError:
            return False
    for spelled in _top_level_ids(text):
        try:
            value = loads(spelled)
        except CONTENT_ERRORS:
            continue
        if is_request_id(value, request_id):
            return True
    return False
