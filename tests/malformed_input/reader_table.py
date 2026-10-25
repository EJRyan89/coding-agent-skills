"""The shape of the reader table test_malformed_input.py runs: one entry per reader of a file or record.

A reader is the function that reads and parses a file the code did not just write, a record an earlier step or
another program wrote, or text a user or a repository gave, such as an argument, a Markdown line, or a condition.
Each entry names the reader, its format, a well-formed document it reads, how to call it on a path, the refusals its
contract names, and each case it documents accepting with the reason. A reader whose contract refuses by a return
value, a finding, or a printed line rather than an exception is called through an adapter that turns that refusal
into Refused, so every refusal is an exception the suite can name; a parser of text is called on the file's text,
which text_of reads. A parser whose grammar nests or takes a number names how its own document nests and where a
number goes, so the suite holds it to the deep-nesting and long-integer cases a format alone cannot build.
readers.py gathers the entries, grouped by area in the readers_*.py modules beside it.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import corpus

REPOSITORY = Path(__file__).resolve().parents[2]


class Refused(Exception):
    """A reader's refusal by return value, finding, or printed line, raised by its adapter."""


class NotText(Refused):
    """A file a text-taking parser's caller cannot decode or read, which never reaches the parser: the caller's
    decoding is its refusal, and the parser is held to the cases that decode."""


@dataclass(frozen=True)
class Reader:
    """One reader: its name as module.function, its corpus format, and how the suite calls it."""

    name: str
    format: str
    valid: bytes
    read: Callable[[Path], object]
    refusals: tuple[type[BaseException], ...]
    # The name the reader looks for, as a path below the case's folder.
    filename: str = "input"
    # Each case the reader documents accepting, mapped to the reason it does.
    accepts: Mapping[str, str] = field(default_factory=dict)
    refuses_links: bool = False
    # Writes the files the reader takes beside its own, given the path of its own, before the case is read; the
    # reader then writes nothing below the case's folder when it refuses.
    prepare: Callable[[Path], None] | None = None
    # For a parser whose grammar nests, such as braces or parentheses: its input nested the given number of levels.
    nesting: Callable[[int], bytes] | None = None
    # For a parser whose grammar takes a number: its input with that number given the digits passed.
    number: Callable[[bytes], bytes] | None = None
    # Why the reader returns each field as the document holds it, for its caller to read by kind, when it does: a
    # field of another type is then accepted, as a missing field may be. Without it, a reader that answers on a field
    # of another type passes over a shape it does not know.
    open_fields: str = ""


def grammar_cases(reader: Reader) -> list[corpus.Case]:
    """The cases a parser's own grammar builds: its input nested past what a walk recurses through, and a number past
    Python's digit limit."""
    return [
        *(
            [
                corpus.Case(
                    "deep_nesting", reader.nesting(corpus.SCANNED_DEPTH), "the grammar nested past the recursion limit"
                )
            ]
            if reader.nesting is not None
            else []
        ),
        *(
            [corpus.Case("long_integer", reader.number(corpus.LONG_INTEGER), "a number past Python's digit limit")]
            if reader.number is not None
            else []
        ),
    ]


def applicable_cases(reader: Reader) -> list[corpus.Case]:
    """The corpus cases of the reader's format, built around its own document, the link case only where it refuses
    links, then the cases its grammar builds, each in place of the format's case of the same name."""
    own = grammar_cases(reader)
    names = {case.name for case in own}
    return [
        *(
            case
            for case in corpus.cases(reader.format, reader.valid)
            if (case.content != corpus.LINK or reader.refuses_links) and case.name not in names
        ),
        *own,
    ]


def text_of(path: Path, encoding: str = "utf-8") -> str:
    """A file's text as a text-taking parser's caller reads it, NotText when it cannot be read or decoded."""
    try:
        return path.read_text(encoding=encoding)
    except (OSError, UnicodeDecodeError) as error:
        raise NotText(f"{type(error).__name__}: {error}") from error


def module_at(path: Path) -> ModuleType:
    """The Python module at path, loaded by its path under its file name: for a repository skill's script, which no
    sibling lookup reaches. Its folder goes first on sys.path, so the module's own imports resolve as they do when it
    runs."""
    sys.path.insert(0, str(path.parent))
    specification = importlib.util.spec_from_file_location(path.stem, path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[path.stem] = module
    specification.loader.exec_module(module)
    return module
