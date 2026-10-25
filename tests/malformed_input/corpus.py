"""The malformed-input corpus: every damaged shape a file or record the repository reads can take, built once.

A case is placed where a reader expects its file: bytes written to the path, a directory made there, or a link put
there. The bytes cases are given per format, since a truncation or a nesting is only one in the format it truncates
or nests, and a format leaves out a case it cannot express, such as an integer in XML. A JSON reader also takes the
field cases, each built from its own well-formed document by removing one field, putting {} in an object's place, or
putting a value of another type in a field's place, an object for a scalar and a string for an object or a list.
test_malformed_input.py holds every reader in readers.py to every case of its format.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

# Past the int-to-string limit Python applies to a decimal integer, 4300 digits by default.
LONG_INTEGER = b"9" * 5000
# Past the default recursion limit of 1000 frames, and past what the C JSON scanner nests before it refuses.
DEPTH = 100_000
# Past what a Python walk over a parsed value can recurse, which the C JSON scanner and the XML parser still read, and
# past nesting_depth's DEPTH_LIMIT, which the shared readers refuse.
SCANNED_DEPTH = 2000
BOM = b"\xef\xbb\xbf"
NOT_UTF8 = b"\xff\xfe\xc3\x28"

# The shapes that are not bytes, which place() builds at the path.
DIRECTORY = "directory"
LINK = "link"
UNREADABLE = "unreadable"

# The field change that gives a field a value of another type, which a reader refuses unless it documents taking it.
WRONG_TYPE = "wrong_type"


@dataclass(frozen=True)
class Case:
    """One damaged shape: its name, the bytes placed at the path or the kind of thing placed there, and what it
    exercises."""

    name: str
    content: bytes | str
    about: str
    # A field case, which a reader may accept where its contract leaves the field optional or open, never by escaping.
    field: bool = False


def _common(valid: bytes) -> list[Case]:
    """The cases every format takes, around one document that format reads."""
    return [
        Case("empty", b"", "a file with no bytes"),
        Case("not_utf8", NOT_UTF8 + valid, "bytes that do not decode as UTF-8"),
        Case("bom", BOM + valid, "a well-formed document after a UTF-8 byte-order mark"),
        Case("nul_bytes", valid + b"\x00\x00", "NUL bytes after a well-formed document"),
        Case(DIRECTORY, DIRECTORY, "a directory in the file's place"),
        Case(LINK, LINK, "a link in the file's place, for a reader that refuses links"),
        *([Case(UNREADABLE, UNREADABLE, "a file the reader is not allowed to open")] if can_deny_reading() else []),
    ]


def can_deny_reading() -> bool:
    """Whether a file can be made unreadable to this process: always on Windows, where a handle that shares nothing
    holds it, and elsewhere for any user but root, whom no file mode stops."""
    return sys.platform == "win32" or os.geteuid() != 0


def _field_paths(value: object, path: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple[str | int, ...], object]]:
    """Each field below value with its path: an object's keys in order, and a list's first item, standing for the
    rest, itself and then its own fields."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield (*path, key), item
            yield from _field_paths(item, (*path, key))
    elif isinstance(value, list) and value:
        yield (*path, 0), value[0]
        yield from _field_paths(value[0], (*path, 0))


def _changed(document: object, path: tuple[str | int, ...], change: str) -> object:
    """A copy of document with the field at path removed, made {}, or given a value of another type."""
    copy = json.loads(json.dumps(document))
    parent = copy
    for step in path[:-1]:
        parent = parent[step]
    if change == "missing":
        del parent[path[-1]]
    elif change == "empty_object":
        parent[path[-1]] = {}
    else:
        parent[path[-1]] = "text" if isinstance(parent[path[-1]], (dict, list)) else {"value": 1}
    return copy


