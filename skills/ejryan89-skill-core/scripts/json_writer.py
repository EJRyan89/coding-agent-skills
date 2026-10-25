"""The one writer of JSON for a file, refusing at the write what the strict reader would refuse on reading it back.

The json module's writer writes `NaN`, `Infinity`, and `-Infinity`, which JSON does not define and `json_reader`
refuses; a lone surrogate, which no UTF-8 file can hold; and a key twice, when a dictionary holds both `1` and `"1"`,
which `json_reader` refuses too. A value holding one would fail only when a later step reads the record back, and that
error names the wrong file. `dumps` refuses each as `JsonWriteRefused`, a `ValueError`, whose message names what the
value holds, and reads back what it wrote as `json_reader` does, so the step that produced the value is the one that
fails. `write` replaces a file with that text through `atomic_file`. The deployer calls `dumps` alone: every write of
its goes through `deployer/fsops.py`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import json_reader
from atomic_file import replace_text

# How the json module's writer words a non-finite number it was told to refuse.
NON_FINITE = "Out of range float values are not JSON compliant"


class JsonWriteRefused(ValueError):
    """A value the writer would not write; the message names what it holds."""


def dumps(
    value: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    separators: tuple[str, str] | None = None,
    ensure_ascii: bool = True,
) -> str:
    """`value` as `json.dumps` writes it with these options, or `JsonWriteRefused` when it holds `NaN`, `Infinity`, or
    `-Infinity`, a lone surrogate, a key twice in one object, or nesting past the recursion limit. Whatever else
    `json.dumps` raises, such as a `TypeError` for a value of no JSON type, it raises."""
    options: dict[str, Any] = {"indent": indent, "sort_keys": sort_keys, "separators": separators}
    try:
        text = json.dumps(value, allow_nan=False, ensure_ascii=False, **options)
    except ValueError as error:
        if str(error).startswith(NON_FINITE):
            raise JsonWriteRefused("the value holds NaN or an infinity, which JSON does not define") from error
        raise
    except RecursionError as error:
        raise JsonWriteRefused("the value nests deeper than the writer can write") from error
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        character = ord(text[error.start])
        raise JsonWriteRefused(f"the value holds a lone surrogate U+{character:04X}, which UTF-8 cannot encode") from (
            error
        )
    try:
        json_reader.loads(text)
    except json_reader.JsonRefused as error:
        raise JsonWriteRefused(f"the value cannot be read back: {error}") from error
    return json.dumps(value, ensure_ascii=True, **options) if ensure_ascii else text


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
