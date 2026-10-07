from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase, forward

from deployer import fsops, hashing, journal, manifest, platform_support, render

ZERO_HASH = "sha256:" + "0" * 64


def journal_line(entry: dict) -> str:
    return json.dumps(entry, separators=(",", ":")) + "\n"


SEE_RECOVERY_FAILED = 'See "When recovery fails" in docs/recovery.md.'


def backup_entry(item: str, backup_hash: str, retain: bool = False, run_id: str = "", root: str = "") -> dict:
    prefix = {"": "skills", "agents": "agents", "claude-agents": "claude-agents"}[root]
    entry: dict = {"op": "backup", **({"root": root} if root else {})}
    entry.update(
        {
            "item": item,
            "from": f"{prefix}/{item}",
            "to": f"{prefix}/{item}.deploying-bak",
            "retain": retain,
            "backup_hash": backup_hash,
        }
    )
    if retain:
        entry["backup_dest"] = f".backups/{run_id}/{item}"
    return entry


def install_entry(item: str, staged_hash: str) -> dict:
    return {
        "op": "install",
        "item": item,
        "from": f"staging/{item}",
        "to": f"skills/{item}",
        "staged_hash": staged_hash,
    }


class RecoveryTestCase(DeployerTestCase):
    def deployed(self, *skills: tuple[str, str]) -> None:
        self.make_source_json()
        for name, content in skills:
            self.make_skill(name, content)
        self.make_config()
        self.deploy_ok("--all")

    def staging_run(self, run_id: str) -> Path:
        directory = self.home / ".claude" / "deployer" / "staging" / run_id
        directory.mkdir(parents=True, exist_ok=True)
        return directory

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

    @property
    def lock_dir(self) -> Path:
        return self.home / ".claude" / "deployer" / ".deploy.lock.d"

    def assert_lines(self, output: str, *lines: str) -> None:
        printed = output.splitlines()
        for line in lines:
            self.assertIn(line, printed, output)


