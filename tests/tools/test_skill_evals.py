"""tools/skill_evals.py: the scenario loader, every expectation kind against literal records, the output, the table,
the review-prs and review-document scenarios themselves, the staging of a document scenario's document, and the run
step with a stub Claude Code. No model is called."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from skill_roots import sibling_scripts

sys.path.insert(0, str(sibling_scripts("code-review-core")))
sys.path.insert(0, str(sibling_scripts("review-document")))
sys.path.insert(0, str(sibling_scripts("analyze-skill-cost")))

import frontmatter
import review_document
import review_guard
import review_pipeline
import review_run
import review_source
import review_specialists
import skill_inventory
from git_client import GitClient, GitResult
from review_config import write_config
from review_documents import document_format, extract, locations, split_label
from review_github import GitHubClient
from review_records import ReviewRecord, build_record, validate_record
from review_runtime import validate_adapter_manifest
from test_review_documents import StubPdftotext

from conftest import GitHome
from deployer import fsops, platform_support
from tools import (
    runtime_canary,
    skill_evals,
    skill_evals_judging,
    skill_evals_records,
    skill_evals_results,
    skill_evals_runs,
    skill_evals_scenarios,
)
from tools.skill_evals_judging import Outcome
from tools.skill_evals_records import RecordError
from tools.skill_evals_scenarios import Scenario, ScenarioError

REVIEW_PRS = skill_evals_scenarios.SCENARIO_ROOT / "review-prs"
REVIEW_DOCUMENT = skill_evals_scenarios.SCENARIO_ROOT / "review-document"
REVIEW_REPOSITORY = skill_evals_scenarios.SCENARIO_ROOT / "review-repository"
REVIEW_REPOSITORY_SCENARIOS = (
    "branch-name-command",
    "build-grant",
    "drifted-guide",
    "misstated-exit-status",
    "tested-change",
    "text-read-policy",
    "undocumented-change",
    "unnoted-flag-rename",
    "untested-change",
    "write-before-refusal",
)
# The Python files the write-before-refusal scenario holds in both trees, each by its path once restored.
WRITE_BEFORE_REFUSAL_HELD = ("deployer/fsops.py", "deployer/install.py", "tests/deployer/test_install.py")
REVIEWER_FOLDER = ".claude/skills/review-repository"
MANIFEST_PATH = ".claude/skills/review-repository/references/specialists.json"
# Files a run of a review-repository scenario places from the checkout: the reviewer's own, its condition scripts,
# and the resources and guidelines its manifest declares.
REVIEW_REPOSITORY_PLACED = (
    ".claude/skills/review-repository/SKILL.md",
    ".claude/skills/review-repository/references/specialists.json",
    ".claude/skills/review-repository/references/documentation-drift.md",
    ".claude/skills/review-repository/references/untested-changes.md",
    ".claude/skills/review-repository/scripts/doc_passages.py",
    ".claude/skills/review-repository/scripts/untested_functions.py",
    "docs/design.md",
    "docs/adding-a-skill.md",
    "skills/skill-core/scripts/console.py",
    "skills/skill-core/scripts/flat_text.py",
    "CLAUDE.md",
    "docs/implementing-changes.md",
)
# What a run never places: the reviewer's own suites, which name the scenarios' files, and the reviewed tree's files a
# condition's `reads` name.
REVIEW_REPOSITORY_UNPLACED = (
    ".claude/skills/review-repository/scripts/test_review_repository.py",
    ".claude/skills/review-repository/scripts/test_doc_passages.py",
    "README.md",
    "CONTRIBUTING.md",
    "docs/skills.md",
    "tests/tools/test_skill_evals.py",
)
SUITE_REFERENCE = (
    skill_evals_scenarios.REPOSITORY_ROOT / "skills" / "code-review-core" / "references" / "design-reviewer.md"
)
# The pdf-gaps draft's text, page by page, one line per block of the page. Its headings are set larger and its body
# lines alternate between two sizes, so even Xpdf's default reading, which joins evenly set lines into one, reads these
# lines; the snapshot's -raw reads them so with either pdftotext.
PDF_GAPS_PAGES = (
    (
        "Retention for report exports",
        "Status: proposed",
        "Context",
        "The export worker writes each report export to object storage as a CSV file.",
        "Nothing deletes them, so storage holds every export since launch.",
        "Requirements",
        "R1. An export is deleted when its retention period ends.",
        "The period depends on the account plan, as the table below gives it.",
        "R2. A customer can download an export until it is deleted.",
        "R3. Every deletion is written to the audit log before the export is removed.",
        "Plan Retention",
        "Free 30 days",
        "Team 90 days",
        "Enterprise 365 days",
        "Design",
        "A nightly sweep selects the exports whose retention period has ended.",
        "For each, it writes an audit entry, deletes the object, then deletes the row.",
        "A row whose object is already gone is deleted without an error.",
        "So a sweep that stops partway and runs again leaves the same state.",
        "Enterprise exports are kept for 90 days, the same as Team.",
        "So the sweep needs only two retention periods.",
        "The sweep skips every account with a legal hold.",
    ),
    (
        "Alternatives considered",
        "Object storage lifecycle rules, which expire objects by age. Rejected.",
        "Rollout",
        "The sweep ships disabled behind the export_retention setting.",
        "It runs one week in report-only mode, logging what it would delete.",
        "Then it is enabled for deletion; turning the setting off stops it.",
        "Open questions",
        "Should a deleted export stay recoverable for 7 days in a holding bucket?",
        "This decides whether the sweep deletes objects or moves them.",
    ),
)


def pdftotext_reads_pdf_gaps(test: unittest.TestCase) -> None:
    """A stub pdftotext, first on PATH for the rest of the test, that reads the pdf-gaps draft as pdftotext does."""
    StubPdftotext(test, output="".join("\n".join(page) + "\n\f" for page in PDF_GAPS_PAGES))


def finding(path: str, line: int, severity: str, identifier: str = "F001", **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "path": path, "line": line, "severity": severity, **extra}


def entry(version: int, identifier: str, *dispositions: tuple[int, str], severity: str = "MUST_FIX") -> dict[str, Any]:
    return {
        "version": version,
        "id": identifier,
        "severity": severity,
        "dispositions": [{"version": judged, "disposition": value} for judged, value in dispositions],
    }


def record(
    findings: Sequence[dict[str, Any]] = (),
    verdict: str = "APPROVED",
    version: int = 1,
    ledger: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {"review": {"version": version, "verdict": verdict}, "findings": list(findings)}
    if ledger is not None:
        value["ledger"] = list(ledger)
    return value


def contract_record(
    found: Sequence[dict[str, Any]] = (), model: str = "claude-haiku-5-5", files_read: int | None = 0
) -> ReviewRecord:
    """An initial review's record as finalize writes it, which code-review-core's record contract accepts: each
    finding raised, by one reviewer that reports `model` and that the guard held, unless `files_read` is None."""
    request = {
        "repository": "example/one",
        "pull_number": 1,
        "pull_url": "https://github.com/example/one/pull/1",
        "title": "A change",
        "base_ref": "main",
        "base_sha": "0" * 40,
        "head_sha": "1" * 40,
        "mode": "initial",
        "adapter": {"name": "generic", "scope": "generic"},
        "reviewers": [
            {
                "id": "generic",
                "category": "General",
                "files": 1,
                "findings": len(found),
                "retries": 0,
                "dispositions_only": False,
                "model": model,
                "files_read": files_read,
                "bytes_read": files_read,
            }
        ],
    }
    result: Any = {
        "summary": "Reviewed.",
        "reviewer": "generic",
        "status": "complete",
        "usage": None,
        "prior_dispositions": [],
        "findings": [
            {
                "candidate_key": f"k{index}",
                "severity": item["severity"],
                "category": "Correctness",
                "path": item["path"],
                "line": item["line"],
                "body": "Planted.",
                "evidence": f"{item['path']}:{item['line']}",
                "source": "generic",
            }
            for index, item in enumerate(found, 1)
        ],
    }
    policy = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
    built = build_record(request, result, version=1, policy=policy, reviewed_at="2026-10-15T09:30:00+00:00")
    return validate_record(built)


def check(spec: dict[str, Any], value: dict[str, Any], mode: str = "re-review") -> str | None:
    return skill_evals_scenarios.expectation(spec, mode).check(value)


def refusal(spec: dict[str, Any], mode: str) -> str:
    """Why the loader refuses the expectation."""
    try:
        skill_evals_scenarios.expectation(spec, mode)
    except ScenarioError as exc:
        return str(exc)
    raise AssertionError(f"{spec} was accepted")


def write_scenario(root: Path, name: str, spec: dict[str, Any], skill: str = "demo") -> Path:
    directory = root / skill / name
    for tree in ("base", "head"):
        (directory / tree).mkdir(parents=True, exist_ok=True)
    (directory / "pull.json").write_text("{}", encoding="utf-8")  # read by prepare, which these tests never run
    (directory / "scenario.json").write_text(json.dumps(spec), encoding="utf-8")
    return directory


def scenario_spec(*expectations: dict[str, Any], mode: str = "initial", **extra: Any) -> dict[str, Any]:
    spec = {"skill": "demo", "mode": mode, **extra}
    spec["expectations"] = list(expectations) or [{"kind": "verdict", "verdict": "APPROVED"}]
    return spec


def run_main(argv: Sequence[str], root: Path) -> tuple[int, list[str]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = skill_evals.main(argv, root)
    return status, output.getvalue().splitlines()


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def load_error(self, spec: dict[str, Any]) -> str:
        write_scenario(self.root, "case", spec)
        with self.assertRaises(ScenarioError) as raised:
            skill_evals_scenarios.load_scenarios("demo", root=self.root)
        return str(raised.exception)

    def test_loads_every_scenario_in_name_order(self) -> None:
        write_scenario(self.root, "b-second", scenario_spec())
        write_scenario(self.root, "a-first", scenario_spec())
        names = [scenario.name for scenario in skill_evals_scenarios.load_scenarios("demo", root=self.root)]
        self.assertEqual(["a-first", "b-second"], names)

    def test_loads_only_the_named_scenarios(self) -> None:
        write_scenario(self.root, "one", scenario_spec())
        write_scenario(self.root, "two", scenario_spec())
        names = [scenario.name for scenario in skill_evals_scenarios.load_scenarios("demo", ["two"], root=self.root)]
        self.assertEqual(["two"], names)

    def test_named_scenarios_are_distinct_in_first_occurrence_order(self) -> None:
        for name in ("one", "two", "three"):
            write_scenario(self.root, name, scenario_spec())
        selected = ["two", "one", "two", "three", "one"]
        names = [scenario.name for scenario in skill_evals_scenarios.load_scenarios("demo", selected, root=self.root)]
        self.assertEqual(["two", "one", "three"], names)

    def test_a_held_file_in_a_fixture_reviewed_in_place_is_refused(self) -> None:
        # Only a repository reviewer's run copies its fixture, so any other would be reviewed under the held name.
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "base" / "store.py.fixture").write_text("", encoding="utf-8")
        with self.assertRaises(ScenarioError) as raised:
            skill_evals_scenarios.load_scenarios("demo", root=self.root)
        self.assertEqual(
            "case: its base/ tree holds store.py.fixture, which only a repository reviewer's copy restores",
            str(raised.exception),
        )

    def test_unknown_scenario_and_skill_fail(self) -> None:
        write_scenario(self.root, "one", scenario_spec())
        with self.assertRaisesRegex(ScenarioError, "no scenario three for demo"):
            skill_evals_scenarios.load_scenarios("demo", ["one", "one", "three"], root=self.root)
        with self.assertRaisesRegex(ScenarioError, "no scenarios for other"):
            skill_evals_scenarios.load_scenarios("other", root=self.root)

    def test_a_skill_with_no_scenario_fails_and_nothing_runs(self) -> None:
        (self.root / "empty-skill" / "not-a-scenario").mkdir(parents=True)
        with self.assertRaisesRegex(ScenarioError, "no scenarios for empty-skill: each is a <scenario>/scenario.json"):
            skill_evals_scenarios.load_scenarios("empty-skill", root=self.root)
        records = self.root / "records"
        status, lines = run_main(["empty-skill", "--records", str(records)], self.root)
        self.assertEqual(1, status)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith('FAILED "no scenarios for empty-skill: '), lines)

        def unreached(*_: Any) -> Any:
            raise AssertionError("a skill with no scenario reached the run")

        seams = skill_evals.Seams(runner=unreached, which=unreached, deploy=unreached, make_home=unreached)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(1, skill_evals.main(["empty-skill", "--write"], self.root, seams))
        self.assertTrue(output.getvalue().startswith('FAILED "no scenarios for empty-skill: '))

    def test_a_path_on_a_drive_is_refused_on_every_platform(self) -> None:
        for prior in ("C:/outside/prior.json", "C:prior.json", "c:\\outside\\prior.json"):
            with self.subTest(prior=prior):
                message = self.load_error(scenario_spec(mode="re-review", prior=prior))
                self.assertIn(f"path must be a plain relative path, not {json.dumps(prior)}", message)
        for path in ("C:/a.go", "D:a.go"):
            with self.subTest(path=path):
                spec = {"kind": "finding", "path": path, "lines": [1], "severity": "MUST_FIX"}
                self.assertIn("plain relative path", refusal(spec, "initial"))

    def test_no_finding_above_the_highest_severity_holds_nothing_and_is_refused(self) -> None:
        highest = skill_evals_scenarios.SEVERITIES[-1]
        self.assertEqual("MUST_FIX", highest)
        message = refusal({"kind": "no_finding_above", "severity": "MUST_FIX"}, "initial")
        self.assertEqual("no_finding_above MUST_FIX holds for every record: no severity ranks above it", message)
        self.assertIn(
            "holds for every record",
            self.load_error(scenario_spec({"kind": "no_finding_above", "severity": "MUST_FIX"})),
        )
        self.assertIsNotNone(
            skill_evals_scenarios.expectation({"kind": "no_finding_above", "severity": "SHOULD_FIX"}, "initial")
        )

    def test_a_category_the_manifest_does_not_list_is_refused(self) -> None:
        shutil.copytree(REVIEW_PRS / "design-gaps", self.root / "review-prs" / "design-gaps")
        shutil.copytree(REVIEW_DOCUMENT / "design-gaps", self.root / "review-document" / "design-gaps")
        scenarios = {"review-prs": self.root / "review-prs", "review-document": self.root / "review-document"}
        for skill, folder in scenarios.items():
            with self.subTest(skill=skill):
                path = folder / "design-gaps" / "scenario.json"
                spec = json.loads(path.read_text(encoding="utf-8"))
                # Ignoring case, as the judge matches a finding's category.
                spec["expectations"][0]["category"] = "INCONSISTENCY"
                path.write_text(json.dumps(spec), encoding="utf-8")
                self.assertEqual(1, len(skill_evals_scenarios.load_scenarios(skill, root=self.root)))
                spec["expectations"][0]["category"] = "inconsistancy"
                path.write_text(json.dumps(spec), encoding="utf-8")
                with self.assertRaisesRegex(
                    ScenarioError,
                    'design-gaps: category "inconsistancy" is not one of the manifest\'s finding_categories: '
                    "requirement-gap, ",
                ):
                    skill_evals_scenarios.load_scenarios(skill, root=self.root)

    def test_unknown_expectation_kind_fails(self) -> None:
        self.assertIn('unknown expectation kind "vibes"', self.load_error(scenario_spec({"kind": "vibes"})))

    def test_expectation_fields_are_exact(self) -> None:
        self.assertIn("verdict expectation lacks verdict", self.load_error(scenario_spec({"kind": "verdict"})))
        extra = scenario_spec({"kind": "verdict", "verdict": "APPROVED", "note": "x"})
        self.assertIn("verdict expectation does not take note", self.load_error(extra))

    def test_scenario_fields_are_exact(self) -> None:
        self.assertIn("must have exactly", self.load_error(scenario_spec(extra="x")))
        # The pull request's title is pull.json's, which prepare reads.
        self.assertIn("must have exactly", self.load_error(scenario_spec(title="A change")))
        spec = scenario_spec()
        del spec["mode"]
        self.assertIn("mode must be one of", self.load_error(spec))

    def test_prior_belongs_to_a_re_review_and_must_exist(self) -> None:
        self.assertIn("must have exactly", self.load_error(scenario_spec(prior="prior.json")))
        self.assertIn("must have exactly", self.load_error(scenario_spec(mode="re-review")))

    def test_missing_prior_record_fails(self) -> None:
        message = self.load_error(scenario_spec(mode="re-review", prior="prior.json"))
        self.assertIn("prior record prior.json is missing", message)

    def test_re_review_scenario_loads_its_prior(self) -> None:
        directory = write_scenario(self.root, "again", scenario_spec(mode="re-review", prior="prior.json"))
        (directory / "prior.json").write_text("{}", encoding="utf-8")
        (scenario,) = skill_evals_scenarios.load_scenarios("demo", root=self.root)
        self.assertEqual(directory / "prior.json", scenario.prior)
        self.assertEqual("re-review", scenario.mode)

    def test_other_scenario_faults(self) -> None:
        self.assertIn("names skill", self.load_error({**scenario_spec(), "skill": "other"}))
        self.assertIn("mode must be one of", self.load_error(scenario_spec(mode="partial")))
        self.assertIn("expectations must be a non-empty list", self.load_error({**scenario_spec(), "expectations": []}))

    def test_a_tree_is_required(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "head").rmdir()
        with self.assertRaisesRegex(ScenarioError, "case: has no head/ tree"):
            skill_evals_scenarios.load_scenarios("demo", root=self.root)

    def test_pull_json_is_required(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "pull.json").unlink()
        with self.assertRaisesRegex(ScenarioError, "case: has no pull.json"):
            skill_evals_scenarios.load_scenarios("demo", root=self.root)

    def test_prepare_arguments_are_one_call_per_mode(self) -> None:
        initial = write_scenario(self.root, "first", scenario_spec())
        again = write_scenario(self.root, "again", scenario_spec(mode="re-review", prior="prior.json"))
        (again / "prior.json").write_text("{}", encoding="utf-8")
        loaded = {scenario.name: scenario for scenario in skill_evals_scenarios.load_scenarios("demo", root=self.root)}
        self.assertEqual(
            ["--canary", "--fixture", str(initial)], skill_evals_scenarios.prepare_arguments(loaded["first"])
        )
        self.assertEqual(
            ["--canary", "--fixture", str(again), "--re-review", "--prior", str(again / "prior.json")],
            skill_evals_scenarios.prepare_arguments(loaded["again"]),
        )

    def test_unreadable_scenario_fails(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "scenario.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ScenarioError, "case: cannot read scenario.json"):
            skill_evals_scenarios.load_scenarios("demo", root=self.root)

    def test_a_document_scenario_names_its_document_and_base_and_needs_no_fixture(self) -> None:
        shared = self.root / "other" / "shared" / "draft.md"
        shared.parent.mkdir(parents=True)
        shared.write_text("# Draft\n", encoding="utf-8")
        for name, fields in {
            "edit": {"document": "draft.md", "base": "../../other/shared/draft.md"},
            "whole": {"document": "../../other/shared/draft.md"},
        }.items():
            directory = self.root / "demo" / name
            directory.mkdir(parents=True)
            (directory / "draft.md").write_text("# Draft, edited\n", encoding="utf-8")
            (directory / "scenario.json").write_text(json.dumps(scenario_spec(**fields)), encoding="utf-8")
        loaded = {scenario.name: scenario for scenario in skill_evals_scenarios.load_scenarios("demo", root=self.root)}
        edited = (self.root / "demo" / "edit" / "draft.md").resolve()
        self.assertEqual((edited, shared.resolve()), (loaded["edit"].document, loaded["edit"].base))
        self.assertEqual((shared.resolve(), None), (loaded["whole"].document, loaded["whole"].base))
        self.assertEqual("initial", loaded["whole"].mode)

    def test_a_document_scenario_may_name_a_stage_the_skill_is_started_at(self) -> None:
        for number, (fields, stage) in enumerate(
            (({"document": "draft.md", "stage": "draft"}, "draft"), ({"document": "draft.md"}, None))
        ):
            directory = write_scenario(self.root, f"case{number}", scenario_spec(**fields))
            (directory / "draft.md").write_text("# Draft\n", encoding="utf-8")
            (scenario,) = skill_evals_scenarios.load_scenarios("demo", [f"case{number}"], root=self.root)
            self.assertEqual(stage, scenario.stage)
        for number, (fields, message) in enumerate(
            (
                ({"document": "draft.md", "stage": "early"}, 'stage must be one of draft, review, final, not "early"'),
                ({"stage": "draft"}, "must have exactly"),
            ),
            start=2,
        ):
            with self.subTest(fields=fields):
                directory = write_scenario(self.root, f"case{number}", scenario_spec(**fields))
                (directory / "draft.md").write_text("# Draft\n", encoding="utf-8")
                with self.assertRaises(ScenarioError) as raised:
                    skill_evals_scenarios.load_scenarios("demo", [f"case{number}"], root=self.root)
                self.assertIn(message, str(raised.exception))

    def test_a_fixture_scenario_names_no_document(self) -> None:
        write_scenario(self.root, "case", scenario_spec())
        (scenario,) = skill_evals_scenarios.load_scenarios("demo", root=self.root)
        self.assertEqual((None, None), (scenario.document, scenario.base))


class FindingTests(unittest.TestCase):
    spec: ClassVar[dict[str, Any]] = {
        "kind": "finding",
        "path": "store.go",
        "lines": [44, 45],
        "severity": "SHOULD_FIX",
    }

    def test_passes_at_a_listed_line_with_that_severity_or_more(self) -> None:
        for severity in ("SHOULD_FIX", "MUST_FIX"):
            with self.subTest(severity=severity):
                self.assertIsNone(check(self.spec, record([finding("store.go", 45, severity)]), "initial"))

    def test_fails_below_the_severity_off_the_lines_or_on_another_path(self) -> None:
        for found in (
            finding("store.go", 44, "SUGGESTION"),
            finding("store.go", 46, "MUST_FIX"),
            finding("labels.go", 44, "MUST_FIX"),
        ):
            with self.subTest(found=found):
                self.assertIsNotNone(check(self.spec, record([found]), "initial"))

    def test_failure_names_what_was_found_on_the_path(self) -> None:
        failure = check(self.spec, record([finding("store.go", 44, "SUGGESTION")]), "initial")
        self.assertEqual(
            "no finding at least SHOULD_FIX on those lines; findings on store.go: SUGGESTION at line 44", failure
        )
        failure = check(self.spec, record(), "initial")
        self.assertEqual("no finding at least SHOULD_FIX on those lines; findings on store.go: none", failure)

    def test_label(self) -> None:
        self.assertEqual(
            "finding store.go:44,45 SHOULD_FIX", skill_evals_scenarios.expectation(self.spec, "initial").label
        )
        categorized = {**self.spec, "category": "Risk"}
        self.assertEqual(
            "finding store.go:44,45 SHOULD_FIX Risk", skill_evals_scenarios.expectation(categorized, "initial").label
        )

    def test_a_category_matches_ignoring_case_and_any_category_passes_without_one(self) -> None:
        categorized = {**self.spec, "category": "open-question"}
        found = finding("store.go", 44, "SHOULD_FIX", category="Open-Question")
        self.assertIsNone(check(categorized, record([found]), "initial"))
        self.assertIsNone(check(self.spec, record([finding("store.go", 44, "SHOULD_FIX", category="risk")]), "initial"))

    def test_a_finding_of_another_category_fails_and_the_failure_names_its_category(self) -> None:
        categorized = {**self.spec, "category": "inconsistency"}
        failure = check(categorized, record([finding("store.go", 44, "MUST_FIX", category="risk")]), "initial")
        self.assertEqual(
            "no finding at least SHOULD_FIX inconsistency on those lines; "
            "findings on store.go: MUST_FIX risk at line 44",
            failure,
        )


class NoFindingAboveTests(unittest.TestCase):
    def test_on_listed_paths(self) -> None:
        spec = {"kind": "no_finding_above", "severity": "SUGGESTION", "paths": ["labels.go"]}
        self.assertIsNone(
            check(spec, record([finding("labels.go", 3, "SUGGESTION"), finding("store.go", 4, "MUST_FIX")]))
        )
        failure = check(spec, record([finding("labels.go", 3, "SHOULD_FIX")]))
        self.assertEqual("found SHOULD_FIX at labels.go:3", failure)
        self.assertEqual(
            "no_finding_above SUGGESTION labels.go", skill_evals_scenarios.expectation(spec, "initial").label
        )

    def test_on_every_path_when_none_are_listed(self) -> None:
        spec = {"kind": "no_finding_above", "severity": "SUGGESTION"}
        self.assertIsNone(check(spec, record([finding("a.go", 1, "SUGGESTION")])))
        failure = check(spec, record([finding("a.go", 1, "MUST_FIX"), finding("b.go", 2, "SHOULD_FIX")]))
        self.assertEqual("found MUST_FIX at a.go:1, SHOULD_FIX at b.go:2", failure)
        self.assertEqual("no_finding_above SUGGESTION *", skill_evals_scenarios.expectation(spec, "initial").label)

    def test_on_listed_lines_of_one_path(self) -> None:
        spec = {"kind": "no_finding_above", "severity": "SUGGESTION", "paths": ["draft.md"], "lines": [9, 4]}
        passing = [
            finding("draft.md", 4, "SUGGESTION"),
            finding("draft.md", 5, "MUST_FIX"),
            finding("b.md", 9, "MUST_FIX"),
        ]
        self.assertIsNone(check(spec, record(passing)))
        failure = check(spec, record([finding("draft.md", 9, "SHOULD_FIX"), finding("draft.md", 5, "MUST_FIX")]))
        self.assertEqual("found SHOULD_FIX at draft.md:9", failure)
        self.assertEqual(
            "no_finding_above SUGGESTION draft.md:4,9", skill_evals_scenarios.expectation(spec, "initial").label
        )
        for fields in ({"lines": [4]}, {"paths": ["a.md", "b.md"], "lines": [4]}):
            with self.subTest(fields=fields), self.assertRaisesRegex(ScenarioError, "give exactly one path in paths"):
                skill_evals_scenarios.expectation(
                    {"kind": "no_finding_above", "severity": "SUGGESTION", **fields}, "initial"
                )
        with self.assertRaisesRegex(ScenarioError, "lines must be distinct positive line numbers"):
            skill_evals_scenarios.expectation({**spec, "lines": [0]}, "initial")


class OnlyAtTests(unittest.TestCase):
    spec: ClassVar[dict[str, Any]] = {"kind": "only_at", "path": "draft.md", "lines": [17]}

    def test_every_finding_of_any_severity_must_be_there(self) -> None:
        there = [finding("draft.md", 17, "MUST_FIX"), finding("draft.md", 17, "SUGGESTION", "F002")]
        self.assertIsNone(check(self.spec, record(there), "initial"))
        self.assertIsNone(check(self.spec, record(), "initial"))
        elsewhere = [finding("draft.md", 3, "SUGGESTION", "F003"), finding("other.md", 17, "SHOULD_FIX", "F004")]
        self.assertEqual(
            "found SUGGESTION at draft.md:3, SHOULD_FIX at other.md:17",
            check(self.spec, record([*there, *elsewhere]), "initial"),
        )

    def test_label_and_fields(self) -> None:
        self.assertEqual("only_at draft.md:17", skill_evals_scenarios.expectation(self.spec, "initial").label)
        self.assertIn("only_at expectation does not take severity", refusal({**self.spec, "severity": "MUST_FIX"}, "x"))
        self.assertIn("distinct positive line", refusal({**self.spec, "lines": []}, "initial"))


class VerdictTests(unittest.TestCase):
    def test_verdict(self) -> None:
        spec = {"kind": "verdict", "verdict": "CHANGES_REQUESTED"}
        self.assertIsNone(check(spec, record(verdict="CHANGES_REQUESTED")))
        self.assertEqual("verdict is APPROVED", check(spec, record(verdict="APPROVED")))
        self.assertEqual("verdict CHANGES_REQUESTED", skill_evals_scenarios.expectation(spec, "initial").label)


class LedgerTests(unittest.TestCase):
    ledger: ClassVar[list[dict[str, Any]]] = [
        entry(1, "F001", (2, "still_present")),
        entry(1, "F002", (2, "still_present")),
        entry(1, "F003", (2, "addressed")),
        entry(1, "F004", (2, "partially_addressed")),
        entry(1, "F005", (2, "addressed"), (3, "still_present")),
        entry(2, "F001"),
    ]

    def test_counts_this_reviews_judgments(self) -> None:
        spec = {"kind": "ledger", "addressed": 2, "still_present": 2}
        self.assertIsNone(check(spec, record(version=2, ledger=self.ledger)))
        self.assertEqual(
            "ledger addressed=2 still_present=2", skill_evals_scenarios.expectation(spec, "re-review").label
        )

    def test_counts_must_match_exactly(self) -> None:
        spec = {"kind": "ledger", "addressed": 1, "still_present": 2}
        self.assertEqual("addressed=2 still_present=2", check(spec, record(version=2, ledger=self.ledger)))

    def test_a_record_without_a_ledger_fails(self) -> None:
        spec = {"kind": "ledger", "addressed": 0, "still_present": 0}
        with self.assertRaisesRegex(RecordError, "no ledger"):
            check(spec, record(version=2))


class DispositionTests(unittest.TestCase):
    ledger: ClassVar[list[dict[str, Any]]] = [
        entry(1, "F001", (2, "still_present")),
        entry(1, "F002", (2, "addressed")),
        entry(1, "F003"),
    ]

    def test_disposition(self) -> None:
        spec = {"kind": "disposition", "entry": "v1:F002", "disposition": "addressed"}
        self.assertIsNone(check(spec, record(version=2, ledger=self.ledger)))
        self.assertEqual("disposition v1:F002 addressed", skill_evals_scenarios.expectation(spec, "re-review").label)

    def test_another_disposition_fails(self) -> None:
        spec = {"kind": "disposition", "entry": "v1:F001", "disposition": "addressed"}
        self.assertEqual("v1:F001 is still_present", check(spec, record(version=2, ledger=self.ledger)))

    def test_an_entry_this_review_did_not_judge_fails(self) -> None:
        for name in ("v1:F003", "v1:F009"):
            spec = {"kind": "disposition", "entry": name, "disposition": "addressed"}
            with self.subTest(entry=name):
                self.assertEqual(
                    f"this review did not judge {name}", check(spec, record(version=2, ledger=self.ledger))
                )


class RepeatsTests(unittest.TestCase):
    spec: ClassVar[dict[str, Any]] = {"kind": "repeats", "path": "store.go", "lines": [44, 45], "entry": "v1:F001"}
    link: ClassVar[dict[str, Any]] = {"version": 1, "id": "F001"}

    def reviewed(self, found: Sequence[dict[str, Any]], severity: str = "MUST_FIX") -> dict[str, Any]:
        """A re-review whose ledger holds v1:F001 at this severity, judged still present."""
        return record(found, version=2, ledger=[entry(1, "F001", (2, "still_present"), severity=severity)])

    def test_a_linked_repeat_passes(self) -> None:
        found = [
            finding("store.go", 45, "MUST_FIX", "F001", repeats=self.link),
            finding("store.go", 70, "MUST_FIX", "F002"),
        ]
        self.assertIsNone(check(self.spec, self.reviewed(found)))
        self.assertEqual(
            "repeats store.go:44,45 v1:F001", skill_evals_scenarios.expectation(self.spec, "re-review").label
        )

    def test_raising_nothing_there_passes(self) -> None:
        self.assertIsNone(check(self.spec, self.reviewed([finding("labels.go", 44, "SUGGESTION")])))

    def test_an_unlinked_or_wrongly_linked_finding_fails(self) -> None:
        found = [
            finding("store.go", 44, "MUST_FIX", "F001"),
            finding("store.go", 45, "MUST_FIX", "F002", repeats={"version": 1, "id": "F003"}),
        ]
        self.assertEqual(
            "F001 at line 44, F002 at line 45 not linked to v1:F001", check(self.spec, self.reviewed(found))
        )

    def test_a_less_severe_finding_on_a_repeated_line_is_another_defect_and_passes(self) -> None:
        # The LIKE-wildcard suggestion beside the still-present injection: a different, lesser problem.
        found = [finding("store.go", 45, "SUGGESTION", "F001"), finding("store.go", 44, "SHOULD_FIX", "F002")]
        self.assertIsNone(check(self.spec, self.reviewed(found)))
        self.assertIsNone(check(self.spec, self.reviewed(found[:1], severity="SHOULD_FIX")))

    def test_an_unlinked_restatement_at_the_entrys_severity_fails(self) -> None:
        for severity in ("MUST_FIX", "SHOULD_FIX", "SUGGESTION"):
            found = [finding("store.go", 45, severity, "F002"), finding("labels.go", 45, severity, "F003")]
            with self.subTest(severity=severity):
                self.assertEqual(
                    "F002 at line 45 not linked to v1:F001", check(self.spec, self.reviewed(found, severity))
                )

    def test_a_more_severe_finding_on_a_repeated_line_is_a_new_finding_and_passes(self) -> None:
        # The record contract refuses a repeats link to a less severe finding, so a review escalating the entry
        # records a new finding beside it; demanding the link here would fail every record the contract allows.
        cases = {
            "SHOULD_FIX": [finding("store.go", 45, "MUST_FIX", "F002"), finding("store.go", 44, "SUGGESTION", "F003")],
            "SUGGESTION": [finding("store.go", 45, "SHOULD_FIX", "F002"), finding("store.go", 44, "MUST_FIX", "F003")],
        }
        for severity, found in cases.items():
            with self.subTest(severity=severity):
                self.assertIsNone(check(self.spec, self.reviewed(found, severity)))

    def test_the_entry_must_be_in_the_ledger(self) -> None:
        found = [finding("store.go", 45, "MUST_FIX", "F002")]
        cases = {
            "the record's ledger has no v1:F001": record(found, version=2, ledger=[entry(1, "F002")]),
            "the record has no ledger": record(found, version=2),
        }
        for reason, value in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(RecordError, reason):
                check(self.spec, value)


class JudgeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scenarios = Path(self.root, "scenarios")
        self.records = Path(self.root, "records")
        write_scenario(
            self.scenarios,
            "defects",
            scenario_spec(
                {"kind": "finding", "path": "a.go", "lines": [3], "severity": "MUST_FIX"},
                {"kind": "verdict", "verdict": "CHANGES_REQUESTED"},
            ),
        )

    def write_record(self, model: str, value: Any, scenario: str = "defects") -> Path:
        folder = self.records / scenario
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{model}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_every_expectation_passes(self) -> None:
        for model, model_id in skill_evals_records.MODELS.items():
            self.write_record(model, contract_record([finding("a.go", 3, "MUST_FIX")], model_id))
        status, lines = run_main(["demo", "--records", str(self.records)], self.scenarios)
        self.assertEqual(0, status)
        self.assertEqual(
            [
                "PASS demo defects haiku finding a.go:3 MUST_FIX",
                "PASS demo defects haiku verdict CHANGES_REQUESTED",
                "PASS demo defects sonnet finding a.go:3 MUST_FIX",
                "PASS demo defects sonnet verdict CHANGES_REQUESTED",
                "PASS demo defects opus finding a.go:3 MUST_FIX",
                "PASS demo defects opus verdict CHANGES_REQUESTED",
                "| Scenario | haiku | sonnet | opus |",
                "| --- | --- | --- | --- |",
                "| defects | 2/2 | 2/2 | 2/2 |",
            ],
            lines,
        )

    def test_a_fail_exits_1_and_a_missing_record_fails_each_expectation(self) -> None:
        self.write_record("haiku", contract_record([finding("a.go", 3, "SHOULD_FIX")]))
        self.write_record("opus", "not a record")
        status, lines = run_main(["demo", "--records", str(self.records)], self.scenarios)
        self.assertEqual(1, status)
        self.assertIn(
            'FAIL demo defects haiku verdict CHANGES_REQUESTED judgment "verdict is APPROVED"',
            lines,
        )
        missing = json.dumps(f"no record at {self.records / 'defects' / 'sonnet.json'}")
        self.assertIn(f"FAIL demo defects sonnet verdict CHANGES_REQUESTED run {missing}", lines)
        self.assertTrue(
            any(
                line.startswith("FAIL demo defects opus finding a.go:3 MUST_FIX record ")
                and "is not a review record" in line
                for line in lines
            )
        )
        # Each record that counts for nothing is told once, before every outcome.
        self.assertEqual(f"NOT_COUNTED defects sonnet run {missing}", lines[0])
        self.assertTrue(lines[1].startswith('NOT_COUNTED defects opus record "'), lines[1])
        self.assertTrue(lines[2].startswith(("PASS ", "FAIL ")), lines[2])
        self.assertEqual("| defects | 0/2 | 0/2 | 0/2 |", lines[-1])

    def test_unreadable_record_fails(self) -> None:
        (self.records / "defects").mkdir(parents=True)
        (self.records / "defects" / "haiku.json").write_text("{", encoding="utf-8")
        status, lines = run_main(["demo", "--records", str(self.records), "--model", "haiku"], self.scenarios)
        self.assertEqual(1, status)
        self.assertIn("cannot read", lines[0])

    def test_only_the_named_models_in_the_order_given(self) -> None:
        self.write_record("opus", contract_record([finding("a.go", 3, "MUST_FIX")], "claude-opus-5-5"))
        argv = ["demo", "--records", str(self.records), "--model", "opus", "--model", "haiku", "--model", "opus"]
        status, lines = run_main(argv, self.scenarios)
        self.assertEqual(1, status)
        self.assertEqual("| Scenario | opus | haiku |", lines[-3])
        self.assertEqual("| defects | 2/2 | 0/2 |", lines[-1])

    def test_repeated_scenarios_print_each_expectation_and_table_row_once(self) -> None:
        write_scenario(self.scenarios, "clean", scenario_spec())
        self.write_record("haiku", contract_record(), "clean")
        argv = [
            "demo",
            "--records",
            str(self.records),
            "--model",
            "haiku",
            "--scenario",
            "clean",
            "--scenario",
            "defects",
            "--scenario",
            "clean",
            "--scenario",
            "defects",
        ]
        status, lines = run_main(argv, self.scenarios)
        self.assertEqual(1, status)
        missing = json.dumps(f"no record at {self.records / 'defects' / 'haiku.json'}")
        self.assertEqual(
            [
                f"NOT_COUNTED defects haiku run {missing}",
                "PASS demo clean haiku verdict APPROVED",
                f"FAIL demo defects haiku finding a.go:3 MUST_FIX run {missing}",
                f"FAIL demo defects haiku verdict CHANGES_REQUESTED run {missing}",
                "| Scenario | haiku |",
                "| --- | --- |",
                "| clean | 1/1 |",
                "| defects | 0/2 |",
            ],
            lines,
        )

    def test_a_record_the_record_contract_refuses_fails_as_unreadable(self) -> None:
        # The shape the judge reads, without the rest of a record: no schema_version, repository, or pull request.
        path = self.write_record("opus", {"review": {"version": 1, "verdict": "CHANGES_REQUESTED"}, "findings": []})
        status, lines = run_main(["demo", "--records", str(self.records), "--model", "opus"], self.scenarios)
        self.assertEqual(1, status)
        reason = f"{path} is not a review record: record.schema_version must be an integer, found null"
        self.assertIn(f"FAIL demo defects opus verdict CHANGES_REQUESTED record {json.dumps(reason)}", lines)
        self.assertEqual("| defects | 0/2 |", lines[-1])

    def test_a_record_a_run_would_not_count_counts_for_nothing(self) -> None:
        defect = [finding("a.go", 3, "MUST_FIX")]
        again = write_scenario(self.scenarios, "again", scenario_spec(mode="re-review", prior="prior.json"))
        (again / "prior.json").write_text("{}", encoding="utf-8")
        cases = {
            # Another model's record, copied under this model's name.
            "its reviewers did not all run on claude-haiku-5-5: generic on claude-opus-5-5": (
                "defects",
                contract_record(defect, "claude-opus-5-5"),
            ),
            "no reviewer guard held generic: its files_read is null": (
                "defects",
                contract_record(defect, files_read=None),
            ),
            "it is review version 1, not the 2 a re-review run records": ("again", contract_record(defect)),
        }
        for reason, (scenario, value) in cases.items():
            with self.subTest(reason=reason):
                path = self.write_record("haiku", value, scenario)
                argv = ["demo", "--records", str(self.records), "--model", "haiku", "--scenario", scenario]
                status, lines = run_main(argv, self.scenarios)
                self.assertEqual(1, status)
                told = json.dumps(f"{path} counts for nothing: {reason}")
                verdict = "APPROVED" if scenario == "again" else "CHANGES_REQUESTED"
                self.assertIn(f"NOT_COUNTED {scenario} haiku record {told}", lines)
                self.assertIn(f"FAIL demo {scenario} haiku verdict {verdict} record {told}", lines)
        # A reviewer that reports the model's ID with a dated suffix ran on it.
        self.write_record("haiku", contract_record(defect, "claude-haiku-5-5-20261001"))
        argv = ["demo", "--records", str(self.records), "--model", "haiku", "--scenario", "defects"]
        self.assertEqual(0, run_main(argv, self.scenarios)[0])

    def test_a_timeout_is_a_positive_number_of_seconds(self) -> None:
        for value in ("0", "-1", "nan", "inf", "-inf", "soon"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                skill_evals.main(["demo", "--timeout", value], self.scenarios)
            self.assertEqual(2, raised.exception.code)
        self.assertEqual(0.5, skill_evals.parser().parse_args(["demo", "--timeout", "0.5"]).timeout)
        self.assertEqual(skill_evals.DEFAULT_TIMEOUT, skill_evals.parser().parse_args(["demo"]).timeout)

    def test_an_unknown_kind_fails_the_run_before_judging(self) -> None:
        write_scenario(self.scenarios, "odd", scenario_spec({"kind": "vibes"}))
        status, lines = run_main(["demo", "--records", str(self.records)], self.scenarios)
        self.assertEqual(1, status)
        self.assertEqual(['FAILED "odd: unknown expectation kind \\"vibes\\""'], lines)

    def test_an_unknown_model_is_a_usage_error(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as raised:
            skill_evals.main(["demo", "--records", str(self.records), "--model", "gpt"], self.scenarios)
        self.assertEqual(2, raised.exception.code)

    def test_judge_takes_any_record_source(self) -> None:
        def source(scenario: Scenario, model: str, index: skill_evals_records.Index) -> dict[str, Any]:
            if model == "sonnet":
                raise RecordError("the run failed")
            if model == "opus":
                raise RecordError("the record is another model's", skill_evals_records.RECORD)
            return record(verdict="CHANGES_REQUESTED")

        scenarios = skill_evals_scenarios.load_scenarios("demo", root=self.scenarios)
        outcomes = skill_evals_judging.judge(scenarios, ["haiku", "sonnet", "opus"], source)
        self.assertEqual(
            [
                Outcome(
                    "defects",
                    "haiku",
                    "finding a.go:3 MUST_FIX",
                    "no finding at least MUST_FIX on those lines; findings on a.go: none",
                    "judgment",
                ),
                Outcome("defects", "haiku", "verdict CHANGES_REQUESTED", None),
                Outcome("defects", "sonnet", "finding a.go:3 MUST_FIX", "the run failed", "run"),
                Outcome("defects", "sonnet", "verdict CHANGES_REQUESTED", "the run failed", "run"),
                Outcome("defects", "opus", "finding a.go:3 MUST_FIX", "the record is another model's", "record"),
                Outcome("defects", "opus", "verdict CHANGES_REQUESTED", "the record is another model's", "record"),
            ],
            outcomes,
        )
        self.assertEqual(
            [
                "| Scenario | haiku | sonnet | opus |",
                "| --- | --- | --- | --- |",
                "| defects | 1/2 | 0/2 | 0/2 |",
            ],
            skill_evals_judging.table(scenarios, ["haiku", "sonnet", "opus"], outcomes),
        )
        self.assertEqual(
            [
                'NOT_COUNTED defects sonnet run "the run failed"',
                'NOT_COUNTED defects opus record "the record is another model\'s"',
            ],
            skill_evals_judging.not_counted(outcomes),
        )

    def test_each_of_several_runs_reads_its_own_record_and_the_runs_that_passed_are_counted(self) -> None:
        self.write_record("haiku-1", contract_record([finding("a.go", 3, "MUST_FIX")], "claude-haiku-5-5"))
        self.write_record("haiku-2", contract_record([finding("a.go", 3, "SHOULD_FIX")], "claude-haiku-5-5"))
        status, lines = run_main(
            ["demo", "--records", str(self.records), "--model", "haiku", "--runs", "3"], self.scenarios
        )
        self.assertEqual(1, status)
        missing = json.dumps(f"no record at {self.records / 'defects' / 'haiku-3.json'}")
        self.assertEqual(
            [
                f"NOT_COUNTED defects haiku run=3 run {missing}",
                "PASS demo defects haiku run=1 finding a.go:3 MUST_FIX",
                "PASS demo defects haiku run=1 verdict CHANGES_REQUESTED",
                "FAIL demo defects haiku run=2 finding a.go:3 MUST_FIX judgment "
                '"no finding at least MUST_FIX on those lines; findings on a.go: SHOULD_FIX at line 3"',
                'FAIL demo defects haiku run=2 verdict CHANGES_REQUESTED judgment "verdict is APPROVED"',
                f"FAIL demo defects haiku run=3 finding a.go:3 MUST_FIX run {missing}",
                f"FAIL demo defects haiku run=3 verdict CHANGES_REQUESTED run {missing}",
                "RUNS defects haiku 1/3 some",
                "| Scenario | haiku |",
                "| --- | --- |",
                "| defects | 2/6 (1/3 runs) |",
            ],
            lines,
        )

    def test_the_runs_line_tells_every_run_passing_from_none(self) -> None:
        for index in (1, 2):
            self.write_record(f"opus-{index}", contract_record([finding("a.go", 3, "MUST_FIX")], "claude-opus-5-5"))
            self.write_record(f"haiku-{index}", contract_record([], "claude-haiku-5-5"))
        argv = ["demo", "--records", str(self.records), "--model", "opus", "--model", "haiku", "--runs", "2"]
        _, lines = run_main(argv, self.scenarios)
        self.assertEqual(["RUNS defects opus 2/2 all", "RUNS defects haiku 0/2 none"], lines[-5:-3])
        self.assertEqual("| defects | 4/4 (2/2 runs) | 0/4 (0/2 runs) |", lines[-1])

    def test_runs_are_a_positive_count(self) -> None:
        for value in ("0", "-1", "two"):
            with (
                self.subTest(value=value),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                run_main(["demo", "--records", str(self.records), "--runs", value], self.scenarios)
            self.assertEqual(2, raised.exception.code)

    def test_a_record_lacking_what_an_expectation_reads_is_a_judgment(self) -> None:
        again = write_scenario(
            self.scenarios,
            "again",
            scenario_spec({"kind": "ledger", "addressed": 1, "still_present": 0}, mode="re-review", prior="prior.json"),
        )
        (again / "prior.json").write_text("{}", encoding="utf-8")
        scenarios = skill_evals_scenarios.load_scenarios("demo", ["again"], self.scenarios)
        outcomes = skill_evals_judging.judge(scenarios, ["haiku"], lambda _scenario, _model, _index: record())
        self.assertEqual(
            [Outcome("again", "haiku", "ledger addressed=1 still_present=0", "the record has no ledger", "judgment")],
            outcomes,
        )
        self.assertEqual([], skill_evals_judging.not_counted(outcomes))


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def prior_of(scenario: Scenario) -> Any:
    if scenario.prior is None:
        raise AssertionError(f"{scenario.name} has no prior record")
    return json.loads(scenario.prior.read_text(encoding="utf-8"))


def constant(value: dict[str, Any]) -> skill_evals_runs.RecordSource:
    """A record source that returns the same record for every run."""
    return lambda _scenario, _model, _index: value


class ReviewPrsScenarioTests(unittest.TestCase):
    """The shipped scenarios load, point at lines that changed, and an ideal run of each passes."""

    def setUp(self) -> None:
        self.scenarios = {scenario.name: scenario for scenario in skill_evals_scenarios.load_scenarios("review-prs")}
        self.specs = {
            name: json.loads((scenario.directory / "scenario.json").read_text(encoding="utf-8"))
            for name, scenario in self.scenarios.items()
        }

    def test_the_five_scenarios(self) -> None:
        self.assertEqual(
            {
                "planted-defects": "initial",
                "clean-change": "initial",
                "re-review": "re-review",
                "design-gaps": "initial",
                "design-clean": "initial",
            },
            {name: scenario.mode for name, scenario in self.scenarios.items()},
        )

    def test_the_design_scenarios_route_their_document_to_the_suites_design_specialist(self) -> None:
        for name in ("design-gaps", "design-clean"):
            directory = self.scenarios[name].directory
            pull = json.loads((directory / "pull.json").read_text(encoding="utf-8"))
            with self.subTest(scenario=name):
                base, head = (directory / tree / pull["manifest_path"] for tree in ("base", "head"))
                self.assertEqual(base.read_bytes(), head.read_bytes(), "the manifest is no part of the change")
                manifest = validate_adapter_manifest(json.loads(base.read_text(encoding="utf-8")))
                (specialist,) = manifest["specialists"]
                self.assertEqual("suite:design-review", specialist["profile"])
                changed = [
                    path.relative_to(directory / "head").as_posix()
                    for path in (directory / "head").rglob("*")
                    if path.is_file() and not (directory / "base" / path.relative_to(directory / "head")).is_file()
                ]
                self.assertEqual(changed, review_specialists.route(manifest, changed, lambda _: True)["design-review"])
                for item in self.specs[name]["expectations"]:
                    if "category" in item:
                        self.assertIn(item["category"], manifest["finding_categories"])

    def test_the_design_gaps_are_one_per_planted_category(self) -> None:
        expectations = self.specs["design-gaps"]["expectations"]
        planted = [item.get("category") for item in expectations if item["kind"] == "finding"]
        self.assertEqual(["inconsistency", "requirement-gap", "open-question", "alternative-unstated"], planted)

    def test_expectations_name_changed_files_and_existing_lines(self) -> None:
        for name, spec in self.specs.items():
            directory = self.scenarios[name].directory
            for item in spec["expectations"]:
                for path in [item["path"]] if "path" in item else item.get("paths", []):
                    with self.subTest(scenario=name, expectation=item, path=path):
                        head = directory / "head" / path
                        self.assertTrue(head.is_file())
                        base = directory / "base" / path
                        self.assertFalse(base.is_file() and lines_of(base) == lines_of(head), "unchanged")
                        self.assertLessEqual(max(item.get("lines", [1])), len(lines_of(head)))

    def test_planted_defects_are_one_per_severity(self) -> None:
        planted = [
            item["severity"] for item in self.specs["planted-defects"]["expectations"] if item["kind"] == "finding"
        ]
        self.assertEqual(sorted(skill_evals_scenarios.SEVERITIES), sorted(planted))

    def test_the_re_review_changes_only_the_fixed_defect(self) -> None:
        first, again = self.scenarios["planted-defects"].directory, self.scenarios["re-review"].directory
        for tree in ("base", "head"):
            names = sorted(
                path.relative_to(first / tree).as_posix() for path in (first / tree).rglob("*") if path.is_file()
            )
            for name in names:
                if tree == "head" and name == "store.go":
                    continue
                with self.subTest(tree=tree, name=name):
                    self.assertEqual((first / tree / name).read_bytes(), (again / tree / name).read_bytes())
        planted, fixed = lines_of(first / "head" / "store.go"), lines_of(again / "head" / "store.go")
        self.assertEqual(planted[:72], fixed[:72])

    def test_the_prior_record_is_a_valid_review_of_the_planted_defects(self) -> None:
        prior = validate_record(prior_of(self.scenarios["re-review"]))
        planted = {
            item["severity"]: item
            for item in self.specs["planted-defects"]["expectations"]
            if item["kind"] == "finding"
        }
        for found in prior["findings"]:
            with self.subTest(finding=found["id"]):
                self.assertIn(found["line"], planted[found["severity"]]["lines"])
                self.assertEqual(planted[found["severity"]]["path"], found["path"])
        entries = {f"v{item['version']}:{item['id']}" for item in prior["ledger"]}
        named = {item["entry"] for item in self.specs["re-review"]["expectations"] if "entry" in item}
        self.assertLessEqual(named, entries)

    def ideal(self, name: str) -> dict[str, Any]:
        """A record that does exactly what the scenario expects."""
        expectations = self.specs[name]["expectations"]
        verdict = next(item["verdict"] for item in expectations if item["kind"] == "verdict")
        found = [
            finding(
                item["path"],
                item["lines"][0],
                item["severity"],
                f"F{index:03}",
                **({"category": item["category"]} if "category" in item else {}),
            )
            for index, item in enumerate(expectations, 1)
            if item["kind"] == "finding"
        ]
        if self.scenarios[name].mode == "initial":
            return record(found, verdict)
        prior = prior_of(self.scenarios[name])
        judged = {item["entry"]: item["disposition"] for item in expectations if item["kind"] == "disposition"}
        ledger = [
            entry(
                item["version"],
                item["id"],
                (2, judged[f"v{item['version']}:{item['id']}"]),
                severity=item["severity"],
            )
            for item in prior["ledger"]
        ]
        repeats = [item for item in expectations if item["kind"] == "repeats"]
        found += [
            finding(
                item["path"],
                item["lines"][0],
                "MUST_FIX",
                f"F{index:03}",
                repeats={"version": 1, "id": item["entry"][3:]},
            )
            for index, item in enumerate(repeats, 1)
        ]
        return record(found, verdict, version=2, ledger=ledger)

    def test_an_ideal_run_passes_every_expectation(self) -> None:
        for name, scenario in self.scenarios.items():
            with self.subTest(scenario=name):
                ideal = self.ideal(name)
                outcomes = skill_evals_judging.judge([scenario], ["opus"], constant(ideal))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])


def document_of(scenario: Scenario) -> Path:
    if scenario.document is None:
        raise AssertionError(f"{scenario.name} names no document")
    return scenario.document


def base_of(scenario: Scenario) -> Path:
    if scenario.base is None:
        raise AssertionError(f"{scenario.name} names no base")
    return scenario.base


def design_document(scenario: str) -> Path:
    """The design document a review-prs design scenario's change adds."""
    (path,) = (REVIEW_PRS / scenario / "head" / "docs" / "design").glob("*.md")
    return path.resolve()


