"""Regression suite for tools/deployable_report.py, with both runtimes replaced by fakes.

No test starts Codex or Copilot CLI: each takes the place of `discovery.list_skills`' process runner, so what the
runtime lists, or why it refuses to, is whatever the test says. The manifest, the adapter paths, and the home with
spaces in its name are real.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from deployer import discovery, manifest
from deployer.paths import Paths
from tools import deployable_report as report

SOURCE_ID = "test/source"
HASH = "sha256:" + "0" * 64
ADAPTERS = ("alpha", "beta")
AGENT = "reviewer.md"
SIGN_IN = "it exited before answering with exit code 1: You are not logged in. Run `copilot login` to sign in."


class FakeRuntimes:
    """Stands in for discovery's process runner: answers a listing, or raises why it cannot."""

    def __init__(self, adapters: Path) -> None:
        self.adapters = adapters
        self.listed: dict[str, list[str]] = {runtime: list(ADAPTERS) for runtime in discovery.RUNTIMES}
        self.errors: dict[str, str] = {}
        self.installed = {*discovery.RUNTIMES, "claude"}

    def find(self, name: str) -> str | None:
        return f"C:/tools/{name}.cmd" if name in self.installed else None

    def talk(self, arguments, cwd, environment, requests, answered, timeout):
        runtime = Path(arguments[0]).stem
        if runtime in self.errors:
            raise discovery.ListingError(self.errors[runtime])
        if runtime == "codex":
            skills = [
                {"name": name, "path": str(self.adapters / name / "SKILL.md"), "enabled": True}
                for name in self.listed[runtime]
            ]
            return [json.dumps({"id": discovery.CODEX_LIST_ID, "result": {"data": [{"skills": skills}]}})]
        skills = [{"name": name, "path": str(self.adapters / name), "enabled": True} for name in self.listed[runtime]]
        return [json.dumps(skills)]


class DeployableReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="deployable-report-test.")
        root = Path(self._temporary.name).resolve()
        self.home = root / "Home With Spaces"
        self.paths = Paths(root / "source", self.home)
        self.paths.manifest_file.parent.mkdir(parents=True)
        entry = {
            "wrappers": {name: {"hash": HASH} for name in ADAPTERS},
            "skills": {name: {"hash": HASH} for name in ADAPTERS},
            "agents": {AGENT: {"hash": HASH}},
        }
        for name in ADAPTERS:
            (self.paths.dest_dir / name).mkdir(parents=True)
            (self.paths.dest_dir / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        self.paths.agent_dest_dir.mkdir(parents=True)
        (self.paths.agent_dest_dir / AGENT).write_text("---\nname: reviewer\n---\n", encoding="utf-8")
        self.paths.manifest_file.write_text(
            json.dumps({"manifest_version": manifest.MANIFEST_VERSION, "sources": {SOURCE_ID: entry}}), encoding="utf-8"
        )
        self.fake = FakeRuntimes(self.paths.adapter_dest_dir)
        self.results_file = root / "results.json"
        self.summary_file = root / "summary.md"

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def check(self) -> dict[str, report.RuntimeResult]:
        results = report.check_runtimes(self.paths, {}, find=self.fake.find, talk=self.fake.talk)
        return {result.runtime: result for result in results}

    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = report.main(list(arguments))
        return code, output.getvalue()

    def verify(self, deploy_exit: int = 0, label: str = "first deploy") -> tuple[int, str]:
        return self.run_main(
            "verify",
            "--label",
            label,
            "--deploy-verify-exit",
            str(deploy_exit),
            "--results",
            str(self.results_file),
            "--home",
            str(self.home),
            "--source",
            str(self.paths.source_dir),
        )

    # -- classification -------------------------------------------------------------------------------------

    def test_every_adapter_found_passes_both_runtimes(self) -> None:
        results = self.check()
        self.assertEqual(
            {"codex": report.PASSED, "copilot": report.PASSED},
            {runtime: result.status for runtime, result in results.items()},
        )
        self.assertEqual(0, report.exit_code(list(results.values()), 0))

    def test_a_missing_adapter_fails_that_runtime_and_names_it(self) -> None:
        self.fake.listed["copilot"] = ["alpha"]
        results = self.check()
        self.assertEqual(report.PASSED, results["codex"].status)
        self.assertEqual(report.FAILED, results["copilot"].status)
        self.assertIn("beta", results["copilot"].detail)
        self.assertIn("NOT FOUND", results["copilot"].detail)
        self.assertEqual(1, report.exit_code(list(results.values()), 1))

    def test_a_refusal_that_asks_for_sign_in_is_skipped_with_its_reason(self) -> None:
        self.fake.errors["copilot"] = SIGN_IN
        results = self.check()
        self.assertEqual(report.SKIPPED, results["copilot"].status)
        self.assertIn("not logged in", results["copilot"].detail)
        self.assertEqual(report.PASSED, results["codex"].status)
        # deploy.py verify exits 1 for the runtime that cannot list; the skip explains it.
        self.assertEqual(0, report.exit_code(list(results.values()), 1))

    def test_any_other_listing_error_fails(self) -> None:
        self.fake.errors["codex"] = "no answer within 120 seconds"
        results = self.check()
        self.assertEqual(report.FAILED, results["codex"].status)
        self.assertIn("no answer within 120 seconds", results["codex"].detail)
        self.assertEqual(1, report.exit_code(list(results.values()), 1))

    def test_only_a_request_to_sign_in_is_a_sign_in_problem(self) -> None:
        for message in (
            "no answer within 120 seconds",
            "it refused the skills/list request: bad",
            "cannot start it: [WinError 2] The system cannot find the file specified",
        ):
            with self.subTest(message=message):
                self.assertFalse(report.sign_in_problem(message))
        for message in ("Error: not authenticated", "please sign in first", "401 Unauthorized", "run copilot login"):
            with self.subTest(message=message):
                self.assertTrue(report.sign_in_problem(message))

    def test_a_runtime_that_is_not_installed_fails_because_the_workflow_installed_it(self) -> None:
        self.fake.installed.discard("codex")
        results = self.check()
        self.assertEqual(report.FAILED, results["codex"].status)
        self.assertIn("not installed", results["codex"].detail)

    def test_every_runtime_skipped_fails_because_nothing_was_checked(self) -> None:
        self.fake.errors.update({"codex": SIGN_IN, "copilot": SIGN_IN})
        results = list(self.check().values())
        self.assertEqual([report.SKIPPED, report.SKIPPED], [result.status for result in results])
        self.assertEqual(1, report.exit_code(results, 1))

    def test_a_failing_deploy_verify_with_nothing_skipped_fails_even_if_the_script_passed(self) -> None:
        results = list(self.check().values())
        self.assertEqual(1, report.exit_code(results, 1))

    def test_a_shadowed_adapter_fails(self) -> None:
        original = self.fake.talk

        def talk(arguments, cwd, environment, requests, answered, timeout):
            lines = original(arguments, cwd, environment, requests, answered, timeout)
            if Path(arguments[0]).stem == "copilot":
                other = [{"name": "alpha", "path": str(self.home / ".copilot" / "skills" / "alpha"), "enabled": True}]
                return [json.dumps(other + json.loads(lines[0]))]
            return lines

        self.fake.talk = talk
        results = self.check()
        self.assertEqual(report.FAILED, results["copilot"].status)
        self.assertIn("SHADOWED", results["copilot"].detail)

    def test_a_manifest_without_adapters_is_an_error(self) -> None:
        self.paths.manifest_file.write_text(
            json.dumps({"manifest_version": manifest.MANIFEST_VERSION, "sources": {}}), encoding="utf-8"
        )
        code, output = self.verify()
        self.assertEqual(1, code)
        self.assertIn("no runtime adapters", output.lower())

    # -- command line ---------------------------------------------------------------------------------------

    def test_verify_prints_a_line_per_runtime_and_records_the_pass(self) -> None:
        self.fake.errors["copilot"] = SIGN_IN
        original = (report.platform_support.find_executable, discovery.converse)
        report.platform_support.find_executable = self.fake.find
        discovery.converse = self.fake.talk
        self.addCleanup(
            lambda: (
                setattr(report.platform_support, "find_executable", original[0]),
                setattr(discovery, "converse", original[1]),
            )
        )
        code, output = self.verify(deploy_exit=1)
        self.assertEqual(0, code, output)
        self.assertIn("PASSED codex", output)
        self.assertIn("SKIPPED copilot", output)
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual("first deploy", recorded[0]["label"])
        self.assertEqual(
            {"codex": "PASSED", "copilot": "SKIPPED"},
            {item["runtime"]: item["status"] for item in recorded[0]["results"]},
        )

    def test_a_second_pass_is_appended_to_the_results(self) -> None:
        self.results_file.write_text(json.dumps([{"label": "first deploy", "results": []}]), encoding="utf-8")
        original = (report.platform_support.find_executable, discovery.converse)
        report.platform_support.find_executable = self.fake.find
        discovery.converse = self.fake.talk
        self.addCleanup(
            lambda: (
                setattr(report.platform_support, "find_executable", original[0]),
                setattr(discovery, "converse", original[1]),
            )
        )
        self.verify(label="after reinstall")
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual(["first deploy", "after reinstall"], [item["label"] for item in recorded])

    # -- Claude Code file layout ----------------------------------------------------------------------------

    def layout(self) -> report.RuntimeResult:
        return report.check_layout(self.paths, find=self.fake.find)

    def test_claude_code_passes_when_every_owned_skill_and_agent_is_in_place(self) -> None:
        result = self.layout()
        self.assertEqual((report.PASSED, "claude"), (result.status, result.runtime))
        self.assertIn("2 skills and 1 agent in place", result.detail)
        self.assertIn("no session", result.detail)

    def test_a_missing_or_empty_skill_file_fails_and_names_the_skill(self) -> None:
        (self.paths.dest_dir / "alpha" / "SKILL.md").unlink()
        (self.paths.dest_dir / "beta" / "SKILL.md").write_text("", encoding="utf-8")
        result = self.layout()
        self.assertEqual(report.FAILED, result.status)
        self.assertIn("alpha/SKILL.md missing or empty", result.detail)
        self.assertIn("beta/SKILL.md missing or empty", result.detail)

    def test_a_missing_agent_fails(self) -> None:
        (self.paths.agent_dest_dir / AGENT).unlink()
        result = self.layout()
        self.assertEqual(report.FAILED, result.status)
        self.assertIn(f"agent {AGENT} missing", result.detail)

    def test_claude_code_not_installed_fails_even_when_the_files_are_in_place(self) -> None:
        self.fake.installed.discard("claude")
        result = self.layout()
        self.assertEqual(report.FAILED, result.status)
        self.assertIn("not installed", result.detail)

    def test_a_manifest_without_skills_is_an_error(self) -> None:
        self.paths.manifest_file.write_text(
            json.dumps({"manifest_version": manifest.MANIFEST_VERSION, "sources": {}}), encoding="utf-8"
        )
        code, output = self.run_main(
            "layout",
            "--label",
            "first deploy",
            "--results",
            str(self.results_file),
            "--home",
            str(self.home),
            "--source",
            str(self.paths.source_dir),
        )
        self.assertEqual(1, code)
        self.assertIn("no skills are deployed", output.lower())

    def test_the_layout_result_joins_the_pass_with_the_same_label(self) -> None:
        self.results_file.write_text(
            json.dumps(
                [
                    {
                        "label": "first deploy",
                        "results": [{"runtime": "codex", "status": "PASSED", "detail": "2 adapters found"}],
                    }
                ]
            ),
            encoding="utf-8",
        )
        with mock.patch.object(report.platform_support, "find_executable", side_effect=self.fake.find):
            code, output = self.run_main(
                "layout",
                "--label",
                "first deploy",
                "--results",
                str(self.results_file),
                "--home",
                str(self.home),
                "--source",
                str(self.paths.source_dir),
            )
        self.assertEqual(0, code, output)
        self.assertIn("PASSED claude", output)
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual(1, len(recorded))
        self.assertEqual(["codex", "claude"], [item["runtime"] for item in recorded[0]["results"]])

    def test_a_layout_failure_exits_non_zero_and_is_still_recorded(self) -> None:
        (self.paths.dest_dir / "alpha" / "SKILL.md").unlink()
        with mock.patch.object(report.platform_support, "find_executable", side_effect=self.fake.find):
            code, _ = self.run_main(
                "layout",
                "--label",
                "after reinstall",
                "--results",
                str(self.results_file),
                "--home",
                str(self.home),
                "--source",
                str(self.paths.source_dir),
            )
        self.assertEqual(1, code)
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual(
            ("after reinstall", "claude", "FAILED"),
            (recorded[0]["label"], recorded[0]["results"][0]["runtime"], recorded[0]["results"][0]["status"]),
        )

    # -- summary --------------------------------------------------------------------------------------------

    def test_the_summary_lists_versions_results_and_notes_and_appends(self) -> None:
        self.summary_file.write_text("# Earlier step\n", encoding="utf-8")
        self.results_file.write_text(
            json.dumps(
                [
                    {
                        "label": "first deploy",
                        "results": [
                            {"runtime": "codex", "status": "PASSED", "detail": "2 adapters found"},
                            {"runtime": "copilot", "status": "SKIPPED", "detail": "not logged in"},
                        ],
                    }
                ]
            ),
            encoding="utf-8",
        )
        versions = [("Python", "3.11.9"), ("Codex CLI", "0.160.0"), ("Copilot CLI", "not installed")]
        text = report.render_summary(
            json.loads(self.results_file.read_text(encoding="utf-8")), versions, ["Codex sandbox set to elevated"]
        )
        for expected in (
            "| Python | 3.11.9 |",
            "| Codex CLI | 0.160.0 |",
            "| Copilot CLI | not installed |",
            "first deploy",
            "PASSED",
            "SKIPPED",
            "not logged in",
            "Codex sandbox set to elevated",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        code, _ = self.run_main(
            "summary",
            "--results",
            str(self.results_file),
            "--summary",
            str(self.summary_file),
            "--home",
            str(self.home),
            "--source",
            str(self.paths.source_dir),
            "--note",
            "Codex sandbox set to elevated",
            "--no-probe",
        )
        self.assertEqual(0, code)
        written = self.summary_file.read_text(encoding="utf-8")
        self.assertTrue(written.startswith("# Earlier step\n"))
        self.assertIn("Codex sandbox set to elevated", written)

    def test_the_summary_is_a_no_op_without_a_summary_file(self) -> None:
        self.results_file.write_text("[]", encoding="utf-8")
        code, output = self.run_main("summary", "--results", str(self.results_file), "--no-probe")
        self.assertEqual(0, code)
        self.assertIn("Deployability", output)

    def test_tool_versions_report_a_missing_tool_and_a_failing_version_command(self) -> None:
        def find(name: str) -> str | None:
            return None if name == "copilot" else f"C:/tools/{name}.cmd"

        def run(arguments: list[str]) -> tuple[int, str]:
            return (1, "boom") if "codex" in arguments[0] else (0, f"{Path(arguments[0]).stem} version 9.8.7\n")

        versions = dict(report.tool_versions(find, run))
        self.assertEqual("not installed", versions["Copilot CLI"])
        self.assertEqual("version could not be read", versions["Codex CLI"])
        self.assertEqual("9.8.7", versions["Node.js"])
        self.assertEqual("9.8.7", versions["Claude Code"])


if __name__ == "__main__":
    unittest.main()
