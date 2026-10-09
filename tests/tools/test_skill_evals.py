"""tools/skill_evals.py: the scenario loader, every expectation kind against literal records, the output, the table,
the review-prs scenarios themselves, and the run step with a stub Claude Code. No model is called."""

from __future__ import annotations

import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-review-core" / "scripts"))

import review_guard
import review_pipeline
from review_config import write_config
from review_github import GitHubClient
from review_records import validate_record

from deployer import platform_support
from tools import skill_evals
from tools.skill_evals import Outcome, RecordError, Scenario, ScenarioError

REVIEW_PRS = skill_evals.SCENARIO_ROOT / "review-prs"


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


def check(spec: dict[str, Any], value: dict[str, Any], mode: str = "re-review") -> str | None:
    return skill_evals.expectation(spec, mode).check(value)


def refusal(spec: dict[str, Any], mode: str) -> str:
    """Why the loader refuses the expectation."""
    try:
        skill_evals.expectation(spec, mode)
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
            skill_evals.load_scenarios("demo", root=self.root)
        return str(raised.exception)

    def test_loads_every_scenario_in_name_order(self) -> None:
        write_scenario(self.root, "b-second", scenario_spec())
        write_scenario(self.root, "a-first", scenario_spec())
        names = [scenario.name for scenario in skill_evals.load_scenarios("demo", root=self.root)]
        self.assertEqual(["a-first", "b-second"], names)

    def test_loads_only_the_named_scenarios(self) -> None:
        write_scenario(self.root, "one", scenario_spec())
        write_scenario(self.root, "two", scenario_spec())
        names = [scenario.name for scenario in skill_evals.load_scenarios("demo", ["two"], root=self.root)]
        self.assertEqual(["two"], names)

    def test_unknown_scenario_and_skill_fail(self) -> None:
        write_scenario(self.root, "one", scenario_spec())
        with self.assertRaisesRegex(ScenarioError, "no scenario three for demo"):
            skill_evals.load_scenarios("demo", ["three"], root=self.root)
        with self.assertRaisesRegex(ScenarioError, "no scenarios for other"):
            skill_evals.load_scenarios("other", root=self.root)

    def test_unknown_expectation_kind_fails(self) -> None:
        self.assertIn('unknown expectation kind "vibes"', self.load_error(scenario_spec({"kind": "vibes"})))

    def test_expectation_fields_are_exact(self) -> None:
        self.assertIn("verdict expectation lacks verdict", self.load_error(scenario_spec({"kind": "verdict"})))
        extra = scenario_spec({"kind": "verdict", "verdict": "APPROVED", "note": "x"})
        self.assertIn("verdict expectation does not take note", self.load_error(extra))

    def test_values_are_checked(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"kind": "verdict", "verdict": "LGTM"}, "verdict must be one of"),
            ({"kind": "finding", "path": "a.go", "lines": [1], "severity": "BLOCKER"}, "severity must be one of"),
            ({"kind": "finding", "path": "../a.go", "lines": [1], "severity": "MUST_FIX"}, "plain relative path"),
            ({"kind": "finding", "path": "a\\b.go", "lines": [1], "severity": "MUST_FIX"}, "plain relative path"),
            ({"kind": "finding", "path": "a.go", "lines": [], "severity": "MUST_FIX"}, "distinct positive line"),
            ({"kind": "finding", "path": "a.go", "lines": [2, 2], "severity": "MUST_FIX"}, "distinct positive line"),
            ({"kind": "finding", "path": "a.go", "lines": [0], "severity": "MUST_FIX"}, "distinct positive line"),
            ({"kind": "no_finding_above", "severity": "SUGGESTION", "paths": []}, "paths must be a list"),
        ]
        for spec, message in cases:
            with self.subTest(spec=spec):
                self.assertIn(message, refusal(spec, "initial"))

    def test_re_review_values_are_checked(self) -> None:
        cases: list[tuple[dict[str, Any], str]] = [
            ({"kind": "ledger", "addressed": -1, "still_present": 0}, "addressed must be a count"),
            ({"kind": "ledger", "addressed": True, "still_present": 0}, "addressed must be a count"),
            ({"kind": "disposition", "entry": "F001", "disposition": "addressed"}, "entry must look like v1:F001"),
            ({"kind": "disposition", "entry": "v1:F001", "disposition": "fixed"}, "disposition must be one of"),
            ({"kind": "repeats", "path": "a.go", "lines": [1], "entry": "v0:F001"}, "entry must look like v1:F001"),
        ]
        for spec, message in cases:
            with self.subTest(spec=spec):
                self.assertIn(message, refusal(spec, "re-review"))

    def test_re_review_kinds_need_a_re_review(self) -> None:
        for spec in (
            {"kind": "ledger", "addressed": 1, "still_present": 0},
            {"kind": "disposition", "entry": "v1:F001", "disposition": "addressed"},
            {"kind": "repeats", "path": "a.go", "lines": [1], "entry": "v1:F001"},
        ):
            with self.subTest(kind=spec["kind"]):
                self.assertIn("needs a re-review", refusal(spec, "initial"))

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
        (scenario,) = skill_evals.load_scenarios("demo", root=self.root)
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
            skill_evals.load_scenarios("demo", root=self.root)

    def test_pull_json_is_required(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "pull.json").unlink()
        with self.assertRaisesRegex(ScenarioError, "case: has no pull.json"):
            skill_evals.load_scenarios("demo", root=self.root)

    def test_prepare_arguments_are_one_call_per_mode(self) -> None:
        initial = write_scenario(self.root, "first", scenario_spec())
        again = write_scenario(self.root, "again", scenario_spec(mode="re-review", prior="prior.json"))
        (again / "prior.json").write_text("{}", encoding="utf-8")
        loaded = {scenario.name: scenario for scenario in skill_evals.load_scenarios("demo", root=self.root)}
        self.assertEqual(["--canary", "--fixture", str(initial)], skill_evals.prepare_arguments(loaded["first"]))
        self.assertEqual(
            ["--canary", "--fixture", str(again), "--re-review", "--prior", str(again / "prior.json")],
            skill_evals.prepare_arguments(loaded["again"]),
        )

    def test_unreadable_scenario_fails(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "scenario.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ScenarioError, "case: cannot read scenario.json"):
            skill_evals.load_scenarios("demo", root=self.root)


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
        self.assertEqual("finding store.go:44,45 SHOULD_FIX", skill_evals.expectation(self.spec, "initial").label)


class NoFindingAboveTests(unittest.TestCase):
    def test_on_listed_paths(self) -> None:
        spec = {"kind": "no_finding_above", "severity": "SUGGESTION", "paths": ["labels.go"]}
        self.assertIsNone(
            check(spec, record([finding("labels.go", 3, "SUGGESTION"), finding("store.go", 4, "MUST_FIX")]))
        )
        failure = check(spec, record([finding("labels.go", 3, "SHOULD_FIX")]))
        self.assertEqual("found SHOULD_FIX at labels.go:3", failure)
        self.assertEqual("no_finding_above SUGGESTION labels.go", skill_evals.expectation(spec, "initial").label)

    def test_on_every_path_when_none_are_listed(self) -> None:
        spec = {"kind": "no_finding_above", "severity": "SUGGESTION"}
        self.assertIsNone(check(spec, record([finding("a.go", 1, "SUGGESTION")])))
        failure = check(spec, record([finding("a.go", 1, "MUST_FIX"), finding("b.go", 2, "SHOULD_FIX")]))
        self.assertEqual("found MUST_FIX at a.go:1, SHOULD_FIX at b.go:2", failure)
        self.assertEqual("no_finding_above SUGGESTION *", skill_evals.expectation(spec, "initial").label)


class VerdictTests(unittest.TestCase):
    def test_verdict(self) -> None:
        spec = {"kind": "verdict", "verdict": "CHANGES_REQUESTED"}
        self.assertIsNone(check(spec, record(verdict="CHANGES_REQUESTED")))
        self.assertEqual("verdict is APPROVED", check(spec, record(verdict="APPROVED")))
        self.assertEqual("verdict CHANGES_REQUESTED", skill_evals.expectation(spec, "initial").label)


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
        self.assertEqual("ledger addressed=2 still_present=2", skill_evals.expectation(spec, "re-review").label)

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
        self.assertEqual("disposition v1:F002 addressed", skill_evals.expectation(spec, "re-review").label)

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
        self.assertEqual("repeats store.go:44,45 v1:F001", skill_evals.expectation(self.spec, "re-review").label)

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

    def test_a_restatement_at_the_entrys_severity_or_higher_fails(self) -> None:
        cases = {
            "MUST_FIX": [finding("store.go", 45, "MUST_FIX", "F002"), finding("store.go", 44, "SUGGESTION", "F003")],
            "SHOULD_FIX": [finding("store.go", 45, "MUST_FIX", "F002"), finding("store.go", 44, "SUGGESTION", "F003")],
            "SUGGESTION": [finding("store.go", 45, "SUGGESTION", "F002")],
        }
        for severity, found in cases.items():
            with self.subTest(severity=severity):
                self.assertEqual(
                    "F002 at line 45 not linked to v1:F001", check(self.spec, self.reviewed(found, severity))
                )

    def test_the_entry_must_be_in_the_ledger_with_a_severity(self) -> None:
        found = [finding("store.go", 45, "MUST_FIX", "F002")]
        cases = {
            "the record's ledger has no v1:F001": record(found, version=2, ledger=[entry(1, "F002")]),
            "the record's ledger gives v1:F001 no severity": record(
                found, version=2, ledger=[{**entry(1, "F001"), "severity": None}]
            ),
            "the record has no ledger": record(found, version=2),
        }
        for reason, value in cases.items():
            with self.subTest(reason=reason), self.assertRaisesRegex(RecordError, reason):
                check(self.spec, value)


class RecordShapeTests(unittest.TestCase):
    def test_malformed_records_raise(self) -> None:
        verdict = skill_evals.expectation({"kind": "verdict", "verdict": "APPROVED"}, "initial")
        clean = skill_evals.expectation({"kind": "no_finding_above", "severity": "SUGGESTION"}, "initial")
        with self.assertRaisesRegex(RecordError, "no review version and verdict"):
            verdict.check({"findings": []})
        for findings in ("none", [{"path": "a.go", "line": "1", "severity": "MUST_FIX"}], [finding("a.go", 1, "HIGH")]):
            with self.subTest(findings=findings), self.assertRaisesRegex(RecordError, "findings are malformed"):
                clean.check({"review": {"version": 1, "verdict": "APPROVED"}, "findings": findings})
        ledger = skill_evals.expectation({"kind": "ledger", "addressed": 0, "still_present": 0}, "re-review")
        with self.assertRaisesRegex(RecordError, "ledger is malformed"):
            ledger.check(record(ledger=[{"version": 1, "id": "F001"}]))


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

    def write_record(self, model: str, value: Any, scenario: str = "defects") -> None:
        folder = self.records / scenario
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{model}.json").write_text(json.dumps(value), encoding="utf-8")

    def test_every_expectation_passes(self) -> None:
        for model in skill_evals.MODELS:
            self.write_record(model, record([finding("a.go", 3, "MUST_FIX")], "CHANGES_REQUESTED"))
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
        self.write_record("haiku", record([finding("a.go", 3, "SHOULD_FIX")], "APPROVED"))
        self.write_record("opus", "not a record")
        status, lines = run_main(["demo", "--records", str(self.records)], self.scenarios)
        self.assertEqual(1, status)
        self.assertIn(
            'FAIL demo defects haiku verdict CHANGES_REQUESTED "verdict is APPROVED"',
            lines,
        )
        missing = self.records / "defects" / "sonnet.json"
        self.assertIn(
            f"FAIL demo defects sonnet verdict CHANGES_REQUESTED {json.dumps(f'no record at {missing}')}", lines
        )
        self.assertTrue(
            any(
                line.startswith("FAIL demo defects opus finding") and "does not hold a record" in line for line in lines
            )
        )
        self.assertEqual("| defects | 0/2 | 0/2 | 0/2 |", lines[-1])

    def test_unreadable_record_fails(self) -> None:
        (self.records / "defects").mkdir(parents=True)
        (self.records / "defects" / "haiku.json").write_text("{", encoding="utf-8")
        status, lines = run_main(["demo", "--records", str(self.records), "--model", "haiku"], self.scenarios)
        self.assertEqual(1, status)
        self.assertIn("cannot read", lines[0])

    def test_only_the_named_models_in_the_order_given(self) -> None:
        self.write_record("opus", record([finding("a.go", 3, "MUST_FIX")], "CHANGES_REQUESTED"))
        argv = ["demo", "--records", str(self.records), "--model", "opus", "--model", "haiku", "--model", "opus"]
        status, lines = run_main(argv, self.scenarios)
        self.assertEqual(1, status)
        self.assertEqual("| Scenario | opus | haiku |", lines[-3])
        self.assertEqual("| defects | 2/2 | 0/2 |", lines[-1])

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
        def source(scenario: Scenario, model: str) -> dict[str, Any]:
            if model == "sonnet":
                raise RecordError("the run failed")
            return record(verdict="CHANGES_REQUESTED")

        scenarios = skill_evals.load_scenarios("demo", root=self.scenarios)
        outcomes = skill_evals.judge(scenarios, ["haiku", "sonnet"], source)
        self.assertEqual(
            [
                Outcome(
                    "defects",
                    "haiku",
                    "finding a.go:3 MUST_FIX",
                    "no finding at least MUST_FIX on those lines; findings on a.go: none",
                ),
                Outcome("defects", "haiku", "verdict CHANGES_REQUESTED", None),
                Outcome("defects", "sonnet", "finding a.go:3 MUST_FIX", "the run failed"),
                Outcome("defects", "sonnet", "verdict CHANGES_REQUESTED", "the run failed"),
            ],
            outcomes,
        )
        self.assertEqual(
            ["| Scenario | haiku | sonnet |", "| --- | --- | --- |", "| defects | 1/2 | 0/2 |"],
            skill_evals.table(scenarios, ["haiku", "sonnet"], outcomes),
        )


def lines_of(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def prior_of(scenario: Scenario) -> Any:
    if scenario.prior is None:
        raise AssertionError(f"{scenario.name} has no prior record")
    return json.loads(scenario.prior.read_text(encoding="utf-8"))


def constant(value: dict[str, Any]) -> skill_evals.RecordSource:
    """A record source that returns the same record for every run."""
    return lambda _scenario, _model: value


class ReviewPrsScenarioTests(unittest.TestCase):
    """The shipped scenarios load, point at lines that changed, and an ideal run of each passes."""

    def setUp(self) -> None:
        self.scenarios = {scenario.name: scenario for scenario in skill_evals.load_scenarios("review-prs")}
        self.specs = {
            name: json.loads((scenario.directory / "scenario.json").read_text(encoding="utf-8"))
            for name, scenario in self.scenarios.items()
        }

    def test_the_three_scenarios(self) -> None:
        self.assertEqual(
            {"planted-defects": "initial", "clean-change": "initial", "re-review": "re-review"},
            {name: scenario.mode for name, scenario in self.scenarios.items()},
        )

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
        self.assertEqual(sorted(skill_evals.SEVERITIES), sorted(planted))

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
            finding(item["path"], item["lines"][0], item["severity"], f"F{index:03}")
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
                outcomes = skill_evals.judge([scenario], ["opus"], constant(ideal))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])


