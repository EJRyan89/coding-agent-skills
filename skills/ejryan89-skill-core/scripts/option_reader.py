"""The one reader of a runner's option words as getopt reads them, by a table of the options the runner takes.

An OptionTable names each option a runner takes and how it takes its value, built from getopt's spelling by
option_table; option_refusal reads a runner's words by it, in every spelling getopt reads, and names the first word
the table does not settle. command_wrappers.py reads past a wrapper's options to the command it runs by its catalogue,
and validation's suite discovery by its own wrappers' tables.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

# A word is its text, or None when the text does not settle its value (an unset variable, a $( ) result).
Word = str | None
# How an option takes its value: none, the rest of its word or the next word, or only the rest of its word.
NO_VALUE, VALUE, OPTIONAL_VALUE = "none", "value", "optional"
# macOS's env, Apple's build of FreeBSD's, by its getopt string, which lists `-` as a letter that clears the
# environment as -i does; it takes no long option, so it reads GNU env's `--unset NAME` as `-`, then `-u` with `nset`
# attached, and NAME as its command. Every table of env names it as its dash_build.
MACOS_ENV_LETTERS = "-0C:iP:S:u:v"


@dataclass(frozen=True)
class OptionTable:
    """The options a table names for a program, each with how it takes its value; option_refusal names the first option
    word the table does not, which its caller refuses, the table naming only the options it knows.

    short maps a short option's letter, and long a long option's name with its dashes, to how it takes a value.
    operands counts the words a runner takes after its options and before its command, as timeout's duration;
    numbered is a pattern of a word that is an option of its own, as nice's `-5`; and assignments says NAME=VALUE words
    and a lone `-` stand among the options, as env's do. dash_build maps each letter of another build of the program,
    one whose getopt lists `-` as a letter, as macOS's env, FreeBSD's, does, to how it takes its value, and dash_safe
    names the ones of them known to be safe: such a build reads a long option word as a bundle of short letters,
    `--unset` as `-` and then `-u` with `nset` attached, so its reading is held to the table's (dash_reading)."""

    short: Mapping[str, str] = field(default_factory=dict)
    long: Mapping[str, str] = field(default_factory=dict)
    operands: int = 0
    numbered: re.Pattern[str] | None = None
    assignments: bool = False
    dash_build: Mapping[str, str] | None = None
    dash_safe: frozenset[str] = frozenset()


def option_table(short: str = "", long: str = "", **form: Any) -> OptionTable:
    """An OptionTable from getopt's spelling: short letters, each followed by `:` for a value or `::` for an optional
    one, and long names separated by spaces, each followed by `=` for a value or `=?` for an optional one; a
    dash_build spelling, given, is the other build's getopt letters, spelled as short is, and dash_safe the letters of
    it known to be safe."""
    if isinstance(form.get("dash_build"), str):
        form["dash_build"] = short_letters(form["dash_build"])
    if isinstance(form.get("dash_safe"), str):
        form["dash_safe"] = frozenset(form["dash_safe"])
    shorts = short_letters(short)
    longs: dict[str, str] = {}
    for spec in long.split():
        name = spec.removesuffix("=?").removesuffix("=")
        longs["--" + name] = OPTIONAL_VALUE if spec.endswith("=?") else VALUE if spec.endswith("=") else NO_VALUE
    return OptionTable(shorts, longs, **form)


def short_letters(short: str) -> dict[str, str]:
    """Each letter of getopt's short spelling with how it takes its value: `:` after it for a value, `::` for an
    optional one."""
    shorts: dict[str, str] = {}
    for match in re.finditer(r"(.)(::?)?", short):
        shorts[match[1]] = {None: NO_VALUE, ":": VALUE, "::": OPTIONAL_VALUE}[match[2]]
    return shorts


@dataclass(frozen=True)
class OptionReading:
    """What option_refusal found: the first option word the table does not settle, or None; the index its reading
    stopped at; and each value-taking option with the value it took, a short option by its dash and letter."""

    refused: str | None
    stop: int
    values: list[tuple[str, Word]]


