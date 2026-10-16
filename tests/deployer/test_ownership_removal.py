from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from harness import (
    FAILS_PARTWAY,
    SOURCE_ID,
    ZERO_HASH,
    DeployerTestCase,
    forward,
    make_directory_link,
    remove_directory_link,
)

from deployer import fsops, hashing
from deployer.errors import DeployError
from deployer.paths import validate_managed_roots

SKILL_ROOTS = Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts" / "skill_roots.py"


class RemovalTests(DeployerTestCase):
    def test_source_deleted_unmodified_skill_is_removed(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha {{HOME}}", ["HOME"])
        self.make_skill("beta", "Beta {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.remove_skill("beta")
        self.deploy_ok("--all")
        self.assertIn("alpha", self.owned("skills"))
        self.assertNotIn("beta", self.owned("skills"))
        self.assertFalse((self.skills_dir / "beta").exists())
        self.assertFalse((self.agents_dir / "beta").exists())

    def test_unrelated_stale_backup_is_not_destroyed(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Original {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.write(self.skills_dir / "orphan-skill.deploying-bak" / "data.txt", "important data\n")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "# edit\n")
        self.deploy_ok("--all", "--force")
        self.assertTrue((self.skills_dir / "orphan-skill.deploying-bak" / "data.txt").is_file())

    def test_same_item_stale_transient_backup_blocks_deployment(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Original")
        self.make_config()
        self.deploy_ok("--all")
        self.write(self.skills_dir / "alpha.deploying-bak" / "stale.txt", "stale backup data\n")
        self.make_skill("alpha", "Updated")
        self.deploy_fails("--all", "--dry-run", pattern="transient backup already exists")
        self.deploy_fails("--all", pattern="transient backup already exists")
        self.assertIn(
            "stale backup data", (self.skills_dir / "alpha.deploying-bak" / "stale.txt").read_text(encoding="utf-8")
        )
        self.assertIn("Original", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha.deploying-bak" / "alpha").exists())

    def test_deselected_unmodified_skill_is_removed(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha {{HOME}}", ["HOME"])
        self.make_skill("beta", "Beta {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.make_skill("alpha", "Alpha updated {{HOME}}", ["HOME"])
        self.deploy_ok(stdin=self.selection_number("alpha") + "\n")
        self.assertIn("alpha", self.owned("skills"))
        self.assertIn("Alpha updated", self.skill_text("alpha"))
        self.assertNotIn("beta", self.owned("skills"))
        self.assertFalse((self.skills_dir / "beta").exists())

    def test_explicit_none_selection_removes_all_owned_skills(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        result = self.deploy_ok(stdin="none\n")
        self.assertIn("Selected nothing; unmodified items this source deployed will be removed.", result.output)
        self.assertEqual({}, self.owned("skills"))
        self.assertEqual({}, self.owned("wrappers"))
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.skills_dir / "beta").exists())

    def test_the_uninstall_option_previews_and_removes_what_the_answer_none_does_and_prompts_for_nothing(self) -> None:
        self.make_source_json(shared_assets={"guide.md": "owner"})
        self.make_shared_asset("guide.md")
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha", shared_deps=["guide.md"], agent_deps=["reviewer"])
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "beta" / "SKILL.md", "local edit\n")
        answered = self.deploy_ok("--dry-run", stdin="none\n")
        given = self.deploy_ok("--uninstall", "--dry-run")
        self.assertEqual(answered.stdout, given.stdout)
        self.assertIn("Select what to deploy:", answered.stderr)
        self.assertNotIn("Select what to deploy:", given.output)
        self.assertEqual(
            {
                "PRESERVE": ["beta (modified since last deploy)"],
                "REMOVE": [
                    "alpha (deselected or absent from source)",
                    "guide.md (shared asset, obsolete)",
                    "reviewer.md (agent, no selected skill needs it)",
                ],
            },
            self.report_groups(given.output, "DRY RUN"),
        )
        # An answer on stdin is never read: the option is the selection.
        result = self.deploy_ok("--uninstall", stdin="1\n")
        self.assertNotIn("Select what to deploy:", result.output)
        self.assertIn("Selected nothing; unmodified items this source deployed will be removed.", result.output)
        self.assertEqual(
            {
                "PRESERVED": ["beta (modified since last deploy)"],
                "REMOVED": [
                    "alpha (deselected or absent from source)",
                    "guide.md (shared asset, obsolete)",
                    "reviewer.md (agent, no selected skill needs it)",
                ],
            },
            self.report_groups(result.output, "DEPLOYED"),
        )
        self.assertEqual(["beta"], list(self.owned("skills")))
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.skills_dir / "guide.md").exists())
        self.assertFalse((self.claude_agents_dir / "reviewer.md").exists())
        self.assertIn("source_dir", self.manifest()["sources"][SOURCE_ID], "the entry keeps its checkout")

    def test_deleting_every_source_skill_removes_prior_installations(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        self.remove_skill("alpha")
        result = self.deploy_ok("--all")
        self.assertNotIn("alpha", self.owned("skills"), result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_modified_deselected_skill_is_preserved_with_ownership(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha {{HOME}}", ["HOME"])
        self.make_skill("beta", "Beta {{HOME}}", ["HOME"])
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "beta" / "SKILL.md", "\nUser change\n")
        result = self.deploy_ok(stdin=self.selection_number("alpha") + "\n")
        self.assertIn("beta (modified since last deploy)", self.report_groups(result.output, "DEPLOYED")["PRESERVED"])
        self.assertIn("beta", self.owned("skills"))
        self.assertIn("User change", self.skill_text("beta"))
        self.assertIn("beta", self.owned("wrappers"), "a preserved skill keeps its runtime adapter")
        self.assertTrue((self.agents_dir / "beta" / "SKILL.md").is_file())

    def test_a_skill_kept_for_a_preserved_skill_keeps_its_runtime_adapter_and_a_removed_one_loses_it(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["beta"])
        self.make_skill("beta", "Beta")
        self.make_skill("gamma", "Gamma")
        self.make_config()
        self.deploy_ok("--all")
        adapters = {name: hashing.hash_path(self.agents_dir / name) for name in ("alpha", "beta")}
        self.append(self.skills_dir / "alpha" / "SKILL.md", "\nUser change\n")
        preview = self.deploy_ok("--dry-run", stdin="none\n")
        groups = self.report_groups(preview.output, "DRY RUN")
        self.assertEqual(["alpha (modified since last deploy)"], groups["PRESERVE"])
        self.assertEqual(["beta (needed by alpha)"], groups["KEEP"], "each adapter's line folds into its skill's")
        self.assertEqual(["gamma (deselected or absent from source)"], groups["REMOVE"])
        for _ in range(2):
            result = self.deploy_ok(stdin="none\n")
            self.assertEqual(["beta (needed by alpha)"], self.report_groups(result.output, "DEPLOYED")["KEPT"])
            self.assertEqual(["alpha", "beta"], sorted(self.owned("wrappers")))
            for name, adapter in adapters.items():
                self.assertEqual(adapter, hashing.hash_path(self.agents_dir / name), name)
                self.assertEqual(adapter, self.owned("wrappers")[name]["hash"], name)
            self.assertFalse((self.agents_dir / "gamma").exists())
            self.assertFalse((self.skills_dir / "gamma").exists())
        self.deploy_ok("--all", "--force-item", "alpha")
        self.deploy_ok(stdin="none\n")
        self.assertEqual({}, self.owned("skills"))
        self.assertEqual({}, self.owned("wrappers"))
        for name in ("alpha", "beta", "gamma"):
            self.assertFalse((self.agents_dir / name).exists(), name)

    def test_a_kept_skill_whose_adapter_was_modified_preserves_it_and_a_missing_one_drops_ownership(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["beta"])
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "\nUser change\n")
        self.append(self.agents_dir / "beta" / "SKILL.md", "\nAdapter change\n")
        shutil.rmtree(self.agents_dir / "alpha")
        groups = self.report_groups(self.deploy_ok(stdin="none\n").output, "DEPLOYED")
        self.assertIn("beta (runtime adapter, modified since last deploy)", groups["PRESERVED"])
        self.assertEqual(["alpha (runtime adapter, already absent)"], groups["DROPPED OWNERSHIP"])
        self.assertEqual(["beta"], sorted(self.owned("wrappers")))
        self.assertIn("Adapter change", (self.agents_dir / "beta" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertFalse((self.agents_dir / "alpha").exists(), "keeping an adapter does not recreate a missing one")

    def dependency_fixture(self) -> None:
        self.make_source_json(shared_assets={"shared.md": "owner"})
        public = self.make_skill("public", "Public", skill_deps=["middle"])
        middle = self.make_skill("middle", "Middle", skill_deps=["core"], selectable=False)
        core = self.make_skill("core", "Core", shared_deps=["shared.md"], selectable=False, agent_deps=["reviewer"])
        self.make_skill("unrelated", "Unrelated")
        self.make_shared_asset("shared.md", "Shared")
        self.make_agent("reviewer")
        self.write(core / "scripts" / "support.py", 'VALUE = "dependency ready"\n')
        # core plays skill-core: it holds the real sibling lookup, which run.py reaches middle through once deployed,
        # where the manifest's recorded skill_deps keep middle installed beside a skipped or preserved public.
        self.write(core / "scripts" / "skill_roots.py", SKILL_ROOTS.read_text(encoding="utf-8"))
        self.write(
            middle / "scripts" / "bridge.py",
            "import sys\nfrom pathlib import Path\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "scripts"))\n'
            "from support import VALUE\n",
        )
        self.write(
            public / "scripts" / "run.py",
            "import sys\nfrom pathlib import Path\n"
            'sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core" / "scripts"))\n'
            "from skill_roots import sibling_scripts\n"
            'sys.path.insert(0, str(sibling_scripts("middle")))\n'
            "from bridge import VALUE\nprint(VALUE)\n",
        )
        self.make_config()
        self.deploy_ok("--all")
        self.assert_dependency_script_runs()
        self.append(self.skills_dir / "public" / "SKILL.md", "\nUser change\n")

    def assert_dependency_script_runs(self) -> None:
        result = subprocess.run(
            [sys.executable, "-B", str(self.skills_dir / "public" / "scripts" / "run.py")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("dependency ready\n", result.stdout)

    def assert_dependency_closure_kept(self, output: str) -> None:
        self.assert_dependency_script_runs()
        groups = self.report_groups(output, "DEPLOYED")
        for name, dependent in (("middle", "public"), ("core", "middle")):
            self.assertIn(f"{name} (needed by {dependent})", groups["KEPT"])
        self.assertIn("shared.md (shared asset, needed by core)", groups["KEPT"])
        self.assertIn("reviewer.md (agent, needed by core)", groups["KEPT"])
        self.assertEqual(["middle"], self.owned("skills")["public"]["skill_deps"])
        self.assertEqual(["core"], self.owned("skills")["middle"]["skill_deps"])
        self.assertEqual([], self.owned("skills")["core"]["skill_deps"])
        self.assertIn("shared.md", self.owned("shared"))
        self.assertIn("reviewer.md", self.owned("agents"))

    def test_modified_deselected_skill_keeps_transitive_dependencies_assets_and_agents(self) -> None:
        self.dependency_fixture()
        for _ in range(2):
            result = self.deploy_ok(stdin="none\n")
            self.assert_dependency_closure_kept(result.output)
            self.assertNotIn("unrelated", self.owned("skills"))
            self.assertFalse((self.skills_dir / "unrelated").exists())
            self.assertEqual([], self.manifest()["sources"][SOURCE_ID]["requested_skills"])
        self.deploy_ok("--all", "--force-item", "public")
        backups = list((self.skills_dir / ".backups").glob("*/public/SKILL.md"))
        self.assertEqual(1, len(backups))
        self.assertIn("User change", backups[0].read_text(encoding="utf-8"))
        self.deploy_ok(stdin="none\n")
        self.assertEqual({}, self.owned("skills"))
        self.assertEqual({}, self.owned("shared"))
        self.assertEqual({}, self.owned("agents"))
        self.assertTrue(backups[0].is_file(), "permanent backups survive the uninstall")

    def test_preserved_dependency_closure_uses_deployed_edges_not_current_metadata(self) -> None:
        self.dependency_fixture()
        for name in ("public", "middle", "core"):
            self.remove_skill(name)
        self.make_source_json()
        result = self.deploy_ok("--all")
        self.assert_dependency_closure_kept(result.output)

    def test_skipped_selected_skill_keeps_dependencies_removed_from_current_metadata(self) -> None:
        self.dependency_fixture()
        self.make_skill("public", "Updated public")
        result = self.deploy_ok("--all")
        self.assert_dependency_closure_kept(result.output)
        self.assertIn("public (modified since last deploy)", self.report_groups(result.output, "DEPLOYED")["SKIPPED"])
        self.assertIn("unrelated", self.owned("skills"))
        self.deploy_ok("--all", "--force-item", "public")
        self.assertEqual([], self.owned("skills")["public"]["skill_deps"])
        self.assertNotIn("middle", self.owned("skills"))
        self.assertNotIn("core", self.owned("skills"))
        self.assertEqual({}, self.owned("shared"))
        self.assertEqual({}, self.owned("agents"))

    def test_a_modified_hidden_dependency_keeps_its_own_closure_without_its_public_root(self) -> None:
        self.dependency_fixture()
        self.write(
            self.skills_dir / "public" / "SKILL.md",
            (self.source / "skills" / "public" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.append(self.skills_dir / "middle" / "SKILL.md", "\nUser change\n")
        result = self.deploy_ok(stdin="none\n")
        self.assertNotIn("public", self.owned("skills"))
        self.assertIn("middle", self.owned("skills"))
        self.assertIn("core (needed by middle)", self.report_groups(result.output, "DEPLOYED")["KEPT"])
        self.assertIn("shared.md", self.owned("shared"))
        self.assertIn("reviewer.md", self.owned("agents"))

    def test_a_missing_dependency_still_drops_ownership_instead_of_reporting_a_kept_skill(self) -> None:
        self.dependency_fixture()
        shutil.rmtree(self.skills_dir / "core")
        result = self.deploy_ok(stdin="none\n")
        self.assertNotIn("core", self.owned("skills"))
        self.assertIn("core (already absent)", self.report_groups(result.output, "DEPLOYED")["DROPPED OWNERSHIP"])
        self.assertIn("middle", self.owned("skills"))
        self.assertFalse((self.skills_dir / "core").exists(), "retention does not recreate missing files")

    def test_legacy_dependency_migration_is_read_only_until_a_journaled_commit(self) -> None:
        self.dependency_fixture()
        data = self.manifest()
        data["manifest_version"] = 7
        for details in data["sources"][SOURCE_ID]["skills"].values():
            del details["skill_deps"]
        self.write_manifest(data)
        before = self.manifest_file.read_bytes()
        for name in ("public", "middle", "core"):
            self.remove_skill(name)
        self.make_source_json()
        preview = self.deploy_ok("--dry-run", stdin="none\n")
        self.assertEqual(before, self.manifest_file.read_bytes())
        self.assertIn("KEEP", preview.output)
        result = self.deploy_ok(stdin="none\n")
        self.assertEqual(8, self.manifest()["manifest_version"])
        self.assertEqual(["core", "middle", "unrelated"], self.owned("skills")["public"]["skill_deps"])
        self.assertIn("unrelated", self.owned("skills"), "unknown historical edges retain only this source's skills")
        self.assertIn("shared.md", self.owned("shared"))
        self.assertIn("reviewer.md", self.owned("agents"))
        self.assert_dependency_script_runs()
        self.assertIn("unrelated (needed by public)", self.report_groups(result.output, "DEPLOYED")["KEPT"])
        self.deploy_ok(stdin="none\n")
        self.assert_dependency_script_runs()

    def assert_legacy_dependency_migration_recovers(self, committed: bool) -> None:
        self.dependency_fixture()
        data = self.manifest()
        data["manifest_version"] = 7
        for details in data["sources"][SOURCE_ID]["skills"].values():
            del details["skill_deps"]
        self.write_manifest(data)
        before = self.manifest_file.read_bytes()
        self.make_skill("unrelated", "Updated unrelated")
        write_atomic = fsops.write_atomic

        def failing_commit(path: Path, content: bytes) -> None:
            if path != self.manifest_file:
                write_atomic(path, content)
                return
            if committed:
                write_atomic(path, content)
            raise OSError("injected manifest commit failure")

        with mock.patch("deployer.fsops.write_atomic", side_effect=failing_commit):
            result = self.deploy_fails("--all", pattern="injected manifest commit failure", changes_home=FAILS_PARTWAY)
        self.assertIn("reconciling the current journal", result.output)
        self.assertEqual(8 if committed else 7, self.manifest()["manifest_version"])
        if not committed:
            self.assertEqual(before, self.manifest_file.read_bytes())
        self.assertIn("Updated unrelated" if committed else "Unrelated", self.skill_text("unrelated"))
        self.assert_dependency_script_runs()
        self.deploy_ok("--all", "--force-item", "public")
        self.deploy_ok(stdin="none\n")

    def test_legacy_dependency_migration_rolls_back_before_commit(self) -> None:
        self.assert_legacy_dependency_migration_recovers(committed=False)

    def test_legacy_dependency_migration_recovers_after_commit(self) -> None:
        self.assert_legacy_dependency_migration_recovers(committed=True)

    def test_legacy_migration_refreshes_exact_edges_and_upgrades_other_sources_without_cross_source_edges(self) -> None:
        self.dependency_fixture()
        data = self.manifest()
        data["manifest_version"] = 6
        for details in data["sources"][SOURCE_ID]["skills"].values():
            del details["skill_deps"]
        data["sources"]["other/source"] = {
            "skills": {name: {"hash": ZERO_HASH} for name in ("other-public", "other-core")}
        }
        self.write_manifest(data)
        self.deploy_ok("--all", "--force-item", "public")
        self.assertEqual(8, self.manifest()["manifest_version"])
        self.assertEqual(["middle"], self.owned("skills")["public"]["skill_deps"])
        self.assertEqual(["core"], self.owned("skills")["middle"]["skill_deps"])
        self.assertEqual([], self.owned("skills")["core"]["skill_deps"])
        others = self.manifest()["sources"]["other/source"]["skills"]
        self.assertEqual(["other-core"], others["other-public"]["skill_deps"])
        self.assertEqual(["other-public"], others["other-core"]["skill_deps"])
        self.assertTrue(all(details["hash"] == ZERO_HASH for details in others.values()))

    def test_obsolete_unmodified_shared_asset_is_removed(self) -> None:
        self.make_source_json(shared_assets={"shared-doc.md": "owner"})
        self.make_skill("alpha", "Alpha", shared_deps=["shared-doc.md"])
        self.make_shared_asset("shared-doc.md", "Shared content")
        self.make_config()
        self.deploy_ok("--all")
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.deploy_ok("--all")
        self.assertFalse((self.skills_dir / "shared-doc.md").exists())
        self.assertNotIn("shared-doc.md", self.owned("shared"))

    def test_modified_obsolete_shared_asset_is_preserved_with_ownership(self) -> None:
        self.make_source_json(shared_assets={"shared-doc.md": "owner"})
        self.make_skill("alpha", "Alpha", shared_deps=["shared-doc.md"])
        self.make_shared_asset("shared-doc.md", "Shared content")
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "shared-doc.md", "\nUser change\n")
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        result = self.deploy_ok("--all")
        self.assertIn(
            "shared-doc.md (shared asset, modified since last deploy)",
            self.report_groups(result.output, "DEPLOYED")["PRESERVED"],
        )
        self.assertIn("User change", (self.skills_dir / "shared-doc.md").read_text(encoding="utf-8"))
        self.assertEqual("owner", self.owned("shared")["shared-doc.md"]["role"])


class ManifestValidationTests(DeployerTestCase):
    def deployed(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")

    def test_malformed_or_missing_recorded_skill_dependencies_fail_before_mutation(self) -> None:
        self.deployed()
        original = self.manifest()
        for value in ("core", ["../escape"], ["Bad_Name"], ["core\n"], [1], None):
            with self.subTest(value=value):
                data = json.loads(json.dumps(original))
                data["sources"][SOURCE_ID]["skills"]["alpha"]["skill_deps"] = value
                self.write_manifest(data)
                self.deploy_fails("--all", pattern="Manifest entry is malformed")
        data = json.loads(json.dumps(original))
        del data["sources"][SOURCE_ID]["skills"]["alpha"]["skill_deps"]
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Manifest entry is malformed")
        self.write_manifest(original)

    def test_owned_names_that_escape_the_deployment_root_are_rejected(self) -> None:
        self.deployed()
        sentinel = self.home / ".claude" / "sentinel" / "data.txt"
        self.write(sentinel, "precious data\n")
        for kind in ("skills", "shared", "wrappers"):
            with self.subTest(kind=kind):
                data = self.manifest()
                data["sources"]["test/skills"][kind]["../sentinel"] = {
                    "hash": hashing.hash_path(sentinel.parent),
                    "role": "owner",
                }
                self.write_manifest(data)
                self.deploy_fails(
                    "--all", pattern=f"Manifest entry is malformed: source 'test/skills' {kind} '../sentinel'"
                )
                self.assertEqual("precious data\n", sentinel.read_text(encoding="utf-8"))
                data["sources"]["test/skills"][kind].pop("../sentinel")
                self.write_manifest(data)

    def test_malformed_hashes_roles_sources_and_selections_are_rejected(self) -> None:
        self.deployed()
        original = self.manifest()
        cases = [
            (
                "Manifest entry is malformed",
                lambda data: data["sources"]["test/skills"]["skills"]["alpha"].update(hash="sha256:bad"),
            ),
            (
                "Manifest entry is malformed: source 'test/skills' shared",
                lambda data: data["sources"]["test/skills"]["shared"].update(
                    {"doc.md": {"hash": ZERO_HASH, "role": "tenant"}}
                ),
            ),
            ("Manifest source ID is malformed", lambda data: data["sources"].update({"../escape": {}})),
            (
                "Manifest selection fields are malformed",
                lambda data: data["sources"]["test/skills"].update(selected_skills=["../x"]),
            ),
            ("Manifest last_run_id is malformed", lambda data: data.update(last_run_id="../run")),
            (
                "Manifest entry is malformed: source 'test/skills' skills 'alpha\\n'",
                lambda data: data["sources"]["test/skills"]["skills"].update(
                    {"alpha\n": data["sources"]["test/skills"]["skills"]["alpha"]}
                ),
            ),
            (
                "Manifest source ID is malformed: 'other/source\\n'",
                lambda data: data["sources"].update({"other/source\n": {}}),
            ),
            (
                "Manifest selection fields are malformed",
                lambda data: data["sources"]["test/skills"].update(requested_skills=["alpha\n"]),
            ),
            (
                "Manifest selection fields are malformed",
                lambda data: data["sources"]["test/skills"].update(requested_bundle_members={"pack": ["../x"]}),
            ),
            (
                "Manifest selection fields are malformed",
                lambda data: data["sources"]["test/skills"].update(requested_bundle_members={"pack\n": ["alpha"]}),
            ),
            (
                "Manifest selection fields are malformed",
                lambda data: data["sources"]["test/skills"].update(requested_bundle_members=["pack"]),
            ),
            (
                "Manifest entry is malformed: source 'test/skills' skills 'alpha'",
                lambda data: data["sources"]["test/skills"]["skills"]["alpha"].update(shared_deps=["../escape"]),
            ),
            (
                "Manifest entry is malformed: source 'test/skills' skills 'alpha'",
                lambda data: data["sources"]["test/skills"]["skills"]["alpha"].update(shared_deps="doc.md"),
            ),
            ("Manifest last_run_id is malformed:", lambda data: data.update(last_run_id=data["last_run_id"] + "\n")),
        ]
        for index, (message, mutate) in enumerate(cases):
            with self.subTest(case=index, message=message):
                data = json.loads(json.dumps(original))
                mutate(data)
                self.write_manifest(data)
                self.deploy_fails("--all", pattern=re.escape(message))
        self.write_manifest(original)
        self.deploy_ok("--all")

    def test_a_null_selection_or_item_field_is_refused_on_read_before_ownership_or_migration_reads_it(self) -> None:
        # Ownership and migration read these fields without a fallback for null, since reading refuses it first.
        self.deployed()
        original = self.manifest()
        cases: list[tuple[str, Callable[[dict[str, Any]], object]]] = [
            ("Manifest selection fields are malformed", lambda entry: entry.update(requested_bundle_members=None)),
            ("Manifest selection fields are malformed", lambda entry: entry.update(skills=None)),
            ("Manifest selection fields are malformed", lambda entry: entry.update(shared=None)),
            (
                "Manifest entry is malformed: source 'old/skills' skills 'alpha'",
                lambda entry: entry["skills"].update(alpha=None),
            ),
        ]
        for message, mutate in cases:
            for arguments in (("--all",), ("--migrate-from", "old/skills")):
                with self.subTest(message=message, arguments=arguments):
                    data = json.loads(json.dumps(original))
                    old = data["sources"]["old/skills"] = data["sources"].pop(SOURCE_ID)
                    mutate(old)
                    self.write_manifest(data)
                    self.deploy_fails(*arguments, pattern=re.escape(message))
        self.write_manifest(original)
        self.deploy_ok("--all")


class DestinationLinkTests(DeployerTestCase):
    def test_junction_inside_an_installed_skill_blocks_deployment_even_when_forced(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        outside = self.root / "outside"
        self.write(outside / "data.txt", "outside data\n")
        make_directory_link(self.skills_dir / "alpha" / "linked", outside)
        for arguments in (("--all",), ("--all", "--force"), ("--all", "--dry-run")):
            with self.subTest(arguments=arguments):
                self.deploy_fails(*arguments, pattern="Destination 'alpha' contains a symlink or junction")
                self.assertTrue((self.skills_dir / "alpha" / "linked").exists())
                self.assertEqual("outside data\n", (outside / "data.txt").read_text(encoding="utf-8"))

    def test_junction_replacing_an_owned_skill_blocks_its_removal(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        outside = self.root / "outside"
        self.write(outside / "SKILL.md", "not managed\n")
        shutil.rmtree(self.skills_dir / "alpha")
        make_directory_link(self.skills_dir / "alpha", outside)
        self.remove_skill("alpha")
        self.deploy_fails("--all", pattern="Destination 'alpha' contains a symlink or junction")
        self.assertEqual("not managed\n", (outside / "SKILL.md").read_text(encoding="utf-8"))


class ManagedRootTests(DeployerTestCase):
    def fixture(self) -> Path:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        outside = self.root / "outside"
        self.write(outside / "20260101-000000-keep" / "data.txt", "external data\n")
        return outside

    def replace_with_junction(self, directory: Path, target: Path) -> None:
        if directory.exists():
            shutil.rmtree(directory)
        directory.parent.mkdir(parents=True, exist_ok=True)
        make_directory_link(directory, target)

    def assert_linked_root_blocks_deployment(self, directory: Path) -> None:
        outside = self.fixture()
        self.replace_with_junction(directory, outside)
        for arguments in (("--all",), ("--all", "--dry-run")):
            self.deploy_fails(*arguments, pattern="Deployment path contains a symlink or junction")
        self.assertEqual(["20260101-000000-keep"], sorted(path.name for path in outside.iterdir()))
        self.assertEqual("external data\n", (outside / "20260101-000000-keep" / "data.txt").read_text(encoding="utf-8"))

    def test_linked_staging_root_is_not_recovered(self) -> None:
        self.assert_linked_root_blocks_deployment(self.home / ".claude" / "deployer" / "staging")

    def test_linked_skills_root_is_not_deployed_to(self) -> None:
        self.assert_linked_root_blocks_deployment(self.skills_dir)

    def test_linked_adapter_root_is_not_deployed_to(self) -> None:
        self.assert_linked_root_blocks_deployment(self.agents_dir)

    def test_linked_claude_directory_is_not_deployed_to(self) -> None:
        self.assert_linked_root_blocks_deployment(self.home / ".claude")

    def test_a_linked_home_is_neither_deployed_to_nor_configured(self) -> None:
        # A junctioned home would redirect every managed root beneath it, so the home itself is checked too.
        self.fixture()
        real_home = self.root / "real home"
        shutil.move(self.home, real_home)
        make_directory_link(self.home, real_home)
        try:
            before = sorted(path.relative_to(real_home) for path in real_home.rglob("*"))
            for arguments in (("--all",), ("--all", "--dry-run")):
                result = self.deploy_fails(*arguments, pattern="Deployment path contains a symlink or junction")
                self.assertIn(f"Deployment path contains a symlink or junction: {forward(self.home)}\n", result.output)
            result = self.configure(stdin="\n")
            self.assertEqual(1, result.code, result.output)
            self.assertIn(f"Deployment path contains a symlink or junction: {forward(self.home)}\n", result.output)
            self.assertEqual(before, sorted(path.relative_to(real_home) for path in real_home.rglob("*")))
        finally:
            remove_directory_link(self.home)

    def test_a_home_that_is_a_file_is_rejected(self) -> None:
        shutil.rmtree(self.home)
        self.home.write_text("not a directory\n", encoding="utf-8")
        with self.assertRaises(DeployError) as raised:
            validate_managed_roots(self.paths)
        self.assertEqual(
            (f"ERROR: Deployment path component is not a directory: {forward(self.home)}",), raised.exception.lines
        )

    def test_configure_rejects_a_linked_config_directory(self) -> None:
        outside = self.fixture()
        self.replace_with_junction(self.home / ".claude" / "deployer" / "config", outside)
        result = self.configure(stdin="\n")
        self.assertEqual(1, result.code)
        self.assertIn("Deployment path contains a symlink or junction", result.output)

    def test_removing_a_link_never_touches_its_target(self) -> None:
        outside = self.fixture()
        link = self.root / "link"
        make_directory_link(link, outside)
        fsops.remove(link)
        self.assertFalse(link.exists())
        self.assertEqual("external data\n", (outside / "20260101-000000-keep" / "data.txt").read_text(encoding="utf-8"))


class SharedAssetLifecycleTests(DeployerTestCase):
    def owner_fixture(self, source_id: str = "test/skills") -> None:
        self.make_source_json(source_id, shared_assets={"shared.md": "owner"})
        self.make_skill("alpha", "Alpha", shared_deps=["shared.md"])
        self.make_skill("beta", "Beta", shared_deps=["shared.md"])
        self.make_shared_asset("shared.md", "Shared content")
        self.make_config(source_id)

    def test_uninstall_removes_owned_shared_assets_and_their_ownership(self) -> None:
        self.owner_fixture()
        self.deploy_ok("--all")
        self.assertTrue((self.skills_dir / "shared.md").is_file())
        self.assertIn(
            "shared.md (shared asset, obsolete)",
            self.report_groups(self.deploy_ok("--dry-run", stdin="none\n").output, "DRY RUN")["REMOVE"],
        )
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "shared.md").exists())
        self.assertEqual({}, self.owned("shared"))

    def test_shared_asset_stays_while_a_selected_skill_needs_it(self) -> None:
        self.owner_fixture()
        self.deploy_ok("--all")
        self.deploy_ok(stdin=self.selection_number("beta") + "\n")
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertTrue((self.skills_dir / "shared.md").is_file())
        self.assertIn("shared.md", self.owned("shared"))

    def test_preserved_skill_keeps_its_shared_asset_on_uninstall(self) -> None:
        self.owner_fixture()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "local edit\n")
        preview = self.deploy_ok("--dry-run", stdin="none\n").output
        self.assertIn("shared.md (shared asset, needed by alpha)", self.report_groups(preview, "DRY RUN")["KEEP"])
        result = self.deploy_ok(stdin="none\n")
        self.assertIn(
            "shared.md (shared asset, needed by alpha)",
            self.report_groups(result.output, "DEPLOYED")["KEPT"],
        )
        self.assertTrue((self.skills_dir / "shared.md").is_file())
        self.assertFalse((self.skills_dir / "beta").exists())
        self.assertEqual(["shared.md"], self.owned("skills")["alpha"]["shared_deps"])

        shutil.rmtree(self.skills_dir / "alpha")
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "shared.md").exists())
        self.assertEqual({}, self.owned("shared"))

    def test_skipped_modified_skill_keeps_the_dependency_of_its_installed_copy(self) -> None:
        self.owner_fixture()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "local edit\n")
        self.remove_skill("beta")
        self.make_skill("alpha", "Alpha without shared asset")
        result = self.deploy_ok("--all")
        self.assertIn(
            "alpha (modified since last deploy)",
            self.report_groups(result.output, "DEPLOYED")["SKIPPED"],
        )
        self.assertIn(
            "shared.md (shared asset, needed by alpha)",
            self.report_groups(result.output, "DEPLOYED")["KEPT"],
        )
        self.assertTrue((self.skills_dir / "shared.md").is_file())

        self.deploy_ok("--all", "--force-item", "alpha")
        self.assertFalse((self.skills_dir / "shared.md").exists())
        self.assertEqual([], self.owned("skills")["alpha"]["shared_deps"])

    def deploy_dependent_source(self, source_id: str, skills: tuple[str, ...]) -> None:
        for name in list(self.source_skill_names()):
            self.remove_skill(name)
        shared = self.source / "skills" / "shared.md"
        if shared.exists():
            shared.unlink()
        self.make_source_json(source_id, shared_assets={"shared.md": "dependency"})
        for name in skills:
            self.make_skill(name, name.title(), shared_deps=["shared.md"])
        self.make_config(source_id)

    def source_skill_names(self) -> list[str]:
        return [path.stem for path in (self.source / "deploy-meta").glob("*.json")]

    def owner_source(self) -> Path:
        self.owner_fixture("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.deploy_from(source_a, "--all")
        return source_a

    def assert_owner_keeps_for(self, source_a: Path, dependent: str) -> None:
        preview = self.deploy_from(source_a, "--dry-run", stdin="none\n").output
        self.assertIn(
            f"shared.md (shared asset, needed by source {dependent})", self.report_groups(preview, "DRY RUN")["KEEP"]
        )
        result = self.deploy_from(source_a, stdin="none\n")
        self.assertIn(
            f"shared.md (shared asset, needed by source {dependent})",
            self.report_groups(result.output, "DEPLOYED")["KEPT"],
        )
        self.assertTrue((self.skills_dir / "shared.md").is_file())
        self.assertIn("shared.md", self.owned("shared", "test/source-a"))

    def test_shared_asset_needed_by_another_source_is_kept_on_uninstall(self) -> None:
        source_a = self.owner_source()
        self.deploy_dependent_source("test/source-b", ("gamma",))
        self.deploy_ok("--all")
        self.assertEqual(["shared.md"], self.owned("skills", "test/source-b")["gamma"]["shared_deps"])
        self.assert_owner_keeps_for(source_a, "test/source-b")

        self.deploy_ok(stdin="none\n")
        self.deploy_from(source_a, stdin="none\n")
        self.assertFalse((self.skills_dir / "shared.md").exists())
        self.assertNotIn("shared.md", self.owned("shared", "test/source-a"))

    def test_preserved_skill_of_another_source_keeps_the_owner_asset(self) -> None:
        source_a = self.owner_source()
        self.deploy_dependent_source("test/source-b", ("gamma",))
        self.deploy_ok("--all")
        self.append(self.skills_dir / "gamma" / "SKILL.md", "local edit\n")
        self.deploy_ok(stdin="none\n")
        self.assertTrue((self.skills_dir / "gamma").is_dir())
        self.assertEqual(["shared.md"], self.owned("skills", "test/source-b")["gamma"]["shared_deps"])
        self.assert_owner_keeps_for(source_a, "test/source-b")

    def test_modified_external_dependency_does_not_block_uninstall(self) -> None:
        self.owner_source()
        self.deploy_dependent_source("test/source-b", ("gamma",))
        self.deploy_ok("--all")
        self.append(self.skills_dir / "shared.md", "local edit\n")
        self.deploy_fails("--all", pattern="Dependency 'shared.md' at destination does not match owner's manifest hash")
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "gamma").exists())
        self.assertEqual({}, self.owned("skills", "test/source-b"))
        self.assertTrue((self.skills_dir / "shared.md").is_file())

    def test_full_migration_carries_dependency_registrations(self) -> None:
        source_a = self.owner_source()
        self.deploy_dependent_source("old/source", ("gamma",))
        self.deploy_ok("--all")
        self.deploy_dependent_source("new/source", ("gamma",))
        self.deploy_ok("--migrate-from", "old/source")
        self.assertNotIn("old/source", self.manifest()["sources"])
        self.assertEqual(["shared.md"], self.owned("skills", "new/source")["gamma"]["shared_deps"])
        self.assert_owner_keeps_for(source_a, "new/source")

    def test_partial_migration_leaves_remaining_registrations_with_the_old_source(self) -> None:
        source_a = self.owner_source()
        self.deploy_dependent_source("old/source", ("delta", "gamma"))
        self.deploy_ok("--all")
        self.deploy_dependent_source("new/source", ("gamma",))
        self.deploy_ok("--migrate-from", "old/source")
        self.assertEqual(["shared.md"], self.owned("skills", "old/source")["delta"]["shared_deps"])
        self.assertEqual(["shared.md"], self.owned("skills", "new/source")["gamma"]["shared_deps"])
        self.deploy_ok(stdin="none\n")
        self.assertFalse((self.skills_dir / "gamma").exists())
        self.assert_owner_keeps_for(source_a, "old/source")


class CrossSourceOwnershipTests(DeployerTestCase):
    def test_duplicate_skill_owners_in_manifest_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        self.deploy_ok("--all")
        data = self.manifest()
        data["sources"]["other/source"] = {
            "skills": {"alpha": {"hash": ZERO_HASH, "skill_deps": []}},
            "shared": {},
        }
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Skill 'alpha' is owned by both")

    def test_duplicate_runtime_adapter_owners_in_manifest_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        self.deploy_ok("--all")
        data = self.manifest()
        data["sources"]["other/source"] = {"wrappers": {"alpha": {"hash": ZERO_HASH}}}
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Runtime adapter 'alpha' is owned by both")

    def test_runtime_adapter_owned_by_another_source_blocks_deployment(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        self.deploy_ok("--all")
        data = self.manifest()
        adapter = data["sources"]["test/skills"]["wrappers"].pop("alpha")
        data["sources"]["other/source"] = {"wrappers": {"alpha": adapter}}
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Runtime adapter 'alpha' is owned by source 'other/source'")

    def test_cross_source_skill_blocks_same_name_shared_asset(self) -> None:
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
        self.deploy_fails(
            "--all", "--dry-run", pattern="Shared asset 'alpha' collides with a skill owned by source 'test/source-a'"
        )
        self.deploy_fails("--all", pattern="Shared asset 'alpha' collides with a skill owned by source 'test/source-a'")
        self.assertIn("Source A skill", self.skill_text("alpha"))
        self.assertFalse((self.skills_dir / "alpha" / "alpha").exists())
        self.assertNotIn("alpha", self.owned("shared", "test/source-b"))

    def test_cross_source_shared_asset_blocks_same_name_skill(self) -> None:
        self.make_source_json("test/source-a", shared_assets={"alpha": "owner"})
        self.make_shared_asset("alpha", "Source A shared asset")
        self.make_skill("user", "Source A user", shared_deps=["alpha"])
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        (self.source / "skills" / "alpha").unlink()
        self.remove_skill("user")
        self.make_source_json("test/source-b")
        self.make_skill("alpha", "Source B skill")
        self.make_config("test/source-b")
        self.deploy_from(source_a, "--all")
        self.deploy_fails("--all", pattern="Skill 'alpha' collides with a shared asset owned by source 'test/source-a'")
        self.assertIn("Source A shared asset", (self.skills_dir / "alpha").read_text(encoding="utf-8"))
        self.assertNotIn("alpha", self.owned("skills", "test/source-b"))

    def test_manifest_cannot_own_one_name_across_item_types(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        self.deploy_ok("--all")
        data = self.manifest()
        data["sources"]["other/source"] = {
            "source_dir": "fixture",
            "deployed_at": None,
            "selected_skills": [],
            "skills": {},
            "shared": {"alpha": {"hash": hashing.hash_path(self.skills_dir / "alpha"), "role": "owner"}},
        }
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="owned as a skill.*and as a shared asset")
        self.assertIn("Alpha content", self.skill_text("alpha"))

    def test_dry_run_reports_the_same_cross_source_rejection_as_apply(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("alpha", "Source A skill")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.make_source_json("test/source-b")
        self.make_skill("alpha", "Source B skill")
        self.make_config("test/source-b")
        self.deploy_from(source_a, "--all")
        result = self.deploy_fails("--all", "--dry-run", pattern="Skill 'alpha' is owned by source 'test/source-a'")
        self.assertNotIn("DRY RUN", result.output)
        self.deploy_fails("--all", pattern="Skill 'alpha' is owned by source 'test/source-a'")

    def test_collision_preflight_occurs_before_pending_removals(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("collision", "Collision owner")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.remove_skill("collision")
        self.make_source_json("test/source-b")
        self.make_skill("obsolete", "Must survive failed deploy")
        self.make_config("test/source-b")
        self.deploy_from(source_a, "--all")
        self.deploy_ok("--all")
        self.remove_skill("obsolete")
        self.make_skill("collision", "Conflicting replacement")
        self.deploy_fails("--all", pattern="owned by source 'test/source-a'")
        self.assertIn("Must survive failed deploy", self.skill_text("obsolete"))
        self.assertFalse((self.skills_dir / "obsolete.deploying-bak").exists())
        self.assertIn("obsolete", self.owned("skills", "test/source-b"))


class CaseVariantOwnershipTests(DeployerTestCase):
    """Names the file system treats as one, such as Guide.md and guide.md on Windows, are one item for ownership."""

    GUIDE = "# Guide shared by both sources"

    def guide_source(self, source_id: str, asset: str, name: str) -> None:
        self.make_source_json(source_id, shared_assets={asset: "owner"})
        self.make_shared_asset(asset, self.GUIDE)
        self.make_skill(name, f"Skill {name}", shared_deps=[asset])
        self.make_config(source_id)

    def deployed_owner(self) -> bytes:
        """Source A deploys Guide.md; the fixture source becomes source B, which declares guide.md with its bytes."""
        self.guide_source("test/source-a", "Guide.md", "alpha")
        source_a = self.snapshot_source("source-a")
        self.deploy_from(source_a, "--all")
        (self.source / "skills" / "Guide.md").unlink()
        self.remove_skill("alpha")
        self.guide_source("test/source-b", "guide.md", "beta")
        return (self.skills_dir / "Guide.md").read_bytes()

    def assert_owner_untouched(self, content: bytes) -> None:
        names = [path.name for path in self.skills_dir.iterdir()]
        self.assertIn("Guide.md", names)
        self.assertNotIn("guide.md", names)
        self.assertEqual(content, (self.skills_dir / "Guide.md").read_bytes())
        self.assertIn("Guide.md", self.owned("shared", "test/source-a"))

    def test_a_case_variant_of_another_sources_shared_asset_is_refused(self) -> None:
        content = self.deployed_owner()
        refusal = (
            "ERROR: Shared asset 'guide.md' collides with shared asset 'Guide.md' owned by source 'test/source-a'.\n"
            "The file system treats 'guide.md' and 'Guide.md' as one name. Rename it in one source, or stop deploying "
            "'Guide.md' from 'test/source-a'.\n"
            'See "Ownership held by another source" in docs/recovery.md.\n'
        )
        self.assertIn(refusal, self.deploy_fails("--all", "--dry-run", pattern="collides").output)
        self.assertIn(refusal, self.deploy_fails("--all", pattern="collides").output)
        self.assert_owner_untouched(content)
        self.assertNotIn("test/source-b", self.manifest()["sources"])
        self.assertFalse((self.skills_dir / "beta").exists())

    def test_a_case_variant_another_source_also_owns_is_released_not_removed(self) -> None:
        content = self.deployed_owner()
        # A manifest written before case variants were refused, in which source B adopted A's file as guide.md.
        data = self.manifest()
        data["sources"]["test/source-b"] = {
            "skills": {},
            "shared": {"guide.md": {"hash": hashing.hash_path(self.skills_dir / "Guide.md"), "role": "owner"}},
        }
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Shared asset 'guide.md' collides with shared asset 'Guide.md'")
        result = self.deploy_ok(stdin="none\n")
        self.assertIn(
            "guide.md (shared asset, the same file as 'Guide.md', owned by source 'test/source-a')",
            self.report_groups(result.output, "DEPLOYED")["DROPPED OWNERSHIP"],
        )
        self.assertNotIn("guide.md", self.owned("shared", "test/source-b"))
        self.assert_owner_untouched(content)

    def test_a_case_variant_of_an_item_the_same_source_owns_is_refused(self) -> None:
        self.guide_source(SOURCE_ID, "Guide.md", "alpha")
        self.deploy_ok("--all")
        content = (self.skills_dir / "Guide.md").read_bytes()
        (self.source / "skills" / "Guide.md").unlink()
        self.guide_source(SOURCE_ID, "guide.md", "alpha")
        result = self.deploy_fails(
            "--all", pattern="collides with shared asset 'Guide.md' owned by source 'test/skills'"
        )
        self.assertIn(
            "Keep the name 'Guide.md', or deploy once without it so that it is removed, then deploy 'guide.md'.",
            result.output,
        )
        self.assertEqual(content, (self.skills_dir / "Guide.md").read_bytes())
        self.assertEqual(["Guide.md"], list(self.owned("shared")))

    def test_a_case_variant_of_another_sources_skill_is_refused(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("alpha", "Source A skill")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.deploy_from(source_a, "--all")
        self.remove_skill("alpha")
        self.guide_source("test/source-b", "Alpha", "beta")
        self.deploy_fails(
            "--all", pattern="ERROR: Shared asset 'Alpha' collides with skill 'alpha' owned by source 'test/source-a'"
        )
        self.assertIn("Source A skill", self.skill_text("alpha"))
        self.assertNotIn("test/source-b", self.manifest()["sources"])

    def test_a_source_cannot_declare_two_names_for_one_file(self) -> None:
        self.make_source_json(shared_assets={"Alpha": "owner"})
        self.make_skill("alpha", "Alpha", shared_deps=["Alpha"])
        self.make_config()
        self.deploy_fails("--all", pattern="ERROR: Shared asset 'Alpha' collides with skill 'alpha'")
        self.make_source_json(shared_assets={"Guide.md": "owner", "guide.md": "owner"})
        self.make_shared_asset("guide.md", self.GUIDE)
        self.make_skill("alpha", "Alpha", shared_deps=["Guide.md", "guide.md"])
        self.deploy_fails("--all", pattern="ERROR: Shared asset 'guide.md' collides with shared asset 'Guide.md'")
        self.assertFalse(self.manifest_file.exists())


if __name__ == "__main__":
    unittest.main()
