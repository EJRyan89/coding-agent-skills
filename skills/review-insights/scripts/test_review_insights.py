from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

SCRIPT_DIRECTORY = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIRECTORY.parents[1] / "code-review-core" / "scripts"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
sys.path.insert(0, str(CORE_SCRIPTS))

import review_insights as ri  # noqa: E402
from review_archive import commit_record  # noqa: E402
from review_config import write_config  # noqa: E402
from review_flags import add_flag, load_store, resolve_flag  # noqa: E402
from review_records import build_record, validate_adapter_result  # noqa: E402

DECIDED_AT = datetime(2026, 2, 3, 9, 30, tzinfo=timezone.utc)


def reviewer(identifier: str, model: str | None = None) -> dict:
    """A `review.reviewers` entry as `finalize` records it; a repository entrypoint reviewer names no model."""
    entry = {"id": identifier, "category": "Fixture", "files": 1, "findings": 0, "retries": 0,
             "dispositions_only": False}
    return entry if model is None else {**entry, "model": model}


def covered(coverage: str, tool: str, rule: str) -> tuple[str, str, dict]:
    """A Correctness finding from source `fixture` that an analyzer rule could catch."""
    return "Correctness", "fixture", {"coverage": coverage, "tool": tool, "rule": rule}


def record(repository: str, number: int, categories: list[str | tuple], *, version: int = 1,
           reviewed_at: str = "2026-01-15T12:00:00+00:00", reviewers: list[dict] | None = None) -> dict:
    """A record whose findings have these categories, each from source `fixture`, a (category, source) pair, or a
    (category, source, analyzer) triple; a finding with analyzer coverage also has a headline."""
    head = f"{version:x}" * 40
    request = {
        "repository": repository, "pull_number": number, "pull_url": f"https://github.com/{repository}/pull/{number}",
        "title": "Fixture", "base_ref": "main", "base_sha": "a" * 40, "head_sha": head, "mode": "initial",
        "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}},
    }
    if reviewers:
        request["reviewers"] = reviewers
    items = [item if isinstance(item, tuple) else (item, "fixture") for item in categories]
    findings = [{"candidate_key": f"k{index}", "severity": "SHOULD_FIX", "category": item[0],
                 "path": f"src/{index}.cs", "line": 3, "body": "Fix this.", "evidence": "Evidence.",
                 "source": item[1], **({"analyzer": item[2], "title": f"Headline {index}"} if len(item) > 2 else {})}
                for index, item in enumerate(items)]
    result = validate_adapter_result(
        {"protocol_version": 1, "repository": repository, "pull_number": number, "head_sha": head,
         "summary": "Fixture", "reviewer": "fixture", "status": "complete", "findings": findings,
         "prior_dispositions": [], "usage": None},
        expected_repository=repository, expected_number=number, expected_head_sha=head,
    )
    return build_record(request, result, version=version,
                        policy={"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3},
                        reviewed_at=reviewed_at)


class InsightFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "insights root with spaces"
        self.archive = self.root / "archive"
        self.summary = self.root / "summary"
        self.flags = self.root / "flags" / "flags.json"
        self.config_path = self.root / "config.json"
        generic = {"reviewer": {"id": "generic", "protocol_version": 1, "trusted_ref": None, "scope": "generic",
                                "manifest_path": None}, "checkout_path": None}
        write_config({
            "schema_version": 1,
            "default_repository_set": "primary",
            "repository_sets": {"primary": ["owner/repo"], "both": ["owner/repo", "owner/other"]},
            "repositories": {"owner/repo": generic, "owner/other": generic},
            "operation_repository_sets": {"review-insights": "both"},
            "archive_root": str(self.archive),
            "local_mirror_root": None,
            "summary_root": str(self.summary),
            "dashboard_file": str(self.root / "dashboard.md"),
            "github_login": "reviewer",
        }, self.config_path)
        self.services = ri.Services(now=lambda: DECIDED_AT, flags_path=lambda: self.flags)

    def commit(self, repository: str, number: int, categories: list[str | tuple], **options: object) -> None:
        version = options.get("version", 1)
        commit_record(self.archive, repository, number, record(repository, number, categories, **options),
                      expected_latest_version=None if version == 1 else version - 1)

    def flag(self, category: str, *, repository: str | None = "owner/repo", pull: int | None = 7,
             version: int | None = 1, finding: str | None = "F001") -> str:
        return add_flag(self.flags, category=category, body="Observation.", repository=repository,
                        pull_number=pull, review_version=version, finding_id=finding)["id"]

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ri.main(["--config", str(self.config_path), *arguments], services=self.services)
        return code, out.getvalue(), err.getvalue()

    def report(self, *arguments: str, reviewers: bool = False) -> tuple[Path, list[str]]:
        """The report's path and its RECOMMENDATION lines, with their REVIEWER lines when `reviewers` is set."""
        code, out, err = self.run_main("report", "--start", "2026-01-01", "--end", "2026-01-31", *arguments)
        self.assertEqual(0, code, err)
        lines = out.splitlines()
        json_path = Path(lines[0].removeprefix("REPORT "))
        self.assertEqual(f"MARKDOWN {json_path.with_suffix('.md')}", lines[1])
        return json_path, [line for line in lines[2:] if reviewers or not line.startswith("REVIEWER ")]


class ReportTests(InsightFixture):
    def test_report_reads_the_configured_scope_and_links_flags_by_finding_category(self) -> None:
        self.commit("owner/repo", 7, ["Correctness", "Style"])
        self.commit("owner/repo", 7, ["Style", "Correctness"], version=2)
        self.commit("owner/other", 3, ["Correctness"])
        self.commit("owner/repo", 9, ["Correctness"], reviewed_at="2026-02-15T12:00:00+00:00")
        latest_style = self.flag("guideline", version=2)
        latest_correctness = self.flag("guideline", version=2, finding="F002")
        other = self.flag("rule", repository="owner/other", pull=3)
        self.flag("unrelated", pull=9)
        self.flag("missing review", version=3)
        self.flag("general", repository=None, pull=None, version=None, finding=None)
        resolve_flag(self.flags, self.flag("done", version=2, finding="F002"), "Handled")
        json_path, lines = self.report()
        self.assertEqual(self.summary / "both" / "2026-01-01--2026-01-31" / "insights.json", json_path)
        self.assertEqual([
            f"RECOMMENDATION REC-001 Correctness findings=3 decision=deferred flags={latest_correctness},{other}",
            f"RECOMMENDATION REC-002 Style findings=2 decision=deferred flags={latest_style}",
        ], lines)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(6, report["schema_version"])
        self.assertEqual(3, report["record_count"], "the out-of-range review is excluded")
        self.assertEqual([], report["recommendations"][0]["decision_history"])
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(f"Linked flags: {latest_style}", markdown)
        self.assertIn("payload", markdown)

    def test_findings_are_counted_by_the_reviewer_and_model_that_raised_them(self) -> None:
        specialists = [reviewer("security", "claude-opus-5-5"), reviewer("style", "claude-haiku-4-5"),
                       reviewer("database")]
        self.commit("owner/repo", 7, [("Correctness", "security + style"), ("Correctness", "security"),
                                      ("Style", "style"), ("Correctness", "database")], reviewers=specialists)
        # A repository entrypoint reviewer is the record's only reviewer, names no model, and writes its own source.
        self.commit("owner/repo", 8, [("Correctness", "Repository review pass")],
                    reviewers=[reviewer("repo-reviewer")])
        # The same specialist on another model is counted apart.
        self.commit("owner/repo", 10, [("Correctness", "security")],
                    reviewers=[reviewer("security", "claude-sonnet-5-5"), reviewer("style", "claude-haiku-4-5")])
        # A record from before reviewers were recorded has only its source, which may be free text.
        self.commit("owner/other", 3, [("Correctness", "legacy-a + legacy|b")])
        merged = self.flag("guideline")  # F001 of pull 7, raised by both security and style
        again = self.flag("guideline")  # a second flag on the same finding counts it once
        style_flag = self.flag("guideline", finding="F003")
        resolve_flag(self.flags, self.flag("done", finding="F002"), "Handled")  # a resolved flag counts nothing
        json_path, lines = self.report(reviewers=True)
        self.assertEqual([
            f"RECOMMENDATION REC-001 Correctness findings=6 decision=deferred flags={merged},{again}",
            "REVIEWER REC-001 security model=claude-opus-5-5 findings=2 flagged=1 addressed=0 still_present=0",
            "REVIEWER REC-001 database model=unknown findings=1 flagged=0 addressed=0 still_present=0",
            "REVIEWER REC-001 legacy-a model=unknown findings=1 flagged=0 addressed=0 still_present=0",
            "REVIEWER REC-001 legacy|b model=unknown findings=1 flagged=0 addressed=0 still_present=0",
            "REVIEWER REC-001 repo-reviewer model=unknown findings=1 flagged=0 addressed=0 still_present=0",
            "REVIEWER REC-001 security model=claude-sonnet-5-5 findings=1 flagged=0 addressed=0 still_present=0",
            "REVIEWER REC-001 style model=claude-haiku-4-5 findings=1 flagged=1 addressed=0 still_present=0",
            f"RECOMMENDATION REC-002 Style findings=1 decision=deferred flags={style_flag}",
            "REVIEWER REC-002 style model=claude-haiku-4-5 findings=1 flagged=1 addressed=0 still_present=0",
        ], lines)
        style = json.loads(json_path.read_text(encoding="utf-8"))["recommendations"][1]
        self.assertEqual([{"reviewer": "style", "model": "claude-haiku-4-5", "findings": 1, "flagged_findings": 1,
                           "addressed": 0, "still_present": 0}], style["reviewers"])
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn("| Reviewer | Model | Findings | Flagged | Addressed | Still present |\n"
                      "| --- | --- | --- | --- | --- | --- |\n| security | claude-opus-5-5 | 2 | 1 | 0 | 0 |\n", markdown)
        self.assertIn("| legacy\\|b | unknown | 1 | 0 | 0 | 0 |\n", markdown)

    def test_later_dispositions_count_findings_addressed_and_still_present_by_reviewer_and_category(self) -> None:
        policy = {"request_changes_for": ["MUST_FIX"], "should_fix_threshold": 3}
        reviewers = [reviewer("security", "claude-opus-5-5"), reviewer("style", "claude-haiku-4-5")]
        ledger: list = []
        # Version 1 is in range; the re-reviews that judged its findings come after it and are read all the same.
        for version, reviewed_at, findings, dispositions in (
            (1, "2026-01-15T12:00:00+00:00",
             [("Correctness", "security"), ("Correctness", "security"), ("Style", "style")], []),
            (2, "2026-02-10T12:00:00+00:00", [],
             [("v1:F001", "addressed"), ("v1:F002", "still_present"), ("v1:F003", "still_present")]),
            (3, "2026-02-11T12:00:00+00:00", [], [("v1:F002", "still_present"), ("v1:F003", "addressed")]),
        ):
            head = f"{version:x}" * 40
            request = {"repository": "owner/repo", "pull_number": 7, "pull_url": "https://github.com/owner/repo/pull/7",
                       "title": "Fixture", "base_ref": "main", "base_sha": "a" * 40, "head_sha": head,
                       "mode": "initial" if version == 1 else "re-review", "reviewers": reviewers,
                       "adapter": {"name": "generic", "scope": "generic", "source_commit": None, "source_hashes": {}}}
            result = {"protocol_version": 1, "repository": "owner/repo", "pull_number": 7, "head_sha": head,
                      "summary": "Fixture", "reviewer": "fixture", "status": "complete", "usage": None,
                      "findings": [{"candidate_key": f"k{index}", "severity": "SHOULD_FIX", "category": category,
                                    "path": f"src/{index}.cs", "line": 3, "body": "Fix this.", "evidence": "Evidence.",
                                    "source": source} for index, (category, source) in enumerate(findings)],
                      "prior_dispositions": [{"finding_id": identifier, "disposition": value, "rationale": "Checked."}
                                             for identifier, value in dispositions]}
            built = build_record(request, result, version=version, policy=policy, reviewed_at=reviewed_at,
                                 prior_ledger=ledger)
            commit_record(self.archive, "owner/repo", 7, built,
                          expected_latest_version=None if version == 1 else version - 1)
            ledger = built["ledger"]
        json_path, lines = self.report(reviewers=True)
        self.assertEqual([
            "RECOMMENDATION REC-001 Correctness findings=2 decision=deferred flags=none",
            "REVIEWER REC-001 security model=claude-opus-5-5 findings=2 flagged=0 addressed=1 still_present=1",
            "RECOMMENDATION REC-002 Style findings=1 decision=deferred flags=none",
            "REVIEWER REC-002 style model=claude-haiku-4-5 findings=1 flagged=0 addressed=1 still_present=0",
        ], lines)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(1, report["record_count"], "the later re-reviews are outside the range")
        self.assertEqual([{"reviewer": "security", "model": "claude-opus-5-5", "findings": 2, "flagged_findings": 0,
                           "addressed": 1, "still_present": 1}], report["recommendations"][0]["reviewers"])
        self.assertIn("| Reviewer | Model | Findings | Flagged | Addressed | Still present |\n"
                      "| --- | --- | --- | --- | --- | --- |\n| security | claude-opus-5-5 | 2 | 0 | 1 | 1 |\n",
                      json_path.with_suffix(".md").read_text(encoding="utf-8"))

    def test_output_survives_a_console_that_cannot_encode_it(self) -> None:
        # Windows pipes default to a legacy code page; RECOMMENDATION quotes a reviewer's category as written.
        self.commit("owner/repo", 7, ["Naming → clarity ✓"])
        result = subprocess.run(
            [sys.executable, "-B", str(SCRIPT_DIRECTORY / "review_insights.py"), "--config", str(self.config_path),
             "report", "--start", "2026-01-01", "--end", "2026-01-31"],
            capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252", "CODE_REVIEW_FLAGS": str(self.flags)},
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))
        self.assertEqual(["RECOMMENDATION REC-001 Naming → clarity ✓ findings=1 decision=deferred flags=none",
                          "REVIEWER REC-001 fixture model=unknown findings=1 flagged=0 addressed=0 still_present=0"],
                         result.stdout.decode("utf-8").splitlines()[-2:])

    def test_a_flag_follows_the_review_it_names_after_a_re_review_renumbers_findings(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        original = self.flag("guideline")  # F001 of the first review is Correctness
        self.commit("owner/repo", 7, ["Style"], version=2)  # F001 of the re-review is Style
        json_path, lines = self.report()
        self.assertEqual([
            f"RECOMMENDATION REC-001 Correctness findings=1 decision=deferred flags={original}",
            "RECOMMENDATION REC-002 Style findings=1 decision=deferred flags=none",
        ], lines)
        code, out, err = self.run_main("decide", "--report", str(json_path), "REC-002", "--category", "Style",
                                       "--flags", "none", "accepted")
        self.assertEqual((0, "DECIDED REC-002 accepted\n"), (code, out), err)
        self.assertEqual({"open"}, {flag["status"] for flag in load_store(self.flags)["flags"]})

    def test_legacy_flags_without_a_review_version_are_never_linked(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        self.flags.parent.mkdir(parents=True)
        self.flags.write_text(json.dumps({"schema_version": 1, "next_id": 2, "flags": [{
            "id": "RF-000001", "status": "open", "created_at": "2026-01-16T00:00:00+00:00", "resolved_at": None,
            "repository": "owner/repo", "pull_number": 7, "finding_id": "F001", "category": "guideline",
            "body": "Observation.", "resolution": None,
        }]}), encoding="utf-8")
        _, lines = self.report()
        self.assertEqual(["RECOMMENDATION REC-001 Correctness findings=1 decision=deferred flags=none"], lines)

    def test_explicit_repositories_get_their_own_summary_directory(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        json_path, lines = self.report("--repository", "Owner/Repo")
        self.assertRegex(json_path.parent.parent.name, r"^repositories-[0-9a-f]{12}$")
        self.assertEqual(["RECOMMENDATION REC-001 Correctness findings=1 decision=deferred flags=none"], lines)
        named, _ = self.report("--repository-set", "primary")
        self.assertEqual("primary", named.parent.parent.name)

    def test_invalid_scopes_and_dates_fail(self) -> None:
        for arguments, message in (
            (["--start", "2026-02-01", "--end", "2026-01-01"], "Start date must not be after end date"),
            (["--start", "2026-13-01", "--end", "2026-01-01"], "Invalid ISO date"),
            (["--start", "2026-01-01", "--end", "2026-01-31", "--repository-set", "missing"], "Unknown repository set"),
        ):
            with self.subTest(message=message):
                code, out, err = self.run_main("report", *arguments)
                self.assertEqual((2, ""), (code, out))
                self.assertIn(f"FAILED {message}", err)
        with self.assertRaisesRegex(ri.InsightError, "non-empty unique"):
            ri.create_report(archive_root=self.root, summary_root=self.summary, repository_set="primary",
                             repositories=[], start=date(2026, 1, 1), end=date(2026, 1, 31))


class DecideTests(InsightFixture):
    def decide(
        self, json_path: Path, recommendation: str, category: str, flags: str, *arguments: str
    ) -> tuple[int, str, str]:
        return self.run_main("decide", "--report", str(json_path), recommendation, "--category", category,
                             "--flags", flags, *arguments)

    def test_accepting_resolves_linked_flags_and_appends_history(self) -> None:
        self.commit("owner/repo", 7, ["Correctness", "Style"])
        correctness = self.flag("guideline")
        style = self.flag("guideline", finding="F002")
        json_path, _ = self.report()
        code, out, err = self.decide(json_path, "REC-001", "Correctness", correctness, "rejected")
        self.assertEqual((0, "DECIDED REC-001 rejected\n"), (code, out), err)
        self.assertEqual({"open"}, {flag["status"] for flag in load_store(self.flags)["flags"]})
        code, out, err = self.decide(json_path, "REC-001", "Correctness", correctness, "accepted",
                                       "--note", "Add a null-check rule")
        self.assertEqual(0, code, err)
        self.assertEqual([f"FLAG_RESOLVED {correctness}", "DECIDED REC-001 accepted"], out.splitlines())
        flags = {flag["id"]: flag for flag in load_store(self.flags)["flags"]}
        self.assertEqual("resolved", flags[correctness]["status"])
        self.assertIn("REC-001 (Correctness)", flags[correctness]["resolution"])
        self.assertIn("Add a null-check rule", flags[correctness]["resolution"])
        self.assertEqual("open", flags[style]["status"], "only the accepted recommendation's flags are resolved")
        item = json.loads(json_path.read_text(encoding="utf-8"))["recommendations"][0]
        self.assertEqual("accepted", item["decision"])
        self.assertEqual([
            {"decision": "rejected", "decided_at": DECIDED_AT.isoformat(), "note": None, "resolved_flags": []},
            {"decision": "accepted", "decided_at": DECIDED_AT.isoformat(), "note": "Add a null-check rule",
             "resolved_flags": [correctness]},
        ], item["decision_history"])
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(f"accepted — Add a null-check rule (resolved {correctness})", markdown)
        code, out, _ = self.decide(json_path, "REC-001", "Correctness", correctness, "accepted")
        self.assertEqual([f"FLAG_ALREADY_RESOLVED {correctness}", "DECIDED REC-001 accepted"], out.splitlines())

    def test_regenerating_keeps_decisions_and_ids_by_category(self) -> None:
        self.commit("owner/repo", 7, ["Style"])
        json_path, _ = self.report()
        self.assertEqual(0, self.decide(json_path, "REC-001", "Style", "none", "deferred", "--note", "Revisit")[0])
        self.commit("owner/repo", 8, ["Correctness", "Correctness"])
        _, lines = self.report()
        # Correctness now ranks first, but REC-001 still names Style: an ID the user saw never moves.
        self.assertEqual(["RECOMMENDATION REC-002 Correctness findings=2 decision=deferred flags=none",
                          "RECOMMENDATION REC-001 Style findings=1 decision=deferred flags=none"], lines)
        style = json.loads(json_path.read_text(encoding="utf-8"))["recommendations"][1]
        self.assertEqual(["Revisit"], [entry["note"] for entry in style["decision_history"]])

    def test_a_decision_for_another_category_is_refused(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        self.flag("guideline")
        json_path, _ = self.report()
        before = json_path.read_text(encoding="utf-8")
        code, _, err = self.decide(json_path, "REC-001", "Style", "none", "accepted")
        self.assertEqual(2, code)
        self.assertIn("REC-001 is now category 'Correctness', not category 'Style'", err)
        self.assertEqual(before, json_path.read_text(encoding="utf-8"))
        self.assertEqual({"open"}, {flag["status"] for flag in load_store(self.flags)["flags"]})

    def test_a_flag_linked_after_the_user_saw_the_report_is_never_resolved(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        shown = self.flag("guideline")
        json_path, lines = self.report()
        self.assertEqual([f"RECOMMENDATION REC-001 Correctness findings=1 decision=deferred flags={shown}"], lines)
        later = self.flag("guideline")
        _, lines = self.report()  # regenerated before the user's answer is recorded: same ID, one more flag
        self.assertEqual([f"RECOMMENDATION REC-001 Correctness findings=1 decision=deferred flags={shown},{later}"],
                         lines)
        before = json_path.read_text(encoding="utf-8")
        code, out, err = self.decide(json_path, "REC-001", "Correctness", shown, "accepted")
        self.assertEqual((2, ""), (code, out))
        self.assertIn(f"REC-001 now links flags {shown},{later}, not {shown}", err)
        self.assertEqual(before, json_path.read_text(encoding="utf-8"))
        self.assertEqual({"open"}, {flag["status"] for flag in load_store(self.flags)["flags"]})
        code, out, err = self.decide(json_path, "REC-001", "Correctness", f"{later},{shown}", "accepted")
        self.assertEqual(0, code, err)
        self.assertEqual([f"FLAG_RESOLVED {shown}", f"FLAG_RESOLVED {later}", "DECIDED REC-001 accepted"],
                         out.splitlines())

    def test_version_one_reports_are_read_and_upgraded(self) -> None:
        json_path = self.summary / "primary" / "2026-01-01--2026-01-31" / "insights.json"
        json_path.parent.mkdir(parents=True)
        json_path.write_text(json.dumps({
            "schema_version": 1, "repository_set": "primary", "repositories": ["owner/repo"],
            "start_date": "2026-01-01", "end_date": "2026-01-31", "record_count": 1, "finding_count": 1,
            "severity_counts": {"SHOULD_FIX": 1}, "category_counts": {"Style": 1},
            "recommendations": [{"id": "REC-001", "category": "Style", "finding_count": 1,
                                 "recommendation": "Review recurring Style findings.", "decision": "rejected"}],
            "records": [],
        }), encoding="utf-8")
        code, out, err = self.decide(json_path, "REC-001", "Style", "none", "accepted")
        self.assertEqual((0, "DECIDED REC-001 accepted\n"), (code, out), err)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(6, report["schema_version"])
        self.assertEqual([], report["recommendations"][0]["linked_flags"])
        self.assertEqual([], report["recommendations"][0]["reviewers"])
        self.assertEqual(1, len(report["recommendations"][0]["decision_history"]))

    def test_version_three_reports_keep_their_links_and_gain_an_empty_breakdown(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        flag = self.flag("guideline")
        json_path, _ = self.report()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        report["schema_version"] = 3
        del report["recommendations"][0]["reviewers"]
        json_path.write_text(json.dumps(report), encoding="utf-8")
        code, out, err = self.decide(json_path, "REC-001", "Correctness", flag, "accepted")
        self.assertEqual(0, code, err)
        self.assertEqual([f"FLAG_RESOLVED {flag}", "DECIDED REC-001 accepted"], out.splitlines())
        upgraded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual((6, [], "category"), (upgraded["schema_version"], upgraded["recommendations"][0]["reviewers"],
                                               upgraded["recommendations"][0]["kind"]))
        self.assertNotIn("| Reviewer |", json_path.with_suffix(".md").read_text(encoding="utf-8"))
        _, lines = self.report(reviewers=True)  # regenerating fills the breakdown and keeps the decision
        self.assertEqual(["RECOMMENDATION REC-001 Correctness findings=1 decision=accepted flags=none",
                          "REVIEWER REC-001 fixture model=unknown findings=1 flagged=0 addressed=0 still_present=0"],
                         lines)

    def test_version_five_reports_have_no_outcome_counts_until_regenerated(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        json_path, _ = self.report()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        report["schema_version"] = 5
        for row in report["recommendations"][0]["reviewers"]:
            del row["addressed"], row["still_present"]
        json_path.write_text(json.dumps(report), encoding="utf-8")
        code, out, err = self.decide(json_path, "REC-001", "Correctness", "none", "deferred")
        self.assertEqual(0, code, err)
        upgraded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual((6, None, None), (upgraded["schema_version"],
                                           upgraded["recommendations"][0]["reviewers"][0]["addressed"],
                                           upgraded["recommendations"][0]["reviewers"][0]["still_present"]))
        self.assertIn("| fixture | unknown | 1 | 0 | - | - |\n", json_path.with_suffix(".md").read_text(encoding="utf-8"))

    def test_an_invalid_reviewers_breakdown_is_refused(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        json_path, _ = self.report()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        valid = report["recommendations"][0]["reviewers"][0]
        for broken in ({**valid, "findings": -1}, {**valid, "findings": True}, {**valid, "model": ""},
                       {**valid, "addressed": -1}, {**valid, "still_present": "1"},
                       {key: value for key, value in valid.items() if key != "flagged_findings"}):
            with self.subTest(row=broken):
                report["recommendations"][0]["reviewers"] = [broken]
                json_path.write_text(json.dumps(report), encoding="utf-8")
                code, _, err = self.decide(json_path, "REC-001", "Correctness", "none", "rejected")
                self.assertEqual(2, code)
                self.assertIn("REC-001.reviewers must be a list of reviewer counts", err)

    def test_links_in_version_two_reports_are_dropped_and_never_resolved(self) -> None:
        # A version 2 report linked a flag to whatever finding had its ID in the latest review: here a flag on the
        # first review's Correctness F001 was linked to the re-review's Style F001.
        self.commit("owner/repo", 7, ["Correctness"])
        self.commit("owner/repo", 7, ["Style"], version=2)
        stale = self.flag("guideline")
        json_path, _ = self.report()
        report = json.loads(json_path.read_text(encoding="utf-8"))
        report["schema_version"] = 2
        for item in report["recommendations"]:
            item["linked_flags"] = [stale] if item["category"] == "Style" else []
        json_path.write_text(json.dumps(report), encoding="utf-8")
        recommendation = next(item["id"] for item in report["recommendations"] if item["category"] == "Style")
        before = json_path.read_text(encoding="utf-8")
        code, out, err = self.decide(json_path, recommendation, "Style", stale, "accepted")
        self.assertEqual((2, ""), (code, out))
        self.assertIn(f"{recommendation} now links flags none, not {stale}; run report again", err)
        self.assertEqual(before, json_path.read_text(encoding="utf-8"))
        code, out, err = self.decide(json_path, recommendation, "Style", "none", "accepted")
        self.assertEqual((0, f"DECIDED {recommendation} accepted\n"), (code, out), err)
        self.assertEqual({"open"}, {flag["status"] for flag in load_store(self.flags)["flags"]})
        upgraded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(6, upgraded["schema_version"])
        self.assertEqual([[], []], [item["linked_flags"] for item in upgraded["recommendations"]])

    def test_failures_leave_report_and_flags_unchanged(self) -> None:
        self.commit("owner/repo", 7, ["Correctness"])
        flag = self.flag("guideline")
        json_path, _ = self.report()
        before = json_path.read_text(encoding="utf-8")
        code, _, err = self.decide(json_path, "REC-404", "Correctness", flag, "accepted")
        self.assertEqual(2, code)
        self.assertIn("FAILED Unknown recommendation: REC-404", err)
        self.flags.unlink()
        code, _, err = self.decide(json_path, "REC-001", "Correctness", flag, "accepted")
        self.assertEqual(2, code)
        self.assertIn("Linked flags are not in the flag store", err)
        self.assertEqual(before, json_path.read_text(encoding="utf-8"))
        json_path.write_text(json.dumps({"schema_version": 9}), encoding="utf-8")
        code, _, err = self.decide(json_path, "REC-001", "Style", "none", "rejected")
        self.assertEqual(2, code)
        self.assertIn("not a supported insights report", err)


class AnalyzerTests(InsightFixture):
    def commit_covered(self) -> None:
        stylecop = {"coverage": "available", "tool": "StyleCop.Analyzers", "rule": "SA1515"}
        self.commit("owner/repo", 7, [
            covered("custom-candidate", "Roslyn", "unbounded-retry-loop"),  # F001
            ("Style", "fixture", stylecop),  # F002
            # Another reviewer's spelling of the same rule: analyzer names are matched without regard to case.
            ("Style", "fixture", {"coverage": "available", "tool": "stylecop.analyzers", "rule": "sa1515"}),  # F003
            "Correctness",  # F004, no analyzer could catch it
        ])
        self.commit("owner/other", 3, [
            covered("known", "Roslynator.Analyzers", "RCS1001"),
            covered("custom-candidate", "Roslyn", "unbounded-retry-loop"),
        ])

    def test_analyzer_findings_become_recommendations_ranked_cheapest_first(self) -> None:
        self.commit_covered()
        style_flag = self.flag("guideline", finding="F002")
        json_path, lines = self.report()
        self.assertEqual([
            "RECOMMENDATION REC-001 Correctness findings=4 decision=deferred flags=none",
            f"RECOMMENDATION REC-002 Style findings=2 decision=deferred flags={style_flag}",
            "ANALYZER REC-003 coverage=available tool=StyleCop.Analyzers rule=SA1515 findings=2 "
            f"repositories=owner/repo decision=deferred flags={style_flag}",
            "EXAMPLE REC-003 owner/repo#7 v1 F002 Headline 1",
            "EXAMPLE REC-003 owner/repo#7 v1 F003 Headline 2",
            "ANALYZER REC-004 coverage=known tool=Roslynator.Analyzers rule=RCS1001 findings=1 "
            "repositories=owner/other decision=deferred flags=none",
            "EXAMPLE REC-004 owner/other#3 v1 F001 Headline 0",
            "ANALYZER REC-005 coverage=custom-candidate tool=Roslyn rule=unbounded-retry-loop findings=2 "
            "repositories=owner/other,owner/repo decision=deferred flags=none",
            "EXAMPLE REC-005 owner/other#3 v1 F002 Headline 1",
            "EXAMPLE REC-005 owner/repo#7 v1 F001 Headline 0",
        ], lines)
        report = json.loads(json_path.read_text(encoding="utf-8"))
        available = report["recommendations"][2]
        self.assertEqual("analyzer", available["kind"])
        self.assertEqual(
            "StyleCop.Analyzers, which owner/repo already has, provides SA1515, but the rule is not enforced. Enable "
            "it or raise its severity in the analyzer's configuration so the build reports it instead of a reviewer.",
            available["recommendation"],
        )
        self.assertEqual({"repository": "owner/repo", "pull_number": 7, "review_version": 1, "finding_id": "F002",
                          "path": "src/1.cs", "line": 3, "title": "Headline 1"}, available["evidence"][0])
        self.assertEqual([{"reviewer": "fixture", "model": "unknown", "findings": 2, "flagged_findings": 1,
                           "addressed": 0, "still_present": 0}], available["reviewers"])
        self.assertIn("owner/other and owner/repo", report["recommendations"][4]["recommendation"])
        self.assertIn("Consider writing a custom Roslyn rule", report["recommendations"][4]["recommendation"])
        self.assertIn("after checking its license, cost, and telemetry", report["recommendations"][3]["recommendation"])
        markdown = json_path.with_suffix(".md").read_text(encoding="utf-8")
        self.assertLess(markdown.index("### REC-002 — Style"), markdown.index("## Analyzer opportunities"))
        self.assertIn("### REC-003 — available: SA1515 (StyleCop.Analyzers)\n\nFinding count: 2\n"
                      "Repositories: owner/repo\n", markdown)
        self.assertIn("Findings:\n\n- owner/repo#7 v1 F002 Headline 1\n- owner/repo#7 v1 F003 Headline 2\n", markdown)
        self.assertLess(markdown.index("REC-003 — available"), markdown.index("REC-004 — known"))
        self.assertLess(markdown.index("REC-004 — known"), markdown.index("REC-005 — custom-candidate"))

    def test_analyzer_decisions_name_the_rule_and_survive_regeneration(self) -> None:
        self.commit_covered()
        style_flag = self.flag("guideline", finding="F002")
        json_path, _ = self.report()

        def decide(*subject: str, flags: str = style_flag, decision: str = "accepted") -> tuple[int, str, str]:
            return self.run_main("decide", "--report", str(json_path), "REC-003", *subject, "--flags", flags, decision)

        before = json_path.read_text(encoding="utf-8")
        code, _, err = decide("--category", "Style")
        self.assertEqual(2, code)
        self.assertIn("REC-003 is now analyzer 'available StyleCop.Analyzers SA1515', not category 'Style'", err)
        code, _, err = decide("--analyzer", "known", "StyleCop.Analyzers", "SA1515")
        self.assertEqual(2, code)
        self.assertIn("not analyzer 'known StyleCop.Analyzers SA1515'", err)
        self.assertEqual(before, json_path.read_text(encoding="utf-8"))
        code, out, err = decide("--analyzer", "available", "stylecop.analyzers", "sa1515")
        self.assertEqual(0, code, err)
        self.assertEqual([f"FLAG_RESOLVED {style_flag}", "DECIDED REC-003 accepted"], out.splitlines())
        flag = next(item for item in load_store(self.flags)["flags"] if item["id"] == style_flag)
        self.assertIn("REC-003 (available StyleCop.Analyzers SA1515)", flag["resolution"])
        # The finding is also in its category's recommendation, whose acceptance finds the flag already resolved.
        code, out, err = self.run_main("decide", "--report", str(json_path), "REC-002", "--category", "Style",
                                       "--flags", style_flag, "accepted")
        self.assertEqual((0, [f"FLAG_ALREADY_RESOLVED {style_flag}", "DECIDED REC-002 accepted"]),
                         (code, out.splitlines()), err)
        self.commit("owner/repo", 8, [covered("available", "Microsoft.CodeAnalysis.NetAnalyzers", "CA2000"),
                                      covered("available", "STYLECOP.ANALYZERS", "SA1515")])
        _, lines = self.report()
        self.assertIn("ANALYZER REC-003 coverage=available tool=StyleCop.Analyzers rule=SA1515 findings=3 "
                      "repositories=owner/repo decision=accepted flags=none", lines)
        self.assertIn("ANALYZER REC-006 coverage=available tool=Microsoft.CodeAnalysis.NetAnalyzers rule=CA2000 "
                      "findings=1 repositories=owner/repo decision=deferred flags=none", lines)
        recommendations = json.loads(json_path.read_text(encoding="utf-8"))["recommendations"]
        history = next(item for item in recommendations if item["id"] == "REC-003")["decision_history"]
        self.assertEqual(["accepted"], [entry["decision"] for entry in history])
        with self.assertRaisesRegex(ri.InsightError, "exactly one subject"):
            ri.decide(json_path, "REC-003", "Style", [], "rejected", analyzer=("available", "x", "y"),
                      services=self.services)

    def test_a_report_with_a_malformed_analyzer_recommendation_is_refused(self) -> None:
        self.commit_covered()
        json_path, _ = self.report()
        original = json.loads(json_path.read_text(encoding="utf-8"))
        for change, message in (
            (lambda item: item.update(tool="Style Cop"), "REC-003 has an invalid analyzer subject"),
            (lambda item: item.update(repositories=[]), "REC-003 has an invalid analyzer subject"),
            (lambda item: item["evidence"][0].pop("title"), "REC-003.evidence must be a list of findings"),
            (lambda item: item.update(kind="rule"), "REC-003 has an invalid kind"),
        ):
            tampered = json.loads(json.dumps(original))
            change(tampered["recommendations"][2])
            json_path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ri.InsightError, message):
                ri.load_report(json_path)


if __name__ == "__main__":
    unittest.main()