def option_refusal(table: OptionTable, arguments: list[Word]) -> OptionReading:
    """A runner's words read as getopt reads them. The reading stops at its command, after its options and the
    operands it takes, or at the end of its words: its options end at its command or at `--`, after which its operands
    and its command follow. Every spelling getopt reads is read: a long option with its value attached by `=` or
    after it, which is safe only spelled in full, and short options bundled in one word, each letter judged, a
    value-taking one ending the bundle with the rest of its word or the next word as its value. A word the text does
    not settle ends the reading, and the caller refuses it."""
    operands = table.operands
    index = 0
    ended = False  # past `--`, where no word is an option
    values: list[tuple[str, Word]] = []

    def reading(refused: str | None, stop: int) -> OptionReading:
        return OptionReading(refused, stop, values)

    while index < len(arguments):
        word = arguments[index]
        if word is None:
            return reading(None, index)
        if word == "--" and not ended:
            ended = True
            index += 1
            continue
        if ended or len(word) < 2 or not word.startswith("-"):
            if not (table.assignments and ("=" in word or (word == "-" and not ended))):
                if operands == 0:
                    return reading(None, index)
                operands -= 1
            index += 1
            continue
        read = option_word(table, word)
        if read is None:
            return reading(word, index)
        taken, valued, attached = read
        if valued is not None:
            values.append((valued, next_word(arguments, index + 1) if taken else attached))
        index += 1 + taken
    return reading(None, len(arguments))


def next_word(arguments: list[Word], index: int) -> Word:
    return arguments[index] if index < len(arguments) else ""


def option_word(table: OptionTable, word: str) -> tuple[int, str | None, str | None] | None:
    """An option word read: how many of the following words it takes as its values, the value-taking option it holds,
    if any, and the value attached to it; or None for a word that is or may hold an option the table does not know
    to be safe, a value given to an option that takes none among them. Where the table names a dash_build, a dash
    word is also read as that build reads it, and is None unless that build refuses it or takes the same following
    words by safe letters (dash_reading), so the two builds never start different commands."""
    read = getopt_word(table, word)
    if read is None or table.dash_build is None:
        return read
    other = dash_reading(table, word[1:])
    return read if other in {BUILD_REFUSES, read[0]} else None


# What dash_reading gives for a word the other build refuses, which then prints its usage and runs nothing.
BUILD_REFUSES = -1


def dash_reading(table: OptionTable, letters: str) -> int | None:
    """A dash word, after its first dash, read as the table's dash_build reads it, BSD getopt's way: a bundle of
    letters, `-` among them, a value-taking letter ending it with the rest of the word or the next word as its value.
    How many following words it takes; BUILD_REFUSES where a letter is none of that build's; None where a letter it
    takes is not one dash_safe names."""
    build = table.dash_build or {}
    for position, letter in enumerate(letters):
        kind = build.get(letter)
        if kind is None:
            return BUILD_REFUSES
        if letter not in table.dash_safe:
            return None
        if kind != NO_VALUE:
            return int(kind == VALUE and position + 1 == len(letters))
    return 0


def getopt_word(table: OptionTable, word: str) -> tuple[int, str | None, str | None] | None:
    """An option word read by the table alone, as option_word reads it."""
    if table.numbered is not None and table.numbered.fullmatch(word):
        return 0, None, None
    if word.startswith("--"):
        name, attached, value = word.partition("=")
        kind = table.long.get(name)
        if kind is None or (attached and kind == NO_VALUE):
            return None
        if kind == NO_VALUE:
            return 0, None, None
        return int(kind == VALUE and not attached), name, value if attached else None
    letters = word[1:]
    for position, letter in enumerate(letters):
        kind = table.short.get(letter)
        if kind is None:
            return None
        if kind != NO_VALUE:
            rest = letters[position + 1 :]
            return int(kind == VALUE and not rest), f"-{letter}", rest or None
    return 0, None, None