class ReviewDocumentScenarioTests(unittest.TestCase):
    """The review-document scenarios review the review-prs design documents, which they name rather than copy, one
    edit of the clean one, and the gaps document written as a Word draft and as a PDF; an ideal run of each passes."""

    def setUp(self) -> None:
        self.scenarios = {
            scenario.name: scenario for scenario in skill_evals_scenarios.load_scenarios("review-document")
        }
        self.specs = {
            name: json.loads((scenario.directory / "scenario.json").read_text(encoding="utf-8"))
            for name, scenario in self.scenarios.items()
        }

    def test_the_scenarios_and_their_documents(self) -> None:
        clean = design_document("design-clean")
        self.assertEqual(
            {
                "design-gaps": (design_document("design-gaps"), None),
                "design-clean": (clean, None),
                "committed-edit": ((REVIEW_DOCUMENT / "committed-edit" / clean.name).resolve(), clean),
                "word-gaps": ((REVIEW_DOCUMENT / "word-gaps" / "export-retention.docx").resolve(), None),
                "pdf-gaps": ((REVIEW_DOCUMENT / "pdf-gaps" / "export-retention.pdf").resolve(), None),
                "design-draft": ((REVIEW_DOCUMENT / "design-draft" / "export-sharing.md").resolve(), None),
                "design-final": ((REVIEW_DOCUMENT / "design-final" / "export-link-expiry.md").resolve(), None),
            },
            {name: (scenario.document, scenario.base) for name, scenario in self.scenarios.items()},
        )
        self.assertEqual(
            {"design-draft": "draft", "design-final": "final"},
            {name: scenario.stage for name, scenario in self.scenarios.items() if scenario.stage is not None},
        )
        self.assertEqual({"initial"}, {scenario.mode for scenario in self.scenarios.values()})

    def test_a_whole_document_is_expected_to_get_what_its_review_prs_change_gets(self) -> None:
        for name in ("design-gaps", "design-clean"):
            with self.subTest(scenario=name):
                theirs = json.loads((REVIEW_PRS / name / "scenario.json").read_text(encoding="utf-8"))["expectations"]
                # Reviewed alone, outside any checkout, the document's path is its file name.
                alone = [
                    {**item, "path": PurePosixPath(item["path"]).name} if "path" in item else item for item in theirs
                ]
                self.assertEqual(alone, self.specs[name]["expectations"])

    def test_the_word_draft_plants_the_gaps_documents_gaps_on_the_lines_its_text_extracts_to(self) -> None:
        markdown = lines_of(design_document("design-gaps"))
        extracted = extract("docx", document_of(self.scenarios["word-gaps"]).read_bytes()).decode("utf-8").splitlines()
        theirs = [item for item in self.specs["design-gaps"]["expectations"] if item["kind"] == "finding"]
        ours = [item for item in self.specs["word-gaps"]["expectations"] if item["kind"] == "finding"]
        self.assertEqual(
            [(item["severity"], item["category"]) for item in theirs],
            [(item["severity"], item["category"]) for item in ours],
        )
        for mine, other in zip(ours, theirs, strict=True):
            with self.subTest(category=mine["category"]):
                self.assertEqual("export-retention.docx", mine["path"])
                # Each extracted line, its place label aside, is the Markdown line that plants the same gap.
                self.assertEqual(
                    [markdown[line - 1] for line in other["lines"]],
                    [split_label(extracted[line - 1])[1] for line in mine["lines"]],
                )
        self.assertEqual(self.specs["design-gaps"]["expectations"][-1], self.specs["word-gaps"]["expectations"][-1])

    def test_the_pdf_draft_plants_the_gaps_documents_gaps_on_the_lines_its_text_extracts_to(self) -> None:
        content = document_of(self.scenarios["pdf-gaps"]).read_bytes()
        # Read from the PDF itself, one shown string per line, independently of any pdftotext.
        streams = re.findall(rb"\nstream\n(.*?)\nendstream", content, re.DOTALL)
        shown = tuple(
            tuple(text.decode("latin-1") for text in re.findall(rb"\((.*?)\) Tj", stream)) for stream in streams
        )
        self.assertEqual(PDF_GAPS_PAGES, shown)
        pdftotext_reads_pdf_gaps(self)
        extracted = extract("pdf", content).decode("utf-8")
        texts, places = [split_label(line)[1] for line in extracted.splitlines()], locations(extracted)
        theirs = [item for item in self.specs["design-gaps"]["expectations"] if item["kind"] == "finding"]
        ours = [item for item in self.specs["pdf-gaps"]["expectations"] if item["kind"] == "finding"]
        self.assertEqual(
            [(item["severity"], item["category"]) for item in theirs],
            [(item["severity"], item["category"]) for item in ours],
        )
        self.assertEqual({"export-retention.pdf"}, {item["path"] for item in ours})
        self.assertEqual(
            [
                [
                    ("page 1, line 14", "Enterprise 365 days"),
                    ("page 1, line 20", "Enterprise exports are kept for 90 days, the same as Team."),
                ],
                [
                    ("page 1, line 7", "R1. An export is deleted when its retention period ends."),
                    ("page 1, line 8", "The period depends on the account plan, as the table below gives it."),
                    ("page 1, line 22", "The sweep skips every account with a legal hold."),
                ],
                [
                    ("page 2, line 8", "Should a deleted export stay recoverable for 7 days in a holding bucket?"),
                    ("page 2, line 9", "This decides whether the sweep deletes objects or moves them."),
                ],
                [("page 2, line 2", "Object storage lifecycle rules, which expire objects by age. Rejected.")],
            ],
            [[(places[line - 1], texts[line - 1]) for line in item["lines"]] for item in ours],
        )
        self.assertEqual(self.specs["design-gaps"]["expectations"][-1], self.specs["pdf-gaps"]["expectations"][-1])

    def test_the_edit_changes_one_line_and_the_expectations_name_only_it(self) -> None:
        scenario = self.scenarios["committed-edit"]
        base, head = lines_of(base_of(scenario)), lines_of(document_of(scenario))
        changed = [number for number, pair in enumerate(zip(base, head, strict=True), 1) if pair[0] != pair[1]]
        self.assertEqual([17], changed)
        located = [item for item in self.specs["committed-edit"]["expectations"] if "lines" in item]
        self.assertEqual(["finding", "only_at"], [item["kind"] for item in located])
        for item in located:
            self.assertEqual((document_of(scenario).name, changed), (item["path"], item["lines"]))

    def test_an_ideal_run_passes_every_expectation(self) -> None:
        for name, scenario in self.scenarios.items():
            expectations = self.specs[name]["expectations"]
            found = [
                finding(
                    item["path"],
                    item["lines"][0],
                    item["severity"],
                    f"F{index:03}",
                    category=item.get("category", "other"),
                )
                for index, item in enumerate(expectations, 1)
                if item["kind"] == "finding"
            ]
            verdict = next(item["verdict"] for item in expectations if item["kind"] == "verdict")
            with self.subTest(scenario=name):
                outcomes = skill_evals_judging.judge([scenario], ["opus"], constant(record(found, verdict)))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])


