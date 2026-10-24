"""The deployer's readers of its own state files and the source's JSON, held to one corpus of what they cannot read.

Each reader refuses a file that is not JSON it can read, such as one holding an integer past Python's digit limit or
lists nested past the recursion limit, and a directory where its file belongs, by name and before anything is planned
or changed: neither may escape as an unclassified failure, nor be read as no file at all.
"""

from __future__ import annotations

import json
import re

from harness import SOURCE_ID, DeployerTestCase

from deployer import discovery, platform_support, render, state_files
from deployer.errors import STATE_FILE, DeployError, see_recovery

# An integer past the digit limit, which json.loads raises ValueError for, and lists nested past the recursion limit,
# which it raises RecursionError for.
UNREADABLE = {
    "digits": '{"manifest_version": ' + "1" * 5000 + "}",
    "nesting": "[" * 100000 + "]" * 100000,
}
RUN_ID = "20260101-000000-bad1"


def shown(path: object) -> str:
    return re.escape(platform_support.normalize(str(path)))


class ReaderTests(DeployerTestCase):
    def test_json_past_the_limits_or_not_json_reads_as_none_and_json_as_its_value(self) -> None:
        for case, text in {**UNREADABLE, "not json": "{", "a lone surrogate": '"\\ud800'}.items():
            with self.subTest(case=case):
                self.assertIsNone(state_files.parse_json(text))
        self.assertEqual({"a": [1, 2]}, state_files.parse_json('{"a": [1, 2]}'))
        self.assertEqual(7, state_files.parse_json("7"))

    def test_a_file_is_present_nothing_is_absent_and_anything_else_is_refused_by_name(self) -> None:
        path = self.root / "state.json"
        self.assertFalse(state_files.is_present(path, "The state"))
        path.write_text("{}", encoding="utf-8")
        self.assertTrue(state_files.is_present(path, "The state"))
        path.unlink()
        path.mkdir()
        with self.assertRaises(DeployError) as raised:
            state_files.is_present(path, "The state", "Move it aside.", "Then retry.")
        self.assertEqual(
            (f"ERROR: The state at {platform_support.normalize(path)} is not a file.", "Move it aside.", "Then retry."),
            raised.exception.lines,
        )


class StateFileTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()

    def test_a_manifest_the_deployer_cannot_read_is_refused_by_name_with_the_step_to_take(self) -> None:
        self.deploy_ok("--all")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                self.write(self.manifest_file, text)
                for arguments in (("--all",), ("--all", "--dry-run")):
                    result = self.deploy_fails(*arguments, pattern=re.escape("ERROR: Manifest is malformed: "))
                    self.assertIn(see_recovery(STATE_FILE), result.output)
                    self.assertNotIn("FAILED", result.output)
                checked = self.run_cli("check")
                self.assertIn("ERROR: Manifest is malformed: ", checked.output)

    def test_a_directory_in_place_of_the_manifest_is_refused_before_a_dry_run_promises_a_fresh_install(self) -> None:
        self.manifest_file.mkdir(parents=True)
        refusal = f"ERROR: The manifest at {shown(self.manifest_file)} is not a file."
        for arguments in (("--all", "--dry-run"), ("--all",)):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, pattern=refusal)
                self.assertNotIn("FRESH INSTALL", result.output)
                self.assertNotIn("INSTALLED", result.output)
                self.assertIn(see_recovery(STATE_FILE), result.output)

    def test_a_directory_in_place_of_the_configuration_is_refused_by_deployment_and_configure(self) -> None:
        config_file = self.config_file()
        config_file.unlink()
        config_file.mkdir()
        refusal = f"ERROR: The configuration at {shown(config_file)} is not a file."
        result = self.deploy_fails("--all", "--dry-run", pattern=refusal)
        self.assertNotIn("No config found", result.output)
        self.assertIn(see_recovery(STATE_FILE), result.output)
        for arguments in ((), ("--reset",)):
            with self.subTest(arguments=arguments):
                configured = self.configure(*arguments, stdin=f"{self.repos}\n")
                self.assertEqual(1, configured.code, configured.output)
                self.assertRegex(configured.output, refusal)
                self.assertNotIn("REPOS_ROOT", configured.output)

    def test_a_journal_line_the_deployer_cannot_read_stops_recovery_and_keeps_the_run(self) -> None:
        self.deploy_ok("--all")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                journal = self.staging_run(RUN_ID) / "journal.jsonl"
                self.write(journal, text + "\n")
                result = self.deploy_fails("--all", pattern=f"Malformed journal entry in run {RUN_ID}")
                self.assertNotIn("the next deployment retries it", result.output)
                self.assertTrue(journal.is_file())
                dry = self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")
                self.assertNotIn("FAILED", dry.output)

    def test_a_directory_in_place_of_a_journal_stops_recovery_and_keeps_what_the_run_holds(self) -> None:
        self.deploy_ok("--all")
        run = self.staging_run(RUN_ID)
        (run / "journal.jsonl").mkdir()
        kept = run / "alpha"
        self.write(kept / "SKILL.md", "What the run moved aside")
        result = self.deploy_fails("--all", pattern=re.escape(f"ERROR: Run {RUN_ID}'s journal is not a file."))
        self.assertIn(f"Retaining {platform_support.normalize(run)} for manual inspection.", result.output)
        self.assertTrue((kept / "SKILL.md").is_file())
        self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: Run {RUN_ID} cannot be recovered")

    def test_lock_metadata_the_deployer_cannot_read_is_refused_with_the_step_to_take(self) -> None:
        self.deploy_ok("--all")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                self.lock_dir.mkdir(parents=True, exist_ok=True)
                self.write(self.lock_dir / "token", "old-token")
                self.write(self.lock_dir / "info.json", text)
                result = self.deploy_fails("--all", pattern=re.escape("ERROR: Lock metadata is malformed (no PID)."))
                lock = platform_support.normalize(self.lock_dir)
                self.assertIn(
                    f"If no deployment is running, the lock is stale: remove {lock}, then retry.", result.output
                )

    def test_source_json_and_metadata_the_deployer_cannot_read_are_refused_with_their_documented_lines(self) -> None:
        metadata = self.source / "deploy-meta" / "alpha.json"
        saved = metadata.read_text(encoding="utf-8")
        for case, text in UNREADABLE.items():
            with self.subTest(case=case, file="deploy-meta"):
                self.write(metadata, text)
                self.deploy_fails("--all", "--dry-run", pattern=re.escape("deploy-meta/alpha.json has an invalid"))
        self.write(metadata, saved)
        source_json = self.source / "source.json"
        document = json.loads(source_json.read_text(encoding="utf-8"))
        self.assertEqual(SOURCE_ID, document["id"])
        for case, text in UNREADABLE.items():
            with self.subTest(case=case, file="source.json"):
                self.write(source_json, text)
                self.deploy_fails("--all", "--dry-run", pattern=re.escape("ERROR: Invalid source ID format: null"))
        source_json.unlink()
        source_json.mkdir()
        self.deploy_fails("--all", "--dry-run", pattern=f"ERROR: source.json at {shown(source_json)} is not a file.")


class RenderedAndListedTests(DeployerTestCase):
    def test_rendered_json_and_toml_past_the_limits_fail_their_validation_by_name(self) -> None:
        with self.assertRaisesRegex(DeployError, "Rendered JSON validation failed: s/config.json"):
            render.render_file("s/config.json", b'{"root": {{X}}}', {"X": "1" * 5000})
        with self.assertRaisesRegex(DeployError, "Rendered TOML validation failed: s/config.toml"):
            render.render_file("s/config.toml", b"root = {{X}}\n", {"X": UNREADABLE["nesting"]})

    def test_a_runtime_listing_past_the_limits_is_refused_as_not_json(self) -> None:
        for case, text in UNREADABLE.items():
            with self.subTest(case=case):
                with self.assertRaisesRegex(discovery.ListingError, "its skill list is not JSON"):
                    discovery.parse_copilot(text)
                with self.assertRaisesRegex(discovery.ListingError, "its prompt input is not JSON"):
                    discovery.parse_codex_catalog([text])
