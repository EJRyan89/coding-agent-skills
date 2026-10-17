from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from harness import (
    DEPLOY_COMMAND,
    FAILS_PARTWAY,
    RECOVERS,
    REPOSITORY_ROOT,
    SEE_RECOVERY_FAILED,
    ZERO_HASH,
    DeployerTestCase,
    Result,
    backup_entry,
    forward,
    install_entry,
    journal_line,
    make_directory_link,
    tree_state,
)

from deployer import fsops, hashing, journal, platform_support, render
from deployer.kinds import KINDS
from deployer.paths import CANARY_MARKER, CANARY_MARKER_TEXT


class RecoveryTestCase(DeployerTestCase):
    def deployed(self, *skills: tuple[str, str]) -> None:
        self.make_source_json()
        for name, content in skills:
            self.make_skill(name, content)
        self.make_config()
        self.deploy_ok("--all")

    def write_journal(self, run_id: str, *entries: dict) -> Path:
        path = self.staging_run(run_id) / "journal.jsonl"
        self.write(path, "".join(journal_line(entry) for entry in entries))
        return path

    def commit(self, run_id: str) -> None:
        data = self.manifest()
        data["last_run_id"] = run_id
        self.write_manifest(data)

    def sentinel(self) -> Path:
        path = self.home / ".claude" / "sentinel" / "data.txt"
        self.write(path, "precious data\n")
        return path


