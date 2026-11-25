"""Generated round trips: each writer and reader pair, held to values Hypothesis builds around edge_values.py.

test_round_trip.py hands each writer the fixed edge values and combinations someone wrote down; this suite hands it
values Hypothesis generates around them, mixing those edge values with generated Unicode text, names, numbers, and
list sizes, so the combinations nobody wrote are tried too. Each test builds its expected result here, from the
generated values and apart from the code under test, and holds the pair to it: where the test's own predicate says a
refusal is due, the writer refuses and the file is not written; otherwise the reader returns the expected value, and
writing what it returned again gives the same bytes. Each test's docstring says what it holds.

Every test runs deterministically, as "Generated tests" in docs/dependency-updates.md says: the same examples on every
run and leg, no example database, and the example budget GENERATED names. A failing run prints the minimized value,
which rerunning the suite reproduces; a defect it finds is kept as an @example row beside the test that found it.
"""

from __future__ import annotations

import copy
import datetime
import string
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from edge_values import NAMES, PATHS, TEXT
from hypothesis import HealthCheck, configuration, example, given, settings
from hypothesis import strategies as st
from reader_table import REPOSITORY

sys.path.insert(0, str(REPOSITORY))
sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests" / "deployer"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("dotnet-format")))
sys.path.insert(0, str(sibling_scripts("github-activity-report")))

import dotnet_format_targets
import github_activity_report
import json_writer
import review_flags
from atomic_file import replace_text
from harness import backup_entry, install_entry

from deployer import journal, manifest, names, source
from deployer.errors import DeployError
from deployer.kinds import ADAPTERS
from deployer.paths import Paths

# The budget: examples per test, with no deadline and no too_slow health check, which fail on wall-clock time, so a slow
# leg is never a failure; derandomize and no database, so every run tries the same examples.
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
# A shared asset's name: one file name of the characters a source allows, as the source's check takes it. It may be a
# skill's name too, which the manifest refuses to record.
SHARED_NAMES = st.text(string.ascii_letters + string.digits + "._ -", min_size=1, max_size=40).filter(
    lambda name: names.safe_name_problem(name, "shared asset") is None
)
HASHES = st.from_regex(r"sha256:[0-9a-f]{64}", fullmatch=True)
SOURCE_IDS = st.from_regex(r"[a-z0-9][a-z0-9-]{0,20}/[a-z0-9][a-z0-9._-]{0,20}", fullmatch=True)