class DocumentStagingTests(unittest.TestCase):
    """A document scenario's document is written into the run's folder for the skill to find by its path."""

    # The home conftest.py's git_home fixture gives the running test's git.
    git_home: GitHome

    def setUp(self) -> None:
        self.root = temporary_root(self)
        self.scenarios = {
            scenario.name: scenario for scenario in skill_evals_scenarios.load_scenarios("review-document")
        }
        self.git = GitClient()

    def test_a_whole_document_is_staged_alone_outside_any_checkout(self) -> None:
        scenario = self.scenarios["design-gaps"]
        staged = skill_evals_scenarios.stage_document(scenario, self.root / "document", self.git)
        self.assertEqual(self.root / "document" / "export-retention.md", staged)
        self.assertEqual(document_of(scenario).read_bytes(), staged.read_bytes())
        self.assertEqual(["export-retention.md"], [path.name for path in staged.parent.iterdir()])
        self.assertNotEqual(0, self.git.run(["rev-parse", "--show-toplevel"], directory=staged.parent).returncode)

    def test_an_edit_is_staged_uncommitted_over_its_base_committed_in_a_new_checkout(self) -> None:
        scenario = self.scenarios["committed-edit"]
        staged = skill_evals_scenarios.stage_document(scenario, self.root / "document", self.git)
        folder = staged.parent
        self.assertEqual(self.root / "document" / "export-download-links.md", staged)
        self.assertEqual(document_of(scenario).read_bytes(), staged.read_bytes())
        top = self.git.output(["rev-parse", "--show-toplevel"], directory=folder).strip()
        self.assertEqual(folder.resolve(), Path(top).resolve())
        self.assertEqual("1", self.git.output(["rev-list", "--count", "HEAD"], directory=folder).strip())
        committed = self.git.run(["show", f"HEAD:{staged.name}"], directory=folder).output_bytes()
        self.assertEqual(base_of(scenario).read_bytes(), committed)
        self.assertEqual(f" M {staged.name}", self.git.output(["status", "--porcelain"], directory=folder).rstrip())

    def test_a_document_base_runs_no_global_prepare_commit_msg_or_post_commit_hook(self) -> None:
        self.assert_staging_ignores_hooks("core.hooksPath")

    def test_a_document_base_runs_no_template_prepare_commit_msg_or_post_commit_hook(self) -> None:
        self.assert_staging_ignores_hooks("init.templateDir")

    def assert_staging_ignores_hooks(self, setting: str) -> None:
        template = self.root / "git template"
        hooks = template / "hooks"
        hooks.mkdir(parents=True)
        for name in ("prepare-commit-msg", "post-commit"):
            hook = hooks / name
            hook.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}' >> '../hook-effects'\n", encoding="utf-8")
            hook.chmod(0o755)
        # The global configuration conftest.py gives the test, its empty hooks folder replaced by the hooks and
        # signing a user may have.
        config = self.git_home.config
        self.git.output(["config", "--file", str(config), "--unset", "core.hooksPath"])
        self.git.output(
            ["config", "--file", str(config), setting, str(hooks if setting == "core.hooksPath" else template)]
        )
        self.git.output(["config", "--file", str(config), "commit.gpgsign", "true"])
        self.git.output(["config", "--file", str(config), "gpg.program", str(self.root / "missing-signer")])
        scenario = self.scenarios["committed-edit"]
        sources = (document_of(scenario), base_of(scenario), config, *hooks.iterdir())
        before = {path: path.read_bytes() for path in sources}
        control = self.root / "control"
        control.mkdir()
        self.git.output(["init", "--quiet"], directory=control)
        self.git.output(
            [*skill_evals_scenarios.COMMIT, "commit", "--quiet", "--no-verify", "--allow-empty", "-m", "Control"],
            directory=control,
        )
        effects = self.root / "hook-effects"
        self.assertEqual(["prepare-commit-msg", "post-commit"], effects.read_text(encoding="utf-8").splitlines())
        effects.unlink()

        staged = skill_evals_scenarios.stage_document(scenario, self.root / "document", self.git)
        self.assertFalse(effects.exists(), "the evaluation base commit ran a user hook")
        self.assertEqual(document_of(scenario).read_bytes(), staged.read_bytes())
        self.assertEqual(
            base_of(scenario).read_bytes(),
            self.git.run(["show", f"HEAD:{staged.name}"], directory=staged.parent).output_bytes(),
        )
        self.assertEqual("1", self.git.output(["rev-list", "--count", "HEAD"], directory=staged.parent).strip())
        self.assertEqual(
            "skill-evals <skill-evals@example.invalid>",
            self.git.output(["log", "-1", "--format=%an <%ae>"], directory=staged.parent).strip(),
        )
        self.assertNotIn("gpgsig", self.git.output(["cat-file", "commit", "HEAD"], directory=staged.parent))
        self.assertEqual(
            f" M {staged.name}", self.git.output(["status", "--porcelain"], directory=staged.parent).rstrip()
        )
        self.assertEqual(before, {path: path.read_bytes() for path in sources})

    def test_the_skill_starts_on_the_staged_document_and_a_fixture_scenario_on_its_fixture(self) -> None:
        run = self.root / "run"
        arguments = skill_evals_scenarios.skill_arguments(self.scenarios["design-clean"], run, self.git)
        self.assertEqual([str(run / "document" / "export-download-links.md")], arguments)
        self.assertEqual(f'/review-document "{arguments[0]}"', skill_evals_runs.prompt("review-document", arguments))
        arguments = skill_evals_scenarios.skill_arguments(
            self.scenarios["design-draft"], self.root / "draft run", self.git
        )
        self.assertEqual(
            [str(self.root / "draft run" / "document" / "export-sharing.md"), "--stage", "draft"], arguments
        )
        self.assertEqual(
            f'/review-document "{arguments[0]}" --stage "draft"', skill_evals_runs.prompt("review-document", arguments)
        )
        fixture = scenario_of("initial")
        self.assertEqual(
            skill_evals_scenarios.prepare_arguments(fixture),
            skill_evals_scenarios.skill_arguments(fixture, self.root / "other", self.git),
        )
        self.assertFalse((self.root / "other").exists())


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def reviewer_spec(**changes: Any) -> dict[str, Any]:
    spec = {"skill": "review-prs", "reviewer": "review-repository", "mode": "initial", **changes}
    spec["expectations"] = [{"kind": "verdict", "verdict": "APPROVED"}]
    return spec