class RecoveryTests(RecoveryTestCase):
    def test_unknown_include_leaves_pending_journal_and_home_untouched(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "interrupted replacement\n")
        replacement = hashing.hash_path(self.skills_dir / "alpha")
        self.write_journal(
            "20260101-000000-fake",
            backup_entry("alpha", original),
            install_entry("alpha", replacement),
        )
        self.sentinel()
        (self.home / CANARY_MARKER).write_bytes(CANARY_MARKER_TEXT)
        for arguments in ((), ("--dry-run",), ("--canary-home", str(self.home))):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(
                    "--all", *arguments, "--include", "missing", code=2, pattern="--include names no bundle or skill"
                )
                self.assertEqual(
                    "\nERROR: --include names no bundle or skill in this source: missing\n"
                    f"Run '{DEPLOY_COMMAND} --help' for usage.\n\n",
                    result.output,
                )
        recovered = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-fake (rolling back)...", recovered.output)
        self.assertIn("Alpha content", self.skill_text("alpha"))

    def test_unknown_forced_item_leaves_pending_journal_and_home_untouched(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "interrupted replacement\n")
        replacement = hashing.hash_path(self.skills_dir / "alpha")
        self.write_journal(
            "20260101-000000-fake",
            backup_entry("alpha", original),
            install_entry("alpha", replacement),
        )
        self.sentinel()
        expected = (
            "\nERROR: --force-item names 'gamma', which this run does not install.\n"
            f"Name one of: alpha.\nRun '{DEPLOY_COMMAND} --help' for usage.\n\n"
        )
        for arguments in (("--all",), (), ("--all", "--dry-run"), ("--dry-run",)):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, "--force-item", "gamma", code=2, pattern="--force-item names")
                self.assertEqual(expected, result.output)
        recovered = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-fake (rolling back)...", recovered.output)
        self.assertIn("Alpha content", self.skill_text("alpha"))

    def test_uncommitted_journal_rolls_back(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "corrupted content\n")
        corrupted = hashing.hash_path(self.skills_dir / "alpha")
        self.write(
            self.staging_run("20260101-000000-fake") / "journal.jsonl",
            journal_line(
                {
                    "op": "backup",
                    "item": "alpha",
                    "from": "skills/alpha",
                    "to": "skills/alpha.deploying-bak",
                    "retain": False,
                    "backup_hash": original,
                }
            )
            + journal_line(
                {
                    "op": "install",
                    "item": "alpha",
                    "from": "staging/alpha",
                    "to": "skills/alpha",
                    "staged_hash": corrupted,
                }
            ),
        )
        result = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-fake (rolling back)...", result.output)
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertNotIn("corrupted", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())

    def assert_adapter_journal_rolls_back(self, staging_label: str) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.agents_dir / "alpha")
        (self.agents_dir / "alpha").rename(self.agents_dir / "alpha.deploying-bak")
        self.write(self.agents_dir / "alpha" / "SKILL.md", "corrupted adapter\n")
        corrupted = hashing.hash_path(self.agents_dir / "alpha")
        self.write(
            self.staging_run("20260101-000000-adapter") / "journal.jsonl",
            journal_line(
                {
                    "op": "backup",
                    "root": "agents",
                    "item": "alpha",
                    "from": "agents/alpha",
                    "to": "agents/alpha.deploying-bak",
                    "retain": False,
                    "backup_hash": original,
                }
            )
            + journal_line(
                {
                    "op": "install",
                    "root": "agents",
                    "item": "alpha",
                    "from": f"{staging_label}/alpha",
                    "to": "agents/alpha",
                    "staged_hash": corrupted,
                }
            ),
        )
        result = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-adapter (rolling back)...", result.output)
        self.assertNotIn("corrupted", (self.agents_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse((self.agents_dir / "alpha.deploying-bak").exists())

    def test_uncommitted_runtime_adapter_journal_rolls_back_across_roots(self) -> None:
        self.assert_adapter_journal_rolls_back("staging/.agent-adapters")

    def test_runtime_adapter_journal_from_before_the_staging_labels_matched_the_tree_still_rolls_back(self) -> None:
        self.assert_adapter_journal_rolls_back("staging-adapters")

    def test_runtime_adapter_journal_from_before_the_adapter_rename_still_rolls_back(self) -> None:
        self.assert_adapter_journal_rolls_back("staging-wrappers")

    def test_journal_staging_labels_name_the_staging_tree(self) -> None:
        # A reader of journal.jsonl during manual recovery finds a staged item where its "from" says: the run's
        # staging directory, with adapters and Claude Code agents in their own folders of it.
        run_id = "20260101-000000-label"
        record = journal.Journal(self.home / "journal.jsonl", run_id)
        record.create()
        record.install("claude", "alpha", ZERO_HASH)
        record.install("agents", "alpha", ZERO_HASH)
        record.install("claude-agents", "reviewer.md", ZERO_HASH)
        text = (self.home / "journal.jsonl").read_text(encoding="utf-8")
        entries = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(
            ["staging/alpha", "staging/.agent-adapters/alpha", "staging/.claude-agents/reviewer.md"],
            [entry["from"] for entry in entries],
        )
        self.assertTrue(all(journal.valid_entry(entry, run_id) for entry in entries))
        staging = self.home / "staging-run"
        render.Staged(
            skills={"alpha": {"SKILL.md": b"a"}}, adapters={"alpha": {"SKILL.md": b"b"}}, agents={"reviewer.md": b"c"}
        ).write(staging)
        self.assertEqual({"alpha", ".agent-adapters", ".claude-agents"}, {path.name for path in staging.iterdir()})
        self.assertTrue((staging / ".agent-adapters" / "alpha" / "SKILL.md").is_file())
        self.assertTrue((staging / ".claude-agents" / "reviewer.md").is_file())

    def test_journals_written_with_earlier_staging_labels_stay_valid(self) -> None:
        run_id = "20260101-000000-label"
        adapter = {"op": "install", "root": "agents", "item": "alpha", "to": "agents/alpha", "staged_hash": ZERO_HASH}
        agent = {
            "op": "install",
            "root": "claude-agents",
            "item": "reviewer.md",
            "to": "claude-agents/reviewer.md",
            "staged_hash": ZERO_HASH,
        }
        for label in ("staging-adapters", "staging-wrappers"):
            self.assertTrue(journal.valid_entry({**adapter, "from": f"{label}/alpha"}, run_id), label)
        self.assertTrue(journal.valid_entry({**agent, "from": "staging-claude-agents/reviewer.md"}, run_id))
        self.assertFalse(journal.valid_entry({**adapter, "from": "staging-other/alpha"}, run_id))
        self.assertFalse(journal.valid_entry({**adapter, "from": "staging/.claude-agents/alpha"}, run_id))
        self.assertFalse(journal.valid_entry({**agent, "from": "staging-wrappers/reviewer.md"}, run_id))
        self.assertFalse(
            journal.valid_entry(
                {**adapter, "root": "claude", "from": "staging-wrappers/alpha", "to": "skills/alpha"}, run_id
            )
        )

    def test_apply_time_failure_rolls_back_the_current_journal_immediately(self) -> None:
        self.deployed(("alpha", "Original alpha"), ("obsolete", "Original obsolete"))
        self.remove_skill("obsolete")
        self.make_skill("alpha", "Updated alpha")
        real_move = fsops.move

        def failing_move(source: Path, destination: Path) -> None:
            if "staging" in source.parts and source.name == "alpha" and destination == self.skills_dir / "alpha":
                raise OSError("synthetic install move failure")
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=failing_move):
            result = self.deploy_fails("--all", pattern="reconciling the current journal")
        self.assertIn("synthetic install move failure", result.output)
        self.assertIn("Original alpha", self.skill_text("alpha"))
        self.assertIn("Original obsolete", self.skill_text("obsolete"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertFalse((self.skills_dir / "obsolete.deploying-bak").exists())
        self.assertIn("alpha", self.owned("skills"))
        self.assertIn("obsolete", self.owned("skills"))
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_malformed_journal_blocks_deployment_and_is_retained(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        journal = self.staging_run("20260101-000000-bad1") / "journal.jsonl"
        self.write(journal, '{"op":"backup","item":"alpha","from":"skills/alpha","to":"skills/alpha.deploying-bak"\n')
        self.deploy_fails("--all", pattern="Malformed journal entry in run 20260101-000000-bad1")
        self.assertTrue(journal.is_file())

    def test_both_missing_state_blocks_rollback(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        journal = self.staging_run("20260101-000000-gone") / "journal.jsonl"
        self.write(
            journal,
            journal_line(
                {
                    "op": "backup",
                    "item": "alpha",
                    "from": "skills/alpha",
                    "to": "skills/alpha.deploying-bak",
                    "retain": False,
                    "backup_hash": ZERO_HASH,
                }
            )
            + journal_line(
                {
                    "op": "install",
                    "item": "alpha",
                    "from": "staging/alpha",
                    "to": "skills/alpha",
                    "staged_hash": ZERO_HASH,
                }
            ),
        )
        result = self.deploy_fails("--all", pattern="Both alpha and backup are missing during rollback")
        self.assertIn("ERROR: Recovery failed, so this deployment stopped before deploying anything.", result.output)
        self.assertTrue(journal.is_file())
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_committed_recovery_retains_backup_when_install_is_missing(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        run_id = self.manifest()["last_run_id"]
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        journal = self.staging_run(run_id) / "journal.jsonl"
        self.write(
            journal,
            journal_line(
                {
                    "op": "backup",
                    "item": "alpha",
                    "from": "skills/alpha",
                    "to": "skills/alpha.deploying-bak",
                    "retain": False,
                    "backup_hash": original,
                }
            )
            + journal_line(
                {
                    "op": "install",
                    "item": "alpha",
                    "from": "staging/alpha",
                    "to": "skills/alpha",
                    "staged_hash": ZERO_HASH,
                }
            ),
        )
        self.deploy_fails("--all", pattern="Install verification failed")
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())
        self.assertTrue(journal.is_file())

    def test_unknown_journal_operation_is_rejected(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        journal = self.staging_run("20260101-000000-unk1") / "journal.jsonl"
        self.write(journal, journal_line({"op": "unknown", "item": "victim"}))
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertTrue(journal.is_file())

    def test_journal_path_traversal_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        sentinel = self.sentinel()
        self.write(
            self.staging_run("20260101-000000-trav") / "journal.jsonl",
            journal_line(
                {
                    "op": "install",
                    "item": "../sentinel",
                    "from": "staging/../sentinel",
                    "to": "skills/../sentinel",
                    "staged_hash": ZERO_HASH,
                }
            ),
        )
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))

    def test_journal_backslash_traversal_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        sentinel = self.sentinel()
        self.write(
            self.staging_run("20260101-000000-bslash") / "journal.jsonl",
            journal_line(
                {
                    "op": "install",
                    "item": "..\\sentinel",
                    "from": "staging/..\\sentinel",
                    "to": "skills/..\\sentinel",
                    "staged_hash": ZERO_HASH,
                }
            ),
        )
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertTrue(sentinel.is_file())

    def test_journal_backup_destination_backslash_traversal_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        sentinel = self.sentinel()
        journal = self.staging_run("20260101-000000-bdest") / "journal.jsonl"
        self.write(
            journal,
            journal_line(
                {
                    "op": "backup",
                    "item": "alpha",
                    "from": "skills/alpha",
                    "to": "skills/alpha.deploying-bak",
                    "retain": True,
                    "backup_hash": hashing.hash_path(self.skills_dir / "alpha"),
                    "backup_dest": "..\\sentinel",
                }
            ),
        )
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))
        self.assertTrue(journal.is_file())

    def test_junction_backed_permanent_backup_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        run_id = "20260101-000000-junction"
        sentinel = self.sentinel()
        backup_hash = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        (self.skills_dir / ".backups").mkdir()
        make_directory_link(self.skills_dir / ".backups" / run_id, sentinel.parent)
        data = self.manifest()
        data["last_run_id"] = run_id
        self.write_manifest(data)
        self.write(
            self.staging_run(run_id) / "journal.jsonl",
            journal_line(
                {
                    "op": "backup",
                    "item": "alpha",
                    "from": "skills/alpha",
                    "to": "skills/alpha.deploying-bak",
                    "retain": True,
                    "backup_hash": backup_hash,
                    "backup_dest": f".backups/{run_id}/alpha",
                }
            ),
        )
        self.deploy_fails("--all", pattern="Backup path contains a symlink or junction")
        self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())


def denied(path: Path) -> PermissionError:
    """What Windows raises when another program, such as an antivirus scanner or an editor, holds a file open."""
    return PermissionError(13, "Access is denied", str(path))


def failing_at(function: str, path: Path, module: str = "deployer.fsops") -> mock._patch:
    """Patch one function so it raises PermissionError for one path and behaves normally for every other."""
    owner = {"deployer.fsops": fsops, "deployer.hashing": hashing}[module]
    real = getattr(owner, function)

    def fail(target: Path, *rest: object) -> object:
        if target == path:
            raise denied(path)
        return real(target, *rest)

    return mock.patch(f"{module}.{function}", side_effect=fail)


def path_method_failing_at(method: str, path: Path) -> mock._patch:
    real = getattr(Path, method)

    def fail(self: Path, *rest: object, **options: object) -> object:
        if self == path:
            raise denied(path)
        return real(self, *rest, **options)

    return mock.patch.object(Path, method, autospec=True, side_effect=fail)


class RecoveryFailureTests(RecoveryTestCase):
    """An OSError during recovery fails the step it hit, names the run and the path, and lets later runs recover."""

    HELD = "20260101-000000-held"
    NEXT = "20260101-000001-next"

    def later_run(self) -> Path:
        """A run sorted after the failing one, whose rollback has nothing to undo, so it always recovers."""
        self.write_journal(self.NEXT, install_entry("ghost", ZERO_HASH))
        return self.staging_run(self.NEXT)

    def rolled_back_fixture(self) -> Path:
        """An uncommitted run that replaced alpha; rolling it back removes the new copy and restores the old one."""
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed by the interrupted run\n")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        return self.write_journal(self.HELD, backup_entry("alpha", original), install_entry("alpha", installed))

    def committed_fixture(self, retain: bool) -> Path:
        """A committed run that replaced alpha and was interrupted before it cleared or kept the previous copy."""
        self.deployed(("alpha", "Alpha content"))
        self.write(self.skills_dir / "alpha.deploying-bak" / "SKILL.md", "previous alpha\n")
        backup_hash = hashing.hash_path(self.skills_dir / "alpha.deploying-bak")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        self.commit(self.HELD)
        return self.write_journal(
            self.HELD, backup_entry("alpha", backup_hash, retain, self.HELD), install_entry("alpha", installed)
        )

    def assert_recovery_failed(self, result_output: str, path: Path, committed: bool = False) -> None:
        run = self.staging_run(self.HELD)
        keep = "keep the copies it installed" if committed else "put back the copies it replaced"
        self.assert_lines(
            result_output,
            f"  ERROR: Recovery of run {self.HELD} failed at {forward(path)}: Access is denied",
            f"  Run {self.HELD} was {'' if committed else 'not '}committed to the manifest, so {keep}. "
            "A replaced copy sits beside its item as <name>.deploying-bak; a retained backup is under "
            f".backups/{self.HELD}/ in the same root.",
            f"  When no .deploying-bak remains, delete {forward(run)} and rerun with --dry-run. {SEE_RECOVERY_FAILED}",
            f"Recovering uncommitted run {self.NEXT} (rolling back)...",
            "ERROR: Recovery failed, so this deployment stopped before deploying anything.",
        )
        self.assertNotIn("Traceback", result_output)
        self.assertTrue((run / "journal.jsonl").is_file())
        self.assertFalse((self.home / ".claude" / "deployer" / "staging" / self.NEXT).exists())
        self.assertFalse(self.lock_dir.exists())

    def test_a_held_installed_copy_fails_its_rollback_step_only(self) -> None:
        self.rolled_back_fixture()
        self.later_run()
        alpha = self.skills_dir / "alpha"
        with failing_at("remove", alpha):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, alpha)
        self.assert_lines(result.output, "  WARNING: Both alpha and alpha.deploying-bak exist during rollback")
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())

    def test_an_unreadable_installed_copy_fails_its_rollback_step_only(self) -> None:
        self.rolled_back_fixture()
        self.later_run()
        alpha = self.skills_dir / "alpha"
        with failing_at("hash_path", alpha, "deployer.hashing"):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, alpha)

    def test_a_held_transient_backup_fails_finalization_of_a_committed_run(self) -> None:
        self.committed_fixture(retain=False)
        self.later_run()
        transient = self.skills_dir / "alpha.deploying-bak"
        with failing_at("remove", transient):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, transient, committed=True)
        self.assertTrue((transient / "SKILL.md").is_file())

    def test_a_backup_directory_that_cannot_be_created_fails_finalization_of_a_committed_run(self) -> None:
        self.committed_fixture(retain=True)
        self.later_run()
        backups = self.skills_dir / ".backups" / self.HELD
        with failing_at("make_directories", backups):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, backups, committed=True)
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())

    def test_an_unreadable_journal_fails_its_run_only(self) -> None:
        journal_file = self.rolled_back_fixture()
        self.later_run()
        with path_method_failing_at("read_text", journal_file):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, journal_file)
        self.assertIn("installed by the interrupted run", self.skill_text("alpha"))

    def test_a_staging_directory_that_cannot_be_removed_fails_its_run_only(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.write_journal(self.HELD, install_entry("ghost", ZERO_HASH))
        self.later_run()
        run = self.staging_run(self.HELD)
        with failing_at("remove", run):
            result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=RECOVERS)
        self.assert_recovery_failed(result.output, run)

    def test_an_unlistable_staging_root_fails_recovery_and_releases_the_lock(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        staging = self.home / ".claude" / "deployer" / "staging"
        with path_method_failing_at("iterdir", staging):
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_lines(
            result.output,
            f"  ERROR: Cannot list the staging runs in {forward(staging)}: Access is denied",
            f"  {SEE_RECOVERY_FAILED}",
            "ERROR: Recovery failed, so this deployment stopped before deploying anything.",
        )
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.lock_dir.exists())

    def test_journaled_runs_says_which_remaining_run_the_manifest_records_as_committed(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.assertEqual([], journal.journaled_runs(self.paths))
        self.write_journal("20260101-000000-done", install_entry("alpha", ZERO_HASH))
        self.write_journal("20260101-000000-kill", install_entry("alpha", ZERO_HASH))
        self.staging_run("20260101-000000-none")
        self.commit("20260101-000000-done")
        self.assertEqual(
            [("20260101-000000-done", True), ("20260101-000000-kill", False)], journal.journaled_runs(self.paths)
        )
        self.write(self.manifest_file, "not a manifest\n")
        self.assertIsNone(journal.journaled_runs(self.paths))

    def interrupted_startup_run(self, committed: bool) -> Path:
        """Deploy alpha, then leave the journal of a run killed after it replaced alpha, committed or not."""
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed by the interrupted run\n")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        journal_file = self.write_journal(
            "20260101-000000-kill", backup_entry("alpha", original), install_entry("alpha", installed)
        )
        if committed:
            data = self.manifest()
            for entry in data["sources"].values():
                entry["skills"]["alpha"]["hash"] = installed
            data["last_run_id"] = "20260101-000000-kill"
            self.write_manifest(data)
        return journal_file

    def assert_startup_recovery_stops(self, committed: bool, state: str) -> None:
        journal_file = self.interrupted_startup_run(committed)
        # Rolling back moves the backup into place; completing a committed run deletes it.
        operation = "remove" if committed else "move"
        real = getattr(fsops, operation)

        def failing(source: Path, *rest: Path) -> None:
            if source.name == "alpha.deploying-bak":
                raise RuntimeError("synthetic recovery failure")
            real(source, *rest)

        with mock.patch(f"deployer.fsops.{operation}", side_effect=failing):
            result = self.deploy_fails("--all", pattern="Unexpected RuntimeError", changes_home=RECOVERS)
        self.assertEqual(1, result.code)
        heading = (
            "committed run 20260101-000000-kill (completing finalization)"
            if committed
            else "uncommitted run 20260101-000000-kill (rolling back)"
        )
        self.assert_lines(result.output, f"Recovering {heading}...")
        self.assertTrue(
            result.output.endswith(
                "\nERROR: Unexpected RuntimeError: synthetic recovery failure\n"
                "Recovery stopped before it finished; the next deployment retries it before deploying anything.\n"
                f"{state}\n"
                f"{SEE_RECOVERY_FAILED}\n\n"
            ),
            result.output,
        )
        self.assertNotIn("nothing was deployed", result.output)
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.lock_dir.exists())
        self.assertTrue(journal_file.is_file())
        self.assertEqual(committed, self.manifest()["last_run_id"] == "20260101-000000-kill")
        result = self.deploy_ok("--all")
        self.assertIn(f"Recovering {heading}...", result.output)
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertFalse(journal_file.parent.exists())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())

    def test_an_unexpected_error_in_startup_recovery_of_an_uncommitted_run_says_it_is_rolled_back(self) -> None:
        self.assert_startup_recovery_stops(
            False,
            "Run 20260101-000000-kill was not committed to the manifest, so the manifest does not hold its changes; "
            "the next deployment rolls them back.",
        )

    def test_an_unexpected_error_in_startup_recovery_of_a_committed_run_says_its_changes_are_installed(self) -> None:
        self.assert_startup_recovery_stops(
            True,
            "Run 20260101-000000-kill was committed to the manifest, so its changes are installed and the manifest "
            "holds them; the next deployment finishes its cleanup.",
        )

    def test_an_os_error_while_reconciling_a_failed_deployment_keeps_the_original_error(self) -> None:
        self.deployed(("alpha", "Original alpha"), ("obsolete", "Original obsolete"))
        self.remove_skill("obsolete")
        self.make_skill("alpha", "Updated alpha")
        real_move = fsops.move

        def failing_move(source: Path, destination: Path) -> None:
            if "staging" in source.parts and source.name == "alpha" and destination == self.skills_dir / "alpha":
                raise OSError("synthetic install move failure")
            real_move(source, destination)

        transient = self.skills_dir / "obsolete.deploying-bak"
        with (
            mock.patch("deployer.fsops.move", side_effect=failing_move),
            failing_at("hash_path", transient, "deployer.hashing"),
        ):
            result = self.deploy_fails("--all", pattern="Immediate recovery failed", changes_home=FAILS_PARTWAY)
        self.assertIn("synthetic install move failure", result.output)
        self.assertRegex(result.output, rf"  ERROR: Recovery of run \S+ failed at {re.escape(forward(transient))}: ")
        self.assertNotIn("Traceback", result.output)
        self.assertTrue((transient / "SKILL.md").is_file())
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assertIn("Recovery complete.", result.output)
        self.assertIn("obsolete", self.report_groups(result.output, "DEPLOYED")["REMOVED"][0])
        self.assertFalse(transient.exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))


