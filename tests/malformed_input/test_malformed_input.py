"""Every reader of a file or record, held to every malformed-input case of its format.

readers.py lists each reader with its format, the refusals its contract names, and the cases it documents accepting;
corpus.py builds the cases. Each case placed where the reader expects its file must end in one of the reader's named
refusals or in a documented acceptance, never an escaped traceback or another exception type. Each reader also reads
its own well-formed document, so a reader whose adapter is broken cannot pass by refusing everything.

A reader that fails a case today is listed in known_failures.json with the open issue that fixes it, never marked as
an expected failure, which validation refuses. A listed case must still fail, so its fix removes the entry, and an
unlisted one must pass, so the list never grows in silence; tests/validation/known_failures.py holds the list's shape
and refuses a pull request that closes an issue an entry still names.
"""

from __future__ import annotations

import sys
import tempfile
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
# Any other exception propagates and fails its test with the traceback, listed as a known failure or not.
ESCAPES = (ArithmeticError, AttributeError, LookupError, OSError, RecursionError, TypeError, ValueError)


def failure(reader: readers.Reader, case: corpus.Case, folder: Path) -> str | None:
    """What is wrong with the reader's outcome on the case, or None when it refuses as named or accepts as
    documented."""
    path = folder / reader.filename
    corpus.place(case, path, reader.valid)
    refusals: tuple[type[BaseException], ...] = (*reader.refusals, readers.NotText)
    try:
        reader.read(path)
    except refusals:
        return None
    except ESCAPES as error:
        return f"raised {type(error).__name__}: {str(error)[:300]}"
    finally:
        if case.content == corpus.LINK:
            corpus.remove_link(path)
    if case.name in reader.accepts:
        return None
    return "accepted the case, which the reader documents neither refusing nor accepting"


@pytest.mark.parametrize("reader", readers.READERS, ids=lambda reader: reader.name)
def test_each_reader_reads_its_own_well_formed_document(reader: readers.Reader) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / reader.filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(reader.valid)
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
