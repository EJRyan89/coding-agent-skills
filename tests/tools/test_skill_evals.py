"""tools/skill_evals.py: the scenario loader, every expectation kind against literal records, the output, the table,
and the review-prs scenarios themselves. No model is called."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "code-review-core" / "scripts"))

from review_records import validate_record

from tools import skill_evals
from tools.skill_evals import Outcome, RecordError, Scenario, ScenarioError

REVIEW_PRS = skill_evals.SCENARIO_ROOT / "review-prs"


def finding(path: str, line: int, severity: str, identifier: str = "F001", **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "path": path, "line": line, "severity": severity, **extra}


def entry(version: int, identifier: str, *dispositions: tuple[int, str]) -> dict[str, Any]:
    return {
        "version": version,
        "id": identifier,
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
    (directory / "scenario.json").write_text(json.dumps(spec), encoding="utf-8")
    return directory


def scenario_spec(*expectations: dict[str, Any], mode: str = "initial", **extra: Any) -> dict[str, Any]:
    spec = {"skill": "demo", "mode": mode, "title": "A change", "body": "What it does.", **extra}
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
        spec = scenario_spec()
        del spec["body"]
        self.assertIn("must have exactly", self.load_error(spec))

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
        self.assertIn("title and body must be text", self.load_error({**scenario_spec(), "title": " "}))
        self.assertIn("expectations must be a non-empty list", self.load_error({**scenario_spec(), "expectations": []}))

    def test_a_tree_is_required(self) -> None:
        directory = write_scenario(self.root, "case", scenario_spec())
        (directory / "head").rmdir()
        with self.assertRaisesRegex(ScenarioError, "case: has no head/ tree"):
            skill_evals.load_scenarios("demo", root=self.root)

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

    def test_a_linked_repeat_passes(self) -> None:
        found = [
            finding("store.go", 45, "MUST_FIX", "F001", repeats=self.link),
            finding("store.go", 70, "MUST_FIX", "F002"),
        ]
        self.assertIsNone(check(self.spec, record(found, version=2)))
        self.assertEqual("repeats store.go:44,45 v1:F001", skill_evals.expectation(self.spec, "re-review").label)

    def test_raising_nothing_there_passes(self) -> None:
        self.assertIsNone(check(self.spec, record([finding("labels.go", 44, "SUGGESTION")], version=2)))

    def test_an_unlinked_or_wrongly_linked_finding_fails(self) -> None:
        found = [
            finding("store.go", 44, "MUST_FIX", "F001"),
            finding("store.go", 45, "MUST_FIX", "F002", repeats={"version": 1, "id": "F003"}),
        ]
        self.assertEqual(
            "F001 at line 44, F002 at line 45 not linked to v1:F001", check(self.spec, record(found, version=2))
        )


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
            entry(item["version"], item["id"], (2, judged[f"v{item['version']}:{item['id']}"]))
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


if __name__ == "__main__":
    unittest.main()