class ImmediateRecoveryExceptionTests(RecoveryTestCase):
    """Real CLI exit statuses after a deployment failure and a second failure while replaying its journal."""

    def assert_stopped_report(self, stderr: str, run_id: str, recovery_failure: str, committed: bool) -> None:
        """The report ends saying how recovery stopped and whether the run was committed to the manifest."""
        state = (
            f"Run {run_id} was committed to the manifest, so its changes are installed and the manifest holds "
            "them; the next deployment finishes its cleanup."
            if committed
            else f"Run {run_id} was not committed to the manifest, so the manifest does not hold its changes; "
            "the next deployment rolls them back."
        )
        if recovery_failure == "KeyboardInterrupt":
            stopped = "Cancelled during recovery; the next deployment finishes recovering before it deploys.\n"
            see = ""
        else:
            stopped = (
                "ERROR: Unexpected RuntimeError: synthetic immediate recovery failure\n"
                "Recovery stopped before it finished; the next deployment retries it before deploying anything.\n"
            )
            see = f"{SEE_RECOVERY_FAILED}\n"
        self.assertTrue(stderr.endswith(f"\n{stopped}{state}\n{see}\n"), stderr)

    def assert_stopped_recovery(
        self, recovery_failure: str, *, deployment_failure: str = "OSError", committed: bool = False
    ) -> None:
        self.deployed(("alpha", "Original alpha"), ("beta", "Original beta"))
        original_manifest = self.manifest_file.read_bytes()
        if committed:
            self.write(self.skills_dir / "alpha" / "SKILL.md", "precious local alpha\n")
        original_alpha = hashing.hash_path(self.skills_dir / "alpha")
        original_beta = hashing.hash_path(self.skills_dir / "beta")
        self.make_skill("alpha", "Updated alpha")
        self.make_skill("beta", "Updated beta")
        source_before = tree_state(self.source)
        home_before = tree_state(self.home)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                """
import io
import os
import sys
from pathlib import Path
from unittest import mock
from deployer import cli, errors, fsops
from deployer.paths import Paths

source, home = Path(sys.argv[1]), Path(sys.argv[2])
committed = sys.argv[3] == "committed"
deployment_error = {"OSError": OSError, "KeyboardInterrupt": KeyboardInterrupt}[sys.argv[4]]
recovery_error = {"RuntimeError": RuntimeError, "KeyboardInterrupt": KeyboardInterrupt}[sys.argv[5]]
real_move = fsops.move
failed = False

def failing_move(source, destination):
    global failed
    installing = "staging" in source.parts and destination == home / ".claude" / "skills" / "beta"
    preserving = ".backups" in destination.parts
    if not failed and (preserving if committed else installing):
        failed = True
        raise deployment_error("synthetic deployment failure")
    if failed and source.name == "alpha.deploying-bak":
        raise recovery_error("synthetic immediate recovery failure")
    real_move(source, destination)

os.environ[errors.DEBUG_VARIABLE] = ""
arguments = ["--all", "--force-item", "alpha"] if committed else ["--all"]
with mock.patch("deployer.fsops.move", side_effect=failing_move):
    raise SystemExit(cli.main(arguments, Paths(source, home), io.StringIO()))
""",
                str(self.source),
                str(self.home),
                "committed" if committed else "uncommitted",
                deployment_failure,
                recovery_failure,
            ],
            cwd=REPOSITORY_ROOT,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assert_run_unchanged(
            Result(result.returncode, result.stdout + result.stderr),
            self.source,
            source_before,
            self.home,
            home_before,
            changes_home=FAILS_PARTWAY,
        )
        self.assertEqual(130 if recovery_failure == "KeyboardInterrupt" else 1, result.returncode, result.stderr)
        self.assertIn("synthetic deployment failure", result.stderr)
        self.assertIn("Deployment failed; reconciling the current journal before exit...", result.stderr)
        runs = list(self.paths.staging_root.iterdir())
        self.assertEqual(1, len(runs))
        self.assert_stopped_report(result.stderr, runs[0].name, recovery_failure, committed)
        self.assertNotIn("nothing was deployed", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("DEPLOYED", result.stdout)
        self.assertFalse(self.lock_dir.exists())
        journal_file = runs[0] / "journal.jsonl"
        self.assertTrue(journal_file.is_file())
        self.assertEqual(original_alpha, hashing.hash_path(self.skills_dir / "alpha.deploying-bak"))
        if committed:
            self.assertEqual(runs[0].name, self.manifest()["last_run_id"])
            self.assertIn("Updated alpha", self.skill_text("alpha"))
        else:
            self.assertEqual(original_manifest, self.manifest_file.read_bytes())
            self.assertFalse((self.skills_dir / "alpha").exists())
            self.assertEqual(original_beta, hashing.hash_path(self.skills_dir / "beta"))
            self.assertFalse((self.skills_dir / "beta.deploying-bak").exists())
        retry = self.deploy_ok("--all")
        self.assertIn(f"Recovering {'committed' if committed else 'uncommitted'} run {runs[0].name}", retry.output)
        self.assertNotIn("Stale lock", retry.output)
        self.assertFalse(runs[0].exists())
        self.assertFalse(self.lock_dir.exists())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))
        self.assertIn("Updated beta", self.skill_text("beta"))
        if committed:
            backup = self.skills_dir / ".backups" / runs[0].name / "alpha"
            self.assertEqual(original_alpha, hashing.hash_path(backup))
            self.assertEqual("precious local alpha\n", (backup / "SKILL.md").read_text(encoding="utf-8"))

    def test_ctrl_c_during_immediate_rollback_exits_130_and_the_next_deployment_recovers(self) -> None:
        self.assert_stopped_recovery("KeyboardInterrupt")

    def test_a_second_ctrl_c_during_immediate_rollback_exits_130_and_releases_the_lock(self) -> None:
        self.assert_stopped_recovery("KeyboardInterrupt", deployment_failure="KeyboardInterrupt")

    def test_an_unexpected_immediate_rollback_error_is_reported_and_releases_the_lock(self) -> None:
        self.assert_stopped_recovery("RuntimeError")

    def test_ctrl_c_during_immediate_committed_recovery_preserves_the_backup_and_releases_the_lock(self) -> None:
        self.assert_stopped_recovery("KeyboardInterrupt", committed=True)

    def test_an_unexpected_immediate_committed_recovery_error_preserves_the_backup_and_releases_the_lock(self) -> None:
        self.assert_stopped_recovery("RuntimeError", committed=True)