def fields(valid: bytes, before: bytes = b"", after: bytes = b"") -> list[Case]:
    """The field cases of a JSON document, each between before and after: each field missing, each object empty, and
    each field of another type. A document that is not JSON, such as a file of several lines, has none."""
    try:
        document = json.loads(valid)
    except ValueError:
        return []
    found = []
    for path, value in _field_paths(document):
        name = ".".join(str(step) for step in path)
        about = {
            "missing": f"the document without its field {name}, which the code may index later",
            "empty_object": f"{{}} in place of the object {name}, its required keys missing",
            WRONG_TYPE: f"a value of another type in place of {name}",
        }
        changes = [
            *(["missing"] if isinstance(path[-1], str) else []),
            *(["empty_object"] if isinstance(value, dict) else []),
            WRONG_TYPE,
        ]
        found += [
            Case(
                f"{change}:{name}",
                before + json.dumps(_changed(document, path, change)).encode() + after,
                about[change],
                field=True,
            )
            for change in changes
        ]
    return found


def _json(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", valid[: len(valid) // 2], "a document cut off part way"),
        Case("top_level_list", b"[]", "a list where the reader expects an object"),
        Case("top_level_string", b'"text"', "a string where the reader expects an object"),
        Case("top_level_null", b"null", "null where the reader expects an object"),
        Case("long_integer", b'{"value": ' + LONG_INTEGER + b"}", "an integer past Python's digit limit"),
        Case("deep_nesting", b"[" * DEPTH + b"]" * DEPTH, "nesting past the recursion limit"),
        Case(
            "scanned_nesting",
            b'{"value": ' + b"[" * SCANNED_DEPTH + b"]" * SCANNED_DEPTH + b"}",
            "a field nested as deep as the scanner reads and a walk over the value cannot recurse",
        ),
        Case("duplicate_keys", b'{"value": 1, "value": 2}', "one key twice in an object"),
        Case("nan", b'{"value": NaN}', "NaN, which Python's JSON reader takes and JSON does not define"),
        *fields(valid),
    ]


def _ended(content: bytes | str) -> bytes | str:
    """Bytes ended by a newline, when there are any; an empty file and the shapes that are not bytes as they are."""
    return content + b"\n" if isinstance(content, bytes) and content else content


