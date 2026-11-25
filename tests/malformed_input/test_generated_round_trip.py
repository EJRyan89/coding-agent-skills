"""Generated round trips: each writer and reader pair, held to values Hypothesis builds around edge_values.py.

test_round_trip.py hands each writer the fixed edge values and combinations someone wrote down; this suite hands it
values Hypothesis generates around them, mixing those edge values with generated Unicode text, names, numbers, and
list sizes, so the combinations nobody wrote are tried too. Each test holds the same contract: the writer refuses a
value before it writes anything, where the test's own predicate, written here apart from the code under test, says a
refusal is due, or the reader returns exactly the value generated, and writing what it returned again gives the same
bytes. The generated value is the independent expected result.

Every test runs deterministically, as "Generated tests" in docs/dependency-updates.md says: the same examples on every
run and leg, no example database, and the example budget GENERATED names. A failing run prints the minimized value,
which rerunning the suite reproduces; a defect it finds is kept as an @example row beside the test that found it.
"""

from __future__ import annotations

import datetime
import string
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from edge_values import NAMES, PATHS, TEXT
from hypothesis import HealthCheck, configuration, example, given, settings
from hypothesis import strategies as st
from reader_table import REPOSITORY

sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("dotnet-format")))
sys.path.insert(0, str(sibling_scripts("github-activity-report")))

import dotnet_format_targets
import github_activity_report
import review_flags
from atomic_file import replace_text

from deployer import journal, manifest, names, source
from deployer.errors import DeployError
from deployer.kinds import ADAPTERS
from deployer.paths import Paths