class RecoveryBranchTests(RecoveryTestCase):
    """Each way recovery completes or stops, with the exact lines it prints and the tree it leaves."""

    RUN = "20260101-000000-done"

    def committed_with_kept_backup(self) -> tuple[Path, Path]:
        """A committed --force-item run interrupted before it moved the copy it kept to .backups/."""
        self.deployed(("alpha", "Alpha content"))
        self.write(self.skills_dir / "alpha.deploying-bak" / "SKILL.md", "previous alpha\n")
        backup_hash = hashing.hash_path(self.skills_dir / "alpha.deploying-bak")
        self.commit(self.RUN)
        journal_file = self.write_journal(
            self.RUN,
            backup_entry("alpha", backup_hash, retain=True, run_id=self.RUN),
            install_entry("alpha", hashing.hash_path(self.skills_dir / "alpha")),
        )
        return journal_file, self.skills_dir / ".backups" / self.RUN / "alpha"

    def assert_completion_stopped(self, journal_file: Path, warning: str, changes_home: str = "") -> None:
        result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=changes_home)
        self.assert_lines(
            result.output,
            f"Recovering committed run {self.RUN} (completing finalization)...",
            warning,
            f"  ERROR: Recovery has unreconciled entries. Journal retained at: {forward(journal_file)}",
            "ERROR: Recovery failed, so this deployment stopped before deploying anything.",
        )
        self.assertNotIn("Preserved backup", result.output)
        self.assertTrue(journal_file.is_file())
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertFalse(self.lock_dir.exists())

    def test_a_run_killed_after_its_commit_is_completed_and_its_kept_backup_preserved(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.write(self.skills_dir / "alpha" / "SKILL.md", "edited by hand\n")
        self.make_skill("alpha", "Updated alpha")
        with mock.patch("deployer.pipeline._finalize", side_effect=SystemExit(1)), self.assertRaises(SystemExit):
            self.deploy("--all", "--force-item", "alpha", changes_home=FAILS_PARTWAY)
        run_id = self.manifest()["last_run_id"]
        self.assertTrue((self.skills_dir / "alpha.deploying-bak").is_dir())
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assert_lines(
            result.output,
            f"Recovering committed run {run_id} (completing finalization)...",
            f"  Preserved backup: alpha -> .backups/{run_id}/alpha",
            "  Recovery complete.",
        )
        self.assertEqual("edited by hand\n", (self.skills_dir / ".backups" / run_id / "alpha" / "SKILL.md").read_text())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))
        self.assertFalse((self.home / ".claude" / "deployer" / "staging" / run_id).exists())

    def test_a_kept_backup_already_preserved_completes_the_run(self) -> None:
        _, destination = self.committed_with_kept_backup()
        destination.parent.mkdir(parents=True)
        (self.skills_dir / "alpha.deploying-bak").rename(destination)
        result = self.deploy_ok("--all")
        self.assert_lines(result.output, f"Recovering committed run {self.RUN} (completing finalization)...")
        self.assertNotIn("Preserved backup", result.output)
        self.assertEqual("previous alpha\n", (destination / "SKILL.md").read_text(encoding="utf-8"))

    def test_both_a_transient_and_a_permanent_backup_stop_completion(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        self.write(destination / "SKILL.md", "previous alpha\n")
        self.assert_completion_stopped(journal_file, "  WARNING: Both transient and permanent backups exist for alpha")
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())
        self.assertTrue((destination / "SKILL.md").is_file())

    def test_a_missing_backup_stops_completion(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        shutil.rmtree(self.skills_dir / "alpha.deploying-bak")
        self.assert_completion_stopped(journal_file, "  WARNING: Cannot find backup for alpha", RECOVERS)
        self.assertFalse(destination.exists())

    def test_a_changed_transient_backup_stops_completion(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        self.write(self.skills_dir / "alpha.deploying-bak" / "SKILL.md", "changed after the run\n")
        self.assert_completion_stopped(journal_file, "  WARNING: Backup hash mismatch for alpha", RECOVERS)
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())
        self.assertFalse(destination.exists())

    def test_a_changed_permanent_backup_stops_completion(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        destination.parent.mkdir(parents=True)
        (self.skills_dir / "alpha.deploying-bak").rename(destination)
        self.write(destination / "SKILL.md", "changed after the run\n")
        self.assert_completion_stopped(
            journal_file, f"  WARNING: Permanent backup hash mismatch for alpha at .backups/{self.RUN}/alpha"
        )
        self.assertEqual("changed after the run\n", (destination / "SKILL.md").read_text(encoding="utf-8"))

    def test_an_installed_copy_changed_after_a_committed_run_stops_completion_and_keeps_both_copies(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        installed = hashing.hash_path(self.skills_dir / "alpha")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "edited after the run\n")
        edited = hashing.hash_path(self.skills_dir / "alpha")
        self.assertNotEqual(installed, edited)
        self.assert_completion_stopped(
            journal_file, f"  WARNING: Installed alpha hash mismatch (expected {installed}, got {edited})"
        )
        self.assertIn("edited after the run", self.skill_text("alpha"))
        self.assertEqual("previous alpha\n", (self.skills_dir / "alpha.deploying-bak" / "SKILL.md").read_text())
        self.assertFalse(destination.exists())

    def uncommitted_replacement(self) -> Path:
        """An uncommitted run that moved alpha aside and installed a new copy, then stopped before its commit."""
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed by the interrupted run\n")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        return self.write_journal(self.RUN, backup_entry("alpha", original), install_entry("alpha", installed))

    def assert_rollback_stopped(self, journal_file: Path, *warnings: str, changes_home: str = "") -> None:
        result = self.deploy_fails("--all", pattern="Recovery failed", changes_home=changes_home)
        self.assert_lines(
            result.output,
            f"Recovering uncommitted run {self.RUN} (rolling back)...",
            *warnings,
            f"  ERROR: Recovery has unreconciled entries. Journal retained at: {forward(journal_file)}",
            "ERROR: Recovery failed, so this deployment stopped before deploying anything.",
        )
        self.assertNotIn("Recovery complete", result.output)
        self.assertTrue(journal_file.is_file())
        self.assertFalse(self.lock_dir.exists())

    def test_an_installed_copy_changed_after_an_uncommitted_run_is_not_rolled_back(self) -> None:
        journal_file = self.uncommitted_replacement()
        self.append(self.skills_dir / "alpha" / "SKILL.md", "edited after the run\n")
        self.assert_rollback_stopped(
            journal_file,
            "  WARNING: Cannot rollback install of alpha (hash mismatch)",
            "  WARNING: Both alpha and alpha.deploying-bak exist during rollback",
        )
        self.assertEqual(
            "installed by the interrupted run\nedited after the run\n",
            (self.skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertIn("Alpha content", (self.skills_dir / "alpha.deploying-bak" / "SKILL.md").read_text())

    def test_an_installed_copy_that_appears_while_rollback_reads_it_is_not_removed(self) -> None:
        journal_file = self.uncommitted_replacement()
        alpha = self.skills_dir / "alpha"
        read = journal._hash_existing

        def appearing(path: Path) -> str | None:
            # Absent when hashed, present when checked: something wrote it between the two reads.
            return None if path == alpha else read(path)

        with mock.patch("deployer.journal._hash_existing", side_effect=appearing):
            self.assert_rollback_stopped(
                journal_file,
                "  WARNING: Unexpected state for alpha during rollback",
                "  WARNING: Both alpha and alpha.deploying-bak exist during rollback",
            )
        self.assertEqual("installed by the interrupted run\n", (alpha / "SKILL.md").read_text(encoding="utf-8"))
        self.assertIn("Alpha content", (self.skills_dir / "alpha.deploying-bak" / "SKILL.md").read_text())

    def test_a_backup_that_cannot_be_moved_back_stops_rollback_and_the_next_deployment_restores_it(self) -> None:
        journal_file = self.uncommitted_replacement()
        transient = self.skills_dir / "alpha.deploying-bak"
        with failing_at("move", transient):
            self.assert_rollback_stopped(
                journal_file, "  WARNING: Failed to restore backup for alpha", changes_home=RECOVERS
            )
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertIn("Alpha content", (transient / "SKILL.md").read_text(encoding="utf-8"))
        result = self.deploy_ok("--all")
        self.assert_lines(
            result.output, f"Recovering uncommitted run {self.RUN} (rolling back)...", "  Recovery complete."
        )
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertFalse(transient.exists())
        self.assertFalse(journal_file.exists())

    def test_an_uncommitted_run_that_preserved_a_backup_puts_it_back(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        destination = self.skills_dir / ".backups" / self.RUN / "alpha"
        destination.parent.mkdir(parents=True)
        (self.skills_dir / "alpha").rename(destination)
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed by the interrupted run\n")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        preserve = {
            "op": "preserve",
            "root": "claude",
            "item": "alpha",
            "from": "skills/alpha.deploying-bak",
            "to": f".backups/{self.RUN}/alpha",
        }
        self.write_journal(
            self.RUN,
            backup_entry("alpha", original, retain=True, run_id=self.RUN),
            install_entry("alpha", installed),
            preserve,
        )
        result = self.deploy_ok("--all")
        self.assert_lines(
            result.output, f"Recovering uncommitted run {self.RUN} (rolling back)...", "  Recovery complete."
        )
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertFalse(destination.exists())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())

    def test_items_already_deleted_by_hand_lose_their_ownership(self) -> None:
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_shared_asset("shared.md")
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha", shared_deps=["shared.md"], agent_deps=["reviewer"])
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        shutil.rmtree(self.skills_dir / "beta")
        shutil.rmtree(self.agents_dir / "alpha")
        (self.skills_dir / "shared.md").unlink()
        (self.claude_agents_dir / "reviewer.md").unlink()
        preview = self.report_groups(self.deploy_ok("--dry-run", stdin="none\n").output, "DRY RUN")
        self.assertEqual(
            [
                "alpha (runtime adapter, already absent)",
                "beta (already absent)",
                "reviewer.md (agent, already absent)",
                "shared.md (shared asset, already absent)",
            ],
            preview["DROP OWNERSHIP"],
        )
        deployed = self.report_groups(self.deploy_ok(stdin="none\n").output, "DEPLOYED")
        self.assertEqual(
            [
                "alpha (runtime adapter, already absent)",
                "beta (already absent)",
                "reviewer.md (agent, already absent)",
                "shared.md (shared asset, already absent)",
            ],
            deployed["DROPPED OWNERSHIP"],
        )
        self.assertEqual(
            ["alpha (deselected or absent from source)", "beta (runtime adapter, obsolete)"], deployed["REMOVED"]
        )
        for kind in ("skills", "shared", "wrappers", "agents"):
            self.assertEqual({}, self.manifest()["sources"]["test/skills"].get(kind, {}), kind)

    def test_an_unexpected_exception_is_named_and_the_run_rolled_back(self) -> None:
        self.deployed(("alpha", "Original alpha"))
        self.make_skill("alpha", "Updated alpha")
        real_move = fsops.move

        def failing_move(source: Path, destination: Path) -> None:
            if "staging" in source.parts and destination == self.skills_dir / "alpha":
                raise RuntimeError("synthetic defect")
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=failing_move):
            result = self.deploy_fails("--all", pattern="Unexpected")
        self.assertEqual(1, result.code)
        self.assert_lines(
            result.output,
            "ERROR: Unexpected RuntimeError: synthetic defect",
            "Deployment failed; reconciling the current journal before exit...",
            "  Recovery complete.",
        )
        self.assertIn("Original alpha", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertEqual([], list((self.home / ".claude" / "deployer" / "staging").iterdir()))
        self.assertFalse(self.lock_dir.exists())

    def test_a_finalization_failure_after_the_commit_is_completed_before_exit(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.write(self.skills_dir / "alpha" / "SKILL.md", "edited by hand\n")
        self.make_skill("alpha", "Updated alpha")
        real_move = fsops.move
        state = {"failed": False}

        def failing_move(source: Path, destination: Path) -> None:
            if ".backups" in destination.parts and not state["failed"]:
                state["failed"] = True
                raise OSError("synthetic backup move failure")
            real_move(source, destination)

        with mock.patch("deployer.fsops.move", side_effect=failing_move):
            result = self.deploy_fails(
                "--all", "--force-item", "alpha", pattern="synthetic backup move failure", changes_home=FAILS_PARTWAY
            )
        run_id = self.manifest()["last_run_id"]
        self.assert_lines(
            result.output,
            "ERROR: Unexpected OSError: synthetic backup move failure",
            "Deployment failed; reconciling the current journal before exit...",
            f"Recovering committed run {run_id} (completing finalization)...",
            f"  Preserved backup: alpha -> .backups/{run_id}/alpha",
            "  Recovery complete.",
        )
        self.assertEqual("edited by hand\n", (self.skills_dir / ".backups" / run_id / "alpha" / "SKILL.md").read_text())
        self.assertIn("Updated alpha", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertFalse(self.lock_dir.exists())


class UnkeptBackupTests(RecoveryTestCase):
    """A backup the run does not keep is deleted only while it matches the hash the plan recorded; otherwise kept."""

    RUN = "20260101-000000-unkp"
    EDIT = "edited during the deployment\n"

    def edited_before_its_move(self, item: str) -> mock._patch:
        """Edit an item after the plan read its hash and just before the move that makes it a transient backup."""
        real_move = fsops.move

        def move(source: Path, destination: Path) -> None:
            if destination == self.skills_dir / f"{item}.deploying-bak":
                self.write(source / "SKILL.md", self.EDIT)
            real_move(source, destination)

        return mock.patch("deployer.fsops.move", side_effect=move)

    def assert_kept(self, output: str, item: str, run_id: str) -> None:
        self.assert_lines(
            output,
            f"WARNING: {item} changed after this deployment planned it, so its previous copy was kept at "
            f".backups/{run_id}/{item} instead of being deleted.",
            "BACKED UP (1):",
            f"  {item}: .backups/{run_id}/{item}",
        )
        self.assertEqual(self.EDIT, (self.skills_dir / ".backups" / run_id / item / "SKILL.md").read_text())
        self.assertFalse((self.skills_dir / f"{item}.deploying-bak").exists())

    def test_an_update_keeps_a_copy_edited_between_the_plan_and_the_move(self) -> None:
        self.deployed(("alpha", "Original alpha"))
        self.make_skill("alpha", "Updated alpha")
        with self.edited_before_its_move("alpha"):
            result = self.deploy_ok("--all")
        self.assert_kept(result.output, "alpha", self.manifest()["last_run_id"])
        self.assertIn("Updated alpha", self.skill_text("alpha"))

    def test_a_removal_keeps_a_copy_edited_between_the_plan_and_the_move(self) -> None:
        self.deployed(("alpha", "Alpha content"), ("obsolete", "Original obsolete"))
        self.remove_skill("obsolete")
        with self.edited_before_its_move("obsolete"):
            result = self.deploy_ok("--all")
        self.assert_kept(result.output, "obsolete", self.manifest()["last_run_id"])
        self.assertFalse((self.skills_dir / "obsolete").exists())
        self.assertNotIn("obsolete", self.owned("skills"))

    def test_an_unchanged_copy_is_deleted(self) -> None:
        self.deployed(("alpha", "Original alpha"))
        self.make_skill("alpha", "Updated alpha")
        result = self.deploy_ok("--all")
        self.assertNotIn("BACKED UP", result.output)
        self.assertNotIn("WARNING", result.output)
        self.assertFalse((self.skills_dir / ".backups").exists())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())

    def test_a_run_killed_while_keeping_an_edited_copy_keeps_it_on_recovery(self) -> None:
        self.deployed(("alpha", "Original alpha"))
        self.make_skill("alpha", "Updated alpha")
        with (
            self.edited_before_its_move("alpha"),
            self.killed_while_journaling("preserve", "alpha"),
            self.assertRaises(SystemExit),
        ):
            self.deploy("--all", changes_home=FAILS_PARTWAY)
        run_id = self.manifest()["last_run_id"]
        self.assertEqual(self.EDIT, (self.skills_dir / "alpha.deploying-bak" / "SKILL.md").read_text())
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assert_lines(
            result.output,
            f"Recovering committed run {run_id} (completing finalization)...",
            f"  WARNING: alpha changed after run {run_id} planned it, so its previous copy is kept at "
            f".backups/{run_id}/alpha instead of being deleted.",
            f"  Preserved backup: alpha -> .backups/{run_id}/alpha",
            "  Recovery complete.",
        )
        self.assertEqual(self.EDIT, (self.skills_dir / ".backups" / run_id / "alpha" / "SKILL.md").read_text())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))

    def committed_with_unkept_backup(self, changed: bool) -> Path:
        """A committed run interrupted before it deleted the backup of an item it updated, edited if changed."""
        self.deployed(("alpha", "Alpha content"))
        transient = self.skills_dir / "alpha.deploying-bak"
        self.write(transient / "SKILL.md", "previous alpha\n")
        backup_hash = hashing.hash_path(transient)
        if changed:
            self.write(transient / "SKILL.md", self.EDIT)
        self.commit(self.RUN)
        return self.write_journal(
            self.RUN,
            backup_entry("alpha", backup_hash),
            install_entry("alpha", hashing.hash_path(self.skills_dir / "alpha")),
        )

    def test_committed_recovery_deletes_an_unchanged_backup(self) -> None:
        self.committed_with_unkept_backup(changed=False)
        result = self.deploy_ok("--all")
        self.assert_lines(result.output, f"Recovering committed run {self.RUN} (completing finalization)...")
        self.assertNotIn("Preserved backup", result.output)
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertFalse((self.skills_dir / ".backups").exists())

    def test_committed_recovery_keeps_a_changed_backup(self) -> None:
        self.committed_with_unkept_backup(changed=True)
        result = self.deploy_ok("--all")
        self.assert_lines(
            result.output,
            f"  WARNING: alpha changed after run {self.RUN} planned it, so its previous copy is kept at "
            f".backups/{self.RUN}/alpha instead of being deleted.",
            f"  Preserved backup: alpha -> .backups/{self.RUN}/alpha",
            "  Recovery complete.",
        )
        self.assertEqual(self.EDIT, (self.skills_dir / ".backups" / self.RUN / "alpha" / "SKILL.md").read_text())
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())

    def test_a_changed_backup_whose_permanent_destination_exists_stops_completion(self) -> None:
        journal_file = self.committed_with_unkept_backup(changed=True)
        destination = self.skills_dir / ".backups" / self.RUN / "alpha"
        self.write(destination / "SKILL.md", "already there\n")
        result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_lines(result.output, "  WARNING: Both transient and permanent backups exist for alpha")
        self.assertTrue(journal_file.is_file())
        self.assertEqual(self.EDIT, (self.skills_dir / "alpha.deploying-bak" / "SKILL.md").read_text())
        self.assertEqual("already there\n", (destination / "SKILL.md").read_text())


class TornJournalTests(RecoveryTestCase):
    """A last journal line with no newline was cut off before its change began, so recovery drops it."""

    def staging_runs(self) -> list[Path]:
        return sorted((self.home / ".claude" / "deployer" / "staging").iterdir())

    def test_a_run_killed_while_journaling_an_install_is_rolled_back(self) -> None:
        self.deployed(("alpha", "Original alpha"))
        self.make_skill("alpha", "Updated alpha")
        with self.killed_while_journaling("install", "alpha"), self.assertRaises(SystemExit):
            self.deploy("--all", changes_home=FAILS_PARTWAY)
        [run] = self.staging_runs()
        text = (run / "journal.jsonl").read_text(encoding="utf-8")
        torn = text.split("\n")[-1]
        self.assertTrue(torn.startswith('{"op":"install"'), text)
        self.assertTrue((self.skills_dir / "alpha.deploying-bak").is_dir())
        self.assertFalse((self.skills_dir / "alpha").exists())
        preview = self.deploy_ok("--all", "--dry-run")
        self.assertIn(f"Pending recovery: the next deployment will recover run {run.name}.", preview.output)
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assert_lines(
            result.output,
            f"  WARNING: Dropped the unterminated last line of run {run.name}'s journal; the run stopped while "
            f"writing it, before the change it records: {torn}",
            f"Recovering uncommitted run {run.name} (rolling back)...",
            "  Recovery complete.",
        )
        self.assertIn("Updated alpha", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak").exists())
        self.assertFalse(run.exists())

    def test_a_journal_holding_only_a_torn_line_is_removed(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.make_skill("alpha", "Updated alpha")
        with self.killed_while_journaling("backup", "alpha"), self.assertRaises(SystemExit):
            self.deploy("--all", changes_home=FAILS_PARTWAY)
        [run] = self.staging_runs()
        self.assertIn("Alpha content", self.skill_text("alpha"))
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assertIn(f"Dropped the unterminated last line of run {run.name}'s journal", result.output)
        self.assertFalse(run.exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))

    def test_a_torn_line_before_the_last_still_stops_recovery(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        run_id = "20260101-000000-torn"
        original = hashing.hash_path(self.skills_dir / "alpha")
        torn = '{"op":"install","item":"alpha"'
        journal_file = self.staging_run(run_id) / "journal.jsonl"
        self.write(journal_file, journal_line(backup_entry("alpha", original)) + torn + "\n" + torn)
        preview = self.deploy_fails("--all", "--dry-run", pattern="cannot be recovered automatically")
        self.assertIn(f"ERROR: Run {run_id} cannot be recovered automatically", preview.output)
        result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_lines(result.output, f"  ERROR: Malformed journal entry in run {run_id}: {torn}")
        self.assertNotIn("Dropped the unterminated", result.output)
        self.assertTrue(journal_file.is_file())
        self.assertIn("Alpha content", self.skill_text("alpha"))


class DryRunRecoveryTests(RecoveryTestCase):
    """A dry run after an interrupted deployment says what the next deployment will recover instead of planning."""

    def interrupted(self, run_id: str) -> Path:
        """An uncommitted run killed after it moved alpha aside and installed its replacement."""
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "installed by the interrupted run\n")
        installed = hashing.hash_path(self.skills_dir / "alpha")
        return self.write_journal(run_id, backup_entry("alpha", original), install_entry("alpha", installed))

    def test_a_dry_run_names_the_run_the_next_deployment_will_recover(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        journal_file = self.interrupted("20260101-000000-kill")
        result = self.deploy_ok("--all", "--dry-run")
        self.assertIn(
            "\nPending recovery: the next deployment will recover run 20260101-000000-kill.\n"
            "The dry run stops here: recovery changes what is installed, so a preview before it would be wrong.\n"
            'See "Interrupted deployments" in docs/recovery.md.\n',
            result.output,
        )
        self.assertNotIn("Resolve or remove the stale backup", result.output)
        self.assertNotIn("DRY RUN", result.output)
        self.assertTrue(journal_file.is_file())
        self.assertTrue((self.skills_dir / "alpha.deploying-bak").is_dir())
        self.assertIn("installed by the interrupted run", self.skill_text("alpha"))
        result = self.deploy_ok("--all")
        self.assertIn("Recovering uncommitted run 20260101-000000-kill (rolling back)...", result.output)
        self.assertIn("Alpha content", self.skill_text("alpha"))

    def test_a_dry_run_says_when_the_next_deployment_cannot_recover_a_run(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.interrupted("20260101-000000-kill")
        self.write(self.staging_run("20260101-000001-bad1") / "journal.jsonl", '{"op":"backup"\n')
        result = self.deploy_fails("--all", "--dry-run", pattern="cannot be recovered automatically")
        self.assertIn(
            "Pending recovery: the next deployment will recover run 20260101-000000-kill.\n"
            "ERROR: Run 20260101-000001-bad1 cannot be recovered automatically; the next deployment will stop until "
            "it is reconciled by hand.\n"
            "The dry run stops here: recovery changes what is installed, so a preview before it would be wrong.\n"
            f"{SEE_RECOVERY_FAILED}\n",
            result.output,
        )

    def test_staging_runs_with_nothing_to_recover_do_not_stop_a_dry_run(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.staging_run("20260101-000000-none")
        self.write(self.staging_run("20260101-000001-empty") / "journal.jsonl", "")
        result = self.deploy_ok("--all", "--dry-run")
        self.assertIn("=== DRY RUN ===", result.output)
        self.assertNotIn("Pending recovery", result.output)


class MigrationTests(DeployerTestCase):
    def old_source_with_agent(self, bundles: dict | None = None, *arguments: str) -> None:
        """Deploy alpha, its runtime adapter, and its agent from test/old, then make this source define them."""
        self.make_source_json("test/old", bundles=bundles)
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all", *arguments)
        self.make_source_json(bundles=bundles)
        self.make_config()

    def test_migration_transfers_runtime_adapters_and_agents(self) -> None:
        self.old_source_with_agent()
        result = self.deploy_ok("--migrate-from", "test/old")
        # The adapter moves with its skill, so the report folds its line into the skill's, as every report does.
        self.assertEqual(["alpha", "reviewer.md (agent)"], self.report_groups(result.output, "MIGRATED")["MIGRATED"])
        self.assertIn("alpha", self.owned("wrappers"))
        self.assertIn("reviewer.md", self.owned("agents"))
        self.assertNotIn("test/old", self.manifest()["sources"])
        deployed = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(["alpha", "reviewer.md (agent)"], deployed["UNCHANGED"])

    def test_a_migration_from_an_entry_with_a_malformed_hash_or_an_unsafe_shared_name_is_refused_on_load(self) -> None:
        self.old_source_with_agent()
        original = self.manifest()
        valid_hash = original["sources"]["test/old"]["skills"]["alpha"]["hash"]
        cases = (
            ("skills", "alpha", {**original["sources"]["test/old"]["skills"]["alpha"], "hash": "nothex"}),
            (
                "skills",
                "alpha",
                {
                    key: value
                    for key, value in original["sources"]["test/old"]["skills"]["alpha"].items()
                    if key != "hash"
                },
            ),
            ("shared", "../x", {"hash": valid_hash, "role": "owner"}),
            ("shared", "a/b", {"hash": valid_hash, "role": "owner"}),
        )
        for kind, name, value in cases:
            with self.subTest(kind=kind, name=name, value=value):
                data = json.loads(json.dumps(original))
                data["sources"]["test/old"].setdefault(kind, {})[name] = value
                self.write_manifest(data)
                self.deploy_fails(
                    "--migrate-from",
                    "test/old",
                    pattern=re.escape(f"ERROR: Manifest entry is malformed: source 'test/old' {kind} '{name}'"),
                )
        self.write_manifest(original)

    def test_a_fully_migrated_source_that_requested_a_bundle_leaves_the_manifest(self) -> None:
        self.old_source_with_agent(bundles={"pack": {"members": ["alpha"]}})
        self.assertEqual(["pack"], self.manifest()["sources"]["test/old"]["requested_bundles"])
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        result = self.deploy_ok("--all")
        self.assertIn("  pack (bundle)\n", result.output)

    def test_a_requested_opt_in_bundle_moves_with_its_members_and_stays_in_all(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.make_skill("beta", "Beta")
        self.assertEqual(["pack"], self.manifest()["sources"]["test/old"]["requested_bundles"])
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual(["pack"], self.manifest()["sources"]["test/skills"]["requested_bundles"])
        result = self.deploy_ok("--all")
        self.assertIn("Selected 2 items (2 skills):\n  pack (bundle)\n    alpha\n  beta\n", result.output)
        self.assertEqual(["alpha", "beta"], sorted(self.owned("skills")))

    def test_a_bundle_request_follows_a_moved_member_this_source_makes_an_opt_in_skill_of_its_own(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.assertEqual({"pack": ["alpha"]}, self.manifest()["sources"]["test/old"]["requested_bundle_members"])
        self.make_source_json()
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"], opt_in=True)
        self.deploy_ok("--migrate-from", "test/old")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual([], entry["requested_bundles"])
        self.assertEqual({}, entry["requested_bundle_members"])
        self.assertEqual(["alpha"], entry["requested_skills"])
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. alpha \(opt-in\)$")
        result = self.deploy_ok("--all")
        self.assertIn("Selected 1 item (1 skill):\n  alpha\n", result.output)
        self.assertEqual({"UNCHANGED": ["alpha", "reviewer.md (agent)"]}, self.report_groups(result.output, "DEPLOYED"))

    def test_a_bundle_request_follows_a_moved_member_into_the_bundle_this_source_puts_it_in(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.make_source_json(bundles={"group": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_skill("beta", "Beta")
        self.deploy_ok("--migrate-from", "test/old")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["group"], entry["requested_bundles"])
        self.assertEqual({"group": ["alpha", "beta"]}, entry["requested_bundle_members"])
        self.assertEqual([], entry["requested_skills"])
        result = self.deploy_ok("--all")
        self.assertIn("Selected 1 item (2 skills):\n  group (bundle)\n    alpha\n    beta\n", result.output)
        self.assertEqual(
            {"INSTALLED": ["beta"], "UNCHANGED": ["alpha", "reviewer.md (agent)"]},
            self.report_groups(result.output, "DEPLOYED"),
        )

    def test_a_bundle_request_stays_with_the_old_source_when_none_of_its_members_moves(self) -> None:
        # The old source's pack holds gamma alone, which this source does not define, so gamma stays and keeps the old
        # entry; alpha, deployed on its own, moves. This source names a different pack, which the request must not join.
        self.make_skill("gamma", "Gamma")
        self.old_source_with_agent({"pack": {"members": ["gamma"], "opt_in": True}}, "--include", "pack")
        self.remove_skill("gamma")
        self.make_source_json(bundles={"pack": {"members": ["beta"], "opt_in": True}})
        self.make_skill("beta", "Beta")
        self.deploy_ok("--migrate-from", "test/old")
        sources = self.manifest()["sources"]
        self.assertEqual(["alpha"], sorted(sources["test/skills"]["skills"]))
        self.assertEqual([], sources["test/skills"]["requested_bundles"])
        self.assertEqual(["gamma"], sorted(sources["test/old"]["skills"]))
        self.assertEqual(["pack"], sources["test/old"]["requested_bundles"])
        self.assertEqual({"pack": ["gamma"]}, sources["test/old"]["requested_bundle_members"])
        self.assertNotIn("pack (bundle)", self.deploy_ok("--all", "--dry-run").output)

    def drop_bundle_members(self) -> None:
        """Rewrite every manifest entry as a deployer that recorded no bundle members wrote it.

        The deployment that records members writes the same entry with requested_bundle_members beside the requests,
        so an entry without it is one a release before that deployment wrote.
        """
        data = self.manifest()
        for entry in data["sources"].values():
            del entry["requested_bundle_members"]
        self.write_manifest(data)

    def migrate_as_before_members_were_recorded(self, old: str) -> None:
        """Move everything the old source owns into this source's entry as --migrate-from did before bundle members
        were recorded: the new entry carried each skill the old one requested on its own, and no bundle request, since
        a bundle whose members were installed counted as chosen then. That migration deployed nothing, so the entry
        stays as it wrote it until the next deployment.
        """
        data = self.manifest()
        old_entry = data["sources"].pop(old)
        data["sources"]["test/skills"] = {
            "source_dir": platform_support.normalize(self.source),
            "deployed_at": None,
            "requested_bundles": [],
            "requested_skills": [name for name in old_entry["requested_skills"] if name in old_entry["skills"]],
            **{kind.key: old_entry[kind.key] for kind in KINDS},
        }
        self.write_manifest(data)

    def kept_by_all(self, selected: str, kept: list[str]) -> None:
        """A plain --all, dry run and deployment alike, selects what is given and reports every kept item UNCHANGED."""
        for arguments, title in ((("--all", "--dry-run"), "DRY RUN"), (("--all",), "DEPLOYED")):
            with self.subTest(title=title):
                output = self.deploy_ok(*arguments).output
                self.assertIn(selected, output)
                self.assertEqual({"UNCHANGED": kept}, self.report_groups(output, title))

    def requests(self) -> tuple[list[str], dict[str, list[str]], list[str]]:
        entry = self.manifest()["sources"]["test/skills"]
        return entry["requested_bundles"], entry["requested_bundle_members"], entry["requested_skills"]

    def test_a_bundle_an_earlier_migration_recorded_no_request_for_stays_in_all(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.migrate_as_before_members_were_recorded("test/old")
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$")
        self.kept_by_all("Selected 1 item (1 skill):\n  pack (bundle)\n    alpha\n", ["alpha", "reviewer.md (agent)"])
        self.assertEqual((["pack"], {"pack": ["alpha"]}, []), self.requests())

    def test_a_skill_an_earlier_migration_recorded_no_request_for_stays_in_all_as_an_opt_in_root(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.make_source_json()
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"], opt_in=True)
        self.migrate_as_before_members_were_recorded("test/old")
        self.kept_by_all("Selected 1 item (1 skill):\n  alpha\n", ["alpha", "reviewer.md (agent)"])
        self.assertEqual(([], {}, ["alpha"]), self.requests())

    def test_a_request_recorded_without_members_follows_a_moved_member_into_a_renamed_bundle(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.drop_bundle_members()
        self.make_source_json(bundles={"kit": {"members": ["alpha"], "opt_in": True}})
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["kit"], {"kit": ["alpha"]}, []), self.requests())
        self.kept_by_all("Selected 1 item (1 skill):\n  kit (bundle)\n    alpha\n", ["alpha", "reviewer.md (agent)"])

    def test_a_request_recorded_without_members_follows_a_moved_member_this_source_offers_on_its_own(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.drop_bundle_members()
        self.make_source_json()
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"], opt_in=True)
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(([], {}, ["alpha"]), self.requests())
        self.kept_by_all("Selected 1 item (1 skill):\n  alpha\n", ["alpha", "reviewer.md (agent)"])

    def test_a_migration_counts_no_skill_installed_only_as_a_dependency_as_chosen(self) -> None:
        self.make_source_json("test/old", bundles={"pack": {"members": ["alpha"], "opt_in": True}})
        self.make_skill("alpha", "Alpha", skill_deps=["beta"])
        self.make_skill("beta", "Beta", opt_in=True)
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all", "--include", "pack")
        self.drop_bundle_members()
        # This source puts the old dependency in a bundle of the old name and offers alpha on its own.
        self.make_source_json(bundles={"pack": {"members": ["beta"], "opt_in": True}})
        self.make_skill("alpha", "Alpha", skill_deps=["beta"], opt_in=True)
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(([], {}, ["alpha"]), self.requests())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. pack \(bundle, opt-in\)$")
        self.kept_by_all("Selected 1 item (2 skills):\n  alpha\n    beta (dependency)\n", ["alpha", "beta"])

    def test_a_migration_into_a_requested_bundle_keeps_the_members_its_request_recorded(self) -> None:
        self.old_source_with_agent({"pack": {"members": ["alpha"], "opt_in": True}}, "--include", "pack")
        self.remove_skill("alpha")
        self.make_source_json(bundles={"kit": {"members": ["b", "c"], "opt_in": True}})
        self.make_skill("b", "B")
        self.make_skill("c", "C")
        self.deploy_ok("--all", "--include", "kit")
        # The source moves c out of the bundle and alpha into it, so --all refuses alpha until it is migrated.
        self.make_source_json(bundles={"kit": {"members": ["alpha", "b"], "opt_in": True}})
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.make_skill("c", "C", opt_in=True)
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["kit"], {"kit": ["alpha", "b", "c"]}, []), self.requests())
        self.kept_by_all(
            "Selected 2 items (3 skills):\n  kit (bundle)\n    alpha\n    b\n  c\n",
            ["alpha", "b", "c", "reviewer.md (agent)"],
        )

    def pack_from_the_old_source(self, bundles: dict | None = None, include: tuple[str, ...] = ()) -> Path:
        """Deploy opt-in bundle pack [alpha, beta] from test/old by request, beside the other bundles given, of which
        those include names are requested too. Return the old source."""
        self.make_source_json(
            "test/old", bundles={"pack": {"members": ["alpha", "beta"], "opt_in": True}, **(bundles or {})}
        )
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config("test/old")
        old = self.snapshot_source("old")
        self.deploy_from(old, "--all", *(argument for name in ("pack", *include) for argument in ("--include", name)))
        return old

    def pack_from_a_version_7_entry(self, requested: list[str]) -> Path:
        """Deploy opt-in bundle pack [alpha, beta] from test/old by request, and rewrite the manifest as the last
        deployer to write version 7 left it, with these bundle requests. Return the old source.
        """
        old = self.pack_from_the_old_source()
        self.rewrite_as_version_7(requested, "test/old")
        return old

    def test_a_migration_from_a_version_7_entry_carries_its_bundle_request_into_a_renamed_bundle(self) -> None:
        self.pack_from_a_version_7_entry(["pack"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual((["kit"], {"kit": ["alpha", "beta"]}, []), self.requests())
        self.kept_by_all("Selected 1 item (2 skills):\n  kit (bundle)\n    alpha\n    beta\n", ["alpha", "beta"])

    def test_a_migration_from_a_version_7_entry_carries_a_request_an_earlier_migration_left_out(self) -> None:
        self.pack_from_a_version_7_entry([])
        self.make_source_json(bundles={"pack": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["pack"], {"pack": ["alpha", "beta"]}, []), self.requests())
        self.kept_by_all("Selected 1 item (2 skills):\n  pack (bundle)\n    alpha\n    beta\n", ["alpha", "beta"])

    def test_a_skill_an_entry_without_members_stood_for_stays_chosen_when_the_source_makes_it_a_dependency(
        self,
    ) -> None:
        # The entry records no dependency between the bundle's members; the source now declares beta alpha's
        # dependency. Read from the source alone, beta would stand for no request, lose its menu mark, and be kept only
        # as alpha's dependency.
        self.pack_from_the_old_source()
        self.migrate_as_before_members_were_recorded("test/old")
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["beta"], opt_in=True)
        self.make_skill("beta", "Beta", opt_in=True)
        self.make_config()
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. alpha \(opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. beta \(opt-in\)$")
        self.kept_by_all("Selected 2 items (2 skills):\n  alpha\n    beta (dependency)\n  beta\n", ["alpha", "beta"])
        self.assertEqual(([], {}, ["alpha", "beta"]), self.requests())

    def test_a_migration_carries_a_bundle_request_beside_a_skill_preserved_through_the_version_8_upgrade(self) -> None:
        self.make_skill("everyday", "Everyday")
        self.pack_from_the_old_source()
        self.rewrite_as_preserved_through_the_upgrade("everyday", "test/old")
        self.assertEqual(["alpha", "beta"], self.owned("skills", "test/old")["everyday"]["skill_deps"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual((["kit"], {"kit": ["alpha", "beta"]}, ["everyday"]), self.requests())
        self.kept_by_all(
            "Selected 2 items (3 skills):\n  kit (bundle)\n    alpha\n    beta\n  everyday\n",
            ["alpha", "beta", "everyday"],
        )

    def test_a_migration_carries_a_bundle_request_beside_a_skill_preserved_by_a_deployment_that_installed_a_new_one(
        self,
    ) -> None:
        self.make_skill("everyday", "Everyday")
        self.make_skill("newer", "Newer")
        self.pack_from_the_old_source()
        self.rewrite_as_preserved_through_the_upgrade("everyday", "test/old", installed_since=("newer",))
        self.assertEqual(["alpha", "beta"], self.owned("skills", "test/old")["everyday"]["skill_deps"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual((["kit"], {"kit": ["alpha", "beta"]}, ["everyday", "newer"]), self.requests())
        self.kept_by_all(
            "Selected 3 items (4 skills):\n  kit (bundle)\n    alpha\n    beta\n  everyday\n  newer\n",
            ["alpha", "beta", "everyday", "newer"],
        )

    def test_a_migration_carries_a_bundle_request_beside_a_skill_preserved_by_a_deployment_that_included_a_new_bundle(
        self,
    ) -> None:
        # The source declares no dependency of everyday's, so its filled-in list explains no skill, though no request
        # on its own reaches np, which that deployment installed through a bundle request.
        self.make_skill("everyday", "Everyday")
        self.make_skill("np", "Np")
        pack2 = {"pack2": {"members": ["np"], "opt_in": True}}
        self.pack_from_the_old_source(pack2, include=("pack2",))
        self.rewrite_as_preserved_through_the_upgrade("everyday", "test/old", installed_since=("np",))
        self.assertEqual(["alpha", "beta"], self.owned("skills", "test/old")["everyday"]["skill_deps"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}, **pack2})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual((["kit", "pack2"], {"kit": ["alpha", "beta"], "pack2": ["np"]}, ["everyday"]), self.requests())
        self.kept_by_all(
            "Selected 3 items (4 skills):\n  kit (bundle)\n    alpha\n    beta\n  pack2 (bundle)\n    np\n  everyday\n",
            ["alpha", "beta", "everyday", "np"],
        )

    def test_a_migration_carries_a_bundle_request_beside_a_preserved_skill_the_deployment_did_not_select(self) -> None:
        self.make_skill("everyday", "Everyday")
        self.make_skill("newer", "Newer")
        self.pack_from_the_old_source()
        self.rewrite_as_preserved_through_the_upgrade(
            "everyday", "test/old", installed_since=("newer",), deselected=True
        )
        self.assertEqual(["newer"], self.manifest()["sources"]["test/old"]["requested_skills"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["kit"], {"kit": ["alpha", "beta"]}, ["everyday", "newer"]), self.requests())
        self.kept_by_all(
            "Selected 3 items (4 skills):\n  kit (bundle)\n    alpha\n    beta\n  everyday\n  newer\n",
            ["alpha", "beta", "everyday", "newer"],
        )

    def test_a_migration_carries_no_request_for_a_member_installed_only_as_a_dependency_beside_a_preserved_skill(
        self,
    ) -> None:
        # everyday's filled-in dependencies name tool, its dependency helper, and more, and tool's list names helper,
        # which the source still declares: helper stays a dependency, and the opt-in bundle hb that holds it stays
        # unrequested.
        self.assert_a_migration_carries_no_request_for_a_dependency_only_member(())

    def test_a_migration_carries_no_request_for_a_member_beside_a_skill_preserved_by_the_deployment_that_installed_it(
        self,
    ) -> None:
        # tool and helper are new, so everyday's filled-in dependencies name only alpha and beta; tool's list names
        # helper, which the source still declares, so hb, which has a member not installed, stays unrequested.
        self.assert_a_migration_carries_no_request_for_a_dependency_only_member(("helper", "tool"))

    def assert_a_migration_carries_no_request_for_a_dependency_only_member(
        self, installed_since: tuple[str, ...]
    ) -> None:
        """Deploy everyday, tool, which depends on helper, a member of the opt-in bundle hb nobody requests, and opt-in
        bundle pack from test/old, rewrite the old entry as preserving everyday in a deployment that also installed the
        skills installed_since names, and migrate: hb stays unrequested and --all keeps what is installed."""
        self.make_skill("everyday", "Everyday")
        self.make_skill("tool", "Tool", skill_deps=["helper"])
        self.make_skill("helper", "Helper")
        self.make_skill("mate", "Mate")
        hb = {"hb": {"members": ["helper", "mate"], "opt_in": True}}
        self.pack_from_the_old_source(hb)
        self.rewrite_as_preserved_through_the_upgrade("everyday", "test/old", installed_since=installed_since)
        self.assertEqual(
            sorted({"alpha", "beta", "helper", "tool"} - set(installed_since)),
            self.owned("skills", "test/old")["everyday"]["skill_deps"],
        )
        self.make_source_json(bundles={"pack": {"members": ["alpha", "beta"], "opt_in": True}, **hb})
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["pack"], {"pack": ["alpha", "beta"]}, ["everyday", "tool"]), self.requests())
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[ \] [0-9]+\. hb \(bundle, opt-in\)$")
        self.kept_by_all(
            "Selected 3 items (5 skills):\n  pack (bundle)\n    alpha\n    beta\n  everyday\n  tool\n"
            "    helper (dependency)\n",
            ["alpha", "beta", "everyday", "helper", "tool"],
        )

    def test_a_migration_carries_a_bundle_request_beside_a_preserved_skill_whose_list_a_new_skills_list_contains(
        self,
    ) -> None:
        # newer's exact list names everyday, alpha, beta, and more, and this source no longer has newer depend on
        # alpha and beta: as for --all, neither list explains them, so kit's request is carried.
        self.make_skill("everyday", "Everyday")
        self.make_skill("helper", "Helper")
        self.make_skill("newer", "Newer", skill_deps=["alpha", "beta", "everyday", "helper"])
        self.pack_from_the_old_source()
        self.rewrite_as_preserved_through_the_upgrade("everyday", "test/old", installed_since=("helper", "newer"))
        self.assertEqual(["alpha", "beta"], self.owned("skills", "test/old")["everyday"]["skill_deps"])
        self.make_source_json(bundles={"kit": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_skill("newer", "Newer", skill_deps=["helper"])
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        self.assertEqual((["kit"], {"kit": ["alpha", "beta"]}, ["everyday", "helper", "newer"]), self.requests())
        self.kept_by_all(
            "Selected 4 items (5 skills):\n  kit (bundle)\n    alpha\n    beta\n  everyday\n  helper\n  newer\n",
            ["alpha", "beta", "everyday", "helper", "newer"],
        )

    def test_a_version_7_entry_left_beside_a_migration_keeps_its_bundle_request(self) -> None:
        self.make_skill("gamma", "Gamma")
        old = self.pack_from_a_version_7_entry([])
        self.remove_skill("alpha")
        self.remove_skill("beta")
        self.make_source_json()
        self.make_config()
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["alpha", "beta"], sorted(self.owned("skills", "test/old")))
        # The migration saved the old entry as version 8 with the dependencies the upgrade filled in.
        self.assertEqual(["beta", "gamma"], self.owned("skills", "test/old")["alpha"]["skill_deps"])
        shutil.rmtree(old / "skills" / "gamma")
        (old / "deploy-meta" / "gamma.json").unlink()
        result = self.deploy_from(old, "--all", "--dry-run")
        self.assertIn("Selected 1 item (2 skills):\n  pack (bundle)\n    alpha\n    beta\n", result.output)
        self.assertEqual({"UNCHANGED": ["alpha", "beta"]}, self.report_groups(result.output, "DRY RUN"))

    def gamma_from_another_source_beside_pack(self) -> None:
        """Deploy gamma from test/old, then opt-in bundle pack [alpha, beta] from this source by request."""
        self.make_source_json("test/old")
        self.make_skill("gamma", "Gamma")
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.remove_skill("gamma")
        self.make_source_json(bundles={"pack": {"members": ["alpha", "beta"], "opt_in": True}})
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all", "--include", "pack")

    def test_a_migration_leaves_the_record_of_a_bundle_it_carries_nothing_into(self) -> None:
        self.gamma_from_another_source_beside_pack()
        self.make_source_json(bundles={"pack": {"members": ["beta"], "opt_in": True}})
        self.make_skill("alpha", "Alpha", opt_in=True)
        self.make_skill("gamma", "Gamma")
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["pack"], {"pack": ["alpha", "beta"]}, ["gamma"]), self.requests())
        self.kept_by_all(
            "Selected 3 items (3 skills):\n  pack (bundle)\n    beta\n  alpha\n  gamma\n", ["alpha", "beta", "gamma"]
        )

    def test_a_migration_into_an_entry_recorded_without_members_keeps_what_its_requests_stood_for(self) -> None:
        self.gamma_from_another_source_beside_pack()
        self.drop_bundle_members()
        self.make_source_json(bundles={"kit": {"members": ["beta"], "opt_in": True}})
        self.make_skill("alpha", "Alpha", opt_in=True)
        self.make_skill("gamma", "Gamma")
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual((["pack"], {"pack": []}, ["alpha", "beta", "gamma"]), self.requests())
        self.kept_by_all(
            "Selected 3 items (3 skills):\n  kit (bundle)\n    beta\n  alpha\n  gamma\n", ["alpha", "beta", "gamma"]
        )

    def test_migration_transfers_the_skill_and_shared_intersection(self) -> None:
        self.make_source_json("new/source", shared_assets={"shared-doc.md": "owner"})
        alpha = self.make_skill("alpha", "Alpha content")
        self.make_skill("beta", "Beta content")
        self.make_shared_asset("shared-doc.md", "Shared content")
        self.make_config("new/source")
        shutil.copytree(alpha, self.skills_dir / "alpha")
        self.write(self.skills_dir / "old-only" / "SKILL.md", "old only\n")
        shutil.copy(self.source / "skills" / "shared-doc.md", self.skills_dir / "shared-doc.md")
        self.write_manifest(
            {
                "manifest_version": 6,
                "sources": {
                    "old/source": {
                        "source_dir": "old",
                        "selected_skills": ["alpha", "old-only"],
                        "skills": {
                            "alpha": {"hash": hashing.hash_path(self.skills_dir / "alpha")},
                            "old-only": {"hash": hashing.hash_path(self.skills_dir / "old-only")},
                        },
                        "shared": {
                            "shared-doc.md": {
                                "hash": hashing.hash_path(self.skills_dir / "shared-doc.md"),
                                "role": "owner",
                            }
                        },
                    }
                },
            }
        )
        result = self.deploy_ok("--migrate-from", "old/source")
        self.assertIn("Moved ownership from 'old/source' to 'new/source'.", result.output)
        self.assertIn(
            "MIGRATED (2):\n  alpha\n  shared-doc.md (shared asset)\n",
            result.output,
        )
        sources = self.manifest()["sources"]
        self.assertIn("alpha", sources["new/source"]["skills"])
        self.assertNotIn("beta", sources["new/source"]["skills"])
        self.assertIn("old-only", sources["old/source"]["skills"])
        self.assertNotIn("selected_skills", sources["new/source"])
        self.assertEqual("owner", sources["new/source"]["shared"]["shared-doc.md"]["role"])
        self.assertNotIn("shared-doc.md", sources["old/source"]["shared"])

    def test_migration_hash_mismatch_leaves_the_manifest_unchanged(self) -> None:
        self.make_source_json("new/source")
        alpha = self.make_skill("alpha", "Alpha content")
        self.make_config("new/source")
        shutil.copytree(alpha, self.skills_dir / "alpha")
        self.write_manifest(
            {
                "manifest_version": 6,
                "sources": {
                    "old/source": {
                        "source_dir": "old",
                        "selected_skills": ["alpha"],
                        "skills": {"alpha": {"hash": hashing.hash_path(self.skills_dir / "alpha")}},
                        "shared": {},
                    }
                },
            }
        )
        self.append(self.skills_dir / "alpha" / "SKILL.md", "modified\n")
        before = hashlib.sha256(self.manifest_file.read_bytes()).hexdigest()
        self.deploy_fails("--migrate-from", "old/source", pattern="differs from the old manifest")
        self.assertEqual(before, hashlib.sha256(self.manifest_file.read_bytes()).hexdigest())

    def test_a_name_this_source_deploys_as_another_kind_stays_with_the_old_source_and_the_deployment_refuses(
        self,
    ) -> None:
        self.make_source_json("test/old")
        self.make_skill("alpha", "Old alpha skill")
        self.make_skill("gamma", "Gamma")
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.remove_skill("alpha")
        self.make_source_json(shared_assets={"alpha": "owner"})
        self.make_shared_asset("alpha", "New alpha shared asset")
        self.make_skill("gamma", "Gamma", shared_deps=["alpha"])
        self.make_config()
        before = self.owned("skills", "test/old")["alpha"]
        result = self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["gamma"], self.report_groups(result.output, "MIGRATED")["MIGRATED"])
        self.assertEqual(before, self.owned("skills", "test/old")["alpha"])
        self.assertNotIn("alpha", self.owned("skills"))
        self.assertNotIn("alpha", self.owned("shared"))
        refused = self.deploy_fails("--all", pattern="collides with")
        self.assertIn(
            "ERROR: Shared asset 'alpha' collides with a skill owned by source 'test/old'.\n"
            "One name cannot be deployed as two kinds. Rename it in one source, or stop deploying it from "
            "'test/old'.\n",
            refused.output,
        )
        self.assertIn("Old alpha skill", self.skill_text("alpha"))

    def test_migration_argument_rules(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_fails("--migrate-from", code=2, pattern="argument --migrate-from: expected one argument")
        self.deploy_fails("--migrate-from", "Not Valid", code=2, pattern="Invalid migration source ID")
        self.deploy_fails("--migrate-from", "test/skills", code=2, pattern="must name a different source")
        self.deploy_fails(
            "--migrate-from", "old/source", "--dry-run", code=2, pattern="cannot be combined with --dry-run"
        )


class ManifestStateTests(RecoveryTestCase):
    """The manifest state the deployer still reads: last_run_id through the validated loader, and no selected_skills."""

    def test_recovery_reads_the_committed_run_only_from_a_valid_manifest(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write_journal("20260101-000000-fake", backup_entry("alpha", original))
        data = self.manifest()
        data["sources"]["test/skills"]["skills"]["alpha"]["hash"] = "not-a-hash"
        self.write_manifest(data)
        # A deployment refuses this manifest before recovery, so recovery is called as the next run would call it.
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            recovered = journal.recover_incomplete(self.paths)
        self.assertFalse(recovered, captured.getvalue())
        self.assertIn("ERROR: Cannot read manifest during recovery.", captured.getvalue())
        self.assertTrue((self.skills_dir / "alpha.deploying-bak").is_dir(), "an unreadable manifest decides nothing")
        self.assertTrue((self.staging_run("20260101-000000-fake") / "journal.jsonl").is_file())

    def test_a_manifest_with_the_old_selected_skills_key_loads_and_loses_it_on_commit(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        data = self.manifest()
        data["sources"]["test/skills"]["selected_skills"] = ["alpha"]
        data["sources"]["other/source"] = {"source_dir": "other", "selected_skills": ["beta"], "skills": {}}
        self.write_manifest(data)
        self.deploy_ok("--all")
        sources = self.manifest()["sources"]
        self.assertNotIn("selected_skills", sources["test/skills"])
        self.assertEqual(["beta"], sources["other/source"]["selected_skills"], "another source's entry is left as is")

    def test_migration_leaves_no_old_source_held_only_by_selected_skills(self) -> None:
        self.make_source_json("new/source")
        alpha = self.make_skill("alpha", "Alpha content")
        self.make_config("new/source")
        shutil.copytree(alpha, self.skills_dir / "alpha")
        self.write_manifest(
            {
                "manifest_version": 7,
                "sources": {
                    "old/source": {
                        "source_dir": "old",
                        "requested_skills": ["alpha"],
                        "selected_skills": ["alpha"],
                        "skills": {"alpha": {"hash": hashing.hash_path(self.skills_dir / "alpha")}},
                    }
                },
            }
        )
        self.deploy_ok("--migrate-from", "old/source")
        sources = self.manifest()["sources"]
        self.assertNotIn("old/source", sources)
        self.assertEqual(["alpha"], sources["new/source"]["requested_skills"])
        self.assertNotIn("selected_skills", sources["new/source"])


if __name__ == "__main__":
    unittest.main()