class RecoveryTests(RecoveryTestCase):
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
        self.write(journal, '{"op":"backup","item":"alpha","from":"skills/alpha","to":"skills/alpha.deploying-bak"')
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
        self.assertIn("ERROR: Recovery failed, so nothing was deployed.", result.output)
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
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(self.skills_dir / ".backups" / run_id), str(sentinel.parent)],
            check=True,
            capture_output=True,
        )
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
            "ERROR: Recovery failed, so nothing was deployed.",
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
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_recovery_failed(result.output, alpha)
        self.assert_lines(result.output, "  WARNING: Both alpha and alpha.deploying-bak exist during rollback")
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())

    def test_an_unreadable_installed_copy_fails_its_rollback_step_only(self) -> None:
        self.rolled_back_fixture()
        self.later_run()
        alpha = self.skills_dir / "alpha"
        with failing_at("hash_path", alpha, "deployer.hashing"):
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_recovery_failed(result.output, alpha)

    def test_a_held_transient_backup_fails_finalization_of_a_committed_run(self) -> None:
        self.committed_fixture(retain=False)
        self.later_run()
        transient = self.skills_dir / "alpha.deploying-bak"
        with failing_at("remove", transient):
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_recovery_failed(result.output, transient, committed=True)
        self.assertTrue((transient / "SKILL.md").is_file())

    def test_a_backup_directory_that_cannot_be_created_fails_finalization_of_a_committed_run(self) -> None:
        self.committed_fixture(retain=True)
        self.later_run()
        backups = self.skills_dir / ".backups" / self.HELD
        with failing_at("make_directories", backups):
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_recovery_failed(result.output, backups, committed=True)
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())

    def test_an_unreadable_journal_fails_its_run_only(self) -> None:
        journal_file = self.rolled_back_fixture()
        self.later_run()
        with path_method_failing_at("read_text", journal_file):
            result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_recovery_failed(result.output, journal_file)
        self.assertIn("installed by the interrupted run", self.skill_text("alpha"))

    def test_a_staging_directory_that_cannot_be_removed_fails_its_run_only(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        self.write_journal(self.HELD, install_entry("ghost", ZERO_HASH))
        self.later_run()
        run = self.staging_run(self.HELD)
        with failing_at("remove", run):
            result = self.deploy_fails("--all", pattern="Recovery failed")
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
            "ERROR: Recovery failed, so nothing was deployed.",
        )
        self.assertNotIn("Traceback", result.output)
        self.assertFalse(self.lock_dir.exists())

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
            result = self.deploy_fails("--all", pattern="Immediate recovery failed")
        self.assertIn("synthetic install move failure", result.output)
        self.assertRegex(result.output, rf"  ERROR: Recovery of run \S+ failed at {re.escape(forward(transient))}: ")
        self.assertNotIn("Traceback", result.output)
        self.assertTrue((transient / "SKILL.md").is_file())
        result = self.deploy_ok("--all", probe=lambda pid: platform_support.ProcessStatus(False, None))
        self.assertIn("Recovery complete.", result.output)
        self.assertIn("obsolete", self.report_groups(result.output, "DEPLOYED")["REMOVED"][0])
        self.assertFalse(transient.exists())
        self.assertIn("Updated alpha", self.skill_text("alpha"))


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

    def assert_completion_stopped(self, journal_file: Path, warning: str) -> None:
        result = self.deploy_fails("--all", pattern="Recovery failed")
        self.assert_lines(
            result.output,
            f"Recovering committed run {self.RUN} (completing finalization)...",
            warning,
            f"  ERROR: Recovery has unreconciled entries. Journal retained at: {forward(journal_file)}",
            "ERROR: Recovery failed, so nothing was deployed.",
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
            self.deploy("--all", "--force-item", "alpha")
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
        self.assert_completion_stopped(journal_file, "  WARNING: Cannot find backup for alpha")
        self.assertFalse(destination.exists())

    def test_a_changed_transient_backup_stops_completion(self) -> None:
        journal_file, destination = self.committed_with_kept_backup()
        self.write(self.skills_dir / "alpha.deploying-bak" / "SKILL.md", "changed after the run\n")
        self.assert_completion_stopped(journal_file, "  WARNING: Backup hash mismatch for alpha")
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
        for kind in ("skills", "shared", manifest.ADAPTERS, "agents"):
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
            result = self.deploy_fails("--all", "--force-item", "alpha", pattern="synthetic backup move failure")
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
    def old_source_with_agent(self, bundles: dict | None = None) -> None:
        """Deploy alpha, its runtime adapter, and its agent from test/old, then make this source define them."""
        self.make_source_json("test/old", bundles=bundles)
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha", agent_deps=["reviewer"])
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.make_source_json(bundles=bundles)
        self.make_config()

    def test_migration_transfers_runtime_adapters_and_agents(self) -> None:
        self.old_source_with_agent()
        result = self.deploy_ok("--migrate-from", "test/old")
        # The adapter moves with its skill, so the report folds its line into the skill's, as every report does.
        self.assertEqual(["alpha", "reviewer.md (agent)"], self.report_groups(result.output, "MIGRATED")["MIGRATED"])
        self.assertIn("alpha", self.owned(manifest.ADAPTERS))
        self.assertIn("reviewer.md", self.owned("agents"))
        self.assertNotIn("test/old", self.manifest()["sources"])
        deployed = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(["alpha", "reviewer.md (agent)"], deployed["UNCHANGED"])

    def test_a_fully_migrated_source_that_requested_a_bundle_leaves_the_manifest(self) -> None:
        self.old_source_with_agent(bundles={"pack": {"members": ["alpha"]}})
        self.assertEqual(["pack"], self.manifest()["sources"]["test/old"]["requested_bundles"])
        self.deploy_ok("--migrate-from", "test/old")
        self.assertEqual(["test/skills"], list(self.manifest()["sources"]))
        result = self.deploy_ok("--all")
        self.assertIn("  pack (bundle)\n", result.output)

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

    def test_migration_argument_rules(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_fails("--migrate-from", pattern="argument --migrate-from: expected one argument")
        self.deploy_fails("--migrate-from", "Not Valid", pattern="Invalid migration source ID")
        self.deploy_fails("--migrate-from", "test/skills", pattern="must name a different source")
        self.deploy_fails("--migrate-from", "old/source", "--dry-run", pattern="cannot be combined with --dry-run")


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