def write_reviewer_scenario(
    root: Path, spec: dict[str, Any], skill: str = "review-repository", manifest_path: str = MANIFEST_PATH
) -> Path:
    directory = write_scenario(root, "case", spec, skill)
    pull = json.loads((REVIEW_REPOSITORY / "drifted-guide" / "pull.json").read_text(encoding="utf-8"))
    (directory / "pull.json").write_text(json.dumps({**pull, "manifest_path": manifest_path}), encoding="utf-8")
    return directory


class ReviewerScenarioTests(unittest.TestCase):
    """A repository reviewer's scenarios: review-prs starts them, and a run places the reviewer's files from the
    checkout in a throwaway copy of the fixture, never in the fixture."""

    def setUp(self) -> None:
        self.root = temporary_root(self)
        self.copies = self.root / "copies"
        self.copies.mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.copies))
        patcher.start()
        self.addCleanup(patcher.stop)

    def refusal(self, skill: str = "review-repository", checkout: Path = skill_evals_scenarios.REPOSITORY_ROOT) -> str:
        with self.assertRaises(ScenarioError) as raised:
            skill_evals_scenarios.load_scenarios(skill, root=self.root, checkout=checkout)
        return str(raised.exception)

    def test_review_repository_s_scenarios_start_review_prs_with_the_checkout_s_reviewer(self) -> None:
        scenarios = skill_evals_scenarios.load_scenarios("review-repository")
        self.assertEqual(list(REVIEW_REPOSITORY_SCENARIOS), [scenario.name for scenario in scenarios])
        for scenario in scenarios:
            with self.subTest(scenario=scenario.name):
                self.assertEqual(
                    ("review-prs", "review-repository", skill_evals_scenarios.REPOSITORY_ROOT),
                    (scenario.skill, scenario.reviewer, scenario.checkout),
                )
                self.assertLessEqual(set(REVIEW_REPOSITORY_PLACED), set(scenario.placed))
                self.assertEqual(set(), set(REVIEW_REPOSITORY_UNPLACED) & set(scenario.placed))
                self.assertFalse(any("__pycache__" in path for path in scenario.placed))
                # The fixtures hold no copy of the skill or of anything else the run places.
                for tree in ("base", "head"):
                    self.assertFalse((scenario.directory / tree / ".claude").exists())
                    self.assertEqual(
                        [], [path for path in scenario.placed if (scenario.directory / tree / path).exists()]
                    )

    def test_each_scenario_s_fixture_is_checked_once(self) -> None:
        for skill in ("review-repository", "review-prs"):
            with (
                self.subTest(skill=skill),
                mock.patch.object(skill_evals_scenarios, "_fixture", wraps=skill_evals_scenarios._fixture) as checked,
            ):
                scenarios = skill_evals_scenarios.load_scenarios(skill)
            self.assertEqual(
                sorted(scenario.directory for scenario in scenarios),
                sorted(call.args[0] for call in checked.call_args_list),
            )

    def test_the_copy_holds_the_checkout_s_files_in_both_trees_and_is_removed_after_the_run(self) -> None:
        (scenario,) = skill_evals_scenarios.load_scenarios("review-repository", ["untested-change"])
        before = tree_bytes(scenario.directory)
        with skill_evals_scenarios.reviewed_fixture(scenario) as fixture:
            self.assertEqual(self.copies, fixture.parent.parent)
            self.assertEqual("untested-change", fixture.name)
            copied = tree_bytes(fixture)
            for tree in ("base", "head"):
                for path in REVIEW_REPOSITORY_PLACED:
                    with self.subTest(tree=tree, path=path):
                        self.assertEqual(
                            (skill_evals_scenarios.REPOSITORY_ROOT / path).read_bytes(), copied[f"{tree}/{path}"]
                        )
                for path in REVIEW_REPOSITORY_UNPLACED:
                    self.assertEqual(before.get(f"{tree}/{path}"), copied.get(f"{tree}/{path}"), "the fixture's own")
            self.assertEqual(before, {path: copied[path] for path in before}, "the fixture's own files, unchanged")
            self.assertEqual(
                ["--canary", "--fixture", str(fixture)],
                skill_evals_scenarios.skill_arguments(scenario, self.root / "run", GitClient(), fixture),
            )
        self.assertEqual([], list(self.copies.iterdir()))
        self.assertEqual(before, tree_bytes(scenario.directory))

    def test_the_copy_is_removed_when_the_run_fails(self) -> None:
        (scenario,) = skill_evals_scenarios.load_scenarios("review-repository", ["drifted-guide"])
        with (
            self.assertRaisesRegex(RuntimeError, "the session broke"),
            skill_evals_scenarios.reviewed_fixture(scenario),
        ):
            self.assertEqual(1, len(list(self.copies.iterdir())))
            raise RuntimeError("the session broke")
        self.assertEqual([], list(self.copies.iterdir()))

    def test_the_copy_is_removed_when_a_file_cannot_be_placed(self) -> None:
        (scenario,) = skill_evals_scenarios.load_scenarios("review-repository", ["drifted-guide"])
        missing = dataclasses.replace(scenario, placed=(*scenario.placed, "docs/no-such-file.md"))
        with self.assertRaises(FileNotFoundError), skill_evals_scenarios.reviewed_fixture(missing):
            self.fail("the run started without its reviewer's files")
        self.assertEqual([], list(self.copies.iterdir()))

    def test_any_other_scenario_is_reviewed_in_place(self) -> None:
        for scenario in skill_evals_scenarios.load_scenarios("review-prs"):
            with self.subTest(scenario=scenario.name), skill_evals_scenarios.reviewed_fixture(scenario) as fixture:
                self.assertEqual((REVIEW_PRS / scenario.name, None, ()), (fixture, scenario.reviewer, scenario.placed))
                self.assertEqual(
                    skill_evals_scenarios.prepare_arguments(scenario),
                    skill_evals_scenarios.skill_arguments(scenario, self.root / "run", GitClient(), fixture),
                )
                self.assertEqual("--fixture", skill_evals_scenarios.prepare_arguments(scenario)[1])
                self.assertEqual(str(REVIEW_PRS / scenario.name), skill_evals_scenarios.prepare_arguments(scenario)[2])
        self.assertEqual([], list(self.copies.iterdir()))

    def test_a_reviewer_that_does_not_exist_is_refused(self) -> None:
        write_reviewer_scenario(
            self.root,
            reviewer_spec(reviewer="review-nothing"),
            "review-nothing",
            ".claude/skills/review-nothing/references/specialists.json",
        )
        status, lines = run_main(["review-nothing", "--records", str(self.root)], self.root)
        self.assertEqual(1, status)
        self.assertEqual(
            [
                'FAILED "case: no repository skill review-nothing: the checkout has no '
                '.claude/skills/review-nothing/SKILL.md"'
            ],
            lines,
        )

    def test_a_file_the_reviewer_declares_that_the_checkout_lacks_is_refused(self) -> None:
        checkout = self.root / "checkout"
        shutil.copytree(skill_evals_scenarios.REPOSITORY_ROOT / REVIEWER_FOLDER, checkout / REVIEWER_FOLDER)
        write_reviewer_scenario(self.root, reviewer_spec())
        self.assertEqual(
            "case: review-repository declares docs/design.md, which the checkout lacks",
            self.refusal(checkout=checkout),
        )

    def test_the_reviewer_s_scenario_shape_is_held(self) -> None:
        cases: list[tuple[dict[str, Any], str, str]] = [
            (
                reviewer_spec(reviewer="review-other"),
                MANIFEST_PATH,
                'names reviewer "review-other", not review-repository',
            ),
            (reviewer_spec(skill="review-document"), MANIFEST_PATH, 'started by review-prs, not "review-document"'),
            (reviewer_spec(skill="review-repository"), MANIFEST_PATH, 'started by review-prs, not "review-repository"'),
            (reviewer_spec(document="draft.md"), MANIFEST_PATH, "a reviewer's scenario is a fixture scenario"),
            (
                reviewer_spec(),
                "review/specialists.json",
                "pull.json's manifest_path must name review-repository's manifest under "
                ".claude/skills/review-repository/",
            ),
            (
                reviewer_spec(),
                ".claude/skills/review-repository/references/missing.json",
                "review-repository's manifest .claude/skills/review-repository/references/missing.json cannot be read",
            ),
        ]
        for spec, manifest_path, message in cases:
            with self.subTest(message=message):
                directory = write_reviewer_scenario(self.root, spec, manifest_path=manifest_path)
                (directory / "draft.md").write_text("# Draft\n", encoding="utf-8")
                self.assertIn(message, self.refusal())

    def test_a_category_the_reviewer_s_manifest_does_not_list_is_refused(self) -> None:
        listed = {"kind": "finding", "path": "tools/a.sh", "lines": [1], "severity": "SHOULD_FIX"}
        spec = reviewer_spec()
        spec["expectations"] = [{**listed, "category": "test coverage"}]
        write_reviewer_scenario(self.root, spec)
        self.assertEqual(1, len(skill_evals_scenarios.load_scenarios("review-repository", root=self.root)))
        spec["expectations"] = [{**listed, "category": "Tset coverage"}]
        write_reviewer_scenario(self.root, spec)
        self.assertIn('case: category "Tset coverage" is not one of the manifest\'s finding_categories', self.refusal())

    def test_a_held_file_is_restored_in_the_copy_and_kept_in_the_fixture(self) -> None:
        (scenario,) = skill_evals_scenarios.load_scenarios("review-repository", ["write-before-refusal"])
        held = [
            f"{tree}/{path}{skill_evals_scenarios.HELD}"
            for tree in ("base", "head")
            for path in WRITE_BEFORE_REFUSAL_HELD
        ]
        self.assertEqual(sorted(held), sorted(scenario.held))
        before = tree_bytes(scenario.directory)
        with skill_evals_scenarios.reviewed_fixture(scenario) as fixture:
            copied = tree_bytes(fixture)
            for path in held:
                with self.subTest(path=path):
                    self.assertEqual(before[path], copied[path.removesuffix(skill_evals_scenarios.HELD)])
            self.assertEqual([], [path for path in copied if path.endswith(skill_evals_scenarios.HELD)])
        self.assertEqual(before, tree_bytes(scenario.directory))
        self.assertEqual([], list(self.copies.iterdir()))

    def test_a_held_file_the_copy_cannot_restore_is_refused(self) -> None:
        cases = [
            ({"head/.fixture": ""}, "case: its head/ tree holds .fixture, which names no file to restore"),
            (
                {"base/tools/a.py": "", "base/tools/a.py.fixture": ""},
                "case: its base/ tree holds both tools/a.py and tools/a.py.fixture",
            ),
            (
                {"head/CLAUDE.md.fixture": ""},
                "case: its head/ tree holds CLAUDE.md.fixture, which the run places from the checkout",
            ),
        ]
        for files, message in cases:
            with self.subTest(message=message):
                directory = write_reviewer_scenario(self.root, reviewer_spec())
                for path, text in files.items():
                    (directory / path).parent.mkdir(parents=True, exist_ok=True)
                    (directory / path).write_text(text, encoding="utf-8")
                self.assertEqual(message, self.refusal())
                shutil.rmtree(directory)

    def test_a_fixture_that_holds_a_file_the_run_places_is_refused(self) -> None:
        directory = write_reviewer_scenario(self.root, reviewer_spec())
        (directory / "head" / "CLAUDE.md").write_text("# A stale copy\n", encoding="utf-8")
        self.assertEqual("case: its head/ tree holds CLAUDE.md, which the run places from the checkout", self.refusal())


class NoGitHub:
    """A gh runner that fails the test on any call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> Any:
        self.calls.append(list(arguments))
        raise AssertionError(f"a fixture canary called gh: {arguments}")


def stub_result(spec: dict[str, Any]) -> dict[str, Any]:
    """A reviewer result that does what the scenario expects: each expected finding at the first of its lines, each
    expected disposition, and each expected repeat as a finding linked to its entry. A document scenario's design
    reviewer gives every finding a category, so one its expectation leaves open is `other`, the fallback."""
    expectations = spec["expectations"]
    fallback = {"category": "other"} if "document" in spec else {}
    found = [
        {
            "path": item["path"],
            "line": item["lines"][0],
            "severity": item["severity"],
            "title": "Planted",
            "body": "x",
            **({"category": item["category"]} if "category" in item else fallback),
        }
        for item in expectations
        if item["kind"] == "finding"
    ]
    found += [
        {
            "path": item["path"],
            "line": item["lines"][0],
            "severity": "MUST_FIX",
            "title": "Still there",
            "body": "x",
            "repeats": item["entry"],
        }
        for item in expectations
        if item["kind"] == "repeats"
    ]
    dispositions = [
        {"finding_id": item["entry"], "disposition": item["disposition"], "rationale": "Judged."}
        for item in expectations
        if item["kind"] == "disposition"
    ]
    return {"model": "stub", "summary": "Reviewed.", "findings": found, "prior_dispositions": dispositions}


def no_tarball(*_: Any) -> None:
    raise AssertionError("a fixture canary fetched a tarball")


class Pipeline:
    """The code-review pipeline run in this process from the checkout, as a deployed review-prs session runs it, with
    a stub reviewer that does what each scenario expects and a GitHub client that fails the test on any call."""

    def __init__(self, test: unittest.TestCase, config: Path) -> None:
        self.test, self.config, self.github = test, config, NoGitHub()
        self.services = review_run.Services(
            github=GitHubClient(runner=self.github), fetch_tarball=no_tarball, resolve_runtime=lambda *_: "claude-code"
        )

    def __call__(self, *arguments: str) -> list[str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = review_pipeline.main(["--config", str(self.config), *arguments], services=self.services)
        lines = output.getvalue().splitlines()
        self.test.assertEqual(0, status, lines)
        return lines

    def review(self, prepare: Sequence[str], spec: dict[str, Any], guarded: bool = False) -> Path:
        """Prepare, review, check, and finalize one fixture, each reviewer doing what the scenario `spec` expects, a
        specialist raising only the expected findings in its own files, as check holds it to; return the record JSON
        finalize wrote. With `guarded`, each role's read log is started as the reviewer guard's claim starts it."""
        prepared = self("prepare", "--host", "claude-code", *prepare)
        run = Path(next(line for line in prepared if line.startswith("RUN ")).split(" ", 2)[2])
        fixture = Path(prepare[list(prepare).index("--fixture") + 1])
        pull = json.loads((fixture / "pull.json").read_text(encoding="utf-8"))
        plan = run / "work" / "plan.json"
        planned = json.loads(plan.read_text(encoding="utf-8"))["roles"] if plan.is_file() else []
        files = {role["id"]: set(role["files"]) for role in planned}
        for role in review_run.load_run(run)["roles"]:
            if guarded:
                review_guard.read_log(run, role["id"]).touch()
            result = stub_result(spec)
            if role["id"] in files:
                result["findings"] = [item for item in result["findings"] if item["path"] in files[role["id"]]]
            Path(role["result_file"]).write_text(json.dumps(result), encoding="utf-8")
        checked = self("check", "--run", str(run))
        self.test.assertEqual([f"ALL_VALID {pull['repository']}#{pull['number']}"], checked)
        finalized = self("finalize", "--run", str(run))
        return Path(
            next(line.split(" ", 2)[2] for line in finalized if line.startswith("SHA256 ") and line.endswith(".json"))
        )


def spec_of(scenario: Scenario) -> dict[str, Any]:
    return json.loads((scenario.directory / "scenario.json").read_text(encoding="utf-8"))


def build_document(path: Path, stage: str | None = None) -> list[str]:
    """What review-document's build step prints for the document, as the skill's first step runs it."""
    return review_document.build(path, None, None, GitClient(), stage)


def fixture_of(built: Sequence[str]) -> str:
    return next(line.removeprefix("FIXTURE ") for line in built if line.startswith("FIXTURE "))


def temporary_root(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name).resolve() / "evals with spaces"
    root.mkdir()
    return root


class FixtureCanaryRunTests(unittest.TestCase):
    """Each shipped scenario runs through prepare_arguments' fixture canary, under the configuration a run writes, a
    stub reviewer, check, and finalize with no gh call, and the record finalize writes is judged by the same code a
    model's run is."""

    def setUp(self) -> None:
        self.root = temporary_root(self)
        (self.root / "tmp").mkdir()
        for patcher in (
            mock.patch.object(tempfile, "tempdir", str(self.root / "tmp")),
            mock.patch.dict(
                os.environ,
                {
                    "CODE_REVIEW_STATE": str(self.root / "state.json"),
                    "CODE_REVIEW_FLAGS": str(self.root / "flags.json"),
                },
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = self.root / "config.json"
        write_config(skill_evals_runs.review_config(self.root), self.config)
        self.pipeline = Pipeline(self, self.config)

    def test_every_scenario_runs_as_a_fixture_canary_and_its_record_passes_the_checker(self) -> None:
        for scenario in skill_evals_scenarios.load_scenarios("review-prs"):
            with self.subTest(scenario=scenario.name):
                written = self.pipeline.review(skill_evals_scenarios.prepare_arguments(scenario), spec_of(scenario))
                written_record = json.loads(written.read_text(encoding="utf-8"))
                self.assertEqual(scenario.mode, written_record["review"]["mode"])
                outcomes = skill_evals_judging.judge([scenario], ["opus"], constant(written_record))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])
        self.assertEqual([], self.pipeline.github.calls)

    def test_a_design_scenario_is_reviewed_by_the_suites_design_specialist_from_its_base_commit(self) -> None:
        scenario = next(
            item for item in skill_evals_scenarios.load_scenarios("review-prs") if item.name == "design-gaps"
        )
        reviewed = self.pipeline.review(skill_evals_scenarios.prepare_arguments(scenario), spec_of(scenario))
        written = json.loads(reviewed.read_text(encoding="utf-8"))
        adapter = written["review"]["adapter"]
        self.assertEqual(
            ("reports-review", "repository", "base"), (adapter["name"], adapter["scope"], adapter["source"])
        )
        suite = SUITE_REFERENCE.read_bytes()
        self.assertEqual({"suite:design-review": hashlib.sha256(suite).hexdigest()}, adapter["source_hashes"])
        self.assertEqual(["design-review"], [reviewer["id"] for reviewer in written["review"]["reviewers"]])
        self.assertEqual(
            {"inconsistency", "requirement-gap", "open-question", "alternative-unstated"},
            {item["category"] for item in written["findings"]},
        )

    def test_every_document_scenario_is_built_by_review_document_and_its_record_passes_the_checker(self) -> None:
        pdftotext_reads_pdf_gaps(self)
        bases = {
            "committed-edit": "BASE committed ",
            "design-clean": "BASE none the file is outside any git checkout",
            "design-gaps": "BASE none the file is outside any git checkout",
            "pdf-gaps": "BASE none the file is outside any git checkout",
            "word-gaps": "BASE none the file is outside any git checkout",
            "design-draft": "BASE none the file is outside any git checkout",
            "design-final": "BASE none the file is outside any git checkout",
        }
        for scenario in skill_evals_scenarios.load_scenarios("review-document"):
            with self.subTest(scenario=scenario.name):
                staged, *stage = skill_evals_scenarios.skill_arguments(scenario, self.root / scenario.name, GitClient())
                self.assertEqual([] if scenario.stage is None else ["--stage", scenario.stage], stage)
                built = build_document(Path(staged), scenario.stage)
                self.assertEqual(f"DOCUMENT {Path(staged).name}", built[0])
                self.assertTrue(built[1].startswith(bases[scenario.name]), built)
                self.assertEqual("ROUTE design-review", built[2])
                fixture = Path(fixture_of(built))
                pull = json.loads((fixture / "pull.json").read_text(encoding="utf-8"))
                self.assertEqual(scenario.stage or "review", pull["document_stage"])
                # A Word document's or a PDF's bytes are its own; a text document's line endings are read as LF.
                document = Path(staged).read_bytes()
                if document_format(Path(staged).name) is None:
                    document = review_document.normalized(document)
                self.assertEqual(document, (fixture / "head" / Path(staged).name).read_bytes())
                prepare = ["--canary", "--fixture", str(fixture)]
                written = json.loads(self.pipeline.review(prepare, spec_of(scenario)).read_text(encoding="utf-8"))
                self.assertEqual(["design-review"], [reviewer["id"] for reviewer in written["review"]["reviewers"]])
                outcomes = skill_evals_judging.judge([scenario], ["opus"], constant(written))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])
        self.assertEqual([], self.pipeline.github.calls)