class NoGitHub:
    """A gh runner that fails the test on any call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, arguments: Sequence[str]) -> Any:
        self.calls.append(list(arguments))
        raise AssertionError(f"a fixture canary called gh: {arguments}")


def stub_result(spec: dict[str, Any]) -> dict[str, Any]:
    """A reviewer result that does what the scenario expects: each expected finding at the first of its lines, each
    expected disposition, and each expected repeat as a finding linked to its entry."""
    expectations = spec["expectations"]
    found = [
        {"path": item["path"], "line": item["lines"][0], "severity": item["severity"], "title": "Planted", "body": "x"}
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
        self.services = review_pipeline.Services(
            github=GitHubClient(runner=self.github), fetch_tarball=no_tarball, resolve_runtime=lambda *_: "claude-code"
        )

    def __call__(self, *arguments: str) -> list[str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = review_pipeline.main(["--config", str(self.config), *arguments], services=self.services)
        lines = output.getvalue().splitlines()
        self.test.assertEqual(0, status, lines)
        return lines

    def review(self, prepare: Sequence[str], guarded: bool = False) -> Path:
        """Prepare, review, check, and finalize one fixture; return the record JSON finalize wrote. With `guarded`,
        each role's read log is started as the reviewer guard's claim starts it."""
        prepared = self("prepare", "--host", "claude-code", *prepare)
        run = Path(next(line for line in prepared if line.startswith("RUN ")).split(" ", 2)[2])
        fixture = Path(prepare[list(prepare).index("--fixture") + 1])
        spec = json.loads((fixture / "scenario.json").read_text(encoding="utf-8"))
        for role in review_pipeline.load_run(run)["roles"]:
            if guarded:
                review_guard.read_log(run, role["id"]).touch()
            Path(role["result_file"]).write_text(json.dumps(stub_result(spec)), encoding="utf-8")
        self.test.assertEqual(["ALL_VALID example/inventory#1"], self("check", "--run", str(run)))
        finalized = self("finalize", "--run", str(run))
        return Path(
            next(line.split(" ", 2)[2] for line in finalized if line.startswith("SHA256 ") and line.endswith(".json"))
        )


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
        write_config(skill_evals.review_config(self.root), self.config)
        self.pipeline = Pipeline(self, self.config)

    def test_every_scenario_runs_as_a_fixture_canary_and_its_record_passes_the_checker(self) -> None:
        for scenario in skill_evals.load_scenarios("review-prs"):
            with self.subTest(scenario=scenario.name):
                written = self.pipeline.review(skill_evals.prepare_arguments(scenario))
                written_record = json.loads(written.read_text(encoding="utf-8"))
                self.assertEqual(scenario.mode, written_record["review"]["mode"])
                outcomes = skill_evals.judge([scenario], ["opus"], constant(written_record))
                self.assertEqual([], [outcome for outcome in outcomes if outcome.failure is not None])
        self.assertEqual([], self.pipeline.github.calls)


def assistant(model: str, subagent: bool = False) -> str:
    """One stream-json assistant event, from the session or, with `subagent`, forwarded from a subagent."""
    message = {"type": "assistant", "message": {"model": model, "content": []}}
    return json.dumps({**message, "parent_tool_use_id": "toolu_1" if subagent else None})


REVIEWER_AGENT_SOURCE = skill_evals.REPOSITORY_ROOT / "agents" / "code-review-reviewer.md"


def fake_deploy(home: Path, _source: Path) -> tuple[int, str]:
    """What the deployment puts where a run reads it: the reviewer agent, and in place of the guard its hook runs, a
    script that prints its own path."""
    agents = home / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "code-review-reviewer.md").write_bytes(REVIEWER_AGENT_SOURCE.read_bytes())
    guard = home / ".claude" / "skills" / "code-review-core" / "scripts" / "review_guard.py"
    guard.parent.mkdir(parents=True)
    guard.write_text('print("GUARD", __file__)\n', encoding="utf-8")
    return 0, ""


def reviewer_of(arguments: Sequence[str]) -> dict[str, Any]:
    """The reviewer definition a session command passes with --agents."""
    agents = json.loads(Path(arguments[list(arguments).index("--agents") + 1]).read_text(encoding="utf-8"))
    return agents["code-review-reviewer"]


def scenario_of(mode: str) -> Scenario:
    return next(item for item in skill_evals.load_scenarios("review-prs") if item.mode == mode)


class RunPieceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_root(self)
        self.initial, self.re_review = scenario_of("initial"), scenario_of("re-review")

    def test_the_prompt_starts_the_skill_with_the_prepare_arguments_quoted(self) -> None:
        self.assertEqual(f'/review-prs --canary --fixture "{self.initial.directory}"', skill_evals.prompt(self.initial))
        self.assertEqual(
            f'/review-prs --canary --fixture "{self.re_review.directory}" --re-review --prior "{self.re_review.prior}"',
            skill_evals.prompt(self.re_review),
        )

    def test_the_command_pins_the_session_and_isolates_the_run(self) -> None:
        agents = self.root / "agents.json"
        command = skill_evals.claude_command("claude.exe", self.initial, agents)
        self.assertEqual(["claude.exe", "-p", skill_evals.prompt(self.initial)], command[:3])
        pairs = {command[index]: command[index + 1] for index in range(3, len(command) - 1)}
        self.assertEqual("opus", pairs["--model"])
        self.assertEqual("project,local", pairs["--setting-sources"])
        self.assertEqual(str(agents), pairs["--agents"])
        self.assertEqual("Workflow", pairs["--disallowedTools"])
        self.assertEqual("acceptEdits", pairs["--permission-mode"])
        self.assertEqual("stream-json", pairs["--output-format"])
        flags = ("--verbose", "--forward-subagent-text", "--include-hook-events", "--strict-mcp-config")
        for flag in (*flags, "--no-session-persistence"):
            self.assertIn(flag, command)
        self.assertEqual(
            [
                'Bash(python -B "*review_pipeline.py" validate-result --run *)',
                'PowerShell(python -B "*review_pipeline.py" validate-result --run *)',
                'Bash(python -B "*review_source.py" source-file --run *)',
                'PowerShell(python -B "*review_source.py" source-file --run *)',
                'Bash(python -B "*review_source.py" source-search --run *)',
                'PowerShell(python -B "*review_source.py" source-search --run *)',
            ],
            command[command.index("--allowedTools") + 1 :],
        )

    def test_the_session_model_is_the_strongest_and_one_of_the_models(self) -> None:
        self.assertEqual(("haiku", "sonnet", "opus"), skill_evals.MODELS)
        self.assertEqual("opus", skill_evals.SESSION_MODEL)

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
            skill_evals.run_environment({"PATH": "kept"}, run, config),
        )

    def test_the_configuration_is_one_the_core_accepts(self) -> None:
        written = write_config(skill_evals.review_config(self.root), self.root / "config.json")
        self.assertEqual("claude-code", written["runtime"])
        self.assertEqual(["MUST_FIX"], written["verdict_policy"]["request_changes_for"])

    def test_the_reviewer_agent_gets_the_model_and_nothing_else_changes(self) -> None:
        fake_deploy(self.root, self.root)
        self.assertIsNone(skill_evals.set_reviewer_model(self.root, "haiku"))
        before = REVIEWER_AGENT_SOURCE.read_text(encoding="utf-8").splitlines()
        after = (self.root / skill_evals.REVIEWER_AGENT).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(
            [("model: inherit", "model: haiku")],
            [pair for pair in zip(before, after, strict=True) if pair[0] != pair[1]],
        )
        self.assertIn(
            "has no frontmatter line `model: inherit`", skill_evals.set_reviewer_model(self.root, "opus") or ""
        )

    def test_a_missing_reviewer_agent_is_a_reason(self) -> None:
        self.assertIn("has no frontmatter line", skill_evals.set_reviewer_model(self.root, "haiku") or "")

    def test_the_session_gets_the_deployed_agent_with_its_hook_on_the_homes_guard(self) -> None:
        fake_deploy(self.root, self.root)
        self.assertIsNone(skill_evals.set_reviewer_model(self.root, "haiku"))
        agents = skill_evals.reviewer_agents(self.root)
        self.assertEqual(["code-review-reviewer"], list(agents))
        definition = agents["code-review-reviewer"]
        self.assertEqual({"description", "prompt", "tools", "model", "omitClaudeMd", "hooks"}, set(definition))
        self.assertTrue(definition["description"].startswith("Internal to the code-review-core pipeline. Started"))
        self.assertTrue(definition["prompt"].startswith("You run one reviewer role of a code review"))
        self.assertTrue(REVIEWER_AGENT_SOURCE.read_text(encoding="utf-8").rstrip("\n").endswith(definition["prompt"]))
        self.assertEqual(["Read", "Grep", "Glob", "Write", "Edit", "Bash"], definition["tools"])
        self.assertEqual("haiku", definition["model"])
        self.assertIs(True, definition["omitClaudeMd"])
        self.assertEqual(["PreToolUse"], list(definition["hooks"]))
        [group] = definition["hooks"]["PreToolUse"]
        self.assertEqual("Read|Grep|Glob|Write|Edit|Bash", group["matcher"])
        [hook] = group["hooks"]
        self.assertEqual({"type", "command", "timeout"}, set(hook))
        self.assertEqual(("command", 30), (hook["type"], hook["timeout"]))
        guard = (self.root / ".claude/skills/code-review-core/scripts/review_guard.py").as_posix()
        self.assertIn(f"runpy.run_path(os.path.expanduser('{guard}'),", hook["command"])
        self.assertNotIn("~/", hook["command"])
        self.assertTrue(hook["command"].startswith('python -I -B -c "import json, os, runpy, sys; sys.excepthook ='))

    def test_the_hook_command_runs_the_homes_guard_with_spaces_in_its_path(self) -> None:
        bash = platform_support.find_bash()
        self.assertIsNotNone(bash, "Git Bash is required")
        self.assertIn(" ", str(self.root))
        fake_deploy(self.root, self.root)
        hook = skill_evals.reviewer_agents(self.root)["code-review-reviewer"]["hooks"]["PreToolUse"][0]["hooks"][0]
        ran = platform_support.run_tool([str(bash), "-c", hook["command"]])
        self.assertEqual(0, ran.returncode, ran.output)
        self.assertEqual(f"GUARD {(self.root / skill_evals.GUARD).as_posix()}", ran.output.strip())

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
                (home / skill_evals.REVIEWER_AGENT).write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(skill_evals.AgentError, re.escape(reason)):
                    skill_evals.reviewer_agents(home)

    def test_a_home_without_the_guard_or_with_an_unquotable_path_is_refused(self) -> None:
        fake_deploy(self.root, self.root)
        (self.root / skill_evals.GUARD).unlink()
        with self.assertRaisesRegex(skill_evals.AgentError, "the home has no .claude/skills/code-review-core/"):
            skill_evals.reviewer_agents(self.root)
        home = self.root / "it's"
        fake_deploy(home, home)
        with self.assertRaisesRegex(skill_evals.AgentError, "its hook cannot quote the home's path"):
            skill_evals.reviewer_agents(home)

    def test_a_record_a_reviewer_no_guard_held_is_told(self) -> None:
        def reviewed(*reviewers: Any) -> dict[str, Any]:
            value = record()
            value["review"]["reviewers"] = list(reviewers)
            return value

        held = {"id": "generic-review", "files_read": 0, "bytes_read": 0}
        self.assertIsNone(skill_evals.unguarded(reviewed(held, {**held, "id": "security", "files_read": 3})))
        self.assertEqual(
            "no reviewer guard held generic-review, security: its files_read is null",
            skill_evals.unguarded(reviewed({**held, "files_read": None}, {"id": "security"}, {**held, "id": "tests"})),
        )
        self.assertEqual("the record names no reviewer", skill_evals.unguarded(reviewed()))
        self.assertEqual("the record names no reviewer", skill_evals.unguarded(record()))
        self.assertEqual("the record has no review version and verdict", skill_evals.unguarded({}))

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
            skill_evals.stream_models(output),
        )

    def test_the_record_is_the_highest_version_in_the_canary_roots(self) -> None:
        pulls = self.root / "code-review-canary-abc" / "example" / "inventory" / "pulls" / "1"
        pulls.mkdir(parents=True)
        (pulls / "v1.json").write_text(json.dumps(record(version=1)), encoding="utf-8")
        (pulls / "review.json").write_text(json.dumps(record(version=2, verdict="CHANGES_REQUESTED")), encoding="utf-8")
        (pulls / "other.json").write_text(json.dumps({"review": "not a record"}), encoding="utf-8")
        (pulls / "broken.json").write_text("{", encoding="utf-8")
        (self.root / "elsewhere.json").write_text(json.dumps(record(version=9)), encoding="utf-8")
        self.assertEqual(2, skill_evals.find_record(self.root)["review"]["version"])
        with self.assertRaisesRegex(RecordError, "recorded no review"):
            skill_evals.find_record(self.root / "code-review-canary-abc")

    def test_why_a_run_counts_for_nothing(self) -> None:
        completed = skill_evals.Completed
        reviewed = completed(0, f"{assistant('claude-opus-5-5')}\n{assistant('claude-haiku-4-5', subagent=True)}", "")
        self.assertIsNone(skill_evals.run_failure("haiku", reviewed, 60, "review-prs", None))
        self.assertEqual(
            "reviewers ran on claude-haiku-4-5, not sonnet",
            skill_evals.run_failure("sonnet", reviewed, 60, "review-prs", None),
        )
        self.assertEqual(
            "no reviewer subagent ran, so the record is not the model's",
            skill_evals.run_failure("haiku", completed(0, assistant("claude-opus-5-5"), ""), 60, "review-prs", None),
        )
        init = json.dumps({"type": "system", "subtype": "init", "skills": ["other"]})
        denied = json.dumps(
            {"type": "result", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "rm x"}}]}
        )
        missing = "the run recorded no review"
        self.assertEqual(
            f"{missing}; timed out after 60 seconds; Claude Code did not list review-prs; denied: Bash: rm x",
            skill_evals.run_failure(
                "haiku", completed(-1, f"{init}\n{denied}", "", timed_out=True), 60, "review-prs", missing
            ),
        )
        self.assertEqual(
            f"{missing}; Claude Code exited with code 3",
            skill_evals.run_failure("haiku", completed(3, "", ""), 60, "review-prs", missing),
        )


def outcome(scenario: str, model: str, failure: str | None = None) -> Outcome:
    return Outcome(scenario, model, "verdict APPROVED", failure)


RESULTS_TEXT = "\n".join(
    [
        "# Skill evaluations",
        "",
        skill_evals.RESULTS_BEGIN,
        *skill_evals.RESULTS_HEADER,
        "| alpha | opus | claude-opus-5-5 | 1/1 | one 1/1 | claude-opus-5-5 | 2.1.0 | 2026-01-01 |",
        "| review-prs | opus | old | 0/1 | old | old | 2.0.0 | 2026-01-01 |",
        "| zeta | opus | claude-opus-5-5 | 1/1 | one 1/1 | claude-opus-5-5 | 2.1.0 | 2026-01-01 |",
        skill_evals.RESULTS_END,
        "",
        "Written by hand after the table.",
        "",
    ]
)


class ResultsTests(unittest.TestCase):
    def test_rows_name_the_models_and_a_model_that_never_ran(self) -> None:
        scenarios = [Scenario("review-prs", name, Path(name), "initial", None, ()) for name in ("a", "b")]
        run = skill_evals.Run
        session = frozenset({"claude-opus-5-5"})
        runs = {
            ("a", "haiku"): run({}, None, frozenset({"claude-haiku-4"}), session),
            ("b", "haiku"): run(None, "x", frozenset({"claude-haiku-4"}), session),
            ("a", "sonnet"): run(None, "no such model | here", frozenset(), frozenset()),
            ("b", "sonnet"): run(None, "no such model", frozenset(), frozenset()),
        }
        outcomes = [
            outcome("a", "haiku"),
            outcome("b", "haiku", "x"),
            outcome("a", "sonnet", "no such model | here"),
            outcome("b", "sonnet", "no such model"),
        ]
        rows = skill_evals.result_rows(
            "review-prs", scenarios, ["haiku", "sonnet"], outcomes, runs, "2.1.291", datetime.date(2026, 10, 8)
        )
        self.assertEqual(
            [
                "| review-prs | haiku | claude-haiku-4 | 1/2 | a 1/1, b 0/1 | claude-opus-5-5 | 2.1.291 | 2026-10-08 |",
                "| review-prs | sonnet | not run | 0/2 | no such model \\| here | unknown | 2.1.291 | 2026-10-08 |",
            ],
            rows,
        )

    def test_the_skills_rows_are_replaced_and_every_other_kept(self) -> None:
        rows = ["| review-prs | haiku | h | 1/1 | a 1/1 | s | 2.1.291 | 2026-10-08 |"]
        original = RESULTS_TEXT.split("\n")
        written = skill_evals.replace_results(RESULTS_TEXT, "review-prs", rows).split("\n")
        begin, end = written.index(skill_evals.RESULTS_BEGIN), written.index(skill_evals.RESULTS_END)
        self.assertEqual([*skill_evals.RESULTS_HEADER, original[5], rows[0], original[7]], written[begin + 1 : end])
        self.assertEqual(original[:begin], written[:begin])
        self.assertEqual(["", "Written by hand after the table.", ""], written[end + 1 :])

    def test_the_markers_are_required_once_each_in_order(self) -> None:
        texts = ("no markers", f"{skill_evals.RESULTS_END}\n{skill_evals.RESULTS_BEGIN}", RESULTS_TEXT * 2)
        for text in texts:
            with self.subTest(text=text[:20]), self.assertRaises(ScenarioError):
                skill_evals.results_table(text)

    def test_the_shipped_results_file_has_its_table(self) -> None:
        text = skill_evals.RESULTS.read_text(encoding="utf-8")
        begin, end = skill_evals.results_table(text)
        lines = text.split("\n")
        self.assertEqual(list(skill_evals.RESULTS_HEADER), lines[begin + 1 : begin + 3])
        columns = len(skill_evals.RESULTS_HEADER[0].split("|"))
        for row in lines[begin + 3 : end]:
            self.assertEqual(columns, len(row.replace("\\|", "").split("|")), row)


def frontmatter_model(path: Path) -> str:
    line = next(line for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("model: "))
    return line.removeprefix("model: ")


class StubClaude:
    """Claude Code as a run starts it: `--version`, or a review of the prompt's fixture through the pipeline in this
    process, with stream-json naming the session's model and the reviewer model the --agents definition sets, and,
    unless `guarded` is false, each role's read log started as the reviewer guard's claim starts it."""

    def __init__(
        self, test: unittest.TestCase, *, review: bool = True, reviewer: str | None = None, guarded: bool = True
    ) -> None:
        self.test, self.review, self.reviewer, self.guarded = test, review, reviewer, guarded
        self.calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def __call__(self, arguments: list[str], cwd: Path, environment: dict[str, str], timeout: float) -> Any:
        self.calls.append((arguments, cwd, environment))
        if arguments[1:] == ["--version"]:
            return skill_evals.Completed(0, "2.1.291 (Claude Code)\n", "")
        model = reviewer_of(arguments)["model"]
        self.test.assertEqual(model, frontmatter_model(cwd / skill_evals.REVIEWER_AGENT))
        if self.review:
            prepare = [word.strip('"') for word in arguments[2].split(" ")[1:]]
            files = {key: environment[key] for key in ("CODE_REVIEW_STATE", "CODE_REVIEW_FLAGS")}
            with mock.patch.object(tempfile, "tempdir", environment["TMPDIR"]), mock.patch.dict(os.environ, files):
                Pipeline(self.test, Path(environment["CODE_REVIEW_CONFIG"])).review(prepare, self.guarded)
        reviewer = self.reviewer or f"claude-{model}-0-0"
        return skill_evals.Completed(0, f"{assistant('claude-opus-5-5')}\n{assistant(reviewer, subagent=True)}", "")


def forward(path: Path) -> str:
    return skill_evals.quote(str(path).replace(os.sep, "/"))


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

    def evaluate(self, argv: Sequence[str], seams: skill_evals.Seams) -> tuple[int, list[str]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            status = skill_evals.main(["review-prs", *argv], seams=seams)
        return status, output.getvalue().splitlines()

    def test_every_scenario_runs_on_every_model_and_the_result_is_recorded(self) -> None:
        claude = StubClaude(self)
        status, lines = self.evaluate(["--write", "--jobs", "1"], self.seams(claude))
        self.assertEqual(0, status, lines)
        self.assertEqual(f"HOME {forward(self.homes)}", lines[0])
        source = skill_evals.runtime_canary.source_id(skill_evals.REPOSITORY_ROOT)
        self.assertEqual([f"DEPLOYED {model} {source}" for model in skill_evals.MODELS], lines[1:4])
        self.assertEqual("RUNTIME claude 2.1.291", lines[4])
        ended = [line.split(" ")[1:3] for line in lines if line.startswith("TRANSCRIPT ")]
        scenarios = ["clean-change", "planted-defects", "re-review"]
        self.assertEqual([[scenario, model] for scenario in scenarios for model in skill_evals.MODELS], ended)
        self.assertIn("REVIEWER haiku claude-haiku-0-0", lines)
        guarded = [line for line in lines if line.startswith("GUARDED ")]
        self.assertEqual(
            [
                f"GUARDED {scenario} {model} generic-review files_read=0"
                for scenario in scenarios
                for model in skill_evals.MODELS
            ],
            guarded,
        )
        self.assertEqual([], [line for line in lines if line.startswith("FAIL")])
        self.assertIn("PASS review-prs re-review haiku ledger addressed=1 still_present=2", lines)
        self.assertEqual(f"REMOVED {forward(self.homes)}", lines[-1])
        self.assertFalse(self.homes.exists())
        written = self.results.read_text(encoding="utf-8")
        self.assertIn(
            "| review-prs | haiku | claude-haiku-0-0 | 14/14 | clean-change 2/2, planted-defects 5/5, re-review 7/7 "
            "| claude-opus-5-5 | 2.1.291 | 2026-10-08 |",
            written,
        )
        self.assertNotIn("| review-prs | opus | old |", written)
        for arguments, cwd, environment in claude.calls:
            if arguments[1:] != ["--version"]:
                self.assertEqual(self.homes, cwd.parent)
                self.assertEqual("kept", environment["PATH"])
                self.assertTrue(Path(environment["TMPDIR"]).is_relative_to(cwd / skill_evals.STATE))
                agents = arguments[arguments.index("--agents") + 1]
                self.assertEqual(str(cwd / skill_evals.STATE / "agents.json"), agents)

    def test_a_record_no_guard_held_fails_the_run(self) -> None:
        claude = StubClaude(self, guarded=False)
        status, lines = self.evaluate(["--model", "haiku", "--scenario", "clean-change"], self.seams(claude))
        self.assertEqual(1, status)
        reason = "no reviewer guard held generic-review: its files_read is null"
        self.assertIn(f'FAIL review-prs clean-change haiku verdict APPROVED "{reason}"', lines)
        self.assertFalse(any(line.startswith(("PASS", "GUARDED")) for line in lines))

    def test_an_agent_that_cannot_be_passed_stops_the_run(self) -> None:
        def no_guard(home: Path, source: Path) -> tuple[int, str]:
            fake_deploy(home, source)
            (home / skill_evals.GUARD).unlink()
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
        self.assertIn('FAIL review-prs clean-change haiku verdict APPROVED "the run recorded no review"', lines)
        self.assertFalse(any(line.startswith(("PASS", "REMOVED")) for line in lines))
        self.assertTrue(self.homes.is_dir())
        self.assertIn("| review-prs | sonnet | claude-sonnet-0-0 | 0/14 |", self.results.read_text(encoding="utf-8"))

    def test_a_model_none_of_whose_reviewers_ran_is_recorded_as_not_run(self) -> None:
        status, lines = self.evaluate(["--write"], self.seams(StubClaude(self, review=False, reviewer="<synthetic>")))
        self.assertEqual(1, status)
        self.assertIn("REVIEWER haiku none", lines)
        self.assertIn(
            "| review-prs | haiku | not run | 0/14 | the run recorded no review |",
            self.results.read_text(encoding="utf-8"),
        )

    def test_a_reviewer_on_another_model_fails_the_run(self) -> None:
        claude = StubClaude(self, reviewer="claude-opus-5-5")
        status, lines = self.evaluate(["--model", "haiku", "--scenario", "clean-change"], self.seams(claude))
        self.assertEqual(1, status)
        reason = "reviewers ran on claude-opus-5-5, not haiku"
        self.assertIn(f'FAIL review-prs clean-change haiku verdict APPROVED "{reason}"', lines)

    def test_nothing_runs_without_claude_code_or_the_results_table(self) -> None:
        no_table = self.root / "empty.md"
        no_table.write_text("# Skill evaluations\n", encoding="utf-8")
        cases: dict[str, tuple[list[str], dict[str, Any]]] = {
            "Claude Code (claude) is not on PATH": ([], {"which": lambda _: None}),
            "needs one": (["--write"], {"results": no_table}),
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
        log = self.homes / "haiku" / skill_evals.STATE / "deploy.log"
        self.assertEqual("broken\n", log.read_text(encoding="utf-8"))
        self.assertEqual([], claude.calls)

    def test_write_records_only_a_full_run(self) -> None:
        for argv in (["--model", "haiku"], ["--scenario", "clean-change"], ["--records", str(self.root)]):
            with (
                self.subTest(argv=argv),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as raised,
            ):
                skill_evals.main(["review-prs", "--write", *argv], seams=self.seams(StubClaude(self)))
            self.assertEqual(2, raised.exception.code)


if __name__ == "__main__":
    unittest.main()
