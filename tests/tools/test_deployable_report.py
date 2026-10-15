"""Regression suite for tools/deployable_report.py, with both runtimes replaced by fakes.

No test starts Codex or Copilot CLI: each takes the place of `discovery.list_skills`' process runner, so what the
runtime lists, or why it refuses to, is whatever the test says. The manifest, the adapter paths, and the home with
spaces in its name are real.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

from git_test_base import GitTestCase

from deployer import discovery, manifest, platform_support
from deployer.paths import Paths
from tools import deployable_report as report

SOURCE_ID = "test/source"
HASH = "sha256:" + "0" * 64
ADAPTERS = ("alpha", "beta")
AGENT = "reviewer.md"
# A shared asset is a file beside the skill directories, as source.json's runtime-compatibility.md is.
SHARED_FILE = "shared-notes.md"
SIGN_IN = "it exited before answering with exit code 1: You are not logged in. Run `copilot login` to sign in."
# What `deploy.py verify` prints when it fails, pinned here as literals.
CODEX_FAILED = "Verification failed for Codex CLI.\nSee docs/codex-support.md.\n"
COPILOT_FAILED = "Verification failed for Copilot CLI.\nSee docs/copilot-support.md.\n"
BYTE_ORDER_MARK = chr(0xFEFF)
# The step the report names when nothing is deployed, pinned as each platform's literal: python on Windows, python3 on
# Linux, where a distribution may install no python command.
DEPLOY_FIRST = (
    "Deploy first with 'python deploy.py'." if platform_support.WINDOWS else "Deploy first with 'python3 deploy.py'."
)
BOTH_FAILED = (
    "Verification failed for Codex CLI and Copilot CLI.\nSee docs/codex-support.md and docs/copilot-support.md.\n"
)


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


class DeployableReportTests(GitTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="deployable-report-test.")
        root = Path(self._temporary.name).resolve()
        self.home = root / "Home With Spaces"
        self.paths = Paths(root / "source", self.home)
        self.paths.manifest_file.parent.mkdir(parents=True)
        entry = {
            "wrappers": {name: {"hash": HASH} for name in ADAPTERS},
            "skills": {name: {"hash": HASH, "skill_deps": []} for name in ADAPTERS},
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

    def use_fake_runtimes(self) -> None:
        """Have the report's command line find and talk to the fake runtimes until the test ends."""
        for patcher in (
            mock.patch.object(report.platform_support, "find_executable", self.fake.find),
            mock.patch.object(discovery, "converse", self.fake.talk),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_main(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = report.main(list(arguments))
        return code, output.getvalue()

    def verify(
        self, deploy_exit: int = 0, label: str = "first deploy", verify_output: str | None = ""
    ) -> tuple[int, str]:
        """Run the report's verify command after a deploy.py verify that exited deploy_exit and printed verify_output,
        or left no log when it is None."""
        log = self.results_file.parent / "verify.log"
        if verify_output is None:
            log.unlink(missing_ok=True)
        else:
            log.write_text(verify_output, encoding="utf-8")
        return self.run_main(
            "verify",
            "--label",
            label,
            "--deploy-verify-exit",
            str(deploy_exit),
            "--deploy-verify-log",
            str(log),
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
        self.assertEqual(0, report.exit_code(list(results.values()), 0, ""))

    def test_a_missing_adapter_fails_that_runtime_and_names_it(self) -> None:
        self.fake.listed["copilot"] = ["alpha"]
        results = self.check()
        self.assertEqual(report.PASSED, results["codex"].status)
        self.assertEqual(report.FAILED, results["copilot"].status)
        self.assertIn("beta", results["copilot"].detail)
        self.assertIn("NOT FOUND", results["copilot"].detail)
        self.assertEqual(1, report.exit_code(list(results.values()), 1, COPILOT_FAILED))

    def test_a_refusal_that_asks_for_sign_in_is_skipped_with_its_reason(self) -> None:
        self.fake.errors["copilot"] = SIGN_IN
        results = self.check()
        self.assertEqual(report.SKIPPED, results["copilot"].status)
        self.assertIn("not logged in", results["copilot"].detail)
        self.assertEqual(report.PASSED, results["codex"].status)
        # deploy.py verify exits 1 for the runtime that cannot list; the skip explains it.
        self.assertEqual(0, report.exit_code(list(results.values()), 1, COPILOT_FAILED))

    def test_any_other_listing_error_fails(self) -> None:
        self.fake.errors["codex"] = "no answer within 120 seconds"
        results = self.check()
        self.assertEqual(report.FAILED, results["codex"].status)
        self.assertIn("no answer within 120 seconds", results["codex"].detail)
        self.assertEqual(1, report.exit_code(list(results.values()), 1, CODEX_FAILED))

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
        self.assertEqual(1, report.exit_code(results, 1, BOTH_FAILED))

    def test_a_failing_deploy_verify_with_nothing_skipped_fails_even_if_the_script_passed(self) -> None:
        results = list(self.check().values())
        self.assertEqual(1, report.exit_code(results, 1, COPILOT_FAILED))

    def test_a_skip_explains_only_a_verify_failure_for_the_runtime_it_skipped(self) -> None:
        # Codex asks for a sign-in here and Copilot finds every adapter, but deploy.py verify, a separate run, may
        # still fail Copilot: an outdated version, or a listing that failed or differed that time.
        self.fake.errors["codex"] = SIGN_IN
        results = list(self.check().values())
        self.assertEqual([report.SKIPPED, report.PASSED], [result.status for result in results])
        self.assertEqual(0, report.exit_code(results, 1, "Adapters: 2\n" + CODEX_FAILED))
        self.assertEqual(0, report.exit_code(results, 0, ""))
        for output in (
            COPILOT_FAILED,
            BOTH_FAILED,
            # A failure that names no runtime, such as an error before any was checked, is not explained by a skip.
            "ERROR: Cannot read the manifest.\n",
            "",
            "Verification failed for Some Other CLI.\n",
        ):
            with self.subTest(output=output):
                self.assertEqual(1, report.exit_code(results, 1, output))

    def test_a_shadowed_adapter_fails(self) -> None:
        original = self.fake.talk

        def talk(arguments, cwd, environment, requests, answered, timeout):
            lines = original(arguments, cwd, environment, requests, answered, timeout)
            if Path(arguments[0]).stem == "copilot":
                other = [{"name": "alpha", "path": str(self.home / ".copilot" / "skills" / "alpha"), "enabled": True}]
                return [json.dumps(other + json.loads(lines[0]))]
            return lines

        with mock.patch.object(self.fake, "talk", talk):
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
        self.assertIn(DEPLOY_FIRST, output)

    # -- command line ---------------------------------------------------------------------------------------

    def test_verify_prints_a_line_per_runtime_and_records_the_pass(self) -> None:
        self.fake.errors["copilot"] = SIGN_IN
        self.use_fake_runtimes()
        code, output = self.verify(deploy_exit=1, verify_output=COPILOT_FAILED)
        self.assertEqual(0, code, output)
        self.assertIn("PASSED codex", output)
        self.assertIn("SKIPPED copilot", output)
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual("first deploy", recorded[0]["label"])
        self.assertEqual(
            {"codex": "PASSED", "copilot": "SKIPPED"},
            {item["runtime"]: item["status"] for item in recorded[0]["results"]},
        )

    def test_verify_fails_a_verify_failure_its_log_does_not_explain(self) -> None:
        self.fake.errors["copilot"] = SIGN_IN
        self.use_fake_runtimes()
        self.assertEqual(1, self.verify(deploy_exit=1, verify_output=CODEX_FAILED)[0])
        # Without the log, nothing shows which runtime verify failed for.
        self.assertEqual(1, self.verify(deploy_exit=1, verify_output=None)[0])
        # A log written by PowerShell may start with a byte order mark.
        self.assertEqual(0, self.verify(deploy_exit=1, verify_output=BYTE_ORDER_MARK + COPILOT_FAILED)[0])

    def test_a_second_pass_is_appended_to_the_results(self) -> None:
        self.results_file.write_text(json.dumps([{"label": "first deploy", "results": []}]), encoding="utf-8")
        self.use_fake_runtimes()
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
        self.assertIn(DEPLOY_FIRST, output)

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

    # -- uninstall ------------------------------------------------------------------------------------------

    def deploy_everything(self) -> None:
        """Add what setUp leaves out: the runtime adapters' directories and a shared asset file the manifest owns."""
        for name in ADAPTERS:
            (self.paths.adapter_dest_dir / name).mkdir(parents=True)
            (self.paths.adapter_dest_dir / name / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
        (self.paths.dest_dir / SHARED_FILE).write_text("notes\n", encoding="utf-8")
        data = json.loads(self.paths.manifest_file.read_text(encoding="utf-8"))
        data["sources"][SOURCE_ID]["shared"] = {SHARED_FILE: {"hash": HASH, "role": "owner"}}
        self.paths.manifest_file.write_text(json.dumps(data), encoding="utf-8")

    def uninstall(self, *, keep: tuple[Path, ...] = ()) -> None:
        """Do what the uninstall does, leaving the paths in keep: remove every item, empty the source's manifest
        entry, which the uninstall keeps, and retain a backup, which it may."""
        for root in (self.paths.dest_dir, self.paths.adapter_dest_dir, self.paths.agent_dest_dir):
            for entry in root.iterdir():
                if entry == self.paths.manifest_file or entry in keep:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        backup = self.paths.dest_dir / ".backups" / "run-1" / "alpha"
        backup.mkdir(parents=True)
        (backup / "SKILL.md").write_text("# kept\n", encoding="utf-8")
        emptied: dict[str, dict[str, object]] = {"skills": {}, "shared": {}, "wrappers": {}, "agents": {}}
        self.paths.manifest_file.write_text(
            json.dumps({"manifest_version": manifest.MANIFEST_VERSION, "sources": {SOURCE_ID: emptied}}),
            encoding="utf-8",
        )

    def record_owned(self) -> Path:
        owned = self.results_file.parent / "owned.json"
        code, output = self.run_main(
            "owned", "--owned", str(owned), "--home", str(self.home), "--source", str(self.paths.source_dir)
        )
        self.assertEqual(0, code, output)
        return owned

    def check_leftovers(self, owned: Path) -> tuple[int, str, list[str]]:
        code, output = self.run_main(
            "leftovers",
            "--owned",
            str(owned),
            "--results",
            str(self.results_file),
            "--home",
            str(self.home),
            "--source",
            str(self.paths.source_dir),
        )
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        return code, output, next(item["left"] for item in recorded if item["label"] == "uninstall")

    def summary_of_record(self) -> str:
        return report.render_summary(json.loads(self.results_file.read_text(encoding="utf-8")), [], [])

    def test_owned_saves_every_owned_path_and_every_entry_where_items_deploy(self) -> None:
        self.deploy_everything()
        (self.paths.dest_dir / "unowned.txt").write_text("made by the run\n", encoding="utf-8")
        saved = json.loads(self.record_owned().read_text(encoding="utf-8"))
        expected = {
            self.paths.dest_dir / "alpha",
            self.paths.dest_dir / "beta",
            self.paths.dest_dir / SHARED_FILE,
            self.paths.dest_dir / "unowned.txt",
            self.paths.adapter_dest_dir / "alpha",
            self.paths.adapter_dest_dir / "beta",
            self.paths.agent_dest_dir / AGENT,
        }
        self.assertEqual(sorted(str(path) for path in expected), saved)

    def test_owned_with_nothing_deployed_is_an_error(self) -> None:
        self.deploy_everything()
        self.uninstall()
        owned = self.results_file.parent / "owned.json"
        code, output = self.run_main(
            "owned", "--owned", str(owned), "--home", str(self.home), "--source", str(self.paths.source_dir)
        )
        self.assertEqual(1, code)
        self.assertIn("Nothing is deployed in", output)
        self.assertIn(DEPLOY_FIRST, output)
        self.assertFalse(owned.exists())

    def test_a_clean_uninstall_leaves_nothing_but_the_manifest_and_retained_backups(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        self.uninstall()
        code, output, left = self.check_leftovers(owned)
        self.assertEqual((0, []), (code, left), output)
        self.assertIn("Nothing deployed is left", output)
        self.assertIn("### After the uninstall\n\nNothing deployed is left.", self.summary_of_record())

    def test_a_shared_file_left_after_the_uninstall_fails_and_is_named(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        shared = self.paths.dest_dir / SHARED_FILE
        self.uninstall(keep=(shared,))
        code, output, left = self.check_leftovers(owned)
        self.assertEqual(1, code)
        self.assertEqual([platform_support.normalize(shared)], left)
        self.assertIn(f"::error::left after uninstall: {platform_support.normalize(shared)}", output)
        self.assertIn(f"- Left: `{platform_support.normalize(shared)}`", self.summary_of_record())

    def test_a_directory_left_after_the_uninstall_fails_and_is_named(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        adapter = self.paths.adapter_dest_dir / "beta"
        self.uninstall(keep=(adapter,))
        code, output, left = self.check_leftovers(owned)
        self.assertEqual((1, [platform_support.normalize(adapter)]), (code, left), output)
        self.assertIn(f"- Left: `{platform_support.normalize(adapter)}`", self.summary_of_record())

    def test_an_entry_the_uninstall_added_where_items_deploy_fails(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        self.uninstall()
        stray = self.paths.dest_dir / "alpha.deploying-bak"
        stray.write_text("stale\n", encoding="utf-8")
        hidden = self.paths.agent_dest_dir / ".stray.md"
        hidden.write_text("stale\n", encoding="utf-8")
        code, output, left = self.check_leftovers(owned)
        self.assertEqual(1, code, output)
        self.assertEqual(sorted(platform_support.normalize(path) for path in (stray, hidden)), left)

    def test_an_item_the_manifest_still_owns_fails_even_when_its_file_is_gone(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        self.uninstall()
        data = json.loads(self.paths.manifest_file.read_text(encoding="utf-8"))
        data["sources"][SOURCE_ID]["shared"] = {SHARED_FILE: {"hash": HASH, "role": "owner"}}
        self.paths.manifest_file.write_text(json.dumps(data), encoding="utf-8")
        code, output, left = self.check_leftovers(owned)
        self.assertEqual(1, code, output)
        shared = platform_support.normalize(self.paths.dest_dir / SHARED_FILE)
        self.assertEqual([f"manifest source '{SOURCE_ID}' still owns shared asset {shared}"], left)

    def test_a_second_leftovers_check_replaces_the_first_record(self) -> None:
        self.deploy_everything()
        owned = self.record_owned()
        shared = self.paths.dest_dir / SHARED_FILE
        self.uninstall(keep=(shared,))
        self.check_leftovers(owned)
        shared.unlink()
        code, _, left = self.check_leftovers(owned)
        self.assertEqual((0, []), (code, left))
        recorded = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual(1, sum(item["label"] == "uninstall" for item in recorded))

    def test_leftovers_without_the_saved_paths_is_an_error(self) -> None:
        missing = self.results_file.parent / "missing.json"
        empty = self.results_file.parent / "empty.json"
        empty.write_text("[]", encoding="utf-8")
        for saved in (missing, empty):
            with self.subTest(saved=saved.name):
                code, output = self.run_main(
                    "leftovers",
                    "--owned",
                    str(saved),
                    "--results",
                    str(self.results_file),
                    "--home",
                    str(self.home),
                    "--source",
                    str(self.paths.source_dir),
                )
                self.assertEqual(1, code)
                self.assertIn("saved before the uninstall", output)

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
        runner = "Windows" if sys.platform == "win32" else "Linux"
        self.assertTrue(text.startswith(f"## Deployability on a fresh {runner} runner\n"), text)
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

    def run_results_command(self, command: str) -> tuple[int, str]:
        """Exercise each results reader with real temporary files and the fake runtime listings."""
        arguments = [
            command,
            "--results",
            str(self.results_file),
            "--home",
            str(self.home),
            "--source",
            str(self.paths.source_dir),
        ]
        if command in ("verify", "layout"):
            arguments += ["--label", "first deploy"]
        if command == "verify":
            arguments += [
                "--deploy-verify-exit",
                "0",
                "--deploy-verify-log",
                str(self.results_file.parent / "verify.log"),
            ]
        elif command == "leftovers":
            arguments += ["--owned", str(self.record_owned())]
        elif command == "summary":
            arguments += ["--no-probe", "--summary", str(self.summary_file)]
        return self.run_main(*arguments)

    def test_unreadable_results_are_contextual_errors_in_every_command(self) -> None:
        self.use_fake_runtimes()
        for content in (b"{not json", b"", b"\xff", b"{}"):
            for command in ("summary", "verify", "layout", "leftovers"):
                with self.subTest(content=content, command=command):
                    self.results_file.write_bytes(content)
                    self.summary_file.write_text("earlier summary\n", encoding="utf-8")
                    code, output = self.run_results_command(command)
                    self.assertEqual(1, code, output)
                    self.assertIn("ERROR:", output)
                    self.assertIn(str(self.results_file), output)
                    self.assertNotIn("FAILED", output)
                    self.assertEqual(content, self.results_file.read_bytes())
                    self.assertEqual("earlier summary\n", self.summary_file.read_text(encoding="utf-8"))

    def test_a_results_directory_is_a_contextual_error_in_every_command(self) -> None:
        self.use_fake_runtimes()
        self.results_file.mkdir()
        for command in ("summary", "verify", "layout", "leftovers"):
            with self.subTest(command=command):
                code, output = self.run_results_command(command)
                self.assertEqual(1, code, output)
                self.assertIn(f"ERROR: Cannot read results file: {self.results_file}:", output)
                self.assertNotIn("FAILED", output)
                self.assertTrue(self.results_file.is_dir())
                self.assertFalse(self.summary_file.exists())

    def test_results_permission_error_names_the_path_and_reason(self) -> None:
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("access denied")):
            code, output = self.run_results_command("summary")
        self.assertEqual(1, code, output)
        self.assertIn(f"ERROR: Cannot read results file: {self.results_file}: access denied", output)
        self.assertFalse(self.summary_file.exists())

    def test_missing_results_still_start_a_complete_report(self) -> None:
        self.use_fake_runtimes()
        self.deploy_everything()
        code, output = self.run_results_command("summary")
        self.assertEqual(0, code, output)
        self.assertFalse(self.results_file.exists())
        for command in ("verify", "layout"):
            code, output = self.run_results_command(command)
            self.assertEqual(0, code, output)
        owned = self.record_owned()
        self.uninstall()
        code, output, left = self.check_leftovers(owned)
        self.assertEqual((0, []), (code, left), output)
        code, output = self.run_results_command("summary")
        self.assertEqual(0, code, output)
        for expected in ("codex", "copilot", "claude", "Nothing deployed is left."):
            self.assertIn(expected, output)
        records = json.loads(self.results_file.read_text(encoding="utf-8"))
        self.assertEqual(["first deploy", "uninstall"], [item["label"] for item in records])

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
