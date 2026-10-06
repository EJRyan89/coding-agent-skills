"""Every refusal a user can meet ends with the step to take; these tests pin each one's wording."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from unittest import mock

from harness import REPOSITORY_ROOT, DeployerTestCase, forward

from deployer import errors, fsops, platform_support
from deployer.errors import DeployError

ZERO_HASH = "sha256:" + "0" * 64
NAME_RULES = (
    "Rename it with only lowercase letters, digits, and hyphens, no leading or trailing hyphen, at most 64 characters, "
    "not a Windows device name such as con or nul, and, for a skill, without the reserved words anthropic or claude; "
    'see "Files" in docs/adding-a-skill.md.'
)
RESET = "Run 'python deploy.py configure --reset' to write a new configuration for this source."
SEE_OWNERSHIP = 'See "Ownership held by another source" in docs/recovery.md.'
SEE_RECOVERY_FAILED = 'See "When recovery fails" in docs/recovery.md.'
SEE_BACKUPS = 'See "Backups" in docs/recovery.md.'
SEE_LOCK = 'See "The deployment lock" in docs/recovery.md.'


def probe_returning(alive: bool, start_time: int | None):
    return lambda pid: platform_support.ProcessStatus(alive, start_time)


class RemedyTestCase(DeployerTestCase):
    def fixture(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()

    def assert_lines(self, output: str, *lines: str) -> None:
        """Each line appears whole, so a remedy cannot be cut short or run into the next message."""
        printed = output.splitlines()
        for line in lines:
            self.assertIn(line, printed, output)

    @property
    def lock_dir(self) -> Path:
        return self.home / ".claude" / "deployer" / ".deploy.lock.d"

    def staging_run(self, run_id: str) -> Path:
        directory = self.home / ".claude" / "deployer" / "staging" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory


class SourceRemedyTests(RemedyTestCase):
    def test_every_invalid_name_states_the_naming_rules(self) -> None:
        self.make_config()
        for name, message in (
            ("Alpha", "ERROR: Skill name 'Alpha' does not match naming grammar"),
            ("a" * 65, f"ERROR: Skill name '{'a' * 65}' exceeds 64 characters"),
            ("con", "ERROR: Skill name 'con' is a Windows reserved name"),
            (
                "claude-helper",
                "ERROR: Skill name 'claude-helper' contains the reserved word 'claude', "
                "which the Agent Skills frontmatter rules forbid",
            ),
            (
                "my-anthropic-tools",
                "ERROR: Skill name 'my-anthropic-tools' contains the reserved word 'anthropic', "
                "which the Agent Skills frontmatter rules forbid",
            ),
        ):
            with self.subTest(name=name):
                self.make_source_json()
                self.make_skill(name, "Test")
                self.assert_lines(self.deploy_fails("--all", pattern=re.escape(message)).output, message, NAME_RULES)
                self.remove_skill(name)
        self.make_skill("alpha", "Test")
        self.make_source_json(bundles={"Bad_Name": {"members": ["alpha"]}})
        result = self.deploy_fails("--all", pattern="Bundle name")
        self.assert_lines(result.output, "ERROR: Bundle name 'Bad_Name' is invalid", NAME_RULES)
        self.make_source_json()
        self.make_agent("Bad_Name")
        result = self.deploy_fails("--all", pattern="Agent name")
        self.assert_lines(result.output, "ERROR: Agent name 'Bad_Name' does not match naming grammar", NAME_RULES)

    def test_invalid_metadata_shape_states_the_expected_keys(self) -> None:
        self.fixture()
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"required_vars": "HOME"}))
        result = self.deploy_fails("--all", pattern="invalid metadata shape")
        self.assert_lines(
            result.output,
            "ERROR: deploy-meta/alpha.json has an invalid metadata shape",
            "It must be a JSON object whose required_vars, shared_deps, skill_deps, tools, and agent_deps, where "
            "present, are lists of strings, and whose selectable and opt_in, where present, are true or false. "
            "docs/adding-a-skill.md describes each key.",
        )


class ConfigurationRemedyTests(RemedyTestCase):
    def test_source_id_mismatch_names_configure_reset_which_clears_it(self) -> None:
        self.make_source_json("real/source")
        self.make_skill("alpha", "Test")
        self.repos.mkdir()
        self.write(self.config_file("real/source"), f"_source_id=wrong/source\nREPOS_ROOT={forward(self.repos)}\n")
        message = "ERROR: Config _source_id (wrong/source) does not match source.json (real/source)"
        self.assert_lines(self.deploy_fails("--all", pattern="does not match source.json").output, message, RESET)
        # configure reads the same file, so only the remedy's --reset gets past it.
        self.assert_lines(self.configure().output, message, RESET)
        self.assertEqual(0, self.configure("--reset", stdin=f"{forward(self.repos)}\n").code)
        self.deploy_ok("--all")

    def test_unrecognized_key_names_configure_reset(self) -> None:
        self.fixture()
        self.make_config(extra="GH_ORG=someone\n")
        result = self.deploy_fails("--all", pattern="not a recognized variable")
        self.assert_lines(result.output, "ERROR: Config key GH_ORG is not a recognized variable", RESET)

    def test_invalid_directory_names_configure(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\nREPOS_ROOT=C:/\n")
        result = self.deploy_fails("--all", pattern="must not be a filesystem root")
        self.assert_lines(result.output, "ERROR: REPOS_ROOT must not be a filesystem root (got: C:/)",
                          "Run 'python deploy.py configure' to change it.")


class OwnershipRemedyTests(RemedyTestCase):
    def test_item_owned_by_another_source_names_migrate_from_which_takes_it_over(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("alpha", "Source A skill")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.make_source_json("test/source-b")
        self.make_skill("alpha", "Source B skill")
        self.make_config("test/source-b")
        self.deploy_from(source_a, "--all")
        result = self.deploy_fails("--all", pattern="is owned by source")
        self.assert_lines(
            result.output,
            "ERROR: Skill 'alpha' is owned by source 'test/source-a'.",
            "To take it over, run 'python deploy.py --migrate-from test/source-a', then rerun this deployment.",
            SEE_OWNERSHIP,
        )
        self.deploy_ok("--migrate-from", "test/source-a")
        self.deploy_ok("--all")
        self.assertIn("Source B skill", self.skill_text("alpha"))

    def test_name_deployed_as_two_kinds_names_rename_or_deselect(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("alpha", "Source A skill")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.remove_skill("alpha")
        self.make_source_json("test/source-b", shared_assets={"alpha": "owner"})
        self.make_shared_asset("alpha", "Source B shared asset")
        self.make_skill("beta", "Source B skill", shared_deps=["alpha"])
        self.make_config("test/source-b")
        self.deploy_from(source_a, "--all")
        result = self.deploy_fails("--all", pattern="collides with")
        self.assert_lines(
            result.output,
            "ERROR: Shared asset 'alpha' collides with a skill owned by source 'test/source-a'.",
            "One name cannot be deployed as two kinds. Rename it in one source, or stop deploying it from "
            "'test/source-a'.",
            SEE_OWNERSHIP,
        )

    def test_migration_refusals_name_their_remedy(self) -> None:
        self.make_source_json("new/source")
        self.make_skill("alpha", "Alpha")
        self.make_config("new/source")
        result = self.deploy_fails("--migrate-from", "old/source", pattern="without an existing manifest")
        self.assert_lines(
            result.output,
            "Nothing is deployed in this home, so there is nothing to take over: deploy without --migrate-from.",
        )
        self.deploy_ok("--all")
        result = self.deploy_fails("--migrate-from", "old/source", pattern="is not present in the manifest")
        self.assert_lines(
            result.output,
            "ERROR: Migration source 'old/source' is not present in the manifest.",
            f'Name a source ID listed under "sources" in {forward(self.manifest_file)}.',
        )
        data = self.manifest()
        data["sources"]["old/source"] = {"skills": {"beta": {"hash": ZERO_HASH}}}
        self.make_skill("beta", "Beta")
        self.write_manifest(data)
        result = self.deploy_fails("--migrate-from", "old/source", pattern="destination is missing")
        self.assert_lines(
            result.output,
            "ERROR: Cannot migrate 'beta': destination is missing.",
            "Restore the copy the old source deployed, for example by deploying from that source again, "
            "then rerun --migrate-from.",
            SEE_OWNERSHIP,
        )


class RecoveryRemedyTests(RemedyTestCase):
    def test_failed_rollback_says_what_to_put_back_and_where(self) -> None:
        self.fixture()
        run = self.staging_run("20260101-000000-gone")
        self.write(
            run / "journal.jsonl",
            json.dumps({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                        "retain": False, "backup_hash": ZERO_HASH}) + "\n",
        )
        result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_lines(
            result.output,
            "  Run 20260101-000000-gone was not committed to the manifest, so put back the copies it replaced. "
            "A replaced copy sits beside its item as <name>.deploying-bak; a retained backup is under "
            ".backups/20260101-000000-gone/ in the same root.",
            f"  When no .deploying-bak remains, delete {forward(run)} and rerun with --dry-run. {SEE_RECOVERY_FAILED}",
            "ERROR: Recovery failed, so nothing was deployed.",
            f"Reconcile the run named above by hand, then rerun with --dry-run. {SEE_RECOVERY_FAILED}",
        )

    def test_failed_completion_says_to_keep_what_the_committed_run_installed(self) -> None:
        self.fixture()
        self.deploy_ok("--all")
        data = self.manifest()
        data["last_run_id"] = "20260101-000000-done"
        self.write_manifest(data)
        run = self.staging_run("20260101-000000-done")
        self.write(
            run / "journal.jsonl",
            json.dumps({"op": "install", "item": "ghost", "from": "staging/ghost", "to": "skills/ghost",
                        "staged_hash": ZERO_HASH}) + "\n",
        )
        result = self.deploy_fails("--all", pattern="Installed destination missing for ghost")
        self.assert_lines(
            result.output,
            "  Run 20260101-000000-done was committed to the manifest, so keep the copies it installed. "
            "A replaced copy sits beside its item as <name>.deploying-bak; a retained backup is under "
            ".backups/20260101-000000-done/ in the same root.",
        )

    def test_malformed_journal_gives_the_same_steps(self) -> None:
        self.fixture()
        run = self.staging_run("20260101-000000-bad1")
        self.write(run / "journal.jsonl", '{"op":"backup"\n')
        result = self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assert_lines(
            result.output,
            f"  When no .deploying-bak remains, delete {forward(run)} and rerun with --dry-run. {SEE_RECOVERY_FAILED}",
        )

    def test_failed_immediate_recovery_says_the_next_run_retries(self) -> None:
        self.fixture()
        with (
            mock.patch("deployer.pipeline._deploy", side_effect=DeployError("ERROR: synthetic failure")),
            mock.patch("deployer.journal.recover_incomplete", side_effect=[True, False]),
        ):
            result = self.deploy_fails("--all", pattern="Immediate recovery failed")
        self.assert_lines(
            result.output,
            "The next run reclaims the lock and retries recovery. If that fails too, reconcile the run by hand. "
            f"{SEE_RECOVERY_FAILED}",
        )


class BackupRemedyTests(RemedyTestCase):
    def test_leftover_transient_backup_points_at_backups(self) -> None:
        self.fixture()
        self.deploy_ok("--all")
        self.write(self.skills_dir / "alpha.deploying-bak" / "stale.txt", "stale\n")
        result = self.deploy_fails("--all", pattern="transient backup already exists")
        self.assert_lines(
            result.output,
            "Resolve or remove the stale backup after verifying its contents, then retry.",
            SEE_BACKUPS,
        )

    def test_existing_permanent_backup_says_to_move_it(self) -> None:
        self.fixture()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "local change\n")
        existing = self.root / "existing-backup"
        existing.mkdir()
        with mock.patch("deployer.journal.prepare_backup_destination", return_value=existing):
            result = self.deploy_fails("--all", "--force-item", "alpha", pattern="Permanent backup destination")
        self.assert_lines(
            result.output,
            f"ERROR: Permanent backup destination already exists: {forward(existing)}",
            "Move it out of the deployment root after checking its contents, then retry.",
            SEE_BACKUPS,
        )


class LockRemedyTests(RemedyTestCase):
    def write_lock(self, info: dict | None) -> None:
        self.lock_dir.mkdir(parents=True)
        if info is not None:
            self.write(self.lock_dir / "token", "old-token")
            self.write(self.lock_dir / "info.json", json.dumps(info))

    def test_running_deployment_says_to_wait(self) -> None:
        self.fixture()
        self.write_lock({"pid": 4242, "token": "old-token", "start_time": 7})
        result = self.deploy_fails("--all", pattern="Another deployment is running", probe=probe_returning(True, 7))
        self.assert_lines(result.output, "ERROR: Another deployment is running (PID 4242).",
                          "Wait for it to finish, then retry.")

    def test_unprovable_lock_says_when_to_remove_it(self) -> None:
        stale = (f"If no deployment is running, the lock is stale: remove {forward(self.lock_dir)}, then retry.",
                 SEE_LOCK)
        self.fixture()
        self.write_lock(None)
        self.assert_lines(self.deploy_fails("--all", pattern="no metadata").output,
                          "ERROR: Lock exists but has no metadata (may be initializing).", *stale)
        self.write(self.lock_dir / "info.json", json.dumps({"token": "x"}))
        self.assert_lines(self.deploy_fails("--all", pattern="no PID").output,
                          "ERROR: Lock metadata is malformed (no PID).", *stale)
        self.write(self.lock_dir / "info.json", json.dumps({"pid": 4242, "start_time": None}))
        self.assert_lines(
            self.deploy_fails("--all", pattern="cannot be verified", probe=probe_returning(True, 7)).output,
            "ERROR: Lock PID 4242 is alive but its process identity cannot be verified.", *stale,
        )
        self.write(self.lock_dir / "info.json", json.dumps({"pid": 4242, "start_time": 7}))
        self.assert_lines(
            self.deploy_fails("--all", pattern="start time cannot be read", probe=probe_returning(True, None)).output,
            "ERROR: Lock PID 4242 is alive but its start time cannot be read.", *stale,
        )

    def test_failed_lock_initialization_says_to_retry_or_remove_the_lock(self) -> None:
        self.fixture()
        real_write, real_remove = fsops.write_atomic, fsops.remove

        def failing_write(path: Path, content: bytes) -> None:
            if path.name == "info.json":
                raise OSError("synthetic write failure")
            real_write(path, content)

        with mock.patch("deployer.fsops.write_atomic", side_effect=failing_write):
            result = self.deploy_fails("--all", pattern="Failed to initialize deployment lock")
        self.assert_lines(result.output, "Retry the deployment.")

        def failing_remove(path: Path) -> None:
            if path == self.lock_dir:
                raise OSError("synthetic remove failure")
            real_remove(path)

        with (
            mock.patch("deployer.fsops.write_atomic", side_effect=failing_write),
            mock.patch("deployer.fsops.remove", side_effect=failing_remove),
        ):
            result = self.deploy_fails("--all", pattern="Failed to initialize deployment lock")
        self.assert_lines(
            result.output,
            f"Remove {forward(self.lock_dir)} after confirming no deployment is running.",
            SEE_LOCK,
        )

    def test_failed_stale_lock_reclaim_says_to_retry(self) -> None:
        self.fixture()
        self.write_lock({"pid": 999999, "token": "stale-token", "start_time": 1})
        real_move, real_make_directory = fsops.move, fsops.make_directory

        def failing_move(source: Path, destination: Path) -> None:
            if source == self.lock_dir:
                raise OSError("synthetic move failure")
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=failing_move):
            result = self.deploy_fails("--all", pattern="Failed to reclaim stale lock",
                                       probe=probe_returning(False, None))
        self.assert_lines(result.output, "Retry the deployment.", SEE_LOCK)

        calls = {"count": 0}

        def contended(path: Path) -> None:
            if path == self.lock_dir:
                calls["count"] += 1
                if calls["count"] == 2:
                    real_make_directory(path)
                    raise FileExistsError(str(path))
            real_make_directory(path)

        with mock.patch("deployer.fsops.make_directory", side_effect=contended):
            result = self.deploy_fails("--all", pattern="after stale reclaim", probe=probe_returning(False, None))
        self.assert_lines(result.output, "Retry the deployment.", SEE_LOCK)


class RecoveryGuideTests(unittest.TestCase):
    def test_every_section_a_refusal_names_is_a_heading_of_the_guide(self) -> None:
        self.assertEqual("docs/recovery.md", errors.RECOVERY_GUIDE)
        self.assertEqual(
            ("When recovery fails", "Backups", "The deployment lock", "Ownership held by another source"),
            errors.RECOVERY_SECTIONS,
        )
        headings = re.findall(r"^## (.+)$", (REPOSITORY_ROOT / "docs" / "recovery.md").read_text(encoding="utf-8"),
                              re.MULTILINE)
        for section in errors.RECOVERY_SECTIONS:
            self.assertIn(section, headings)

    def test_a_section_the_guide_lacks_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            errors.see_recovery("Recovering")

    def test_readme_links_the_guide(self) -> None:
        self.assertIn("[Recovery](docs/recovery.md)", (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
