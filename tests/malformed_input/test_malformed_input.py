"""Every reader of a file or record, held to every malformed-input case of its format.

readers.py lists each reader with its format, the refusals its contract names, and the cases it documents accepting;
corpus.py builds the cases. Each case placed where the reader expects its file must end in one of the reader's named
refusals or in a documented acceptance, never an escaped traceback or another exception type. A field case that
removes a field or empties an object may also end in an acceptance, since whether a field is required is the reader's
own contract, which its suite holds; one that gives a field a value of another type may not, unless the reader
documents taking it, since a reader that answers past a shape it does not know answers on evidence it never read. A
refusal leaves the case's folder as it was, so a step that writes before it validates fails; the files a reader takes
beside its own are written by its prepare before the case is read. Each reader also reads its own well-formed
document, so a reader whose adapter is broken cannot pass by refusing everything.

A reader that fails a case today is listed in known_failures.json with the open issue that fixes it, never marked as
an expected failure, which validation refuses. A listed case must still fail, so its fix removes the entry, and an
unlisted one must pass, so the list never grows in silence; tests/validation/known_failures.py holds the list's shape
and refuses a pull request that closes an issue an entry still names.
"""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import corpus
import readers
from known_failures import KNOWN_FAILURES, read_known_failures

REPOSITORY = Path(__file__).resolve().parents[2]
KNOWN = {(entry.reader, entry.case): entry.issue for entry in read_known_failures(REPOSITORY / KNOWN_FAILURES)}
PAIRS = [(reader, case) for reader in readers.READERS for case in readers.applicable_cases(reader)]
# The exceptions a malformed file makes a reader's parse or shape check raise when nothing turns them into its refusal.
# Any other exception propagates and fails its test with the traceback, listed as a known failure or not. Python's
# parser raises MemoryError for source nested past its own stack.
ESCAPES = (ArithmeticError, AttributeError, LookupError, MemoryError, OSError, RecursionError, TypeError, ValueError)


def tree(folder: Path) -> dict[str, bytes | None]:
    """Every entry below folder by its relative path: a file's bytes, or None for a folder or a file this process
    cannot read."""
    found: dict[str, bytes | None] = {}
    for entry in sorted(folder.rglob("*")):
        try:
            found[entry.relative_to(folder).as_posix()] = entry.read_bytes() if entry.is_file() else None
        except OSError:
            found[entry.relative_to(folder).as_posix()] = None
    return found


def failure(reader: readers.Reader, case: corpus.Case, folder: Path) -> str | None:
    """What is wrong with the reader's outcome on the case, or None when it refuses as named, having written nothing
    below the case's folder, or accepts as documented, or accepts a field case."""
    path = folder / reader.filename
    refusals: tuple[type[BaseException], ...] = (*reader.refusals, readers.NotText)
    with corpus.placed(case, path, reader.valid):
        if reader.prepare is not None:
            reader.prepare(path)
        before = tree(folder)
        try:
            reader.read(path)
        except refusals:
            after = tree(folder)
            written = sorted(
                name
                for name in before.keys() | after.keys()
                if name not in before or name not in after or before[name] != after[name]
            )
            return f"refused the case after writing {written}" if written else None
        except ESCAPES as error:
            return f"raised {type(error).__name__}: {str(error)[:300]}"
    if case.name in reader.accepts:
        return None
    if case.field and (not case.name.startswith(f"{corpus.WRONG_TYPE}:") or reader.open_fields):
        return None
    if case.field:
        return "accepted a field of another type, which the reader documents neither refusing nor accepting"
    return "accepted the case, which the reader documents neither refusing nor accepting"


@pytest.mark.parametrize("reader", readers.READERS, ids=lambda reader: reader.name)
def test_each_reader_reads_its_own_well_formed_document(reader: readers.Reader) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / reader.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(reader.valid)
        if reader.prepare is not None:
            reader.prepare(path)
        reader.read(path)


@pytest.mark.parametrize(("reader", "case"), PAIRS, ids=[f"{reader.name}-{case.name}" for reader, case in PAIRS])
def test_each_reader_refuses_or_documents_accepting_each_case(reader: readers.Reader, case: corpus.Case) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        found = failure(reader, case, Path(temporary))
    issue = KNOWN.get((reader.name, case.name))
    if issue is None:
        assert found is None, f"{reader.name} on case {case.name} ({case.about}): {found}"
    else:
        assert found is not None, (
            f"{reader.name} now passes case {case.name}: remove its entry from {KNOWN_FAILURES.as_posix()} in the "
            f"pull request that fixes #{issue}"
        )


def test_each_known_failure_names_a_listed_reader_and_one_of_its_cases() -> None:
    pairs = {(reader.name, case.name) for reader, case in PAIRS}
    unknown = [f"{name} on case {case}" for name, case in KNOWN if (name, case) not in pairs]
    assert not unknown, unknown