@st.composite
def source_entries(draw: st.DrawFn) -> dict[str, Any]:
    """A source's manifest entry in the shape a deployment records it, built here field by field, with the source
    directory and the deployment time any text."""
    skills = draw(st.lists(SKILL_NAMES, max_size=4, unique=True))
    shared = draw(st.lists(SHARED_NAMES | st.sampled_from(skills or ["a"]), max_size=3, unique=True))
    agents = [f"{name}.md" for name in draw(st.lists(SKILL_NAMES, max_size=3, unique=True))]
    adapters = draw(st.lists(st.sampled_from(skills), unique=True)) if skills else []
    entry: dict[str, Any] = {"source_dir": draw(edge_or(PATHS, st.text(ANY_CHARACTER, min_size=1, max_size=60)))}
    commit = draw(st.none() | st.from_regex(r"[0-9a-f]{40}|[0-9a-f]{64}", fullmatch=True))
    if commit is not None:
        entry["source_commit"] = commit
        entry["source_ref"] = draw(st.from_regex(r"[A-Za-z0-9._/@{}~^+][A-Za-z0-9._/@{}~^+-]{0,40}", fullmatch=True))
    bundles = draw(st.lists(SKILL_NAMES, max_size=2, unique=True))
    entry |= {
        "deployed_at": draw(st.none() | ANY_TEXT),
        "requested_bundles": bundles,
        "requested_bundle_members": {
            bundle: draw(st.lists(SKILL_NAMES, max_size=3, unique=True)) for bundle in bundles
        },
        "requested_skills": skills,
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
    return entry


def manifest_refusal_due(entry: dict[str, Any]) -> bool:
    """Whether saving the entry is refused before the manifest is written: a text no UTF-8 file holds, or one name
    recorded as a skill and as a shared asset, which share one destination folder."""
    texts = [value for value in (entry["source_dir"], entry["deployed_at"]) if isinstance(value, str)]
    return any(has_surrogate(text) for text in texts) or bool(set(entry["skills"]) & set(entry["shared"]))


SHARED_WITH_A_SKILL = {
    "source_dir": "/a",
    "deployed_at": None,
    "requested_bundles": [],
    "requested_bundle_members": {},
    "requested_skills": ["0"],
    "skills": {"0": {"hash": "sha256:" + "0" * 64, "skill_deps": [], "shared_deps": ["0"], "agent_deps": []}},
    "shared": {"0": {"hash": "sha256:" + "1" * 64, "role": "owner"}},
    ADAPTERS: {},
    "agents": {},
}


@GENERATED
@given(source_id=SOURCE_IDS, entry=source_entries(), run_id=st.sampled_from(("", "20260101-000000-ab12")))
# save once wrote this entry, and load refused it: one name owned as a skill and as a shared asset.
@example(source_id="a/b", entry=SHARED_WITH_A_SKILL, run_id="")
def test_the_manifest_reads_back_every_generated_source_entry_or_refuses_it_unwritten(
    source_id: str, entry: dict[str, Any], run_id: str
) -> None:
    """The manifest saves the generated entry and reads back exactly it and the run ID, and saving what it read gives
    the same bytes; or, where manifest_refusal_due says so, save refuses and no manifest is written."""
    expected = {"last_run_id": run_id, "sources": {source_id: entry}}
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ".deploy-manifest.json"
        written = manifest.load(path)
        written.data["last_run_id"] = run_id
        written.sources[source_id] = copy.deepcopy(entry)  # type: ignore[assignment]  # the shape a run records
        if manifest_refusal_due(entry):
            with pytest.raises(DeployError):
                written.save()
            assert not path.exists(), "the manifest was written before its refusal"
            return
        written.save()
        saved = path.read_bytes()
        read = manifest.load(path)
        assert {key: value for key, value in read.data.items() if key != "manifest_version"} == expected
        again = Path(folder) / "again.json"
        manifest.Manifest(again, read.data).save()
        assert again.read_bytes() == saved


def test_a_manifest_naming_one_destination_as_a_skill_and_a_shared_asset_is_refused_unwritten() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / ".deploy-manifest.json"
        written = manifest.load(path)
        written.sources["a/b"] = copy.deepcopy(SHARED_WITH_A_SKILL)  # type: ignore[assignment]  # as a run records it
        with pytest.raises(DeployError) as refused:
            written.save()
        assert str(refused.value).splitlines()[0] == (
            "ERROR: Destination name '0' is owned as a skill by 'a/b' and as a shared asset by 'a/b' in manifest"
        )
        assert not path.exists()


ROOTS = {"claude": "shared", "agents": ADAPTERS, "claude-agents": "agents"}
RUN_ID = "20260101-000000-ab12"


def expected_entry(root: str, item: str, backup_hash: str, staged_hash: str, retain: bool, step: str) -> dict[str, Any]:
    """The entry a step journals, built from the step by the harness's own entry builders, which name each root's
    folders as literals, and, for a preserve, here."""
    label = "" if root == "claude" else root
    if step == "backup":
        return backup_entry(item, backup_hash, retain, RUN_ID, label)
    if step == "install":
        return install_entry(item, staged_hash, label)
    prefix = {"claude": "skills", "agents": "agents", "claude-agents": "claude-agents"}[root]
    return {
        "op": "preserve",
        "root": root,
        "item": item,
        "from": f"{prefix}/{item}.deploying-bak",
        "to": f".backups/{RUN_ID}/{item}",
    }


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
    """The journal reads back the entries built here from the generated steps, in order, and journaling what it read
    into a second journal gives the same bytes; a run whose journal remains is pending, and recoverable."""
    with tempfile.TemporaryDirectory() as folder:
        paths = Paths(source_dir=Path(folder), home=Path(folder))
        record = journal.Journal(paths.staging_root / RUN_ID / "journal.jsonl", RUN_ID)
        journal.prepare_roots(paths)
        record.create()
        expected = []
        for name, backup_hash, staged_hash, retain, step in steps:
            item = f"{name}.md" if ROOTS[root] == "agents" else name
            if step == "backup":
                record.backup(root, item, backup_hash, retain)
            elif step == "install":
                record.install(root, item, staged_hash)
            else:
                record.preserve(root, item)
            expected.append(expected_entry(root, item, backup_hash, staged_hash, retain, step))
        read = journal._read_entries(record.path, RUN_ID).entries
        assert read == expected
        assert journal.pending_runs(paths) == [(RUN_ID, True)]
        again = journal.Journal(Path(folder) / "again.jsonl", RUN_ID)
        again.create()
        for entry in read:
            again.write(entry)
        assert again.path.read_bytes() == record.path.read_bytes()


def flag_refusal_due(category: str, body: str) -> bool:
    """Whether the flag store refuses a flag before writing it: a blank category or body, one past its length, 120
    characters for a category and 4000 for a body, or text no UTF-8 store can hold."""
    return any(
        not text or all(character.isspace() for character in text) or len(text) > limit or has_surrogate(text)
        for text, limit in ((category, 120), (body, 4000))
    )


@GENERATED
@given(category=ANY_TEXT, body=ANY_TEXT)
# A body no UTF-8 store can hold, which the JSON writer refuses before the store is written.
@example(category="plain text", body="\ud800")
def test_the_flag_store_reads_back_every_generated_flag_or_refuses_it_unwritten(category: str, body: str) -> None:
    """The store reads back one open flag holding the generated category and body and no target, and writing the
    store it read gives the same bytes; or, where flag_refusal_due says so, the flag is refused and no store written."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "flags.json"
        if flag_refusal_due(category, body):
            with pytest.raises((review_flags.FlagError, json_writer.JsonWriteRefused)):
                review_flags.add_flag(path, category=category, body=body)
            assert not path.exists(), "the store refused the flag after writing it"
            return
        review_flags.add_flag(path, category=category, body=body)
        stored = review_flags.load_store(path)
        [flag] = stored["flags"]
        assert (flag["category"], flag["body"], flag["status"]) == (category, body, "open")
        assert (flag["repository"], flag["pull_number"], flag["review_version"], flag["finding_id"]) == (None,) * 4
        again = Path(folder) / "again.json"
        review_flags._write_store(again, stored)
        assert again.read_bytes() == path.read_bytes()


# A login GitHub allows, built by its rule apart from the reader's pattern: alphanumeric runs joined by single hyphens,
# at most one underscore suffix, 39 characters at most.
LOGINS = st.from_regex(r"[A-Za-z0-9]{1,8}(?:-[A-Za-z0-9]{1,8}){0,3}(?:_[A-Za-z0-9]{1,4})?", fullmatch=True).filter(
    lambda login: len(login) <= 39
)
METRICS = tuple(github_activity_report.METRICS)
# A UTC offset, fixed rather than a named zone, which needs a time zone database Windows lacks and differs from machine
# to machine.
OFFSETS = st.builds(
    datetime.timezone,
    st.timedeltas(
        min_value=-datetime.timedelta(hours=23, minutes=59), max_value=datetime.timedelta(hours=23, minutes=59)
    ),
) | st.just(datetime.UTC)


@st.composite
def history_rows(draw: st.DrawFn) -> github_activity_report.HistoryRow:
    month = draw(st.dates(min_value=datetime.date(1970, 1, 1))).strftime("%Y-%m")
    moment = draw(st.datetimes(timezones=OFFSETS))
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
    """The history reads back exactly the generated rows, keyed by month and login, and rendering what it read again
    gives the same bytes."""
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
    """The list reads back exactly the generated names, in order, and writing what it read gives the same bytes; or,
    for no names, an empty name, one holding a NUL, or one no UTF-8 file holds, the writer refuses before it makes a
    file."""
    if not names_listed or any(not name or "\0" in name or has_surrogate(name) for name in names_listed):
        with (
            mock.patch.object(tempfile, "mkstemp") as made,
            pytest.raises(dotnet_format_targets.Failed),
        ):
            dotnet_format_targets.write_file_list(names_listed)
        made.assert_not_called()
        return
    written = Path(dotnet_format_targets.write_file_list(names_listed))
    try:
        read = dotnet_format_targets.read_file_list(written)
        assert read == names_listed
        again = Path(dotnet_format_targets.write_file_list(read))
        try:
            assert again.read_bytes() == written.read_bytes()
        finally:
            again.unlink()
    finally:
        written.unlink()