def assistant(model: str, subagent: bool = False) -> str:
    """One stream-json assistant event, from the session or, with `subagent`, forwarded from a subagent."""
    message = {"type": "assistant", "message": {"model": model, "content": []}}
    return json.dumps({**message, "parent_tool_use_id": "toolu_1" if subagent else None})


REVIEWER_AGENT_SOURCE = skill_evals_scenarios.REPOSITORY_ROOT / "agents" / "code-review-reviewer.md"


def fake_deploy(home: Path, _source: Path) -> tuple[int, str]:
    """What the deployment puts where a run reads it: the reviewer agent, and in place of the guard its hook runs, a
    script that prints its own path."""
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    # As the deployer renders it, with Python named as this platform names it.
    rendered = REVIEWER_AGENT_SOURCE.read_bytes().replace(b"{{PYTHON}}", platform_support.PYTHON.encode())
    (agents / "code-review-reviewer.md").write_bytes(rendered)
    guard = home / ".claude" / "skills" / "code-review-core" / "scripts" / "review_guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text('print("GUARD", __file__)\n', encoding="utf-8")
    return 0, ""


def reviewer_of(arguments: Sequence[str]) -> dict[str, Any]:
    """The reviewer definition a session command passes with --agents."""
    agents = json.loads(Path(arguments[list(arguments).index("--agents") + 1]).read_text(encoding="utf-8"))
    return agents["code-review-reviewer"]


def scenario_of(mode: str) -> Scenario:
    return next(item for item in skill_evals_scenarios.load_scenarios("review-prs") if item.mode == mode)


class RunPieceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_root(self)
        self.initial, self.re_review = scenario_of("initial"), scenario_of("re-review")

    def test_the_prompt_starts_the_skill_with_the_prepare_arguments_quoted(self) -> None:
        def prompt(scenario: Scenario) -> str:
            return skill_evals_runs.prompt(scenario.skill, skill_evals_scenarios.prepare_arguments(scenario))

        self.assertEqual(f'/review-prs --canary --fixture "{self.initial.directory}"', prompt(self.initial))
        self.assertEqual(
            f'/review-prs --canary --fixture "{self.re_review.directory}" --re-review --prior "{self.re_review.prior}"',
            prompt(self.re_review),
        )

    def test_the_command_pins_the_session_and_isolates_the_run(self) -> None:
        home = self.root / "haiku"
        command = skill_evals_runs.claude_command("claude.exe", '/review-prs "fixture"', home, "review-prs")
        self.assertEqual(["claude.exe", "-p", '/review-prs "fixture"'], command[:3])
        pairs = {command[index]: command[index + 1] for index in range(3, len(command) - 1)}
        self.assertEqual("claude-opus-5-5", pairs["--model"])
        self.assertEqual("project,local", pairs["--setting-sources"])
        self.assertEqual(str(home / ".skill-evals" / "agents.json"), pairs["--agents"])
        self.assertEqual("Workflow", pairs["--disallowedTools"])
        self.assertEqual("acceptEdits", pairs["--permission-mode"])
        self.assertEqual("stream-json", pairs["--output-format"])
        flags = ("--verbose", "--forward-subagent-text", "--include-hook-events", "--strict-mcp-config")
        for flag in (*flags, "--no-session-persistence"):
            self.assertIn(flag, command)
        # Each script at the home's deployed core, spelled through the started skill as Claude Code fills in its
        # folder, and as its resolved path, which on a POSIX system is the same text.
        started = f"{home.as_posix()}/.claude/skills/review-prs/../code-review-core/scripts/"
        resolved = f"{home / '.claude' / 'skills' / 'code-review-core' / 'scripts'}{os.sep}"
        python = platform_support.PYTHON
        self.assertEqual(
            [
                f'{shell}({python} -B "{folder}{script}" {subcommand} --run *)'
                for folder in dict.fromkeys([started, resolved])
                for script, subcommand in (
                    ("review_pipeline.py", "validate-result"),
                    ("review_source.py", "source-file"),
                    ("review_source.py", "source-search"),
                )
                for shell in ("Bash", "PowerShell")
            ],
            command[command.index("--allowedTools") + 1 :],
        )

    def test_the_grants_match_the_commands_the_pipeline_writes_and_no_other_script(self) -> None:
        # The home holds a space, as a profile folder may. The commands are the ones the pipeline writes into a role's
        # prompt, for the script spelled as the session started it and as its resolved path.
        home = self.root / "sonnet"
        grants = skill_evals_runs.reviewer_grants(home, "review-document")
        bash = [pattern for tool, pattern in skill_inventory.allowed_entries(grants) if tool == "Bash" and pattern]
        run, role = self.root / "run", "generic-review"
        core = home / ".claude" / "skills" / "code-review-core" / "scripts"
        for folder in (
            f"{home.as_posix()}/.claude/skills/review-document/../code-review-core/scripts/",
            f"{core}{os.sep}",
        ):
            commands = [
                review_run.SELF_CHECK_COMMAND.format(script=f"{folder}review_pipeline.py", run=run, role=role),
                *(
                    command.replace("<path>", "src/a b.py").replace("<pattern>", "TODO")
                    for command in review_source.source_commands(run, role, script=f"{folder}review_source.py")
                ),
            ]
            for command in commands:
                with self.subTest(command=command):
                    self.assertTrue(any(skill_inventory.grants(pattern, command) for pattern in bash))
        self_check = review_run.SELF_CHECK_COMMAND
        elsewhere = [
            # A file named like the script beside it, in the home, or through a path out of the core's folder.
            self_check.format(script=f"{core}{os.sep}x_review_pipeline.py", run=run, role=role),
            self_check.format(script=f"{home.as_posix()}/review_pipeline.py", run=run, role=role),
            self_check.format(script=f"{core}{os.sep}..{os.sep}..{os.sep}review_pipeline.py", run=run, role=role),
            self_check.format(script=f"{self.root.as_posix()}/elsewhere/review_pipeline.py", run=run, role=role),
        ]
        for command in elsewhere:
            with self.subTest(command=command):
                self.assertFalse(any(skill_inventory.grants(pattern, command) for pattern in bash))

    def test_a_home_whose_path_holds_a_star_is_refused_before_anything_is_deployed(self) -> None:
        home = self.root / "a*b" / "haiku"
        reason = skill_evals_runs.prepare_home(home, "haiku", fake_deploy)
        self.assertEqual(
            f"the home's path {home.as_posix()} holds a *, which a reviewer's grant would read as any text", reason
        )
        self.assertFalse(home.parent.exists())

    def test_the_session_model_is_the_strongest_and_one_of_the_models(self) -> None:
        self.assertEqual(
            {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"},
            skill_evals_records.MODELS,
        )
        self.assertEqual("opus", skill_evals_runs.SESSION_MODEL)

    def test_the_environment_keeps_the_users_and_gives_the_run_its_own_files(self) -> None:
        run, config = self.root / "run", self.root / "config.json"
        self.assertEqual(
            {
                "PATH": "kept",
                "TMPDIR": str(run / "tmp"),
                "CODE_REVIEW_CONFIG": str(config),
                "CODE_REVIEW_STATE": str(run / "state.json"),
                "CODE_REVIEW_FLAGS": str(run / "flags.json"),
            },
            skill_evals_runs.run_environment({"PATH": "kept"}, run, config),
        )

    def test_the_configuration_is_one_the_core_accepts(self) -> None:
        written = write_config(skill_evals_runs.review_config(self.root), self.root / "config.json")
        self.assertEqual("claude-code", written["runtime"])
        self.assertEqual(["MUST_FIX"], written["verdict_policy"]["request_changes_for"])

    def test_the_reviewer_agent_gets_the_model_and_nothing_else_changes(self) -> None:
        fake_deploy(self.root, self.root)
        # The deployed agent, which names Python as rendered, before and after its model is set.
        before = (self.root / skill_evals_runs.REVIEWER_AGENT).read_text(encoding="utf-8").splitlines()
        self.assertIsNone(skill_evals_runs.set_reviewer_model(self.root, "haiku"))
        after = (self.root / skill_evals_runs.REVIEWER_AGENT).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [("model: inherit", "model: claude-haiku-5-5")],
            [pair for pair in zip(before, after, strict=True) if pair[0] != pair[1]],
        )
        self.assertIn(
            "has no frontmatter line `model: inherit`", skill_evals_runs.set_reviewer_model(self.root, "opus") or ""
        )

    def test_a_missing_reviewer_agent_is_a_reason(self) -> None:
        self.assertIn("has no frontmatter line", skill_evals_runs.set_reviewer_model(self.root, "haiku") or "")

    def test_the_session_gets_the_deployed_agent_with_its_hook_on_the_homes_guard(self) -> None:
        fake_deploy(self.root, self.root)
        self.assertIsNone(skill_evals_runs.set_reviewer_model(self.root, "haiku"))
        agents = skill_evals_runs.reviewer_agents(self.root)
        self.assertEqual(["code-review-reviewer"], list(agents))
        definition = agents["code-review-reviewer"]
        self.assertEqual({"description", "prompt", "tools", "model", "omitClaudeMd", "hooks"}, set(definition))
        self.assertTrue(definition["description"].startswith("Internal to the code-review skills. Started"))
        self.assertTrue(definition["prompt"].startswith("You run one role a code-review script prepared"))
        self.assertTrue(REVIEWER_AGENT_SOURCE.read_text(encoding="utf-8").rstrip("\n").endswith(definition["prompt"]))
        self.assertEqual(["Read", "Grep", "Glob", "Write", "Edit", "Bash"], definition["tools"])
        self.assertEqual("claude-haiku-5-5", definition["model"])
        self.assertIs(True, definition["omitClaudeMd"])
        self.assertEqual(["PreToolUse"], list(definition["hooks"]))
        [group] = definition["hooks"]["PreToolUse"]
        self.assertEqual("Read|Grep|Glob|Write|Edit|Bash", group["matcher"])
        [hook] = group["hooks"]
        self.assertEqual({"type", "command", "timeout", "onFailure"}, set(hook))
        self.assertEqual(("command", 30, "block"), (hook["type"], hook["timeout"], hook["onFailure"]))
        guard = (self.root / ".claude/skills/code-review-core/scripts/review_guard.py").as_posix()
        self.assertIn(f"runpy.run_path(os.path.expanduser('{guard}'),", hook["command"])
        self.assertNotIn("~/", hook["command"])
        self.assertTrue(
            hook["command"].startswith(
                f'{platform_support.PYTHON} -I -B -c "import json, os, runpy, sys; sys.excepthook ='
            )
        )

    def test_the_hook_command_runs_the_homes_guard_with_spaces_in_its_path(self) -> None:
        bash = platform_support.find_bash()
        self.assertIsNotNone(bash, "Git Bash is required")
        self.assertIn(" ", str(self.root))
        fake_deploy(self.root, self.root)
        hook = skill_evals_runs.reviewer_agents(self.root)["code-review-reviewer"]["hooks"]["PreToolUse"][0]["hooks"][0]
        ran = platform_support.run_tool([str(bash), "-c", hook["command"]])
        self.assertEqual(0, ran.returncode, ran.output)
        self.assertEqual(f"GUARD {(self.root / skill_evals_runs.GUARD).as_posix()}", ran.output.strip())

    def test_an_agent_the_definition_cannot_carry_is_refused(self) -> None:
        source = REVIEWER_AGENT_SOURCE.read_text(encoding="utf-8")
        profile_guard = "'~/.claude/skills/code-review-core/scripts/review_guard.py'"
        cases = {
            "the evaluation does not pass color with --agents": source.replace(
                "model: inherit", "model: inherit\ncolor: red"
            ),
            "it is named other, not code-review-reviewer": source.replace("name: code-review-reviewer", "name: other"),
            "its hook does not name ~/.claude/skills/code-review-core/scripts/review_guard.py once": source.replace(
                profile_guard, "'~/elsewhere.py'"
            ),
            "its hooks are not one PreToolUse command hook": source.replace("  PreToolUse:", "  PostToolUse:"),
            "its hooks name onFailure 0 times, not once": source.replace("          onFailure: block\n", ""),
            "its hook's onFailure is continue, not block": source.replace("onFailure: block", "onFailure: continue"),
            "its hooks name type 2 times, not once": source.replace(
                "          timeout: 30", "          timeout: 30\n        - type: command\n          command: echo"
            ),
            "it has no hooks": re.sub(r"hooks:\n(?: .*\n)+", "", source),
            "it has no frontmatter": "No frontmatter here.\n",
        }
        for number, (reason, text) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                home = self.root / str(number)
                fake_deploy(home, home)
                (home / skill_evals_runs.REVIEWER_AGENT).write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(skill_evals_runs.AgentError, re.escape(reason)):
                    skill_evals_runs.reviewer_agents(home)

    def test_a_home_without_the_guard_or_with_an_unquotable_path_is_refused(self) -> None:
        fake_deploy(self.root, self.root)
        (self.root / skill_evals_runs.GUARD).unlink()
        with self.assertRaisesRegex(skill_evals_runs.AgentError, "the home has no .claude/skills/code-review-core/"):
            skill_evals_runs.reviewer_agents(self.root)
        home = self.root / "it's"
        fake_deploy(home, home)
        with self.assertRaisesRegex(skill_evals_runs.AgentError, "its hook cannot quote the home's path"):
            skill_evals_runs.reviewer_agents(home)

    def test_a_record_a_reviewer_no_guard_held_is_told(self) -> None:
        def reviewed(*reviewers: Any) -> dict[str, Any]:
            value = record()
            value["review"]["reviewers"] = list(reviewers)
            return value

        held = {"id": "generic-review", "files_read": 0, "bytes_read": 0}
        self.assertIsNone(skill_evals_records.unguarded(reviewed(held, {**held, "id": "security", "files_read": 3})))
        self.assertEqual(
            "no reviewer guard held generic-review, security: its files_read is null",
            skill_evals_records.unguarded(
                reviewed({**held, "files_read": None}, {"id": "security"}, {**held, "id": "tests"})
            ),
        )
        self.assertEqual("the record names no reviewer", skill_evals_records.unguarded(reviewed()))
        self.assertEqual("the record names no reviewer", skill_evals_records.unguarded(record()))

    def test_stream_models_tells_the_session_from_its_subagents(self) -> None:
        output = "\n".join(
            [
                "not json",
                assistant("claude-opus-5-5"),
                assistant("claude-haiku-4-5-20251001", subagent=True),
                assistant("<synthetic>", subagent=True),
                json.dumps({"type": "user", "message": {"model": "claude-other"}}),
            ]
        )
        self.assertEqual(
            (frozenset({"claude-opus-5-5"}), frozenset({"claude-haiku-4-5-20251001"})),
            skill_evals_runs.stream_models(output),
        )

    def test_only_a_pull_requests_record_in_a_canary_root_is_the_runs(self) -> None:
        # A record anywhere else, or a file in the pull request's folder that is not one of its records, is none.
        (self.root / "elsewhere.json").write_text(json.dumps(record(version=1)), encoding="utf-8")
        with self.assertRaisesRegex(RecordError, "^the run recorded no review$"):
            skill_evals_runs.find_record(self.root, 1)
        pulls = self.root / "code-review-canary-abc" / "example" / "inventory" / "pulls" / "1"
        pulls.mkdir(parents=True)
        (pulls / "v1.json").write_text(json.dumps(record(version=1)), encoding="utf-8")
        with self.assertRaisesRegex(RecordError, "^the run recorded no review version 1; it holds no record$"):
            skill_evals_runs.find_record(self.root, 1)

    def test_why_a_run_counts_for_nothing(self) -> None:
        completed = runtime_canary.Completed
        reviewed = completed(0, f"{assistant('claude-opus-5-5')}\n{assistant('claude-haiku-5-5', subagent=True)}", "")
        self.assertIsNone(skill_evals_runs.run_failure("haiku", reviewed, 60, "review-prs", None))
        self.assertEqual(
            "reviewers ran on claude-haiku-5-5, not claude-sonnet-5-5",
            skill_evals_runs.run_failure("sonnet", reviewed, 60, "review-prs", None),
        )
        # Another generation of the family is another model, and an alias may resolve to it.
        older = completed(0, f"{assistant('claude-opus-5-5')}\n{assistant('claude-haiku-4-5-20251001', True)}", "")
        self.assertEqual(
            "reviewers ran on claude-haiku-4-5-20251001, not claude-haiku-5-5",
            skill_evals_runs.run_failure("haiku", older, 60, "review-prs", None),
        )
        elsewhere = completed(0, f"{assistant('claude-sonnet-5-5')}\n{assistant('claude-haiku-5-5', True)}", "")
        self.assertEqual(
            "the session ran on claude-sonnet-5-5, not claude-opus-5-5",
            skill_evals_runs.run_failure("haiku", elsewhere, 60, "review-prs", None),
        )
        self.assertEqual(
            "no reviewer subagent ran, so the record is not the model's",
            skill_evals_runs.run_failure(
                "haiku", completed(0, assistant("claude-opus-5-5"), ""), 60, "review-prs", None
            ),
        )
        init = json.dumps({"type": "system", "subtype": "init", "skills": ["other"]})
        denied = json.dumps(
            {"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm x"}}]}
        )
        missing = "the run recorded no review"
        self.assertEqual(
            f"{missing}; timed out after 60 seconds; Claude Code did not list review-prs; denied: Bash: rm x",
            skill_evals_runs.run_failure(
                "haiku", completed(-1, f"{init}\n{denied}", "", timed_out=True), 60, "review-prs", missing
            ),
        )
        self.assertEqual(
            f"{missing}; Claude Code exited with code 3",
            skill_evals_runs.run_failure("haiku", completed(3, "", ""), 60, "review-prs", missing),
        )
        unstarted = completed(-1, "", "", error="cannot run C:/bin/claude.exe: The system cannot find the file")
        self.assertEqual(
            f"{missing}; cannot run C:/bin/claude.exe: The system cannot find the file",
            skill_evals_runs.run_failure("haiku", unstarted, 60, "review-prs", missing),
        )

    def test_the_reason_the_results_file_records_names_no_path(self) -> None:
        completed = runtime_canary.Completed
        home = "D:/scratch/skill-evals-x1/haiku"
        command = f'python -B "{home}/.claude/skills/code-review-core/scripts/review_pipeline.py" prepare'
        denials = [{"tool_name": "Bash", "tool_input": {"command": command}}] * 2
        denied = json.dumps({"type": "result", "permission_denials": denials})
        missing = f"the run recorded no review version 1; its latest record cannot be read: {home}/x.json"
        cases = [
            (completed(1, denied, ""), "the run recorded no review; Claude Code exited with code 1; denied 2 commands"),
            (
                completed(-1, "", "", error=f"cannot run {home}/claude.exe: gone"),
                "the run recorded no review; Claude Code could not start",
            ),
            (completed(-1, "", "", timed_out=True), "the run recorded no review; timed out after 60 seconds"),
        ]
        for run, phrase in cases:
            with self.subTest(phrase=phrase):
                self.assertIn(home, skill_evals_runs.run_failure("haiku", run, 60, "review-prs", missing) or "")
                self.assertEqual(phrase, skill_evals_runs.run_failure("haiku", run, 60, "review-prs", missing, True))
        one = json.dumps({"type": "result", "permission_denials": denials[:1]})
        self.assertEqual(
            "the run recorded no review; denied 1 command",
            skill_evals_runs.run_failure("haiku", completed(0, one, ""), 60, "review-prs", missing, recorded=True),
        )
        reviewed = completed(0, f"{assistant('claude-opus-5-5')}\n{assistant('claude-haiku-5-5', subagent=True)}", "")
        self.assertIsNone(skill_evals_runs.run_failure("haiku", reviewed, 60, "review-prs", None, recorded=True))


def outcome(scenario: str, model: str, failure: str | None = None) -> Outcome:
    return Outcome(scenario, model, "verdict APPROVED", failure)


RESULTS_TEXT = "\n".join(
    [
        "# Skill evaluations",
        "",
        skill_evals_results.RESULTS_BEGIN,
        *skill_evals_results.RESULTS_HEADER,
        "| alpha | opus | claude-opus-5-5 | 1/1 | 1 | one 1/1 | claude-opus-5-5 | 2.1.0 | 2026-01-01 |",
        "| review-prs | opus | old | 0/1 | 1 | old | old | 2.0.0 | 2026-01-01 |",
        "| zeta | opus | claude-opus-5-5 | 1/1 | 1 | one 1/1 | claude-opus-5-5 | 2.1.0 | 2026-01-01 |",
        skill_evals_results.RESULTS_END,
        "",
        "Written by hand after the table.",
        "",
    ]
)


class CitationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        write_scenario(root, "clean", scenario_spec())
        write_scenario(root, "defects", scenario_spec({"kind": "verdict", "verdict": "CHANGES_REQUESTED"}))
        self.scenarios = skill_evals_scenarios.load_scenarios("demo", root=root)

    def test_the_block_cites_the_run_and_each_judgment_and_no_path_of_the_machine(self) -> None:
        held = contract_record(model="claude-sonnet-5-5")
        runs: dict[skill_evals_runs.RunKey, skill_evals_runs.Run] = {
            ("clean", "sonnet", None): skill_evals_runs.Run(held, None, frozenset({"claude-sonnet-5-5"}), frozenset()),
            ("defects", "sonnet", None): skill_evals_runs.Run(
                held, None, frozenset({"claude-sonnet-5-5"}), frozenset()
            ),
            ("clean", "haiku", None): skill_evals_runs.Run(
                None, "C:/home/x: timed out", frozenset(), frozenset(), "the run recorded no review", "run"
            ),
            ("defects", "haiku", None): skill_evals_runs.Run(
                None, "reviewers ran on claude-opus-5-5", frozenset({"claude-opus-5-5"}), frozenset(), "same", "record"
            ),
        }
        outcomes = [
            Outcome("clean", "sonnet", "verdict APPROVED", None),
            Outcome("clean", "haiku", "verdict APPROVED", "C:/home/x: timed out", "run"),
            Outcome("defects", "sonnet", "verdict CHANGES_REQUESTED", "verdict is ```APPROVED```", "judgment"),
            Outcome("defects", "haiku", "verdict CHANGES_REQUESTED", "reviewers ran on claude-opus-5-5", "record"),
        ]
        options = skill_evals.Options.parsed(
            skill_evals.parser().parse_args(
                ["demo", "--model", "sonnet", "--model", "haiku", "--model", "sonnet", "--timeout", "90", "--jobs", "5"]
            )
        )
        self.assertEqual(
            [
                "`python -B tools/skill_evals.py demo --model sonnet --model haiku --timeout 90`",
                "",
                "| Scenario | sonnet | haiku |",
                "| --- | --- | --- |",
                "| clean | 1/1 | 0/1 |",
                "| defects | 0/1 | 0/1 |",
                "",
                "````text",
                "RUNTIME claude 2.1.291",
                "REVIEWER sonnet claude-sonnet-5-5",
                "REVIEWER haiku claude-opus-5-5",
                "GUARDED clean sonnet generic files_read=0",
                "GUARDED defects sonnet generic files_read=0",
                'NOT_COUNTED clean haiku run "the run recorded no review"',
                'NOT_COUNTED defects haiku record "same"',
                'FAIL demo defects sonnet verdict CHANGES_REQUESTED judgment "verdict is ```APPROVED```"',
                "````",
            ],
            skill_evals.citation("demo", options, "2.1.291", self.scenarios, ["sonnet", "haiku"], runs, outcomes),
        )


class ResultsTests(unittest.TestCase):
    def test_rows_name_the_models_and_a_model_that_never_ran(self) -> None:
        scenarios = [
            Scenario(skill="review-prs", name=name, directory=Path(name), mode="initial", prior=None, expectations=())
            for name in ("a", "b")
        ]
        run = skill_evals_runs.Run
        session = frozenset({"claude-opus-5-5"})
        # The reason a run failed may name the machine's paths; the row takes the phrase recorded for it.
        failed = "the run recorded no review; denied: Bash: D:/scratch/home/script.py | here"
        runs: dict[skill_evals_runs.RunKey, skill_evals_runs.Run] = {
            ("a", "haiku", None): run({}, None, frozenset({"claude-haiku-4"}), session),
            ("b", "haiku", None): run(None, "x", frozenset({"claude-haiku-4"}), session, "x"),
            ("a", "sonnet", None): run(None, failed, frozenset(), frozenset(), "no such model | here"),
            ("b", "sonnet", None): run(None, "no such model", frozenset(), frozenset(), "no such model"),
        }
        outcomes = [
            outcome("a", "haiku"),
            outcome("b", "haiku", "x"),
            outcome("a", "sonnet", failed),
            outcome("b", "sonnet", "no such model"),
        ]
        rows = skill_evals_results.result_rows(
            "review-prs", scenarios, ["haiku", "sonnet"], outcomes, runs, "2.1.291", datetime.date(2026, 10, 8)
        )
        self.assertEqual(
            [
                "| review-prs | haiku | claude-haiku-4 | 1/2 | 1 | a 1/1, b 0/1 | claude-opus-5-5 | 2.1.291 "
                "| 2026-10-08 |",
                "| review-prs | sonnet | not run | 0/2 | 1 | no such model \\| here | unknown | 2.1.291 | 2026-10-08 |",
            ],
            rows,
        )

    def test_a_scenario_no_reviewer_ran_on_is_not_run_and_left_out_of_the_count(self) -> None:
        scenarios = [
            Scenario(
                skill="review-document", name=name, directory=Path(name), mode="initial", prior=None, expectations=()
            )
            for name in ("a", "b", "c")
        ]
        run = skill_evals_runs.Run
        session = frozenset({"claude-opus-5-5"})
        reviewer = frozenset({"claude-haiku-5-5"})
        runs: dict[skill_evals_runs.RunKey, skill_evals_runs.Run] = {
            ("a", "haiku", None): run({}, None, reviewer, session),
            # The session refused before any reviewer started, as an unsupported PDF parser makes it.
            ("b", "haiku", None): run(None, "the run recorded no review", frozenset(), session),
            # A reviewer ran and the run still recorded nothing: that is the model's run, and it counts.
            ("c", "haiku", None): run(None, "the run recorded no review; timed out", reviewer, session),
        }
        outcomes = [
            outcome("a", "haiku"),
            outcome("a", "haiku", "x"),
            outcome("b", "haiku", "the run recorded no review"),
            outcome("b", "haiku", "the run recorded no review"),
            outcome("c", "haiku", "the run recorded no review; timed out"),
        ]
        rows = skill_evals_results.result_rows(
            "review-document", scenarios, ["haiku"], outcomes, runs, "2.1.291", datetime.date(2026, 10, 8)
        )
        self.assertEqual(
            [
                "| review-document | haiku | claude-haiku-5-5 | 1/3 | 1 | a 1/2, b not run, c 0/1 | claude-opus-5-5 "
                "| 2.1.291 | 2026-10-08 |"
            ],
            rows,
        )

    def test_over_several_runs_each_scenario_counts_the_runs_that_passed_every_expectation(self) -> None:
        scenarios = [
            Scenario(skill="review-prs", name=name, directory=Path(name), mode="initial", prior=None, expectations=())
            for name in ("a", "b", "c", "d")
        ]
        run = skill_evals_runs.Run
        reviewer, session = frozenset({"claude-haiku-5-5"}), frozenset({"claude-opus-5-5"})
        unreviewed = run(None, "the run recorded no review", frozenset(), session, "the run recorded no review", "run")
        runs: dict[skill_evals_runs.RunKey, skill_evals_runs.Run] = {
            (name, "haiku", index): run({}, None, reviewer, session) for name in "abc" for index in (1, 2, 3)
        }
        # No reviewer ran on c's third run, nor on any of d's: they hold no judgment of the model.
        runs |= {("c", "haiku", 3): unreviewed, **{("d", "haiku", index): unreviewed for index in (1, 2, 3)}}
        failed = {("b", 2), ("c", 3), ("d", 1), ("d", 2), ("d", 3)}
        outcomes = [
            Outcome(name, "haiku", "verdict APPROVED", "x" if (name, index) in failed else None, None, index)
            for name in "abcd"
            for index in (1, 2, 3)
        ]
        rows = skill_evals_results.result_rows(
            "review-prs", scenarios, ["haiku"], outcomes, runs, "2.1.291", datetime.date(2026, 10, 8)
        )
        self.assertEqual(
            [
                "| review-prs | haiku | claude-haiku-5-5 | 7/8 | 3 | a 3/3 (3/3 runs), b 2/3 (2/3 runs), "
                "c 2/2 (2/2 runs), d not run | claude-opus-5-5 | 2.1.291 | 2026-10-08 |"
            ],
            rows,
        )

    def test_the_skills_rows_are_replaced_and_every_other_kept(self) -> None:
        rows = ["| review-prs | haiku | h | 1/1 | 1 | a 1/1 | s | 2.1.291 | 2026-10-08 |"]
        original = RESULTS_TEXT.split("\n")
        written = skill_evals_results.replace_results(RESULTS_TEXT, "review-prs", rows).split("\n")
        begin, end = written.index(skill_evals_results.RESULTS_BEGIN), written.index(skill_evals_results.RESULTS_END)
        self.assertEqual(
            [*skill_evals_results.RESULTS_HEADER, original[5], rows[0], original[7]], written[begin + 1 : end]
        )
        self.assertEqual(original[:begin], written[:begin])
        self.assertEqual(["", "Written by hand after the table.", ""], written[end + 1 :])

    def test_another_skills_row_and_its_order_are_kept_as_written(self) -> None:
        lines = RESULTS_TEXT.split("\n")
        # A row written by hand without the spaces, an aligned separator, and two rows of one skill out of order.
        lines[4] = "| :--- | --- | --- | ---: | --- | --- | --- | --- | --- |"
        lines[5] = "|alpha|sonnet|claude-sonnet-5-5|1/1|1|one 1/1|claude-opus-5-5|2.1.0|2026-01-01|"
        lines.insert(
            6, "| alpha | haiku | claude-haiku-5-5 | 1/1 | 1 | one 1/1 | claude-opus-5-5 | 2.1.0 | 2026-01-01 |"
        )
        text = "\n".join(lines)
        rows = ["| review-prs | haiku | h | 1/1 | 1 | a 1/1 | s | 2.1.291 | 2026-10-08 |"]
        written = skill_evals_results.replace_results(text, "review-prs", rows).split("\n")
        begin, end = written.index(skill_evals_results.RESULTS_BEGIN), written.index(skill_evals_results.RESULTS_END)
        self.assertEqual(
            [*skill_evals_results.RESULTS_HEADER, lines[5], lines[6], rows[0], lines[8]], written[begin + 1 : end]
        )

    def test_a_table_it_cannot_read_is_refused(self) -> None:
        empty = "\n".join([skill_evals_results.RESULTS_BEGIN, skill_evals_results.RESULTS_END])
        with self.assertRaisesRegex(ScenarioError, "must start with the header"):
            skill_evals_results.results_table(empty)

    def test_the_shipped_results_file_has_its_table(self) -> None:
        text = skill_evals.RESULTS.read_text(encoding="utf-8")
        begin, end, _ = skill_evals_results.results_table(text)
        lines = text.split("\n")
        self.assertEqual(list(skill_evals_results.RESULTS_HEADER), lines[begin + 1 : begin + 3])
        columns = len(skill_evals_results.RESULTS_HEADER[0].split("|"))
        for row in lines[begin + 3 : end]:
            self.assertEqual(columns, len(row.replace("\\|", "").split("|")), row)


def frontmatter_model(path: Path) -> str:
    line = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("model: "))
    return line.removeprefix("model: ")


