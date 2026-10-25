"""The shape of the reader table test_malformed_input.py runs: one entry per reader of a file or record.

A reader is the function that reads and parses a file the code did not just write, or a record an earlier step or
another program wrote. Each entry names the reader, its format, a well-formed document it reads, how to call it on a
path, the refusals its contract names, and each case it documents accepting with the reason. A reader whose contract
refuses by a return value, a finding, or a printed line rather than an exception is called through an adapter that
turns that refusal into Refused, so every refusal is an exception the suite can name. readers.py gathers the entries,
grouped by area in the readers_*.py modules beside it.
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


def applicable_cases(reader: Reader) -> list[corpus.Case]:
    """The corpus cases of the reader's format, built around its own document, the link case only where it refuses
    links."""
    return [
        case
        for case in corpus.cases(reader.format, reader.valid)
        if case.content != corpus.LINK or reader.refuses_links
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
