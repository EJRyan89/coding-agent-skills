from __future__ import annotations

import time
import unittest
from unittest import mock

from harness import DeployerTestCase, forward


class CoreLifecycleTests(DeployerTestCase):
    def test_fresh_install_expands_tokens_creates_manifest_and_leaves_source_unchanged(self) -> None:
        self.make_source_json(shared_assets={"shared-doc.md": "owner"})
        self.make_skill(
            "alpha",
            "Path is {{HOME}}/.claude and repos at {{REPOS_ROOT}}/stuff",
            ["HOME", "REPOS_ROOT"],
            ["shared-doc.md"],
        )
        self.make_shared_asset("shared-doc.md", "Shared at {{HOME}}")
        self.make_config()
        self.deploy_ok("--all")
        text = self.skill_text("alpha")
        self.assertIn(forward(self.home), text)
        self.assertIn(forward(self.repos), text)
        self.assertNotIn("{{", text)
        self.assertIn("alpha", self.owned("skills"))
        self.assertTrue((self.skills_dir / "shared-doc.md").is_file())
        self.assertIn("{{HOME}}", (self.source / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8"))

    def test_upgrade_replaces_changed_template(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.make_config(repos_root=self.root / "repos2")
        self.make_skill("alpha", "Updated path is {{HOME}} v2", ["HOME"])
        self.deploy_ok("--all")
        self.assertIn("Updated path", self.skill_text("alpha"))
        self.assertIn("v2", self.skill_text("alpha"))

    def test_modified_skill_is_skipped(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "# user modification\n")
        result = self.deploy_ok("--all")
        self.assertIn(
            "alpha (modified since last deploy)",
            self.report_groups(result.output, "DEPLOYED")["SKIPPED"],
        )
        self.assertIn("user modification", self.skill_text("alpha"))

    def test_bytecode_cache_does_not_count_as_a_modification(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        cache = self.skills_dir / "alpha" / "scripts" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "tool.cpython-312.pyc").write_bytes(b"\x00bytecode")
        (self.skills_dir / "alpha" / "stray.pyc").write_bytes(b"\x00bytecode")
        self.make_skill("alpha", "Updated path is {{HOME}} v2", ["HOME"])
        result = self.deploy_ok("--all")
        self.assertNotIn("SKIPPED", self.report_groups(result.output, "DEPLOYED"))
        self.assertIn("v2", self.skill_text("alpha"))

    def test_force_overwrites_and_retains_backup(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "# user modification\n")
        self.deploy_ok("--all", "--force")
        self.assertNotIn("user modification", self.skill_text("alpha"))
        backups = list((self.skills_dir / ".backups").glob("*/alpha/SKILL.md"))
        self.assertEqual(1, len(backups))
        self.assertIn("user modification", backups[0].read_text(encoding="utf-8"))

    def test_force_item_forces_only_the_named_skill(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha {{HOME}}", ["HOME"])
        self.make_skill("beta", "Beta {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "# mod alpha\n")
        self.append(self.skills_dir / "beta" / "SKILL.md", "# mod beta\n")
        self.deploy_ok("--all", "--force-item", "alpha")
        self.assertNotIn("mod alpha", self.skill_text("alpha"))
        self.assertIn("mod beta", self.skill_text("beta"))

    def test_byte_identical_unmanaged_skill_is_adopted(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "No tokens here")
        self.make_config()
        (self.skills_dir / "alpha").mkdir()
        (self.skills_dir / "alpha" / "SKILL.md").write_bytes(
            (self.source / "skills" / "alpha" / "SKILL.md").read_bytes()
        )
        result = self.deploy_ok("--all")
        self.assertIn("alpha", self.owned("skills"))
        self.assertIn("ADOPTED (1):\n  alpha (byte-identical)\n", result.output)

    def test_differing_unmanaged_skill_is_skipped_with_diff(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Template content")
        self.make_config()
        (self.skills_dir / "alpha").mkdir()
        self.write(self.skills_dir / "alpha" / "SKILL.md", "---\nname: alpha\n---\n\nDifferent content\n")
        result = self.deploy_ok("--all")
        self.assertRegex(
            result.output, r"SKIPPED \(1\):\n  alpha \(unmanaged and differs from the rendered skill\)\n--- "
        )
        self.assertIn("+Template content", result.output)
        self.assertIn("Different content", self.skill_text("alpha"))
        self.assertNotIn("alpha", self.owned("skills"))

    def test_force_item_replaces_differing_unmanaged_skill_with_backup(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Template content")
        self.make_config()
        (self.skills_dir / "alpha").mkdir()
        self.write(self.skills_dir / "alpha" / "SKILL.md", "Different content\n")
        result = self.deploy_ok("--all", "--force-item", "alpha")
        self.assertIn("Template content", self.skill_text("alpha"))
        self.assertIn(
            "alpha (forced, was unmanaged, previous copy backed up)",
            self.report_groups(result.output, "DEPLOYED")["REPLACED"],
        )
        self.assertEqual(1, len(list((self.skills_dir / ".backups").glob("*/alpha/SKILL.md"))))
        self.assertIn("alpha", self.owned("skills"))

    def test_redeploy_without_changes_leaves_no_lock_or_staging(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        self.deploy_ok("--all")
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())
        self.assertEqual([], list((self.home / ".claude" / "deployer" / "staging").iterdir()))


class RunIdTests(DeployerTestCase):
    def test_the_run_id_and_deployed_at_both_use_utc(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        # A UTC instant no local clock shows today, so a run ID taken from the local time cannot match it.
        utc = time.struct_time((2030, 1, 2, 3, 4, 5, 2, 2, 0))
        with mock.patch.object(time, "gmtime", return_value=utc):
            self.deploy_ok("--all")
        manifest = self.manifest()
        self.assertRegex(manifest["last_run_id"], r"^20300102-030405-[0-9a-f]{4}$")
        self.assertEqual("2030-01-02T03:04:05Z", manifest["sources"]["test/skills"]["deployed_at"])


if __name__ == "__main__":
    unittest.main()
