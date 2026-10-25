"""Every document a writer produces passes its reader: a round trip for each writer and reader pair.

Each test builds documents with the writer's own code over the valid edge values in edge_values.py, writes each, and
holds the reader to it: the writer refuses a value before it writes anything, or the reader accepts the document and
returns what was written. A writer that saves what its own reader then refuses, as repo-cleanup's sweep once saved a
plan holding a branch named only by Unicode whitespace, fails here. corpus.py and test_malformed_input.py hold the
same readers to damaged input; a pair added later takes a round trip here, as "Validation" in docs/adding-a-skill.md
says.
"""

from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path
from typing import cast

import pytest
from edge_values import NOT_BLANK, ONE_LINE, PATHS, SIZES, TEXT, sized
from reader_table import REPOSITORY
from readers_core_review import POLICY
from readers_repository import GUIDANCE_SUBJECTS, audit_repository
from readers_skills import DASHBOARD_ITEM, _Detector

sys.path.insert(0, str(REPOSITORY / "skills" / "ejryan89-skill-core" / "scripts"))
sys.path.insert(0, str(REPOSITORY / "tests" / "deployer"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
sys.path.insert(0, str(sibling_scripts("github-activity-report")))
sys.path.insert(0, str(sibling_scripts("update-pr-dashboard")))

import dashboard_pipeline
import github_activity_report
import review_flags
import review_records
import review_run
import review_state
import test_review_records as review_records_suite
import test_review_run as review_run_suite
import update_pr_dashboard
from atomic_file import replace_text
from guidance_record import Changelog, GuidanceRecord, Page
from harness import SOURCE_ID, DeployerTestCase, forward

from deployer import config, journal, manifest, platform_support
from deployer.kinds import ADAPTERS
from deployer.paths import Paths

AREAS = audit_repository.AREA_NAMES


@pytest.mark.parametrize("size", SIZES)
def test_the_rotation_record_reads_back_every_entry_written(tmp_path: Path, size: int) -> None:
    entries = [
        audit_repository.Entry(
            f"2026-{index % 12 + 1:02d}-{index % 28 + 1:02d}", f"v0.{index}.0", AREAS[index % len(AREAS)]
        )
        for index in range(size)
    ]
    path = tmp_path / "rotation.json"
    audit_repository.write_record(path, entries)
    assert audit_repository.read_record(path) == entries


def _pages(size: int, governs: str, reads: int) -> list[Page]:
    return [
        Page(
            f"https://example.com/{name}/page-{index}?section=Ünï",
            GUIDANCE_SUBJECTS[index % len(GUIDANCE_SUBJECTS)],
            governs,
            tuple(f"v1.{read}.{index}" for read in range(reads)),
        )
        for index, name in enumerate(sized(("docs", "日本語", "a"), size))
    ]


@pytest.mark.parametrize(
    ("size", "governs", "reads", "changelogs"), list(itertools.product((1, 3), ONE_LINE, SIZES, (0, 1)))
)
def test_the_guidance_record_reads_back_every_page_and_changelog_written(
    tmp_path: Path, size: int, governs: str, reads: int, changelogs: int
) -> None:
    record = GuidanceRecord(
        _pages(size, governs, reads),
        [
            Changelog(GUIDANCE_SUBJECTS[0], "https://example.com/changelog.md", "markdown", "1.0.10")
            for _ in range(changelogs)
        ],
    )
    path = tmp_path / "guidance.json"
    audit_repository.write_guidance(path, record)
    assert audit_repository.read_guidance_record(path, GUIDANCE_SUBJECTS) == record


@pytest.mark.parametrize(
    ("users", "capped", "partial"),
    list(itertools.product(SIZES, (None, (), tuple(github_activity_report.METRICS)), (False, True))),
)
def test_the_activity_history_reads_back_every_row_written(
    tmp_path: Path, users: int, capped: tuple[str, ...] | None, partial: bool
) -> None:
    # A login is one GitHub accepts, which the report checks before it searches: one character, the longest, and
    # with each separator a login may hold.
    logins = sized(("a", "A" * 39, "a-b-c", "x1_y2"), users)
    history = {
        (month, login.casefold()): github_activity_report.HistoryRow(
            month,
            login,
            dict(zip(github_activity_report.METRICS, (0, 1, 10**12, 7, 0), strict=True)),
            partial,
            captured_at,
            capped,
        )
        for login in logins
        for month, captured_at in (("2026-01", "2026-02-01T00:00:00Z"), ("2025-12", "2026-01-31T23:59:59+05:30"))
    }
    path = tmp_path / "history.csv"
    replace_text(path, github_activity_report.render_history(history))
    assert github_activity_report.read_history(path) == history


def _hash(index: int) -> str:
    return f"sha256:{index:064x}"


def _deployed_names(kind: str) -> tuple[str, ...]:
    """The names a deployment records for a kind of item, each one its source can hold: a skill's or an adapter's a
    valid skill name, one character, the longest, or two words, an agent's such a name ending in .md, and a shared
    asset's any safe name no skill holds."""
    skill_names = ("a", "s" * 64, "two-words")
    return {
        "skills": skill_names,
        "shared": ("b", "shared asset.v2", "t" * 64),
        ADAPTERS: skill_names,
        "agents": tuple(f"{name}.md" for name in skill_names),
    }[kind]


@pytest.mark.parametrize(
    ("size", "commit", "source_dir"),
    list(itertools.product(SIZES, (None, "c" * 40, "d" * 64), ("C:/Repos/skills", "/home/a user/Ünïcödé/日本語"))),
)
def test_the_manifest_reads_back_every_source_entry_written(
    tmp_path: Path, size: int, commit: str | None, source_dir: str
) -> None:
    items: dict[str, dict[str, dict[str, object]]] = {
        kind: {name: {"hash": _hash(index)} for index, name in enumerate(_deployed_names(kind)[:size])}
        for kind in ("skills", "shared", ADAPTERS, "agents")
    }
    skills, shared, agents = list(items["skills"]), list(items["shared"]), list(items["agents"])
    for entry in items["skills"].values():
        entry.update(skill_deps=skills[:1], shared_deps=shared, agent_deps=agents)
    for entry in items["shared"].values():
        entry["role"] = "owner"
    path = tmp_path / "skills" / ".deploy-manifest.json"
    path.parent.mkdir()
    written = manifest.load(path)
    written.data["last_run_id"] = "20260101-000000-ab12" if size else ""
    written.sources["example/source"] = manifest.source_entry(
        source_dir,
        "2026-01-01T00:00:00Z" if size else None,
        commit=commit,
        ref=None if commit is None else "refs/tags/v1.0.0",
        requested_bundles=skills[:1],
        requested_bundle_members=dict.fromkeys(skills[:1], skills),
        requested_skills=skills,
        items=items,  # type: ignore[arg-type]  # each entry holds the fields its kind records
    )
    written.save()
    assert manifest.load(path).data == written.data


@pytest.mark.parametrize(
    ("root", "retain"), list(itertools.product(("claude", "agents", "claude-agents"), (False, True)))
)
def test_the_journal_reads_back_every_entry_written_and_its_run_needs_no_one(
    tmp_path: Path, root: str, retain: bool
) -> None:
    paths = Paths(source_dir=tmp_path, home=tmp_path)
    run_id = "20260101-000000-ab12"
    record = journal.Journal(paths.staging_root / run_id / "journal.jsonl", run_id)
    record.create()
    kind = {"claude": "shared", "agents": ADAPTERS, "claude-agents": "agents"}[root]
    for index, name in enumerate(_deployed_names(kind)):
        record.backup(root, name, _hash(index), retain)
        record.install(root, name, _hash(index + 1))
        record.preserve(root, name)
    assert journal._read_entries(record.path, run_id).entries == record.entries
    assert journal.pending_runs(paths) == [(run_id, True)]


@pytest.mark.parametrize(("dispatch", "roles", "path"), list(itertools.product(("inline", "subagents"), (1, 3), PATHS)))
def test_a_run_file_reads_back_as_prepare_writes_it(tmp_path: Path, dispatch: str, roles: int, path: str) -> None:
    # A repository path git can hold names a file the run patches, leaves uncovered, or reads as a document; an inline
    # run seals every file prepare wrote.
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "plan.json").write_text("written by prepare", encoding="utf-8")
    identities = sized(("generic-review", "python-reviewer", "r"), roles)
    state = review_run_suite.prepared_state(
        runtime="claude-code",
        host="claude-code",
        dispatch=dispatch,
        roles=[
            {
                "id": identity,
                "prompt_file": f"{tmp_path.as_posix()}/work/{identity}.md",
                "result_file": f"{tmp_path.as_posix()}/work/{identity}.json",
                **({"model": "opus", "effort": "high"} if index % 2 else {}),
            }
            for index, identity in enumerate(identities)
        ],
        attempts=dict.fromkeys(identities, 0),
        dispatched_at={},
        reads={},
        notes=list(NOT_BLANK),
        uncovered_files=[path],
        patches={path: {"sha256": "c" * 64, "lines": []}},
        documents={path: list(TEXT)},
    )
    del state["seal"]
    review_run._write_run(tmp_path, cast(review_run.RunState, state))
    assert review_run.load_run(tmp_path) == state


@pytest.mark.parametrize(("text", "path", "findings"), list(itertools.product(NOT_BLANK, PATHS, SIZES)))
def test_a_review_record_pair_reads_back_as_written(tmp_path: Path, text: str, path: str, findings: int) -> None:
    # The pull request's title and the reviewer's free text as GitHub and the reviewer give them, and a repository path
    # git can hold as the file a finding names and the file no reviewer covered.
    request = {**review_records_suite.valid_request(), "title": text, "uncovered_files": [path]}
    result = review_records_suite.valid_adapter_result()
    result["summary"] = text
    # A finding's path is the reviewer's to give, which the result's validator holds to a non-blank one.
    finding_path = path if path.strip() else "src/file.cs"
    result["findings"] = [
        {
            **result["findings"][0],
            "candidate_key": f"candidate-{index}",
            "path": finding_path,
            "body": text,
            "evidence": text,
        }
        for index in range(findings)
    ]
    validated = review_records.validate_adapter_result(
        result, expected_repository="example/one", expected_number=12, expected_head_sha="b" * 40
    )
    record = review_records.build_record(
        request, validated, version=1, policy=POLICY, reviewed_at="2026-01-01T00:00:00+00:00"
    )
    json_path, markdown_path = tmp_path / "review.json", tmp_path / "review.md"
    persisted = review_records.write_record_pair(json_path, markdown_path, record)
    assert review_records.validate_record_pair(json_path, markdown_path) == persisted


@pytest.mark.parametrize("size", SIZES)
def test_the_review_state_reads_back_every_repository_written(tmp_path: Path, size: int) -> None:
    # A repository GitHub names, one character each or with every separator its names take, and each watermark the
    # pipeline records: a merge date, a time, or none yet.
    marks: tuple[dict[str, str], ...] = (
        {"merged_since": "2026-01-02"},
        {"merged_since": "2026-01-02T03:04:05Z", "updated_at": ""},
        {},
    )
    repositories = {
        identity: marks[index % len(marks)]
        for index, identity in enumerate(sized(("a/b", "Owner-1/repo.name_x"), size))
    }
    path = tmp_path / "state.json"
    written = review_state.update_state(path, lambda state: {**state, "repositories": repositories})
    assert review_state.load_state(path) == written


@pytest.mark.parametrize(("category", "body"), list(itertools.product(NOT_BLANK, NOT_BLANK)))
def test_the_flag_store_reads_back_every_flag_written(tmp_path: Path, category: str, body: str) -> None:
    path = tmp_path / "flags.json"
    try:
        review_flags.add_flag(path, category=category, body=body)
    except review_flags.FlagError:
        assert not path.exists(), "the store refused the flag after writing it"
        return
    review_flags.add_flag(
        path, category="noise", body=body, repository="a/b", pull_number=1, review_version=2, finding_id="F001"
    )
    flag = review_flags.edit_flag(path, "RF-000002", category=category, body=body)
    stored = review_flags.load_store(path)
    assert stored["flags"][1] == flag


@pytest.mark.parametrize(("size", "title"), list(itertools.product(SIZES, TEXT)))
def test_the_dashboard_tracker_record_reads_back_as_the_update_wrote_it(tmp_path: Path, size: int, title: str) -> None:
    # Pull requests in repositories GitHub names, numbered from the first to the longest number the record takes,
    # titled with any text GitHub holds, one reviewed at its head and one not yet reviewed.
    items = [
        {
            **DASHBOARD_ITEM,
            "repository": repository,
            "number": number,
            "url": f"https://github.com/{repository}/pull/{number}",
            "title": title,
            "reviewed_head_sha": DASHBOARD_ITEM["head_sha"] if index % 2 == 0 else None,
        }
        for index, (repository, number) in enumerate(
            itertools.islice(itertools.cycle((("a/b", 1), ("Owner-1/repo.name_x", 9_999_999_999))), size)
        )
    ]
    if size == 3:
        items[2]["number"] = 2
    source = tmp_path / "input.json"
    source.write_text(json.dumps(items), encoding="utf-8")
    dashboard = tmp_path / "dashboard.md"
    dashboard.write_text(f"{update_pr_dashboard.START_MARKER}\n{update_pr_dashboard.END_MARKER}\n", encoding="utf-8")
    record = tmp_path / "tracker.json"
    try:
        update_pr_dashboard.update_dashboard_rows(source, dashboard, "reviewer", detector=_Detector(), record=record)
    except update_pr_dashboard.TrackerError:
        assert size, "an update with no pull request has no title to refuse"
        assert not title.strip(), "only a blank title, which the input check refuses, stops the update"
        assert not record.exists(), "the update refused its input after writing the record"
        return
    assert dashboard_pipeline.read_tracker_record(record) == json.loads(record.read_text(encoding="utf-8"))


class ConfigurationRoundTripTests(DeployerTestCase):
    def test_the_configuration_configure_saves_reads_back_and_saves_again_unchanged(self) -> None:
        # A repositories folder named as the allowlist lets one be, and each optional value: none, the prompt's own
        # example, and every character its allowlist takes; configure refuses the rest before it writes.
        folders = ("repos", "Repos Root (main) @x.y_z-1")
        regenerable = ("-", ".venv .tox=tox.ini,setup.cfg", "*.tmp a/b=c,d e.f_g-h")
        protected = ("-", "hotfix/ support/", "a.b_c-d/e f")
        self.make_source_json()
        for folder, channel, regenerated, kept in itertools.product(
            folders, ("release", "main"), regenerable, protected
        ):
            with self.subTest(folder=folder, channel=channel, regenerable=regenerated, protected=kept):
                repos = self.root / folder
                repos.mkdir(exist_ok=True)
                answers = [forward(repos), channel, regenerated, kept]
                result = self.configure(stdin="".join(f"{answer}\n" for answer in answers))
                self.assertEqual(0, result.code, result.output)
                expected = {
                    config.SOURCE_KEY: SOURCE_ID,
                    "REPOS_ROOT": platform_support.normalize_path_input(forward(repos)),
                    "UPDATE_CHANNEL": channel,
                }
                expected.update(
                    {
                        key: value
                        for key, value in zip(config.OPTIONAL_VARIABLES, (regenerated, kept), strict=True)
                        if value != "-"
                    }
                )
                self.assertEqual(expected, config.read(self.config_file(), SOURCE_ID))
                saved = self.config_file().read_bytes()
                rerun = self.configure(stdin="\n\n\n\n")
                self.assertEqual(0, rerun.code, rerun.output)
                self.assertEqual(saved, self.config_file().read_bytes())