class StubClaude:
    """Claude Code as a run starts it: `--version`, or a review through the pipeline in this process of the prompt's
    fixture, or of the fixture review-document's build step writes for the prompt's document, with stream-json naming
    the session's model and the reviewer model the --agents definition sets, and, unless `guarded` is false, each
    role's read log started as the reviewer guard's claim starts it. Each reviewer does what the scenario the run's
    folder is named for expects. `after`, if given, is called with the record JSON finalize wrote, as what happened to
    the run's canary root after it. `session` names the model the session's own messages report, and without
    `subagent` no subagent message is forwarded, as when the session works the roles itself. `vary`, if given, is
    called with the run's folder and the scenario, and its reviewers do what the scenario it returns expects instead,
    as a model that does the task on some runs and not on others."""

    def __init__(
        self,
        test: unittest.TestCase,
        *,
        review: bool = True,
        reviewer: str | None = None,
        guarded: bool = True,
        after: Callable[[Path], None] | None = None,
        session: str = "claude-opus-5-5",
        subagent: bool = True,
        vary: Callable[[Path, dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.test, self.review, self.reviewer, self.guarded, self.after = test, review, reviewer, guarded, after
        self.session, self.subagent, self.vary = session, subagent, vary
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []
        self.built: dict[str, list[str]] = {}
        # Each fixture run's fixture, by its scenario and model, and its files as the session found them.
        self.fixtures: dict[str, tuple[Path, dict[str, bytes]]] = {}

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Any:
        self.calls.append((arguments, cwd, environment))
        if arguments[1:] == ["--version"]:
            return runtime_canary.Completed(0, "2.1.291 (Claude Code)\n", "")
        model = reviewer_of(arguments)["model"]
        self.test.assertEqual(model, frontmatter_model(cwd / skill_evals_runs.REVIEWER_AGENT))
        if self.review:
            words = [quoted or bare for quoted, bare in re.findall(r'"([^"]*)"|(\S+)', arguments[2])]
            skill, prepare = words[0].removeprefix("/"), words[1:]
            run = Path(environment["TMPDIR"]).parent
            scenario = skill_evals_scenarios.SCENARIO_ROOT / skill / run.name / "scenario.json"
            if skill != "review-document":
                fixture = Path(prepare[prepare.index("--fixture") + 1])
                self.fixtures[f"{run.name} {model}"] = (fixture, tree_bytes(fixture))
                scenario = fixture / "scenario.json"
            spec = json.loads(scenario.read_text(encoding="utf-8"))
            spec = spec if self.vary is None else self.vary(run, spec)
            files = {key: environment[key] for key in ("CODE_REVIEW_STATE", "CODE_REVIEW_FLAGS")}
            with mock.patch.object(tempfile, "tempdir", environment["TMPDIR"]), mock.patch.dict(os.environ, files):
                if skill == "review-document":
                    # The document's path, and the stage a staged scenario names, as --stage <stage>.
                    stage = prepare[prepare.index("--stage") + 1] if "--stage" in prepare else None
                    built = self.built[f"{run.name} {model}"] = build_document(Path(prepare[0]), stage)
                    prepare = ["--canary", "--fixture", fixture_of(built)]
                written = Pipeline(self.test, Path(environment["CODE_REVIEW_CONFIG"])).review(
                    prepare, spec, self.guarded
                )
            if self.after is not None:
                self.after(written)
        reviewer = self.reviewer or model
        forwarded = [assistant(reviewer, subagent=True)] if self.subagent else []
        return runtime_canary.Completed(0, "\n".join([assistant(self.session), *forwarded]), "")


def forward(path: Path) -> str:
    return skill_evals_scenarios.quote(str(path).replace(os.sep, "/"))


class EvaluateTests(unittest.TestCase):
    """The run step with a stub Claude Code: no model is called."""

    def setUp(self) -> None:
        self.root = temporary_root(self)
        self.results = self.root / "skill-evaluations.md"
        self.results.write_text(RESULTS_TEXT, encoding="utf-8")
        self.homes = self.root / "homes"

    def make_home(self) -> Path:
        self.homes.mkdir()
        return self.homes

    def seams(self, runner: Any, **changes: Any) -> skill_evals.Seams:
        values: dict[str, Any] = {
            "runner": runner,
            "which": lambda name: f"{name}.exe",
            "deploy": fake_deploy,
            "environment": {"PATH": "kept"},
            "make_home": self.make_home,
            "today": lambda: datetime.date(2026, 10, 8),
            "results": self.results,
            **changes,
        }
        return skill_evals.Seams(**values)

    def evaluate(
        self, argv: Sequence[str], seams: skill_evals.Seams, skill: str = "review-prs"
    ) -> tuple[int, list[str]]:
        """The run's status and its output lines but the citation block, which `self.output` keeps with the rest and
        `self.cited` alone, between its BEGIN and END lines."""
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = skill_evals.main([skill, *argv], seams=seams)
        self.output = output.getvalue().splitlines()
        self.cited: list[str] = []
        if "CITATION BEGIN" not in self.output:
            return status, self.output
        begin, end = self.output.index("CITATION BEGIN"), self.output.index("CITATION END")
        self.cited = self.output[begin + 1 : end]
        return status, self.output[:begin] + self.output[end + 1 :]

    def test_every_scenario_runs_on_every_model_and_the_result_is_recorded(self) -> None:
        claude = StubClaude(self)
        status, lines = self.evaluate(["--write", "--jobs", "1"], self.seams(claude))
        self.assertEqual(0, status, lines)
        self.assertEqual(f"HOME {forward(self.homes)}", lines[0])
        source = runtime_canary.source_id(skill_evals_scenarios.REPOSITORY_ROOT)
        self.assertEqual([f"DEPLOYED {model} {source}" for model in skill_evals_records.MODELS], lines[1:4])
        self.assertEqual("RUNTIME claude 2.1.291", lines[4])
        ended = [line.split(" ")[1:3] for line in lines if line.startswith("TRANSCRIPT ")]
        scenarios = ["clean-change", "design-clean", "design-gaps", "planted-defects", "re-review"]
        self.assertEqual([[scenario, model] for scenario in scenarios for model in skill_evals_records.MODELS], ended)
        # A design scenario's manifest routes its one changed document to the suite's design specialist alone.
        role = {"design-clean": "design-review", "design-gaps": "design-review"}
        self.assertIn("REVIEWER haiku claude-haiku-5-5", lines)
        guarded = [line for line in lines if line.startswith("GUARDED ")]
        self.assertEqual(
            [
                f"GUARDED {scenario} {model} {role.get(scenario, 'generic-review')} files_read=0"
                for scenario in scenarios
                for model in skill_evals_records.MODELS
            ],
            guarded,
        )
        self.assertEqual([], [line for line in lines if line.startswith(("FAIL", "NOT_COUNTED"))])
        self.assertIn("PASS review-prs re-review haiku ledger addressed=1 still_present=2", lines)
        self.assertEqual(f"REMOVED {forward(self.homes)}", lines[-1])
        # The citation: the command, the table, and the runtime, reviewer, and guard lines in a text fence.
        reviewers = [line for line in lines if line.startswith("REVIEWER ")]
        self.assertEqual(
            [
                "`python -B tools/skill_evals.py review-prs --write`",
                "",
                "| Scenario | haiku | sonnet | opus |",
                "| --- | --- | --- | --- |",
                "| clean-change | 2/2 | 2/2 | 2/2 |",
                "| design-clean | 2/2 | 2/2 | 2/2 |",
                "| design-gaps | 5/5 | 5/5 | 5/5 |",
                "| planted-defects | 5/5 | 5/5 | 5/5 |",
                "| re-review | 7/7 | 7/7 | 7/7 |",
                "",
                "```text",
                "RUNTIME claude 2.1.291",
                *reviewers,
                *guarded,
                "```",
            ],
            self.cited,
        )
        self.assertEqual(3, len(reviewers))
        self.assertTrue(self.output[self.output.index("CITATION END") + 1].startswith("WROTE "))
        self.assertFalse(self.homes.exists())
        written = self.results.read_text(encoding="utf-8")
        self.assertIn(
            "| review-prs | haiku | claude-haiku-5-5 | 21/21 | 1 | clean-change 2/2, design-clean 2/2, "
            "design-gaps 5/5, planted-defects 5/5, re-review 7/7 | claude-opus-5-5 | 2.1.291 | 2026-10-08 |",
            written,
        )
        self.assertNotIn("| review-prs | opus | old |", written)
        for arguments, cwd, environment in claude.calls:
            if arguments[1:] != ["--version"]:
                self.assertEqual(self.homes, cwd.parent)
                self.assertEqual("kept", environment["PATH"])
                self.assertTrue(Path(environment["TMPDIR"]).is_relative_to(cwd / skill_evals_runs.STATE))
                agents = arguments[arguments.index("--agents") + 1]
                self.assertEqual(str(cwd / skill_evals_runs.STATE / "agents.json"), agents)

    def test_claude_code_sees_no_repository_selecting_variable_the_shell_exported(self) -> None:
        # Written out here rather than read from skill-core; a lower-case name is the same variable on Windows.
        selecting = {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"}
        exported = {name: "C:/elsewhere/.git" for name in selecting} | {"git_namespace": "elsewhere"}
        configuring = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "fixture.scope", "GIT_CONFIG_VALUE_0": "on"}
        claude = StubClaude(self)
        seams = self.seams(claude, environment={"PATH": "kept", **exported, **configuring})
        status, lines = self.evaluate(["--scenario", "clean-change", "--model", "haiku", "--jobs", "1"], seams)
        self.assertEqual(0, status, lines)
        started = [" ".join(arguments[1:2]) for arguments, _, _ in claude.calls]
        self.assertEqual(["--version", "-p"], started)
        for arguments, _, environment in claude.calls:
            with self.subTest(start=arguments[1]):
                names = {name.upper() for name in environment}
                self.assertEqual(set(), names & (selecting | {"GIT_NAMESPACE"}))
                self.assertLessEqual(set(configuring) | {"PATH"}, names)

    def test_a_document_scenario_runs_the_skill_on_its_staged_document(self) -> None:
        pdftotext_reads_pdf_gaps(self)
        claude = StubClaude(self)
        status, lines = self.evaluate(["--write", "--jobs", "1"], self.seams(claude), "review-document")
        self.assertEqual(0, status, lines)
        scenarios = {
            "committed-edit": "export-download-links.md",
            "design-clean": "export-download-links.md",
            "design-draft": "export-sharing.md",
            "design-final": "export-link-expiry.md",
            "design-gaps": "export-retention.md",
            "pdf-gaps": "export-retention.pdf",
            "word-gaps": "export-retention.docx",
        }
        # A scenario that names a stage starts the skill at it.
        stages = {"design-draft": ' --stage "draft"', "design-final": ' --stage "final"'}
        prompts = [arguments[2] for arguments, _, _ in claude.calls if arguments[1:] != ["--version"]]
        self.assertEqual(
            [
                f'/review-document "{self.homes / model / skill_evals_runs.STATE / scenario / "document" / name}"'
                + stages.get(scenario, "")
                for scenario, name in scenarios.items()
                for model in skill_evals_records.MODELS
            ],
            prompts,
        )
        # The edit is reviewed against its committed base, and a whole document against nothing.
        self.assertEqual(
            {
                f"{scenario} {model}": "committed" if scenario == "committed-edit" else "none"
                for scenario in scenarios
                for model in skill_evals_records.MODELS.values()
            },
            {key: built[1].split(" ")[1] for key, built in claude.built.items()},
        )
        guarded = [line for line in lines if line.startswith("GUARDED ")]
        self.assertEqual(
            [
                f"GUARDED {scenario} {model} design-review files_read=0"
                for scenario in scenarios
                for model in skill_evals_records.MODELS
            ],
            guarded,
        )
        self.assertEqual([], [line for line in lines if line.startswith("FAIL")])
        self.assertIn("PASS review-document committed-edit haiku only_at export-download-links.md:17", lines)
        written = self.results.read_text(encoding="utf-8")
        self.assertIn(
            "| review-document | haiku | claude-haiku-5-5 | 25/25 | 1 | committed-edit 3/3, design-clean 2/2, "
            "design-draft 3/3, design-final 2/2, design-gaps 5/5, pdf-gaps 5/5, word-gaps 5/5 | claude-opus-5-5 | "
            "2.1.291 | 2026-10-08 |",
            written,
        )
        self.assertIn("| review-prs | opus | old |", written)

    def test_repeated_scenarios_run_once_per_distinct_model_in_requested_order(self) -> None:
        claude = StubClaude(self)
        argv = [
            "--jobs",
            "1",
            "--model",
            "sonnet",
            "--model",
            "haiku",
            "--model",
            "sonnet",
            "--scenario",
            "planted-defects",
            "--scenario",
            "clean-change",
            "--scenario",
            "planted-defects",
            "--scenario",
            "clean-change",
        ]
        status, lines = self.evaluate(argv, self.seams(claude))
        self.assertEqual(0, status, lines)
        expected = [
            (scenario, model) for scenario in ("planted-defects", "clean-change") for model in ("sonnet", "haiku")
        ]
        calls = [
            (Path(environment["TMPDIR"]).parent.name, cwd.name)
            for arguments, cwd, environment in claude.calls
            if arguments[1:] != ["--version"]
        ]
        self.assertEqual(expected, calls)
        self.assertEqual(expected, [tuple(line.split(" ")[1:3]) for line in lines if line.startswith("TRANSCRIPT ")])
        self.assertEqual(14, sum(line.startswith("PASS ") for line in lines))
        self.assertEqual(4, sum(line.startswith("GUARDED ") for line in lines))
        self.assertEqual(
            [
                "| Scenario | sonnet | haiku |",
                "| --- | --- | --- |",
                "| planted-defects | 5/5 | 5/5 |",
                "| clean-change | 2/2 | 2/2 |",
            ],
            [line for line in self.cited if line.startswith("|")],
        )
        self.assertEqual(
            "`python -B tools/skill_evals.py review-prs --scenario planted-defects --scenario clean-change "
            "--model sonnet --model haiku`",
            self.cited[0],
        )
        self.assertFalse(self.homes.exists())
        self.assertEqual(RESULTS_TEXT, self.results.read_text(encoding="utf-8"))

    def test_a_document_that_cannot_be_staged_fails_its_run(self) -> None:
        def broken(_command: Sequence[str], _timeout: float) -> GitResult:
            return GitResult(128, "", "fatal: cannot create a repository here\n")

        claude = StubClaude(self)
        argv = ["--model", "haiku", "--scenario", "committed-edit"]
        status, lines = self.evaluate(argv, self.seams(claude, git=GitClient(runner=broken)), "review-document")
        self.assertEqual(1, status)
        reason = "the document could not be staged: fatal: cannot create a repository here"
        self.assertIn(f'NOT_COUNTED committed-edit haiku run "{reason}"', lines)
        self.assertIn(f'FAIL review-document committed-edit haiku verdict CHANGES_REQUESTED run "{reason}"', lines)
        # The citation names the run problem by its fixed phrase, without git's message.
        self.assertIn('NOT_COUNTED committed-edit haiku run "the document could not be staged"', self.cited)
        self.assertIn("REVIEWER haiku none", lines)
        self.assertEqual([["claude.exe", "--version"]], [arguments for arguments, _, _ in claude.calls])

    def test_a_run_whose_own_record_is_not_its_latest_at_its_version_fails_and_never_judges_another(self) -> None:
        # A re-review's canary root holds the prior record as version 1 before the run's review, so a finalize that
        # failed after committing it leaves the prior as the latest record, which no run may be judged by.
        def dropped(path: Path) -> None:
            path.unlink()
            path.with_suffix(".md").unlink()

        def unreadable(path: Path) -> None:
            path.write_text("{", encoding="utf-8")

        def followed(path: Path) -> None:
            shutil.copy(path, path.with_name("review-v2.json"))
            shutil.copy(path.with_suffix(".md"), path.with_name("review-v2.md"))

        def repeated(path: Path) -> None:
            root = path.parents[4]
            shutil.copytree(root, root.with_name(f"{root.name}-again"))

        no_review = "the run recorded no review version"
        cases = (
            ("re-review", dropped, f"{no_review} 2; its latest record is version 1"),
            ("re-review", unreadable, f"{no_review} 2; its latest record cannot be read: "),
            ("clean-change", unreadable, f"{no_review} 1; its latest record cannot be read: "),
            ("clean-change", followed, f"{no_review} 1; its latest record is version 2"),
            ("clean-change", repeated, "the run recorded 2 reviews at version 1, so which is its own is unknown"),
        )
        for index, (scenario, damage, reason) in enumerate(cases):
            with self.subTest(scenario=scenario, damage=damage.__name__):
                self.homes = self.root / f"homes-{index}"
                claude = StubClaude(self, after=damage)
                status, lines = self.evaluate(["--model", "haiku", "--scenario", scenario], self.seams(claude))
                self.assertEqual(1, status)
                failed = [line for line in lines if line.startswith("FAIL ")]
                self.assertTrue(failed, lines)
                for line in failed:
                    self.assertIn(f'"{reason}', line)
                self.assertFalse(any(line.startswith(("PASS", "GUARDED")) for line in lines))

    def test_a_record_no_guard_held_fails_the_run(self) -> None:
        claude = StubClaude(self, guarded=False)
        status, lines = self.evaluate(["--model", "haiku", "--scenario", "clean-change"], self.seams(claude))
        self.assertEqual(1, status)
        reason = "no reviewer guard held generic-review: its files_read is null"
        self.assertIn(f'FAIL review-prs clean-change haiku verdict APPROVED record "{reason}"', lines)
        self.assertFalse(any(line.startswith(("PASS", "GUARDED")) for line in lines))

    def test_an_agent_that_cannot_be_passed_stops_the_run(self) -> None:
        def no_guard(home: Path, source: Path) -> tuple[int, str]:
            fake_deploy(home, source)
            (home / skill_evals_runs.GUARD).unlink()
            return 0, ""

        claude = StubClaude(self)
        status, lines = self.evaluate([], self.seams(claude, deploy=no_guard))
        self.assertEqual(1, status)
        self.assertTrue(lines[-1].startswith('DEPLOY_FAILED haiku "'), lines)
        self.assertIn("cannot be passed with --agents: the home has no", lines[-1])
        self.assertEqual([], claude.calls)

    def test_a_run_without_a_record_fails_its_expectations_keeps_the_home_and_records_not_run(self) -> None:
        status, lines = self.evaluate(["--write", "--jobs", "2"], self.seams(StubClaude(self, review=False)))
        self.assertEqual(1, status)
        self.assertIn('FAIL review-prs clean-change haiku verdict APPROVED run "the run recorded no review"', lines)
        self.assertFalse(any(line.startswith(("PASS", "REMOVED")) for line in lines))
        # Each run that counts for nothing is told once before the outcomes, and the citation follows them.
        told = [line for line in lines if line.startswith("NOT_COUNTED ")]
        self.assertEqual(15, len(told))
        first = lines.index(told[0])
        self.assertEqual(told, lines[first : first + 15])
        self.assertTrue(lines[first + 15].startswith("FAIL "), lines[first + 15])
        self.assertEqual(told, [line for line in self.cited if line.startswith("NOT_COUNTED ")])
        self.assertTrue(self.output[self.output.index("CITATION BEGIN") - 1].startswith("FAIL "))
        self.assertTrue(self.output[self.output.index("CITATION END") + 1].startswith("WROTE "))
        self.assertTrue(self.homes.is_dir())
        self.assertIn("| review-prs | sonnet | claude-sonnet-5-5 | 0/21 |", self.results.read_text(encoding="utf-8"))

    def test_a_model_none_of_whose_reviewers_ran_is_recorded_as_not_run(self) -> None:
        status, lines = self.evaluate(["--write"], self.seams(StubClaude(self, review=False, reviewer="<synthetic>")))
        self.assertEqual(1, status)
        self.assertIn("REVIEWER haiku none", lines)
        self.assertIn(
            "| review-prs | haiku | not run | 0/21 | 1 | the run recorded no review |",
            self.results.read_text(encoding="utf-8"),
        )

    def test_a_model_that_did_not_run_is_recorded_without_the_machine_s_paths(self) -> None:
        def denied(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Any:
            if arguments[1:] == ["--version"]:
                return runtime_canary.Completed(0, "2.1.291 (Claude Code)\n", "")
            command = f'python -B "{cwd}/.claude/skills/code-review-core/scripts/review_pipeline.py" prepare'
            refused = {
                "type": "result",
                "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": command}}],
            }
            return runtime_canary.Completed(1, f"{assistant('claude-opus-5-5')}\n{json.dumps(refused)}", "")

        status, lines = self.evaluate(["--write"], self.seams(denied))
        self.assertEqual(1, status)
        home = str(self.homes / "haiku")
        self.assertTrue(
            any(
                line.startswith("FAIL review-prs clean-change haiku")
                and "denied: Bash" in line
                and json.dumps(home)[1:-1] in line
                for line in lines
            ),
            lines,
        )
        results = self.results.read_text(encoding="utf-8")
        self.assertIn(
            "| review-prs | haiku | not run | 0/21 | 1 | the run recorded no review; Claude Code exited with code 1; "
            "denied 1 command |",
            results,
        )
        self.assertNotIn(str(self.root), results)
        self.assertNotIn(self.root.as_posix(), results)
        # The citation names the run problem by the phrases the results file records, and no path of the machine.
        self.assertIn(
            'NOT_COUNTED clean-change haiku run "the run recorded no review; Claude Code exited with code 1; '
            'denied 1 command"',
            self.cited,
        )
        cited = "\n".join(self.cited)
        for path in (str(self.root), self.root.as_posix(), json.dumps(str(self.root))[1:-1]):
            self.assertNotIn(path, cited)

    def test_a_session_ends_at_its_limit_with_everything_it_started(self) -> None:
        # Claude Code started through a launcher, as npm's claude.cmd starts node, whose child holds its output.
        started = self.root / "child.pid"
        child = self.root / "child.py"
        child.write_text(
            f"import os, pathlib, time\npathlib.Path({str(started)!r}).write_text(str(os.getpid()))\ntime.sleep(60)\n",
            encoding="utf-8",
        )
        launcher = self.root / "launcher.py"
        launcher.write_text(
            f"import subprocess, sys\nsubprocess.run([sys.executable, {str(child)!r}])\n", encoding="utf-8"
        )
        began = time.monotonic()
        completed = skill_evals.Seams().runner([sys.executable, "-B", str(launcher)], self.root, dict(os.environ), 5)
        self.assertLess(time.monotonic() - began, 30, "the session's limit waited for the child it started")
        self.assertTrue(completed.timed_out, completed)
        self.assertTrue(started.is_file(), "the launcher's child never started within the limit")
        pid = int(started.read_text(encoding="utf-8"))
        self.assertFalse(platform_support.process_status(pid).alive, "the session's child outlived its limit")

    def test_a_reviewer_on_another_model_fails_the_run(self) -> None:
        claude = StubClaude(self, reviewer="claude-opus-5-5")
        status, lines = self.evaluate(["--model", "haiku", "--scenario", "clean-change"], self.seams(claude))
        self.assertEqual(1, status)
        reason = "reviewers ran on claude-opus-5-5, not claude-haiku-5-5"
        self.assertIn(f'FAIL review-prs clean-change haiku verdict APPROVED record "{reason}"', lines)

    def test_a_record_that_does_not_show_the_model_judged_is_told_as_a_record(self) -> None:
        cases = {
            "no reviewer subagent ran, so the record is not the model's": StubClaude(self, subagent=False),
            "reviewers ran on claude-opus-5-5, not claude-haiku-5-5": StubClaude(self, reviewer="claude-opus-5-5"),
            "the session ran on claude-sonnet-5-5, not claude-opus-5-5": StubClaude(self, session="claude-sonnet-5-5"),
            "no reviewer guard held generic-review: its files_read is null": StubClaude(self, guarded=False),
        }
        for index, (reason, claude) in enumerate(cases.items()):
            with self.subTest(reason=reason):
                self.homes = self.root / f"homes-{index}"
                status, lines = self.evaluate(["--model", "haiku", "--scenario", "clean-change"], self.seams(claude))
                self.assertEqual(1, status)
                told = f'NOT_COUNTED clean-change haiku record "{reason}"'
                self.assertEqual([told], [line for line in lines if line.startswith("NOT_COUNTED ")])
                self.assertIn(f'FAIL review-prs clean-change haiku verdict APPROVED record "{reason}"', lines)
                self.assertIn(told, self.cited)
                self.assertFalse(any(line.startswith("FAIL ") for line in self.cited), self.cited)

    def test_claude_code_is_looked_up_where_check_looks_for_it(self) -> None:
        # skill-core's locator through the deployer, so a Claude Code only in WinGet's Links folder runs too.
        self.assertIs(platform_support.find_executable, skill_evals.Seams().which)

    def test_nothing_runs_without_claude_code_or_the_results_table(self) -> None:
        no_table = self.root / "empty.md"
        no_table.write_text("# Skill evaluations\n", encoding="utf-8")
        stray = self.root / "stray.md"
        stray.write_text(RESULTS_TEXT.replace("| zeta |", "a hand note\n| zeta |"), encoding="utf-8")
        cases: dict[str, tuple[list[str], dict[str, Any]]] = {
            "Claude Code (claude) is not installed": ([], {"which": lambda _: None}),
            "needs one": (["--write"], {"results": no_table}),
            "is not a row": (["--write"], {"results": stray}),
        }
        for reason, (argv, changes) in cases.items():
            with self.subTest(reason=reason):
                claude = StubClaude(self)
                status, lines = self.evaluate(argv, self.seams(claude, **changes))
                self.assertEqual(1, status)
                self.assertEqual(1, len(lines), lines)
                self.assertTrue(lines[0].startswith("FAILED ") and reason in lines[0], lines)
                self.assertEqual([], claude.calls)
                self.assertFalse(self.homes.exists())

    def test_a_failed_deployment_stops_the_run(self) -> None:
        claude = StubClaude(self)
        status, lines = self.evaluate([], self.seams(claude, deploy=lambda *_: (1, "broken\n")))
        self.assertEqual(1, status)
        self.assertTrue(lines[-1].startswith('DEPLOY_FAILED haiku "the deployment failed; its log is '), lines)
        log = self.homes / "haiku" / skill_evals_runs.STATE / "deploy.log"
        self.assertEqual("broken\n", log.read_text(encoding="utf-8"))
        self.assertEqual([], claude.calls)

    def test_a_repository_reviewer_s_scenarios_run_through_review_prs_on_copies_holding_the_checkout_s_files(
        self,
    ) -> None:
        copies = self.root / "copies"
        copies.mkdir()
        before = tree_bytes(REVIEW_REPOSITORY)
        claude = StubClaude(self)
        with mock.patch.object(tempfile, "tempdir", str(copies)):
            status, lines = self.evaluate(["--write", "--jobs", "1"], self.seams(claude), "review-repository")
        self.assertEqual(0, status, lines)
        runs = [(scenario, model) for scenario in REVIEW_REPOSITORY_SCENARIOS for model in skill_evals_records.MODELS]
        prompts = [arguments[2] for arguments, _, _ in claude.calls if arguments[1:] != ["--version"]]
        fixtures = [claude.fixtures[f"{scenario} {skill_evals_records.MODELS[model]}"][0] for scenario, model in runs]
        self.assertEqual([f'/review-prs --canary --fixture "{fixture}"' for fixture in fixtures], prompts)
        self.assertEqual([(scenario, copies) for scenario, _ in runs], [(f.name, f.parent.parent) for f in fixtures])
        for key, (_, files) in claude.fixtures.items():
            scenario = key.split(" ")[0]
            for tree in ("base", "head"):
                for path in REVIEW_REPOSITORY_PLACED:
                    with self.subTest(run=key, tree=tree, path=path):
                        self.assertEqual(
                            (skill_evals_scenarios.REPOSITORY_ROOT / path).read_bytes(), files[f"{tree}/{path}"]
                        )
            own = {
                path[len(scenario) + 1 :].removesuffix(skill_evals_scenarios.HELD): content
                for path, content in before.items()
                if path.startswith(f"{scenario}/")
            }
            self.assertEqual(own, {path: files[path] for path in own})
            self.assertEqual([], [path for path in files if path.endswith(skill_evals_scenarios.HELD)])
        self.assertEqual([], list(copies.iterdir()), "every copy is removed")
        self.assertEqual(before, tree_bytes(REVIEW_REPOSITORY), "the fixtures are untouched")
        # Each route's condition opens on the copy as on the fixture, with the reviewer's files beside it, and each
        # route's scenario starts its specialist on the held files the copy restores.
        roles = {
            "branch-name-command": "skill-scripts,trust-boundary",
            "build-grant": "documentation-drift,generic-review,skill-contract,skill-scripts,upgrade-notes",
            "drifted-guide": "documentation-drift,generic-review",
            "misstated-exit-status": "documentation",
            "tested-change": "generic-review",
            "text-read-policy": "validation-policy",
            "undocumented-change": "generic-review",
            "unnoted-flag-rename": "deployer,upgrade-notes",
            "untested-change": "generic-review,untested-changes",
            "write-before-refusal": "deployer",
        }
        guarded: dict[str, set[str]] = {}
        for line in lines:
            if line.startswith("GUARDED "):
                _, scenario, model, role, _ = line.split(" ")
                guarded.setdefault(f"{scenario} {model}", set()).add(role)
        self.assertEqual(
            {f"{scenario} {model}": roles[scenario] for scenario, model in runs},
            {key: ",".join(sorted(value)) for key, value in guarded.items()},
        )
        self.assertEqual([], [line for line in lines if line.startswith("FAIL")])
        self.assertIn(
            "PASS review-repository untested-change haiku finding tools/report_age.sh:7,8,9,10 "
            "SHOULD_FIX Test coverage",
            lines,
        )
        self.assertIn(
            "PASS review-repository write-before-refusal haiku finding deployer/install.py:27,28,29,30,31 "
            "MUST_FIX Unheld invariant",
            lines,
        )
        written = self.results.read_text(encoding="utf-8")
        for model, identifier in skill_evals_records.MODELS.items():
            self.assertIn(
                f"| review-repository | {model} | {identifier} | 11/11 | 1 | branch-name-command 1/1, build-grant 1/1, "
                "drifted-guide 2/2, misstated-exit-status 1/1, tested-change 1/1, text-read-policy 1/1, "
                "undocumented-change 1/1, unnoted-flag-rename 1/1, untested-change 1/1, write-before-refusal 1/1 "
                "| claude-opus-5-5 | 2.1.291 | 2026-10-08 |",
                written,
            )
        self.assertIn("| review-prs | opus | old |", written)

    def test_a_repository_reviewer_s_copy_is_removed_when_its_run_fails(self) -> None:
        copies = self.root / "copies"
        copies.mkdir()
        argv = ["--model", "haiku", "--scenario", "untested-change"]

        def broken(arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Any:
            if arguments[1:] == ["--version"]:
                return runtime_canary.Completed(0, "2.1.291 (Claude Code)\n", "")
            self.assertEqual(1, len(list(copies.iterdir())))
            raise RuntimeError("the session broke")

        with mock.patch.object(tempfile, "tempdir", str(copies)):
            status, lines = self.evaluate(argv, self.seams(StubClaude(self, review=False)), "review-repository")
            self.assertEqual(1, status)
            self.assertIn(
                "FAIL review-repository untested-change haiku finding tools/report_age.sh:7,8,9,10 SHOULD_FIX "
                'Test coverage run "the run recorded no review"',
                lines,
            )
            self.assertEqual([], list(copies.iterdir()))
        fsops.remove(self.homes)
        with mock.patch.object(tempfile, "tempdir", str(copies)), self.assertRaisesRegex(RuntimeError, "broke"):
            self.evaluate(argv, self.seams(broken), "review-repository")
        self.assertEqual([], list(copies.iterdir()))

    def test_a_scenario_that_passes_two_of_three_runs_is_reported_as_such(self) -> None:
        def second_run_misses_the_must_fix(run: Path, spec: dict[str, Any]) -> dict[str, Any]:
            if run.parent.name != "2":
                return spec
            kept = [item for item in spec["expectations"] if item.get("severity") != "MUST_FIX"]
            return {**spec, "expectations": kept}

        claude = StubClaude(self, vary=second_run_misses_the_must_fix)
        argv = ["--scenario", "planted-defects", "--model", "haiku", "--runs", "3", "--jobs", "1"]
        status, lines = self.evaluate(argv, self.seams(claude))
        self.assertEqual(1, status, lines)
        ended = [line.split(" ")[1:4] for line in lines if line.startswith("TRANSCRIPT ")]
        self.assertEqual([["planted-defects", "haiku", f"run={index}"] for index in (1, 2, 3)], ended)
        transcript = self.homes / "haiku" / skill_evals_runs.STATE / "2" / "planted-defects" / "transcript.jsonl"
        self.assertIn(f"TRANSCRIPT planted-defects haiku run=2 {forward(transcript)}", lines)
        self.assertEqual(
            [f"GUARDED planted-defects haiku run={index} generic-review files_read=0" for index in (1, 2, 3)],
            [line for line in lines if line.startswith("GUARDED ")],
        )
        failed = [line for line in lines if line.startswith("FAIL ")]
        self.assertEqual(
            [
                "FAIL review-prs planted-defects haiku run=2 finding store.go:44,45 MUST_FIX judgment",
                "FAIL review-prs planted-defects haiku run=2 verdict CHANGES_REQUESTED judgment",
            ],
            [line.split(' "')[0] for line in failed],
        )
        self.assertEqual(13, sum(1 for line in lines if line.startswith("PASS review-prs planted-defects haiku run=")))
        self.assertIn("RUNS planted-defects haiku 2/3 some", lines)
        self.assertEqual(
            [
                "`python -B tools/skill_evals.py review-prs --scenario planted-defects --model haiku --runs 3`",
                "",
                "| Scenario | haiku |",
                "| --- | --- |",
                "| planted-defects | 13/15 (2/3 runs) |",
            ],
            self.cited[:5],
        )
        self.assertEqual(
            [line.removeprefix("FAIL ") for line in failed],
            [line.removeprefix("FAIL ") for line in self.cited if line.startswith("FAIL ")],
        )
        self.assertTrue(self.homes.exists())

    def test_a_trigger_scenario_judges_whether_each_request_started_the_skill_and_records_it(self) -> None:
        scenario = skill_evals_scenarios.load_scenarios("analyze-skill-cost", ["description"])[0]
        first, second, third = scenario.requests
        near, other = scenario.near_misses
        # The third request starts nothing, and the second near miss starts the skill: two judgments fail.
        claude = StubTrigger(
            self,
            {
                first: "analyze-skill-cost",
                second: "/analyze-skill-cost",
                near: "audit-ai-config",
                other: "analyze-skill-cost",
            },
        )
        status, lines = self.evaluate(["--write", "--jobs", "1"], self.seams(claude), "analyze-skill-cost")
        self.assertEqual(1, status, lines)
        ended = [line.split(" ")[1:3] for line in lines if line.startswith("TRANSCRIPT ")]
        self.assertEqual([["description", model] for model in skill_evals_records.MODELS for _ in range(5)], ended)
        self.assertEqual(
            [f"REVIEWER {model} {model_id}" for model, model_id in skill_evals_records.MODELS.items()],
            [line for line in lines if line.startswith("REVIEWER ")],
        )
        self.assertEqual([], [line for line in lines if line.startswith(("GUARDED ", "NOT_COUNTED "))])
        haiku = [line for line in lines if line.startswith(("PASS analyze-skill-cost description haiku", "FAIL "))]
        self.assertEqual(
            [
                f"PASS analyze-skill-cost description haiku starts {json.dumps(first)}",
                f"PASS analyze-skill-cost description haiku starts {json.dumps(second)}",
                f'FAIL analyze-skill-cost description haiku starts {json.dumps(third)} judgment "the session started '
                'no skill"',
                f"PASS analyze-skill-cost description haiku does_not_start {json.dumps(near)}",
                f"FAIL analyze-skill-cost description haiku does_not_start {json.dumps(other)} judgment "
                '"the session started analyze-skill-cost"',
            ],
            haiku[:5],
        )
        self.assertIn("| description | 3/5 | 3/5 | 3/5 |", self.cited)
        record = json.loads(
            (self.homes / "haiku" / skill_evals_runs.STATE / "description" / "trigger.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {
                "models": ["claude-haiku-5-5"],
                "requests": [
                    {"request": first, "started": ["analyze-skill-cost"]},
                    {"request": second, "started": ["analyze-skill-cost"]},
                    {"request": third, "started": []},
                    {"request": near, "started": ["audit-ai-config"]},
                    {"request": other, "started": ["analyze-skill-cost"]},
                ],
            },
            record,
        )
        self.assertIn(
            "| analyze-skill-cost | haiku | claude-haiku-5-5 | 3/5 | 1 | description 3/5 | claude-haiku-5-5 | 2.1.291 "
            "| 2026-10-08 |",
            self.results.read_text(encoding="utf-8"),
        )
        for arguments, cwd, _ in claude.calls[1:]:
            self.assertEqual(skill_evals_runs.trigger_command("claude.exe", arguments[2], cwd.name), arguments)

    def test_a_trigger_session_that_does_not_count_makes_its_run_count_for_nothing(self) -> None:
        cases = {
            "unlisted": (
                StubTrigger(self, {}, listed=False),
                "run",
                "request 1: Claude Code did not list analyze-skill-cost",
            ),
            "other model": (
                StubTrigger(self, {}, session="claude-sonnet-5-5"),
                "record",
                "request 1: the session ran on claude-sonnet-5-5, not claude-haiku-5-5",
            ),
        }
        for name, (claude, kind, reason) in cases.items():
            with self.subTest(name):
                shutil.rmtree(self.homes, ignore_errors=True)
                argv = ["--model", "haiku", "--jobs", "1"]
                status, lines = self.evaluate(argv, self.seams(claude), "analyze-skill-cost")
                self.assertEqual(1, status, lines)
                self.assertIn(f"NOT_COUNTED description haiku {kind} {json.dumps(reason)}", lines)
                failed = [line for line in lines if line.startswith("FAIL ")]
                self.assertEqual(5, len(failed))
                self.assertTrue(all(line.endswith(f"{kind} {json.dumps(reason)}") for line in failed), failed)

    def test_write_records_only_a_full_run(self) -> None:
        for argv in (["--model", "haiku"], ["--scenario", "clean-change"], ["--records", str(self.root)]):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                skill_evals.main(["review-prs", "--write", *argv], seams=self.seams(StubClaude(self)))
            self.assertEqual(2, raised.exception.code)


@pytest.mark.parametrize(
    "name",
    [".", "..", "demo/one", "demo\\one", "Demo", ""],
    ids=["dot", "dot dot", "a slash", "a backslash", "capitals", "empty"],
)
def test_a_skill_is_named_by_its_name_alone(name: str, tmp_path: Path) -> None:
    write_scenario(tmp_path, "one", scenario_spec())
    with contextlib.redirect_stderr(io.StringIO()) as error, pytest.raises(SystemExit) as raised:
        skill_evals.main([name, "--records", str(tmp_path / "records")], tmp_path)
    assert raised.value.code == 2
    assert "must be a skill's name" in error.getvalue()


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        pytest.param({"kind": "verdict", "verdict": "LGTM"}, "verdict must be one of", id="unknown verdict"),
        pytest.param(
            {"kind": "finding", "path": "a.go", "lines": [1], "severity": "BLOCKER"},
            "severity must be one of",
            id="unknown severity",
        ),
        pytest.param(
            {"kind": "finding", "path": "../a.go", "lines": [1], "severity": "MUST_FIX"},
            "plain relative path",
            id="a parent path",
        ),
        pytest.param(
            {"kind": "finding", "path": "a\\b.go", "lines": [1], "severity": "MUST_FIX"},
            "plain relative path",
            id="a backslash path",
        ),
        pytest.param(
            {"kind": "finding", "path": "a.go", "lines": [], "severity": "MUST_FIX"},
            "distinct positive line",
            id="no lines",
        ),
        pytest.param(
            {"kind": "finding", "path": "a.go", "lines": [2, 2], "severity": "MUST_FIX"},
            "distinct positive line",
            id="a repeated line",
        ),
        pytest.param(
            {"kind": "finding", "path": "a.go", "lines": [0], "severity": "MUST_FIX"},
            "distinct positive line",
            id="line 0",
        ),
        pytest.param(
            {"kind": "no_finding_above", "severity": "SUGGESTION", "paths": []},
            "paths must be a list",
            id="no paths",
        ),
        pytest.param(
            {"kind": "finding", "path": "a.go", "lines": [1], "severity": "MUST_FIX", "category": " "},
            "category must be a category name",
            id="a blank category",
        ),
    ],
)
def test_values_are_checked(spec: dict[str, Any], message: str) -> None:
    assert message in refusal(spec, "initial")


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        pytest.param(
            {"kind": "ledger", "addressed": -1, "still_present": 0}, "addressed must be a count", id="a negative count"
        ),
        pytest.param(
            {"kind": "ledger", "addressed": True, "still_present": 0}, "addressed must be a count", id="a boolean count"
        ),
        pytest.param(
            {"kind": "disposition", "entry": "F001", "disposition": "addressed"},
            "entry must look like v1:F001",
            id="an entry without its version",
        ),
        pytest.param(
            {"kind": "disposition", "entry": "v1:F001", "disposition": "fixed"},
            "disposition must be one of",
            id="an unknown disposition",
        ),
        pytest.param(
            {"kind": "repeats", "path": "a.go", "lines": [1], "entry": "v0:F001"},
            "entry must look like v1:F001",
            id="version 0",
        ),
    ],
)
def test_re_review_values_are_checked(spec: dict[str, Any], message: str) -> None:
    assert message in refusal(spec, "re-review")


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "ledger", "addressed": 1, "still_present": 0},
        {"kind": "disposition", "entry": "v1:F001", "disposition": "addressed"},
        {"kind": "repeats", "path": "a.go", "lines": [1], "entry": "v1:F001"},
    ],
    ids=["ledger", "disposition", "repeats"],
)
def test_re_review_kinds_need_a_re_review(spec: dict[str, Any]) -> None:
    assert "needs a re-review" in refusal(spec, "initial")


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        pytest.param(
            {"document": "../../../elsewhere.md"},
            "document ../../../elsewhere.md is outside the scenarios",
            id="outside the scenarios",
        ),
        pytest.param({"document": "missing.md"}, "document missing.md is missing", id="a missing document"),
        pytest.param({"document": "draft.md", "base": "gone.md"}, "base gone.md is missing", id="a missing base"),
        pytest.param({"document": "/draft.md"}, "document must be a relative path", id="a rooted path"),
        pytest.param({"document": "C:/draft.md"}, "document must be a relative path", id="a drive path"),
        pytest.param({"document": "sub\\draft.md"}, "document must be a relative path", id="a backslash path"),
        pytest.param({"document": 3}, "document must be a relative path", id="a number"),
        pytest.param({"document": ""}, "document must be a relative path", id="empty"),
        pytest.param({"base": "draft.md"}, "must have exactly", id="a base without a document"),
        pytest.param(
            {"document": "draft.md", "mode": "re-review", "prior": "prior.json"},
            "a document scenario is an initial review",
            id="a re-review",
        ),
    ],
)
def test_a_document_is_a_file_among_the_scenarios_and_reviewed_once(
    fields: dict[str, Any], message: str, tmp_path: Path
) -> None:
    directory = write_scenario(tmp_path, "case", scenario_spec(**fields))
    (directory / "draft.md").write_text("# Draft\n", encoding="utf-8")
    (directory / "prior.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ScenarioError) as raised:
        skill_evals_scenarios.load_scenarios("demo", ["case"], root=tmp_path)
    assert message in str(raised.value)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("claude-haiku-5-5", True),
        ("claude-haiku-5-5-20261001", True),
        ("claude-haiku-4-5-20251001", False),
        ("claude-haiku-5", False),
        ("claude-haiku-5-5-fast", False),
        ("claude-haiku-5-55", False),
    ],
)
def test_a_model_is_named_by_its_id_with_or_without_a_dated_suffix(reported: str, expected: bool) -> None:
    assert expected is skill_evals_records.names_model(reported, "claude-haiku-5-5"), reported