# The budget: examples per test, with no deadline and no too_slow health check, the two checks that fail on wall-clock
# time, so a slow leg is never a failure; derandomize and no database, so every run tries the same examples.
GENERATED = settings(
    derandomize=True,
    database=None,
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

# Any code point, a lone surrogate included, which no UTF-8 file can hold and each writer must refuse or never take.
ANY_CHARACTER = st.characters(codec=None, exclude_categories=())


def edge_or(values: tuple[str, ...], generated: st.SearchStrategy[str]) -> st.SearchStrategy[str]:
    """An edge value from edge_values.py, or one generated, so both kinds and their mixtures are tried."""
    return st.one_of(st.sampled_from(values), generated)


ANY_TEXT = edge_or(TEXT, st.text(ANY_CHARACTER, max_size=60))
UTF8_TEXT = edge_or(TEXT, st.text(max_size=60))


def has_surrogate(text: str) -> bool:
    """Whether text holds a lone surrogate, which UTF-8 cannot encode."""
    return any("\ud800" <= character <= "\udfff" for character in text)


@pytest.fixture(autouse=True, scope="module")
def hypothesis_storage() -> Iterator[None]:
    """Hypothesis's own files in a temporary folder of this suite's, never a .hypothesis folder in the checkout."""
    checkout = REPOSITORY / ".hypothesis"
    before = hypothesis_files(checkout)
    with tempfile.TemporaryDirectory(prefix="hypothesis-") as folder:
        configuration.set_hypothesis_home_dir(folder)
        try:
            yield
        finally:
            configuration.set_hypothesis_home_dir(None)
    assert hypothesis_files(checkout) == before, "a generated test wrote into .hypothesis in the checkout"


def hypothesis_files(folder: Path) -> dict[str, tuple[int, int]] | None:
    """Each file in a .hypothesis folder with its size and modification time, or None when there is none: a folder an
    earlier run outside the runner left must stay as it was, as a missing one must stay missing."""
    if not folder.exists():
        return None
    return {
        path.relative_to(folder).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in folder.rglob("*")
        if path.is_file()
    }


# A skill, bundle, or adapter name a source gives: lowercase letters, digits, and inner hyphens, the source's own
# validator deciding the rest (its length and the Windows device names), since a source refuses any other name before
# anything is written. The validator defines which names are generated, never what reads back.
SKILL_NAMES = st.from_regex(r"[a-z0-9](?:[a-z0-9-]{0,40}[a-z0-9])?", fullmatch=True).filter(source.is_valid_name)
# A shared asset's name: one file name of the characters a source allows, as the source's check takes it.
SHARED_NAMES = st.text(string.ascii_letters + string.digits + "._ -", min_size=1, max_size=40).filter(
    lambda name: names.safe_name_problem(name, "shared asset") is None
)
HASHES = st.from_regex(r"sha256:[0-9a-f]{64}", fullmatch=True)


@st.composite
def source_entries(draw: st.DrawFn) -> tuple[str, dict[str, object]]:
    """A source's manifest entry as a deployment writes it, with the source directory and deployment time any text."""
    skills = draw(st.lists(SKILL_NAMES, max_size=4, unique=True))
    shared = draw(st.lists(SHARED_NAMES, max_size=3, unique=True))
    agents = [f"{name}.md" for name in draw(st.lists(SKILL_NAMES, max_size=3, unique=True))]
    adapters = draw(st.lists(st.sampled_from(skills), unique=True)) if skills else []
    items: dict[str, dict[str, dict[str, object]]] = {
        "skills": {
            name: {
                "hash": draw(HASHES),
                "skill_deps": draw(st.lists(st.sampled_from(skills), unique=True)),
                "shared_deps": draw(st.lists(st.sampled_from(shared), unique=True)) if shared else [],
                "agent_deps": draw(st.lists(st.sampled_from(agents), unique=True)) if agents else [],
            }
            for name in skills
        },
        "shared": {
            name: {"hash": draw(HASHES), "role": draw(st.sampled_from(("owner", "dependency")))} for name in shared
        },
        ADAPTERS: {name: {"hash": draw(HASHES)} for name in adapters},
        "agents": {name: {"hash": draw(HASHES)} for name in agents},
    }
    commit = draw(st.none() | st.from_regex(r"[0-9a-f]{40}|[0-9a-f]{64}", fullmatch=True))
    bundles = draw(st.lists(SKILL_NAMES, max_size=2, unique=True))
    entry = manifest.source_entry(
        draw(edge_or(PATHS, st.text(ANY_CHARACTER, min_size=1, max_size=60)).filter(bool)),
        draw(st.none() | ANY_TEXT),
        commit=commit,
        ref=None
        if commit is None
        else draw(st.from_regex(r"[A-Za-z0-9._/@{}~^+][A-Za-z0-9._/@{}~^+-]{0,40}", fullmatch=True)),
        requested_bundles=bundles,
        requested_bundle_members={bundle: draw(st.lists(SKILL_NAMES, max_size=3, unique=True)) for bundle in bundles},
        requested_skills=skills,
        items=items,  # type: ignore[arg-type]  # each entry holds the fields its kind records
    )
    source_id = draw(st.from_regex(r"[a-z0-9][a-z0-9-]{0,20}/[a-z0-9][a-z0-9._-]{0,20}", fullmatch=True))
    return source_id, dict(entry)


@GENERATED
@given(entry=source_entries(), run_id=st.sampled_from(("", "20260101-000000-ab12")))
def test_the_manifest_reads_back_every_generated_source_entry_or_refuses_it_unwritten(
    entry: tuple[str, dict[str, object]], run_id: str
) -> None:
    source_id, written_entry = entry
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ".deploy-manifest.json"
        written = manifest.load(path)
        written.data["last_run_id"] = run_id
        written.sources[source_id] = written_entry  # type: ignore[assignment]  # as source_entry built it
        if any(isinstance(value, str) and has_surrogate(value) for value in written_entry.values()):
            with pytest.raises(DeployError):
                written.save()
            assert not path.exists(), "the manifest was written before its refusal"
            return
        written.save()
        saved = path.read_bytes()
        read = manifest.load(path)
        assert read.data == written.data
        read.save()
        assert path.read_bytes() == saved


ROOTS = {"claude": "shared", "agents": ADAPTERS, "claude-agents": "agents"}


@GENERATED
@given(
    root=st.sampled_from(sorted(ROOTS)),
    steps=st.lists(
        st.tuples(SKILL_NAMES, HASHES, HASHES, st.booleans(), st.sampled_from(("backup", "install", "preserve"))),
        min_size=1,
        max_size=6,
    ),
)
def test_the_journal_reads_back_every_generated_entry_written(
    root: str, steps: list[tuple[str, str, str, bool, str]]
) -> None:
    with tempfile.TemporaryDirectory() as folder:
        paths = Paths(source_dir=Path(folder), home=Path(folder))
        run_id = "20260101-000000-ab12"
        record = journal.Journal(paths.staging_root / run_id / "journal.jsonl", run_id)
        journal.prepare_roots(paths)
        record.create()
        for name, backup_hash, staged_hash, retain, step in steps:
            item = f"{name}.md" if ROOTS[root] == "agents" else name
            if step == "backup":
                record.backup(root, item, backup_hash, retain)
            elif step == "install":
                record.install(root, item, staged_hash)
            else:
                record.preserve(root, item)
        assert journal._read_entries(record.path, run_id).entries == record.entries
        assert journal.pending_runs(paths) == [(run_id, True)]


def flag_refusal_due(category: str, body: str) -> bool:
    """Whether the flag store refuses a flag before writing it: a blank category or body, one past its length, 120
    characters for a category and 4000 for a body, or text no UTF-8 store can hold."""
    return any(
        not text or all(character.isspace() for character in text) or len(text) > limit or has_surrogate(text)
        for text, limit in ((category, 120), (body, 4000))
    )


@GENERATED
@given(category=ANY_TEXT, body=ANY_TEXT)
def test_the_flag_store_reads_back_every_generated_flag_or_refuses_it_unwritten(category: str, body: str) -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "flags.json"
        if flag_refusal_due(category, body):
            with pytest.raises(review_flags.FlagError):
                review_flags.add_flag(path, category=category, body=body)
            assert not path.exists(), "the store refused the flag after writing it"
            return
        flag = review_flags.add_flag(path, category=category, body=body)
        saved = path.read_bytes()
        stored = review_flags.load_store(path)
        assert [(item["category"], item["body"]) for item in stored["flags"]] == [(category, body)]
        assert stored["flags"][0] == flag
        assert path.read_bytes() == saved


# A login GitHub allows, built by its rule apart from the reader's pattern: alphanumeric runs joined by single hyphens,
# at most one underscore suffix, 39 characters at most.
LOGINS = st.from_regex(r"[A-Za-z0-9]{1,8}(?:-[A-Za-z0-9]{1,8}){0,3}(?:_[A-Za-z0-9]{1,4})?", fullmatch=True).filter(
    lambda login: len(login) <= 39
)
METRICS = tuple(github_activity_report.METRICS)


@st.composite
def history_rows(draw: st.DrawFn) -> github_activity_report.HistoryRow:
    month = draw(st.dates(min_value=datetime.date(1970, 1, 1))).strftime("%Y-%m")
    moment = draw(
        st.datetimes(timezones=st.timezones() | st.just(datetime.UTC)).filter(
            lambda moment: moment.utcoffset() is not None
        )
    )
    capped = draw(
        st.none()
        | st.lists(st.sampled_from(METRICS), unique=True).map(lambda chosen: tuple(m for m in METRICS if m in chosen))
    )
    return github_activity_report.HistoryRow(
        month,
        draw(LOGINS),
        {metric: draw(st.integers(min_value=0, max_value=10**30)) for metric in METRICS},
        draw(st.booleans()),
        moment.isoformat(),
        capped,
    )


@GENERATED
@given(rows=st.lists(history_rows(), max_size=5, unique_by=lambda row: (row.month, row.user.casefold())))
def test_the_activity_history_reads_back_every_generated_row(rows: list[github_activity_report.HistoryRow]) -> None:
    history = {(row.month, row.user.casefold()): row for row in rows}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "history.csv"
        replace_text(path, github_activity_report.render_history(history))
        saved = path.read_bytes()
        read = github_activity_report.read_history(path)
        assert read == history
        replace_text(path, github_activity_report.render_history(read))
        assert path.read_bytes() == saved


# A changed file's name: any text, the empty name and a NUL included, which git never lists and the writer refuses.
FILE_NAMES = edge_or((*PATHS, *NAMES), st.text(ANY_CHARACTER, max_size=40))


@GENERATED
@given(names_listed=st.lists(FILE_NAMES, max_size=6))
# The writer once wrote both, and the reader refused the empty name and split the other in two.
@example(names_listed=[""])
@example(names_listed=["a\0b"])
def test_the_dotnet_format_file_list_reads_back_every_generated_name_or_refuses_it_unwritten(
    names_listed: list[str],
) -> None:
    if not names_listed or any(not name or "\0" in name or has_surrogate(name) for name in names_listed):
        with pytest.raises(dotnet_format_targets.Failed):
            dotnet_format_targets.write_file_list(names_listed)
        return
    written = Path(dotnet_format_targets.write_file_list(names_listed))
    try:
        assert dotnet_format_targets.read_file_list(written) == names_listed
    finally:
        written.unlink()
