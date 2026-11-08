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

Every function reader_scan.py finds calling a shared reader is held by a table, reached from a held reader of its own
module, owed in readers.py's PENDING, or named in its NOT_READERS with why it is no reader; an entry of either that the
scan no longer finds unheld fails, and no function is in both, or in a table and NOT_READERS.

A reader that fails a case today is listed in known_failures.json with the open issue that fixes it, never marked as
an expected failure, which validation refuses. A listed case must still fail, so its fix removes the entry, and an
unlisted one must pass, so the list never grows in silence; tests/validation/known_failures.py holds the list's shape
and refuses a pull request that closes an issue an entry still names.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "validation"))

import corpus
import reader_scan
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


def test_the_journal_survey_stops_at_a_well_formed_run_beside_no_manifest() -> None:
    # A run saves the manifest before it writes its journal, so a journal beside none means the manifest was moved or
    # lost: whether the run was committed cannot be told, and the survey stops at it as recovery does, however well
    # formed its journal. Every other journal case is read beside the manifest the reader's prepare writes.
    journal_readers = [reader for reader in readers.READERS if reader.name.startswith("deployer.journal.pending_runs")]
    assert journal_readers
    for reader in journal_readers:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / reader.filename
            path.parent.mkdir(parents=True)
            path.write_bytes(reader.valid)
            with pytest.raises(reader.refusals, match=r"the run needs the user: \[\('run', False\)\]"):
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


def test_a_function_named_no_reader_is_in_no_table_and_not_pending() -> None:
    held = {reader_scan.entry_name(reader.name) for reader in readers.READERS}
    assert not held & readers.NOT_READERS.keys(), (
        f"a reader a table holds is no NOT_READERS entry: {sorted(held & readers.NOT_READERS.keys())}"
    )
    assert not readers.PENDING.keys() & readers.NOT_READERS.keys(), (
        f"a function is owed as a reader or named no reader, never both: "
        f"{sorted(readers.PENDING.keys() & readers.NOT_READERS.keys())}"
    )


def test_every_function_that_calls_a_shared_reader_is_held_reached_pending_or_named_no_reader() -> None:
    held = {reader_scan.entry_name(reader.name) for reader in readers.READERS}
    unheld = reader_scan.unheld_readers(held)
    assert [name for name in unheld if name not in readers.PENDING and name not in readers.NOT_READERS] == [], (
        "add each to its area's readers_*.py table, to PENDING with the issue that adds it, or to NOT_READERS with why "
        "it is no reader"
    )
    assert [name for name in readers.PENDING if name not in unheld] == [], (
        "remove each from PENDING: a table holds it, a held reader reaches it, or it calls no shared reader"
    )
    assert [name for name in readers.NOT_READERS if name not in unheld] == [], (
        "remove each from NOT_READERS: a table holds it, a held reader reaches it, or it calls no shared reader"
    )


def test_the_scan_finds_each_spelling_of_a_shared_reader_call_and_credits_a_held_reader_s_helpers() -> None:
    source = (
        "import json_reader\n"
        "import frontmatter as fm\n"
        "from xml_reader import read_xml\n"
        "from json_reader import loads as parse, JsonRefused\n"
        "def attribute(text):\n    return json_reader.loads(text)\n"
        "def aliased_module(path):\n    return fm.read(path)\n"
        "def imported_name(text):\n    return read_xml(text)\n"
        "def renamed(text):\n    return parse(text)\n"
        "def outer(text):\n    def inner():\n        return json_reader.raw_decode(text)\n    return inner()\n"
        "class Store:\n    def load(self, path):\n        return json_reader.read_file(path)\n"
        "def refusal_only():\n    return JsonRefused\n"
        "def held(text):\n    return _helper(text)\n"
        "def _helper(text):\n    return json_reader.loads(text)\n"
    )
    assert reader_scan.reading_functions("tools.example", source) == [
        "tools.example.attribute",
        "tools.example.aliased_module",
        "tools.example.imported_name",
        "tools.example.renamed",
        "tools.example.outer",
        "tools.example.Store.load",
        "tools.example._helper",
    ]
    covered = reader_scan.reached("tools.example", source, {"tools.example.held"})
    assert "tools.example._helper" in covered
    assert "tools.example.attribute" not in covered
    assert reader_scan.entry_name("deployer.journal.pending_runs (backup entry)") == "deployer.journal.pending_runs"
    assert reader_scan.entry_name("review_analyzers.inventory:package.json") == "review_analyzers.inventory"


def test_the_scan_credits_a_held_reader_with_the_functions_a_module_level_table_it_reads_names() -> None:
    source = (
        "import json_reader\n"
        "def held(name, text):\n    return PARSERS[name](text)\n"
        "def _tabled(text):\n    return json_reader.loads(text)\n"
        "PARSERS: dict = {'a.json': _tabled}\n"
        "def _untabled(text):\n    return json_reader.loads(text)\n"
        "OTHER = {'b.json': _untabled}\n"
        "def _loaded(path):\n    return json_reader.read_file(path)\n"
        "def also_held(name):\n    return DEFAULTS[name]\n"
        "DEFAULTS = _loaded('defaults.json')\n"
    )
    covered = reader_scan.reached("tools.example", source, {"tools.example.held", "tools.example.also_held"})
    assert "tools.example._tabled" in covered
    assert "tools.example._untabled" not in covered
    # A loader a module-level value calls reads at import time, never through the value, so no reader of it is held.
    assert "tools.example._loaded" not in covered


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


def test_a_link_case_is_a_link_to_the_well_formed_file_where_the_platform_makes_one(tmp_path: Path) -> None:
    target = tmp_path / "file.target"
    target.write_bytes(b"well-formed")
    made: list[tuple[Path, Path]] = []
    with mock.patch.object(Path, "symlink_to", autospec=True, side_effect=lambda path, to: made.append((path, to))):
        corpus.link(target, tmp_path / "file")
    # A file link, which a reader that followed it would read and accept, and no junction beside it.
    assert made == [(tmp_path / "file", target)]
    assert not (tmp_path / "file.target.folder").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is the fallback on Windows alone")
def test_a_link_case_falls_back_to_a_junction_only_where_windows_withholds_the_privilege(tmp_path: Path) -> None:
    target = tmp_path / "file.target"
    target.write_bytes(b"well-formed")
    # Windows' own refusal: its winerror is the fourth argument, from which it sets errno.
    withheld = OSError(22, "A required privilege is not held by the client", None, corpus.WINDOWS_PRIVILEGE_NOT_HELD)
    with mock.patch.object(Path, "symlink_to", side_effect=withheld):
        corpus.link(target, tmp_path / "file")
    # A junction: a folder that is a reparse point, not a symbolic link.
    attributes = getattr(os.lstat(tmp_path / "file"), "st_file_attributes", 0)
    assert (tmp_path / "file").is_dir()
    assert not (tmp_path / "file").is_symlink()
    assert attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    (tmp_path / "file").rmdir()
    denied = OSError(13, "Access is denied", None, 5)
    with mock.patch.object(Path, "symlink_to", side_effect=denied), pytest.raises(OSError, match="Access is denied"):
        corpus.link(target, tmp_path / "file")
    assert not (tmp_path / "file").exists()
