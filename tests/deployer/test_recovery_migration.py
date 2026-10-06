from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase

from deployer import fsops, hashing, journal, render

ZERO_HASH = "sha256:" + "0" * 64


def journal_line(entry: dict) -> str:
    return json.dumps(entry, separators=(",", ":")) + "\n"


class RecoveryTests(DeployerTestCase):
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

    def sentinel(self) -> Path:
        path = self.home / ".claude" / "sentinel" / "data.txt"
        self.write(path, "precious data\n")
        return path

    def test_uncommitted_journal_rolls_back(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        original = hashing.hash_path(self.skills_dir / "alpha")
        (self.skills_dir / "alpha").rename(self.skills_dir / "alpha.deploying-bak")
        self.write(self.skills_dir / "alpha" / "SKILL.md", "corrupted content\n")
        corrupted = hashing.hash_path(self.skills_dir / "alpha")
        self.write(
            self.staging_run("20260101-000000-fake") / "journal.jsonl",
            journal_line({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                          "retain": False, "backup_hash": original})
            + journal_line({"op": "install", "item": "alpha", "from": "staging/alpha", "to": "skills/alpha",
                            "staged_hash": corrupted}),
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
            journal_line({"op": "backup", "root": "agents", "item": "alpha", "from": "agents/alpha",
                          "to": "agents/alpha.deploying-bak", "retain": False, "backup_hash": original})
            + journal_line({"op": "install", "root": "agents", "item": "alpha", "from": f"{staging_label}/alpha",
                            "to": "agents/alpha", "staged_hash": corrupted}),
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
        render.Staged(skills={"alpha": {"SKILL.md": b"a"}}, adapters={"alpha": {"SKILL.md": b"b"}},
                      agents={"reviewer.md": b"c"}).write(staging)
        self.assertEqual({"alpha", ".agent-adapters", ".claude-agents"}, {path.name for path in staging.iterdir()})
        self.assertTrue((staging / ".agent-adapters" / "alpha" / "SKILL.md").is_file())
        self.assertTrue((staging / ".claude-agents" / "reviewer.md").is_file())

    def test_journals_written_with_earlier_staging_labels_stay_valid(self) -> None:
        run_id = "20260101-000000-label"
        adapter = {"op": "install", "root": "agents", "item": "alpha", "to": "agents/alpha", "staged_hash": ZERO_HASH}
        agent = {"op": "install", "root": "claude-agents", "item": "reviewer.md", "to": "claude-agents/reviewer.md",
                 "staged_hash": ZERO_HASH}
        for label in ("staging-adapters", "staging-wrappers"):
            self.assertTrue(journal.valid_entry({**adapter, "from": f"{label}/alpha"}, run_id), label)
        self.assertTrue(journal.valid_entry({**agent, "from": "staging-claude-agents/reviewer.md"}, run_id))
        self.assertFalse(journal.valid_entry({**adapter, "from": "staging-other/alpha"}, run_id))
        self.assertFalse(journal.valid_entry({**adapter, "from": "staging/.claude-agents/alpha"}, run_id))
        self.assertFalse(journal.valid_entry({**agent, "from": "staging-wrappers/reviewer.md"}, run_id))
        self.assertFalse(journal.valid_entry({**adapter, "root": "claude", "from": "staging-wrappers/alpha",
                                              "to": "skills/alpha"}, run_id))

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
            journal_line({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                          "retain": False, "backup_hash": ZERO_HASH})
            + journal_line({"op": "install", "item": "alpha", "from": "staging/alpha", "to": "skills/alpha",
                            "staged_hash": ZERO_HASH}),
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
            journal_line({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                          "retain": False, "backup_hash": original})
            + journal_line({"op": "install", "item": "alpha", "from": "staging/alpha", "to": "skills/alpha",
                            "staged_hash": ZERO_HASH}),
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
            journal_line({"op": "install", "item": "../sentinel", "from": "staging/../sentinel",
                          "to": "skills/../sentinel", "staged_hash": ZERO_HASH}),
        )
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))

    def test_journal_backslash_traversal_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        sentinel = self.sentinel()
        self.write(
            self.staging_run("20260101-000000-bslash") / "journal.jsonl",
            journal_line({"op": "install", "item": "..\\sentinel", "from": "staging/..\\sentinel",
                          "to": "skills/..\\sentinel", "staged_hash": ZERO_HASH}),
        )
        self.deploy_fails("--all", pattern="Malformed journal entry")
        self.assertTrue(sentinel.is_file())

    def test_journal_backup_destination_backslash_traversal_is_blocked(self) -> None:
        self.deployed(("alpha", "Alpha content"))
        sentinel = self.sentinel()
        journal = self.staging_run("20260101-000000-bdest") / "journal.jsonl"
        self.write(
            journal,
            journal_line({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                          "retain": True, "backup_hash": hashing.hash_path(self.skills_dir / "alpha"),
                          "backup_dest": "..\\sentinel"}),
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
            journal_line({"op": "backup", "item": "alpha", "from": "skills/alpha", "to": "skills/alpha.deploying-bak",
                          "retain": True, "backup_hash": backup_hash, "backup_dest": f".backups/{run_id}/alpha"}),
        )
        self.deploy_fails("--all", pattern="Backup path contains a symlink or junction")
        self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))
        self.assertTrue((self.skills_dir / "alpha.deploying-bak" / "SKILL.md").is_file())


class MigrationTests(DeployerTestCase):
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
                            "shared-doc.md": {"hash": hashing.hash_path(self.skills_dir / "shared-doc.md"), "role": "owner"}
                        },
                    }
                },
            }
        )
        result = self.deploy_ok("--migrate-from", "old/source")
        self.assertIn("Moved ownership from 'old/source' to 'new/source'.", result.output)
        self.assertIn(
            "MIGRATED (2):\n"
            "  alpha\n"
            "  shared-doc.md (shared asset)\n",
            result.output,
        )
        sources = self.manifest()["sources"]
        self.assertIn("alpha", sources["new/source"]["skills"])
        self.assertNotIn("beta", sources["new/source"]["skills"])
        self.assertIn("old-only", sources["old/source"]["skills"])
        self.assertIn("alpha", sources["new/source"]["selected_skills"])
        self.assertNotIn("alpha", sources["old/source"]["selected_skills"])
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
                "sources": {"old/source": {"source_dir": "old", "selected_skills": ["alpha"],
                                           "skills": {"alpha": {"hash": hashing.hash_path(self.skills_dir / "alpha")}},
                                           "shared": {}}},
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


if __name__ == "__main__":
    unittest.main()