def _json_lines(valid: bytes) -> list[Case]:
    """The JSON cases as a record's last line, each ended by its newline, since a last line without one is a torn
    write, and the cases a file of lines adds. The field cases change the last record, after the lines before it."""
    valid = valid.rstrip(b"\n")
    head, _, last = valid.rpartition(b"\n")
    return [
        *(replace(case, content=_ended(case.content)) for case in _json(valid) if not case.field),
        *fields(last, head + b"\n" if head else b"", b"\n"),
        Case("torn_line", valid + b"\n" + valid[: len(valid) // 2], "a last line cut off part way"),
        Case("blank_lines", b"\n\n" + valid + b"\n\n", "blank lines around a record"),
    ]


def _toml(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", b"value = [1, 2", "a document cut off part way"),
        Case("long_integer", b"value = " + LONG_INTEGER + b"\n", "an integer past Python's digit limit"),
        Case("deep_nesting", b"value = " + b"[" * DEPTH + b"]" * DEPTH + b"\n", "nesting past the recursion limit"),
        Case("duplicate_keys", b"value = 1\nvalue = 2\n", "one key twice in a table"),
        Case("wrong_field_types", b"tool = 1\nproject = 'x'\n", "scalars where tables are expected"),
    ]


def _xml(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", valid[: len(valid) // 2], "a document cut off part way"),
        Case("deep_nesting", b"<a>" * DEPTH + b"</a>" * DEPTH, "elements nested past the recursion limit"),
        Case(
            "scanned_nesting",
            b"<a>" * SCANNED_DEPTH + b"</a>" * SCANNED_DEPTH,
            "elements nested as deep as the parser reads and a walk over the tree cannot recurse",
        ),
        Case("duplicate_attributes", b'<Project a="1" a="2"/>', "one attribute twice on an element"),
        Case(
            "entity_declaration",
            b'<!DOCTYPE Project [<!ENTITY e "x">]><Project>&e;</Project>',
            "a DOCTYPE declaring an entity, which could expand without bound",
        ),
    ]


def _frontmatter(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", b"---\nname: example\ndescription: cut", "a frontmatter block with no closing line"),
        Case("no_frontmatter", b"# Title\n\nText.\n", "a Markdown file with no frontmatter block"),
        Case("top_level_list", b"---\n- name\n- description\n---\n", "a list where keys are expected"),
        Case(
            "deep_nesting",
            b"---\nname:\n" + b"".join(b" " * depth + b"a:\n" for depth in range(1, 3000)) + b"---\n",
            "a mapping nested thousands of levels",
        ),
        Case("duplicate_keys", b"---\nname: one\nname: two\n---\n", "one key twice"),
    ]


def _key_value(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", b"KEY", "a line cut off before its equals sign"),
        Case("duplicate_keys", valid + valid, "one key twice"),
        Case("long_line", b"KEY=" + b"x" * 1_000_000 + b"\n", "a value a million characters long"),
    ]


def _text(valid: bytes) -> list[Case]:
    return [
        *_common(valid),
        Case("truncated", valid[: len(valid) // 2], "a file cut off part way"),
        Case("long_line", b"x" * 1_000_000 + b"\n", "a line a million characters long"),
    ]


FORMATS = {
    "json": _json,
    "json-lines": _json_lines,
    "toml": _toml,
    "xml": _xml,
    "frontmatter": _frontmatter,
    "key-value": _key_value,
    "text": _text,
}
# The document each format's cases are built around, where a case needs a well-formed one.
VALID = {
    "json": b'{"value": 1}',
    "json-lines": b'{"value": 1}',
    "toml": b"value = 1\n",
    "xml": b"<Project><Item>value</Item></Project>",
    "frontmatter": b"---\nname: example\ndescription: An example.\n---\n\nBody.\n",
    "key-value": b"KEY=value\n",
    "text": b"line one\nline two\n",
}


def cases(format_name: str, valid: bytes | None = None) -> list[Case]:
    """Every case of a format, built around valid, or the format's own document when none is given."""
    return FORMATS[format_name](VALID[format_name] if valid is None else valid)


def link(target: Path, path: Path) -> None:
    """A link at path to target: a junction to target's folder on Windows, which needs no privilege where a file link
    does, else a symbolic link to the file. Either is a link a reader that refuses links must refuse."""
    if sys.platform == "win32":
        folder = target.parent / f"{target.name}.folder"
        folder.mkdir(exist_ok=True)
        import _winapi

        _winapi.CreateJunction(str(folder), str(path))
    else:
        path.symlink_to(target)


@contextmanager
def denied(path: Path) -> Iterator[None]:
    """The file at path unreadable to this process while the context lasts: held by a handle that shares nothing on
    Windows, else given no permissions."""
    if sys.platform == "win32":
        import _winapi

        handle = _winapi.CreateFile(str(path), _winapi.GENERIC_READ, 0, 0, _winapi.OPEN_EXISTING, 0, 0)
        try:
            yield
        finally:
            _winapi.CloseHandle(handle)
    else:
        path.chmod(0)
        try:
            yield
        finally:
            path.chmod(0o600)


@contextmanager
def placed(case: Case, path: Path, valid: bytes) -> Iterator[None]:
    """The case at path while the context lasts, its parent made: its bytes, a directory, a link to a well-formed file
    beside it, removed afterwards and never what it points to, or a well-formed file the process cannot read."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if case.content == DIRECTORY:
        path.mkdir()
        yield
    elif case.content == LINK:
        target = path.with_name(f"{path.name}.target")
        target.write_bytes(valid)
        link(target, path)
        try:
            yield
        finally:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink()
    elif case.content == UNREADABLE:
        path.write_bytes(valid)
        with denied(path):
            yield
    else:
        assert isinstance(case.content, bytes)
        path.write_bytes(case.content)
        yield
