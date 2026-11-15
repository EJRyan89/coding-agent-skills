"""The one writer of JSON text, refusing at the write what the strict reader would refuse on reading it back.

The json module's writer writes `NaN`, `Infinity`, and `-Infinity`, which JSON does not define and `json_reader`
refuses; a lone surrogate, which no UTF-8 file can hold; a key twice, when a dictionary holds both `1` and `"1"`, which
`json_reader` refuses too; and a value nested past `nesting_depth`'s `DEPTH_LIMIT`, which `json_reader` refuses, and
which the json module's writer stops with a `RecursionError` on one Python version and writes whole on another. A
value holding one would fail only when a later step reads the record back, and that error names the wrong file.
`dumps` refuses each as `JsonWriteRefused`, a `ValueError`, whose message names what the value holds, and reads back
what it wrote as `json_reader` does, so the step that produced the value is the one that fails. `nests_too_deep` is
its judgment of the depth, a walk of its own that answers the same on every Python version, for a caller that writes
JSON another way. `write` replaces a file with that text through `atomic_file`. The deployer calls `dumps` alone:
every write of its goes through `deployer/fsops.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import json_reader
import nesting_depth
from atomic_file import replace_text

# How the json module's writer words a non-finite number it was told to refuse.
NON_FINITE = "Out of range float values are not JSON compliant"
# How the interpreter words an integer past its integer-string limit, which the json module's writer meets writing it.
INTEGER_LIMIT = "Exceeds the limit"
# The refusal of a value nested past DEPTH_LIMIT.
NESTED = "the value nests deeper than the writer can write"


class JsonWriteRefused(ValueError):
    """A value the writer would not write; the message names what it holds."""


# What `dumps` and `write` raise on a value no JSON file can hold; a value of no JSON type, or one that refers to
# itself, is the caller's error, which `dumps` raises as the json module does.
CONTENT_ERRORS: tuple[type[Exception], ...] = (JsonWriteRefused,)
# The functions that raise CONTENT_ERRORS on content.
CONTENT_READERS = ("dumps", "write")


def nests_too_deep(value: Any) -> bool:
    """Whether `value` nests past `nesting_depth`'s `DEPTH_LIMIT`, each list, tuple, or dictionary one level, as
    `json_reader` counts the levels of what it reads. A value that holds itself is measured down to the repeat, which
    the json module's writer refuses as circular."""
    return nesting_depth.nests_past(value, json_reader.containers)


def dumps(
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = True,
) -> str:
    """`value` as `json.dumps` writes it with these options, or `JsonWriteRefused` when it holds `NaN`, `Infinity`, or
    `-Infinity`, an integer of more digits than `json_reader` reads, a lone surrogate, a key twice in one object, or
    nesting past `DEPTH_LIMIT`, which it judges before writing. Whatever else `json.dumps` raises, such as a
    `TypeError` for a value of no JSON type or a `ValueError` for one that holds itself, it raises."""
    if nests_too_deep(value):
        raise JsonWriteRefused(NESTED)
    try:
        text = json.dumps(
            value, allow_nan=False, ensure_ascii=False, indent=indent, sort_keys=sort_keys, separators=separators
        )
    except ValueError as error:
        if str(error).startswith(NON_FINITE):
            raise JsonWriteRefused("the value holds NaN or an infinity, which JSON does not define") from error
        if str(error).startswith(INTEGER_LIMIT):
            raise JsonWriteRefused("the value holds an integer of more digits than the reader takes") from error
        raise
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        character = ord(text[error.start])
        raise JsonWriteRefused(f"the value holds a lone surrogate U+{character:04X}, which UTF-8 cannot encode") from (
            error
        )
    try:
        json_reader.loads(text)
    except json_reader.CONTENT_ERRORS as error:
        raise JsonWriteRefused(f"the value cannot be read back: {error}") from error
    if not ensure_ascii:
        return text
    return json.dumps(value, ensure_ascii=True, indent=indent, sort_keys=sort_keys, separators=separators)


def write(
    path: Path,
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    ensure_ascii: bool = True,
    mode: int = 0o600,
) -> None:
    """Replace `path` with `value` as `dumps` writes it and a newline, encoded as UTF-8, through atomic_file's
    replace_text, given `mode`. A value `dumps` refuses raises before a copy exists, and `path` is untouched."""
    replace_text(path, dumps(value, indent=indent, sort_keys=sort_keys, ensure_ascii=ensure_ascii) + "\n", mode=mode)
