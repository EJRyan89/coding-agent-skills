from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

from harness import DeployerTestCase, forward

from deployer import fsops, hashing
from deployer.errors import DeployError
from deployer.paths import validate_managed_roots


def make_junction(link, target) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


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
                    {"doc.md": {"hash": "sha256:" + "0" * 64, "role": "tenant"}}
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


class DestinationLinkTests(DeployerTestCase):
    def test_junction_inside_an_installed_skill_blocks_deployment_even_when_forced(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        outside = self.root / "outside"
        self.write(outside / "data.txt", "outside data\n")
        make_junction(self.skills_dir / "alpha" / "linked", outside)
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
        make_junction(self.skills_dir / "alpha", outside)
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
        make_junction(directory, target)

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
        make_junction(self.home, real_home)
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
            self.home.rmdir()  # removes the junction only, never the directory it points to

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
        make_junction(link, outside)
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
        data["sources"]["other/source"] = {"skills": {"alpha": {"hash": "sha256:" + "0" * 64}}, "shared": {}}
        self.write_manifest(data)
        self.deploy_fails("--all", pattern="Skill 'alpha' is owned by both")

    def test_duplicate_runtime_adapter_owners_in_manifest_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()
        self.deploy_ok("--all")
        data = self.manifest()
        data["sources"]["other/source"] = {"wrappers": {"alpha": {"hash": "sha256:" + "0" * 64}}}
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


if __name__ == "__main__":
    unittest.main()
