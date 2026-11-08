"""The one reader of a word of gh shorthand flags, as gh's flag parser, pflag, reads it.

pflag reads a word that starts with one dash as a cluster of shorthands, each letter its own flag: a letter that takes
no value is set and the next letter read, and the first letter that takes a value takes the rest of the word, a `=`
before it dropped, or the next word when the rest is empty. A `=` after any letter ends the cluster the same way, so
`-X=POST` is `-X POST` and `-ifbody=x` is `-i -f body=x`. `github_client.py` reads the method of a `gh api` command
and the limit of a listing through it, and validation reads a filter flag in a skill script's gh command through it,
so the repository holds one parser of gh's shorthand rules.
"""

from __future__ import annotations

from collections.abc import Set
from typing import NamedTuple

# gh's flag parser reads a word of shorthands as a cluster, each letter its own flag, until a letter that takes a
# value, which takes the rest of the word: -iq.name is -i (api's --include) and -q .name, and -ftitle=x is -f title=x.
# These are the letters that take a value in every gh command that takes a filter and has them, as `gh help
# reference` lists them, with the global -R (--repo); a listing `capped_list` runs is such a command. Every other
# letter is read as one that takes none, a letter gh adds later and one that takes a value in one such command and
# none in another included, so a flag after it is still found wherever it may be one.
GH_VALUE_SHORTHANDS = frozenset("ABFHLORSXbefjklmoprs")


class Shorthand(NamedTuple):
    """One flag of a shorthand cluster: its name, as `-X`, the value the rest of its word gives it, a `=` before it
    dropped, or None, and whether it takes the next word as its value instead."""

    flag: str
    value: str | None
    takes_next_word: bool


def shorthands(word: str, value_letters: Set[str]) -> list[Shorthand]:
    """The flags a word of shorthands sets, in order, reading each letter in `value_letters` as one that takes a value
    and every other as one that takes none; [] for a word that is no cluster: one without a single leading dash, or a
    lone dash."""
    if not word.startswith("-") or word.startswith("--") or word == "-":
        return []
    found: list[Shorthand] = []
    rest = word[1:]
    while rest:
        letter, after = rest[0], rest[1:]
        if len(after) > 1 and after[0] == "=":
            found.append(Shorthand(f"-{letter}", after[1:], False))
            break
        if letter in value_letters:
            found.append(Shorthand(f"-{letter}", after or None, not after))
            break
        found.append(Shorthand(f"-{letter}", None, False))
        rest = after
    return found