RESULTS_LINES = RESULTS_TEXT.split("\n")


@pytest.mark.parametrize(
    ("reason", "index", "line"),
    [
        (
            "must start with the header",
            3,
            "| Skill | Model | Model ID | Passed | By scenario | Session model | Claude Code | Date | Note |",
        ),
        ("must be followed by its separator", 4, "| --- | --- |"),
        ("line 6 of the results file is not a row", 5, ""),
        ("line 7 of the results file is not a row", 6, "a hand note"),
        ("line 8 of the results file is not a row", 7, f"{RESULTS_LINES[7]} n |"),
        (
            "line 6 of the results file is not a row of the table's 9 columns naming a skill",
            5,
            RESULTS_LINES[5].replace("alpha", " "),
        ),
    ],
    ids=["an old header", "no separator", "an empty line", "a hand note", "an extra column", "no skill"],
)
def test_a_table_it_cannot_read_is_refused(reason: str, index: int, line: str) -> None:
    text = "\n".join([*RESULTS_LINES[:index], line, *RESULTS_LINES[index + 1 :]])
    with pytest.raises(ScenarioError, match=reason):
        skill_evals_results.results_table(text)
    with pytest.raises(ScenarioError, match=reason):
        skill_evals_results.replace_results(text, "review-prs", [])


@pytest.mark.parametrize(
    "text",
    ["no markers", f"{skill_evals_results.RESULTS_END}\n{skill_evals_results.RESULTS_BEGIN}", RESULTS_TEXT * 2],
    ids=["no markers", "out of order", "twice each"],
)
def test_the_markers_are_required_once_each_in_order(text: str) -> None:
    with pytest.raises(ScenarioError):
        skill_evals_results.results_table(text)


class StubTrigger:
    """Claude Code as a trigger run starts it: `--version`, or one request's session, whose init event lists the
    skills a deployment of the worked example's two skills would, without the started skill when `listed` is false,
    and whose one message, on the model given or `session`, calls the Skill tool on the skill `starts` maps its
    request to, if any."""

    def __init__(
        self, test: unittest.TestCase, starts: dict[str, str], listed: bool = True, session: str | None = None
    ) -> None:
        self.test, self.starts, self.listed, self.session = test, starts, listed, session
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Any:
        self.calls.append((arguments, cwd, environment))
        if arguments[1:] == ["--version"]:
            return runtime_canary.Completed(0, "2.1.291 (Claude Code)\n", "")
        self.test.assertTrue(Path(environment["TMPDIR"]).is_relative_to(cwd / skill_evals_runs.STATE))
        request, model = arguments[2], arguments[arguments.index("--model") + 1]
        skills = ["audit-ai-config", *(["analyze-skill-cost"] if self.listed else [])]
        content: list[dict[str, Any]] = [{"type": "text", "text": "On it."}]
        if request in self.starts:
            content.append(
                {"type": "tool_use", "id": "toolu_1", "name": "Skill", "input": {"skill": self.starts[request]}}
            )
        events = [
            {"type": "system", "subtype": "init", "skills": skills},
            {
                "type": "assistant",
                "message": {"model": self.session or model, "content": content},
                "parent_tool_use_id": None,
            },
        ]
        return runtime_canary.Completed(0, "\n".join(json.dumps(event) for event in events), "")


def trigger_scenario(root: Path, spec: dict[str, Any], checkout: Path) -> Scenario:
    folder = root / "demo" / "triggers"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "scenario.json").write_text(json.dumps(spec), encoding="utf-8")
    return skill_evals_scenarios.load_scenario("demo", folder, checkout)


def demo_checkout(root: Path, frontmatter: str = "") -> Path:
    """A checkout holding one shipped skill, demo, with the frontmatter lines given."""
    skill = root / "checkout" / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"---\nname: demo\ndescription: A demo.\n{frontmatter}---\n\nBody.\n", encoding="utf-8")
    return root / "checkout"


TRIGGER_SPEC = {"skill": "demo", "mode": "trigger", "requests": ["Run the demo."], "near_misses": ["Run a test."]}


def test_a_trigger_scenario_expects_each_request_to_start_its_skill_and_each_near_miss_not_to(tmp_path: Path) -> None:
    spec = {**TRIGGER_SPEC, "requests": ["Run the demo.", "Show the demo."]}
    scenario = trigger_scenario(tmp_path, spec, demo_checkout(tmp_path))
    assert scenario.mode == skill_evals_scenarios.TRIGGER
    assert scenario.requests == ("Run the demo.", "Show the demo.")
    assert scenario.near_misses == ("Run a test.",)
    assert [item.label for item in scenario.expectations] == [
        'starts "Run the demo."',
        'starts "Show the demo."',
        'does_not_start "Run a test."',
    ]
    record = {
        "requests": [
            {"request": "Run the demo.", "started": ["demo"]},
            {"request": "Show the demo.", "started": ["other"]},
            {"request": "Run a test.", "started": ["demo", "other"]},
        ],
        "models": ["claude-haiku-5-5"],
    }
    assert [item.check(record) for item in scenario.expectations] == [
        None,
        "the session started other",
        "the session started demo, other",
    ]
    with pytest.raises(RecordError, match="no session of"):
        scenario.expectations[0].check({"requests": [], "models": []})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"expectations": []}, "must have exactly mode, near_misses, requests, skill"),
        ({"near_misses": None}, "must have exactly"),
        ({"skill": "other"}, 'names skill "other", not demo'),
        ({"requests": []}, "requests must be a non-empty list"),
        ({"near_misses": ["Run a test.", 3]}, "near_misses must be a non-empty list"),
        ({"requests": ["Run the demo.", "Run the demo."]}, "requests name a request twice"),
        ({"requests": ["Run the demo.\nNow."]}, "must be one line of text"),
        ({"requests": [" Run the demo."]}, "must be one line of text"),
        ({"requests": ["--help"]}, "starts with -"),
        ({"requests": ["/demo"]}, "starts with /"),
        ({"near_misses": ["Run the demo."]}, '"Run the demo." is both a request and a near miss'),
    ],
    ids=[
        "expectations",
        "a missing field",
        "another skill",
        "no request",
        "a request not text",
        "a request twice",
        "two lines",
        "a space around it",
        "an option",
        "a command",
        "in both lists",
    ],
)
def test_a_trigger_scenario_is_checked(changes: dict[str, Any], message: str, tmp_path: Path) -> None:
    spec = {key: value for key, value in {**TRIGGER_SPEC, **changes}.items() if value is not None}
    with pytest.raises(ScenarioError, match=re.escape(message)):
        trigger_scenario(tmp_path, spec, demo_checkout(tmp_path))


def test_a_trigger_scenario_s_skill_is_a_shipped_one_the_model_may_start(tmp_path: Path) -> None:
    with pytest.raises(ScenarioError, match="the checkout has no skills/demo"):
        trigger_scenario(tmp_path, TRIGGER_SPEC, tmp_path / "empty")
    user_only = demo_checkout(tmp_path, "disable-model-invocation: true\n")
    with pytest.raises(ScenarioError, match="demo sets disable-model-invocation"):
        trigger_scenario(tmp_path, TRIGGER_SPEC, user_only)


def test_the_worked_trigger_scenario_draws_on_its_description_and_a_neighbour_s() -> None:
    scenario = skill_evals_scenarios.load_scenarios("analyze-skill-cost")[0]
    assert (scenario.name, scenario.mode) == ("description", skill_evals_scenarios.TRIGGER)
    description = frontmatter.read(skill_evals_scenarios.REPOSITORY_ROOT / "skills" / "analyze-skill-cost" / "SKILL.md")
    neighbour = frontmatter.read(skill_evals_scenarios.REPOSITORY_ROOT / "skills" / "audit-ai-config" / "SKILL.md")
    # The requests ask what the description's "Use it when" sentence names, and the near misses what the neighbour's.
    assert "Use it when asked what a skill costs, why it is slow or expensive, or to check one before shipping it." in (
        description.string("description") or ""
    )
    assert "Use it when asked to audit or check a repository's agent instructions or setup" in (
        neighbour.string("description") or ""
    )
    assert len(scenario.requests) == 3
    assert all("skill" in request for request in scenario.requests), scenario.requests
    assert all("repository" in request for request in scenario.near_misses), scenario.near_misses


def test_only_the_session_s_own_skill_tool_call_is_a_start() -> None:
    def assistant_with(content: list[Any], subagent: bool = False) -> str:
        event = {"type": "assistant", "message": {"model": "claude-haiku-5-5", "content": content}}
        return json.dumps({**event, "parent_tool_use_id": "toolu_9" if subagent else None})

    skill = {"type": "tool_use", "name": "Skill", "input": {"skill": "demo"}}
    output = "\n".join(
        [
            "not json",
            assistant_with([{"type": "tool_use", "name": "Read", "input": {"skill": "read"}}]),
            assistant_with([{**skill, "input": {"skill": "from-subagent"}}], subagent=True),
            assistant_with([{**skill, "input": "demo"}, {**skill, "input": {"skill": " "}}]),
            assistant_with([skill, {**skill, "input": {"skill": "/other"}}, skill]),
        ]
    )
    assert skill_evals_runs.skills_in_stream(output) == ["demo", "other"]


def test_a_trigger_session_runs_on_the_judged_model_with_a_turn_limit_and_no_grant() -> None:
    command = skill_evals_runs.trigger_command("claude", "Run the demo.", "haiku")
    assert command[:5] == ["claude", "-p", "Run the demo.", "--model", "claude-haiku-5-5"]
    assert str(skill_evals_runs.TRIGGER_TURNS) == command[command.index("--max-turns") + 1]
    assert command[command.index("--setting-sources") + 1] == "project,local"
    for absent in ("--allowedTools", "--agents", "--permission-mode"):
        assert absent not in command


def test_records_hold_a_trigger_record_to_its_scenario_and_model(tmp_path: Path) -> None:
    scenario = trigger_scenario(tmp_path, TRIGGER_SPEC, demo_checkout(tmp_path))
    good: dict[str, Any] = {
        "requests": [{"request": "Run a test.", "started": []}, {"request": "Run the demo.", "started": ["demo"]}],
        "models": ["claude-haiku-5-5-20261001"],
    }
    records = tmp_path / "records" / "triggers"
    records.mkdir(parents=True)
    (records / "haiku-2.json").write_text(json.dumps(good), encoding="utf-8")
    source = skill_evals_judging.records_in(tmp_path / "records")
    assert good == source(scenario, "haiku", 2)
    refused = {
        "another model": {**good, "models": ["claude-opus-5-5"]},
        "no model": {**good, "models": []},
        "a request missing": {**good, "requests": good["requests"][:1]},
        "a request twice": {**good, "requests": [*good["requests"], good["requests"][0]]},
        "a started skill not text": {**good, "requests": [{"request": "Run a test.", "started": [1]}]},
        "a review record": contract_record(),
    }
    for name, value in refused.items():
        (records / "haiku.json").write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(RecordError) as raised:
            source(scenario, "haiku", None)
        assert raised.value.kind == skill_evals_records.RECORD, name