def test_a_pending_reader_is_not_yet_in_a_table() -> None:
    held = {reader.name for reader in readers.READERS}
    assert not held & readers.PENDING.keys(), (
        f"remove each held reader from PENDING: {sorted(held & readers.PENDING.keys())}"
    )


def _refuse(path: Path) -> object:
    raise ValueError(f"refused {path.name}")


def _write_then_refuse(path: Path) -> object:
    path.with_name("written").write_text("partial", encoding="utf-8")
    raise ValueError("refused after writing")


def _open(path: Path) -> object:
    return path.read_bytes()


def _prepared(path: Path) -> None:
    path.with_name("companion").write_text("beside", encoding="utf-8")


@pytest.mark.parametrize(
    ("read", "case_name", "prepare", "expected"),
    [
        (_refuse, "empty", None, None),
        (_refuse, "empty", _prepared, None),
        (_write_then_refuse, "empty", None, "refused the case after writing ['written']"),
        (_open, "empty", None, "accepted the case, which the reader documents neither refusing nor accepting"),
        (_open, "missing:value", None, None),
        (
            _open,
            "wrong_type:value",
            None,
            "accepted a field of another type, which the reader documents neither refusing nor accepting",
        ),
        *([(_open, corpus.UNREADABLE, None, "raised PermissionError")] if corpus.can_deny_reading() else []),
    ],
)
def test_failure_names_a_write_before_a_refusal_an_escape_and_an_undocumented_acceptance(
    read: Callable[[Path], object], case_name: str, prepare: Callable[[Path], None] | None, expected: str | None
) -> None:
    reader = readers.Reader("example.read", "json", b'{"value": 1}', read, (ValueError,), prepare=prepare)
    case = next(case for case in readers.applicable_cases(reader) if case.name == case_name)
    with tempfile.TemporaryDirectory() as temporary:
        found = failure(reader, case, Path(temporary))
    if expected is None:
        assert found is None
    else:
        assert found is not None
        assert found.startswith(expected), found


def test_a_field_of_another_type_passes_where_the_reader_documents_taking_it_or_leaves_fields_to_its_caller() -> None:
    documented = readers.Reader(
        "example.read", "json", b'{"value": 1}', _open, (ValueError,), accepts={"wrong_type:value": "unread"}
    )
    open_fields = readers.Reader(
        "example.read", "json", b'{"value": 1}', _open, (ValueError,), open_fields="it returns the document whole"
    )
    for reader in (documented, open_fields):
        case = next(case for case in readers.applicable_cases(reader) if case.name == "wrong_type:value")
        with tempfile.TemporaryDirectory() as temporary:
            assert failure(reader, case, Path(temporary)) is None


def test_a_parsers_grammar_builds_its_nesting_and_number_cases_in_place_of_the_formats() -> None:
    reader = readers.Reader(
        "example.parse",
        "text",
        b"(a)",
        _open,
        (ValueError,),
        nesting=lambda levels: b"(" * levels + b"a" + b")" * levels,
        number=lambda digits: b"#" + digits,
    )
    found = {case.name: case.content for case in readers.applicable_cases(reader)}
    assert found["deep_nesting"] == b"(" * corpus.SCANNED_DEPTH + b"a" + b")" * corpus.SCANNED_DEPTH
    assert found["long_integer"] == b"#" + corpus.LONG_INTEGER
    json_reader = readers.Reader("example.read", "json", b'{"value": 1}', _open, (ValueError,), nesting=lambda n: b"x")
    nested = [case for case in readers.applicable_cases(json_reader) if case.name == "deep_nesting"]
    assert [case.content for case in nested] == [b"x"]


def test_a_field_case_changes_one_field_of_the_readers_own_document() -> None:
    found = {case.name: json.loads(case.content) for case in corpus.fields(b'{"name": "a", "items": [{"id": 1}]}')}
    assert found == {
        "missing:name": {"items": [{"id": 1}]},
        "wrong_type:name": {"name": {"value": 1}, "items": [{"id": 1}]},
        "missing:items": {"name": "a"},
        "wrong_type:items": {"name": "a", "items": "text"},
        "empty_object:items.0": {"name": "a", "items": [{}]},
        "wrong_type:items.0": {"name": "a", "items": ["text"]},
        "missing:items.0.id": {"name": "a", "items": [{}]},
        "wrong_type:items.0.id": {"name": "a", "items": [{"id": {"value": 1}}]},
    }


def test_a_json_lines_field_case_changes_the_last_record_after_the_lines_before_it() -> None:
    found = {case.name: case.content for case in corpus.cases("json-lines", b'{"a": 1}\n{"b": {"c": 2}}\n')}
    assert found["empty_object:b"] == b'{"a": 1}\n{"b": {}}\n'
    assert found["missing:b.c"] == b'{"a": 1}\n{"b": {}}\n'
    assert "missing:a" not in found
