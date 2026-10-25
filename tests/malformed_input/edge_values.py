"""The valid edge values a writer may be handed, built once and shared by every round trip in test_round_trip.py.

corpus.py holds the damaged shapes a reader must refuse; this module holds the well-formed values at the edges of what
a source allows: text that is empty, blank, only Unicode whitespace, non-ASCII, or long, names of each such kind,
repository paths git can hold, and list sizes from none to several. A round trip hands each value to the writer's own
code and holds the reader to the document it wrote: the writer refuses the value before it writes anything, or the
reader returns what was written. A value no source can produce, such as a name git refuses, is left to the writer's
refusal, never to the reader's.
"""

from __future__ import annotations

# Free text a field may hold: plain, empty, blank, only Unicode whitespace, a line separator that is not a newline,
# non-ASCII, the characters a format quotes or escapes, more than one line, and long.
TEXT = (
    "plain text",
    "",
    " ",
    "\u00a0",
    "\u3000",
    "\t",
    "one\u2028two",
    "Ünïcödé ✓",
    "日本語",
    'comma, "quoted", back\\slash',
    "two\nlines",
    "x" * 10_000,
)

# The text a field whose source is a line someone wrote and a reader already took holds: one trimmed line, not blank.
ONE_LINE = tuple(value for value in TEXT if value.strip() and value == value.strip() and value.splitlines() == [value])
# The text a field whose source refuses only blank text holds.
NOT_BLANK = tuple(value for value in TEXT if value.strip())

# A name, such as a branch's or a user's, where the source allows each: one character, blank or only Unicode
# whitespace, non-ASCII, with spaces, with punctuation, and long.
NAMES = (
    "a",
    " ",
    "\u00a0",
    "\u3000",
    "Ünïcödé",
    "日本語",
    "name with spaces",
    "dots.and-dashes_and/slashes",
    "n" * 200,
)

# A repository-relative path git can hold: one character, a name that is only a space or Unicode whitespace, a
# folder's, non-ASCII, with spaces, and deep.
PATHS = (
    "a",
    " ",
    "\u00a0",
    "folder/ ",
    "Ünïcödé/日本語.txt",
    "a folder/a file.txt",
    "/".join(["deep"] * 30) + "/file",
)

# The number of entries a list holds: none, one, and several.
SIZES = (0, 1, 3)


def sized(values: tuple[str, ...], size: int) -> list[str]:
    """The first `size` of values, repeating them when there are fewer, each made distinct by its position."""
    return [f"{values[index % len(values)]}{index // len(values) or ''}" for index in range(size)]
