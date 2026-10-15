"""Typed fields: the reader of a JSON field's kind for the code-review pipeline's and the pull request tracker's files,
and for the GitHub search responses the activity report reads.

A validator built on it reads each field through a `FieldReader`, which checks the field's kind and returns its value,
or raises `FieldError`, naming the field's path, the kind expected, and what was found, in one shape:
`<path> must be <expected>, found <found>`. The validator then checks only the value, such as a pattern, a range, or
a reference to another field; it never checks a field's type itself.

The kinds are `string`, `integer`, `number`, `boolean`, `hex_digest`, `one_of`, `array`, and `json_object`;
`optional` allows null in place of any of them, and `either` takes the first of several kinds that accepts a value.
A string is non-empty, meaning not blank, unless `empty` allows it; an integer is never a boolean or a float; a
number is finite and never a boolean; a hex digest is lowercase.

A skill's own error type stays what its callers catch: the skill declares one class deriving from both `FieldError`
and its error, and builds its reader with that class, so a field error is both.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Collection, Iterable
from typing import Any, Concatenate, ParamSpec, TypeVar, overload

T = TypeVar("T")
P = ParamSpec("P")

# The longest string or integer quoted in what an error found; a longer one is described by its length.
QUOTED_CHARACTERS = 40


class FieldError(ValueError):
    """A field whose value is not of the kind its format declares."""

    def __init__(self, path: str, expected: str, found: str) -> None:
        self.path = path
        self.expected = expected
        self.found = found
        super().__init__(f"{path} must be {expected}, found {found}")


def describe(value: object) -> str:
    """What a value is, as an error names what it found: its JSON type, with the value itself when it is short."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return f"boolean {'true' if value else 'false'}"
    if isinstance(value, int):
        return f"integer {value}" if abs(value) < 10**QUOTED_CHARACTERS else "an integer too long to quote"
    if isinstance(value, float):
        return f"number {value!r}"
    if isinstance(value, str):
        if len(value) > QUOTED_CHARACTERS:
            return _string_of(len(value))
        return f"string {json.dumps(value)}"
    if isinstance(value, list):
        return "an array"
    if isinstance(value, dict):
        return "an object"
    return f"a {type(value).__name__}, not a JSON value"


def _names(values: Iterable[object]) -> str:
    return ", ".join(json.dumps(value) for value in values)


def _ordered(values: Collection[T]) -> list[T]:
    """A set's values sorted, so an error names them the same way every run; a sequence's in its own order."""
    if isinstance(values, set | frozenset):
        return sorted(values, key=lambda value: (type(value).__name__, str(value)))
    return list(values)


