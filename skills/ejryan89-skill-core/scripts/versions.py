"""The one reader of a version number: its dot-separated whole numbers, compared by number and never as text.

A version is read as a tuple of whole numbers, so tuples order versions as their programs do: `1.10` follows `1.9`,
and `1.0.98` is not `1.0.9`. Each number is ASCII digits read through `whole_number`, so a number past its digit limit
is no version. `find` reads the first version in a program's output, `read` a text that is a version and nothing
else, and `latest` the highest of a list of names, such as release tags, by the version each holds. The deployer's tool
floors, the release tags, and the code-review host's Copilot CLI check read versions here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

import whole_number

Version = tuple[int, ...]

# The separator between a version's numbers.
SEPARATOR = "."


def _pattern(least: int, most: int | None) -> str:
    """A version of `least` to `most` numbers, or at least `least` when `most` is None, as a regular expression. It
    starts only where a run of digits starts, so a search tries each run once, in time linear in the text, rather than
    from each of its digits; any match from inside a run starts at the run's start too, so none is lost."""
    upper = "" if most is None else str(most - 1)
    return rf"(?<![0-9])[0-9]+(?:{re.escape(SEPARATOR)}[0-9]+){{{least - 1},{upper}}}"


def _numbers(text: str) -> Version | None:
    numbers = [whole_number.parse(part) for part in text.split(SEPARATOR)]
    return None if None in numbers else tuple(number for number in numbers if number is not None)


def find(text: str, *, least: int = 2, most: int | None = 3) -> Version | None:
    """The first version of `least` to `most` numbers in `text`, as a version command prints it, or None when it holds
    none. A longer version is read up to `most` numbers, so `1.2.3.4` is `(1, 2, 3)` when `most` is 3."""
    match = re.search(_pattern(least, most), text)
    return _numbers(match.group(0)) if match else None


def read(text: str, *, least: int = 2, most: int | None = None) -> Version | None:
    """The version `text` is, with nothing before or after it, or None when it is not one."""
    return _numbers(text) if re.fullmatch(_pattern(least, most), text) else None


def latest(names: Iterable[str], version_of: Callable[[str], Version | None]) -> str | None:
    """The name holding the highest version, names `version_of` reads none in left out, or None when none is left;
    between two names of one version, such as `v1.0.0` and `v01.0.0`, the one that sorts last."""
    held = [(version, name) for name in names if (version := version_of(name)) is not None]
    return max(held)[1] if held else None


def text(version: Version) -> str:
    """A version as its numbers are written, dot-separated."""
    return SEPARATOR.join(str(number) for number in version)