class FieldReader:
    """Reads fields as their kinds, raising `error`, a `FieldError` class, for a value of another kind."""

    def __init__(self, error: type[FieldError] = FieldError) -> None:
        self.error = error

    def fail(self, path: str, expected: str, found: str) -> FieldError:
        return self.error(path, expected, found)

    def string(
        self,
        value: Any,
        path: str,
        *,
        empty: bool = False,
        single_line: bool = False,
        trimmed: bool = False,
        maximum: int | None = None,
    ) -> str:
        """A string, not blank unless `empty`, one line when `single_line`, without surrounding whitespace when
        `trimmed`, and at most `maximum` characters."""
        words = [] if empty else ["non-empty"]
        words += ["trimmed"] if trimmed else []
        words += ["single-line"] if single_line else []
        expected = f"a {' '.join([*words, 'string'])}"
        expected = expected if maximum is None else f"{expected} of at most {maximum} characters"
        if not isinstance(value, str):
            raise self.fail(path, expected, describe(value))
        if not empty and not value.strip():
            raise self.fail(path, expected, "an empty string" if not value else "a blank string")
        if single_line and value and value.splitlines() != [value]:
            raise self.fail(path, expected, "a string of more than one line")
        if trimmed and value != value.strip():
            raise self.fail(path, expected, "a string with whitespace around it")
        if maximum is not None and len(value) > maximum:
            raise self.fail(path, expected, _string_of(len(value)))
        return value

    def integer(self, value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
        """An integer, never a boolean or a float, from `minimum` to `maximum` when they are given."""
        expected = "an integer"
        if minimum is not None and maximum is not None:
            expected = f"an integer from {minimum} to {maximum}"
        elif minimum is not None:
            expected = f"an integer of at least {minimum}"
        elif maximum is not None:
            expected = f"an integer of at most {maximum}"
        if type(value) is not int:
            raise self.fail(path, expected, describe(value))
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            raise self.fail(path, expected, describe(value))
        return value

    def number(self, value: Any, path: str) -> int | float:
        """A finite number, an integer or a float but never a boolean, nor an integer too large for a float to hold,
        which JSON reads intact."""
        if type(value) not in {int, float} or not _finite(value):
            raise self.fail(path, "a finite number", describe(value))
        return value  # type: ignore[no-any-return]  # value is an int or a float, checked above

    def boolean(self, value: Any, path: str) -> bool:
        if not isinstance(value, bool):
            raise self.fail(path, "a boolean", describe(value))
        return value

    def hex_digest(self, value: Any, path: str, *lengths: int) -> str:
        """A lowercase hexadecimal digest of one of `lengths` characters, such as 64 for SHA-256."""
        if not lengths:
            raise ValueError("a hex digest needs at least one length")
        sizes = " or ".join(str(length) for length in lengths)
        expected = f"a lowercase hexadecimal digest of {sizes} characters"
        if not isinstance(value, str):
            raise self.fail(path, expected, describe(value))
        if len(value) not in lengths or any(character not in "0123456789abcdef" for character in value):
            found = _string_of(len(value))
            if len(value) in lengths:
                found = "a string with a character that is not a lowercase hexadecimal digit"
            raise self.fail(path, expected, found)
        return value

    def one_of(self, value: Any, path: str, allowed: Collection[T]) -> T:
        """One of `allowed`, of the same type as the value it equals, so `true` never passes for `1`."""
        for option in allowed:
            if type(value) is type(option) and value == option:
                return option
        raise self.fail(path, f"one of {_names(_ordered(allowed))}", describe(value))

    @overload
    def array(
        self, value: Any, path: str, item: None = None, *, nonempty: bool = False, maximum: int | None = None
    ) -> list[Any]: ...

    @overload
    def array(
        self,
        value: Any,
        path: str,
        item: Callable[[Any, str], T],
        *,
        nonempty: bool = False,
        maximum: int | None = None,
    ) -> list[T]: ...

    def array(
        self,
        value: Any,
        path: str,
        item: Callable[[Any, str], Any] | None = None,
        *,
        nonempty: bool = False,
        maximum: int | None = None,
    ) -> list[Any]:
        """An array, non-empty when `nonempty`, of at most `maximum` items, each read by `item` at `path[index]`."""
        expected = "a non-empty array" if nonempty else "an array"
        expected = expected if maximum is None else f"{expected} of at most {maximum} items"
        if not isinstance(value, list):
            raise self.fail(path, expected, describe(value))
        if nonempty and not value:
            raise self.fail(path, expected, "an empty array")
        if maximum is not None and len(value) > maximum:
            raise self.fail(path, expected, f"an array of {len(value)} items")
        if item is None:
            return value
        return [item(element, f"{path}[{index}]") for index, element in enumerate(value)]

    def json_object(
        self,
        value: Any,
        path: str,
        *,
        required: Collection[str] | None = None,
        optional: Collection[str] = (),
    ) -> dict[str, Any]:
        """An object; with `required`, one holding every required field and no field outside the two."""
        if not isinstance(value, dict):
            raise self.fail(path, "an object", describe(value))
        if required is None:
            if optional:
                self._known(value, path, set(optional))
            return value
        missing = [field for field in _ordered(set(required)) if field not in value]
        if missing:
            listed = ", ".join(missing)
            without = "an object without it" if len(missing) == 1 else "an object without them"
            raise self.fail(path, f"an object with {_plural(missing, 'field')} {listed}", without)
        self._known(value, path, set(required) | set(optional))
        return value

    def _known(self, value: dict[str, Any], path: str, known: set[str]) -> None:
        unknown = sorted(str(field) for field in set(value) - known)
        if unknown:
            raise self.fail(path, "an object of known fields", f"{_plural(unknown, 'unknown field')} {_names(unknown)}")

    def optional(
        self, read: Callable[Concatenate[Any, str, P], T], value: Any, path: str, *args: P.args, **kwargs: P.kwargs
    ) -> T | None:
        """Null, or the value `read` reads; an error then expects the kind or null, except an object's missing or
        unknown fields, which are faults of an object that is there."""
        if value is None:
            return None
        try:
            return read(value, path, *args, **kwargs)
        except FieldError as exc:
            if exc.path != path or exc.expected.startswith("an object "):
                raise
            raise self.fail(path, f"{exc.expected} or null", exc.found) from None

    def either(self, value: Any, path: str, *reads: Callable[[Any, str], Any]) -> Any:
        """The value the first of `reads` that accepts it reads; an error expects any of their kinds and names what
        the value is."""
        expected = []
        for read in reads:
            try:
                return read(value, path)
            except FieldError as exc:
                if exc.path != path:
                    raise
                expected.append(exc.expected)
        raise self.fail(path, " or ".join(expected), describe(value))


def _finite(value: int | float) -> bool:
    """Whether a number is finite as a float, which an integer too large to convert to one is not."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _plural(names: list[str], noun: str) -> str:
    return noun if len(names) == 1 else f"{noun}s"


def _string_of(length: int) -> str:
    return "a string of 1 character" if length == 1 else f"a string of {length} characters"
