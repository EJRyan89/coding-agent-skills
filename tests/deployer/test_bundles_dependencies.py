from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import frontmatter
from harness import DEPLOY_COMMAND, REPOSITORY_ROOT, DeployerTestCase, forward

BUNDLE = {"operations": {"members": ["alpha", "beta"]}}


class BundleAndDependencyTests(DeployerTestCase):
    def bundle_fixture(self) -> None:
        self.make_source_json(bundles=BUNDLE)
        self.make_skill("alpha", "Alpha", skill_deps=["core"])
        self.make_skill("beta", "Beta", skill_deps=["core"])
        self.make_skill("core", "Core", selectable=False)
        self.make_config()

    def test_bundle_is_one_selectable_item_and_expands_members_and_hidden_dependencies(self) -> None:
        self.bundle_fixture()
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. operations \(bundle\)$")
        self.assertNotRegex(menu, r"(?m)^  \[ \] [0-9]+\. (alpha|beta|core)$")
        result = self.deploy_ok(stdin=self.selection_number("operations", bundle=True) + "\n")
        self.assertIn(
            "Selected 1 item (3 skills):\n  operations (bundle)\n    alpha\n    beta\n    core (dependency)\n",
            result.output,
        )
        for name in ("alpha", "beta", "core"):
            self.assertTrue((self.skills_dir / name / "SKILL.md").is_file())
        self.assertTrue((self.agents_dir / "alpha" / "SKILL.md").is_file())
        self.assertTrue((self.agents_dir / "beta" / "SKILL.md").is_file())
        self.assertFalse((self.agents_dir / "core").exists())
        self.assertIn(
            f"{forward(self.home)}/.claude/skills/alpha/SKILL.md",
            (self.agents_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
        )
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(8, self.manifest()["manifest_version"])
        self.assertEqual(["operations"], entry["requested_bundles"])
        self.assertEqual([], entry["requested_skills"])
        self.assertEqual(["alpha", "beta", "core"], sorted(entry["skills"]))
        self.assertEqual(["alpha", "beta"], sorted(entry["wrappers"]))
        rerun = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(rerun, r"(?m)^  \[\*\] [0-9]+\. operations \(bundle\)$")

    def renamed_member(self) -> None:
        """Deploy the bundle, then rename its member beta to gamma in the source, as a skill rename does."""
        self.bundle_fixture()
        self.deploy_ok("--all")
        self.remove_skill("beta")
        self.make_skill("gamma", "Beta", skill_deps=["core"])
        self.make_source_json(bundles={"operations": {"members": ["alpha", "gamma"]}})

    def test_a_bundle_member_the_source_renames_is_removed_and_installed_under_its_new_name(self) -> None:
        self.renamed_member()
        report = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(["beta (deselected or absent from source)"], report["REMOVED"])
        self.assertEqual(["gamma"], report["INSTALLED"])
        self.assertEqual(["alpha", "core", "gamma"], sorted(self.owned("skills")))
        for directory in (self.skills_dir, self.agents_dir):
            self.assertFalse((directory / "beta").exists())
            self.assertTrue((directory / "gamma" / "SKILL.md").is_file())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["operations"], entry["requested_bundles"])
        self.assertEqual({"operations": ["alpha", "gamma"]}, entry["requested_bundle_members"])

    def test_a_modified_bundle_member_the_source_renames_is_preserved_until_its_folder_is_deleted(self) -> None:
        self.renamed_member()
        self.append(self.skills_dir / "beta" / "SKILL.md", "# local note\n")
        report = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(["beta (modified since last deploy)"], report["PRESERVED"])
        self.assertEqual(["gamma"], report["INSTALLED"])
        self.assertTrue((self.agents_dir / "beta" / "SKILL.md").is_file(), "its adapter stays with it")
        shutil.rmtree(self.skills_dir / "beta")
        report = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(["beta (already absent)"], report["DROPPED OWNERSHIP"])
        self.assertEqual(["beta (runtime adapter, obsolete)"], report["REMOVED"])
        self.assertFalse((self.agents_dir / "beta").exists())
        self.assertEqual(["alpha", "core", "gamma"], sorted(self.owned("skills")))

    def test_run_header_names_the_source_and_shows_the_selection_as_a_tree(self) -> None:
        self.bundle_fixture()
        self.make_skill("tool", "Tool", skill_deps=["middle"])
        self.make_skill("middle", "Middle", skill_deps=["helper"], selectable=False)
        self.make_skill("helper", "Helper", selectable=False)
        result = self.deploy_ok("--all", "--dry-run")
        self.assertEqual(
            "\n"
            "Source: Test Skills (test/skills)\n"
            f"Home: {forward(self.home)}\n"
            "Found 6 skills and 1 bundle.\n"
            "\n"
            "Selected 2 items (6 skills):\n"
            "  operations (bundle)\n"
            "    alpha\n"
            "    beta\n"
            "    core (dependency)\n"
            "  tool\n"
            "    helper (dependency)\n"
            "    middle (dependency)\n"
            "\n"
            "Rendered and validated in memory (dry run).\n"
            "\n"
            "=== DRY RUN ===\n",
            result.output[: result.output.index("=== DRY RUN ===\n") + len("=== DRY RUN ===\n")],
        )

    def test_run_header_without_a_source_name_uses_the_source_id_and_singular_counts(self) -> None:
        self.make_source_json()
        document = json.loads((self.source / "source.json").read_text(encoding="utf-8"))
        del document["name"]
        (self.source / "source.json").write_text(json.dumps(document), encoding="utf-8")
        self.make_skill("alpha", "Alpha")
        self.make_config()
        result = self.deploy_ok("--all", "--dry-run")
        self.assertTrue(
            result.output.startswith(
                f"\nSource: test/skills\nHome: {forward(self.home)}\nFound 1 skill.\n\n"
                "Selected 1 item (1 skill):\n  alpha\n\n"
            ),
            result.output,
        )

    def test_all_excludes_unreachable_non_selectable_skills(self) -> None:
        self.make_source_json()
        self.make_skill("public", "Public")
        self.make_skill("hidden", "Hidden", selectable=False)
        self.make_config()
        self.deploy_ok("--all")
        self.assertTrue((self.skills_dir / "public" / "SKILL.md").is_file())
        self.assertTrue((self.agents_dir / "public" / "SKILL.md").is_file())
        self.assertFalse((self.skills_dir / "hidden").exists())
        self.assertFalse((self.agents_dir / "hidden").exists())

    def test_transitive_closure_includes_required_variables_and_shared_assets(self) -> None:
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_skill("public", "Public", skill_deps=["middle"])
        self.make_skill("middle", "Middle", skill_deps=["core"], selectable=False)
        self.make_skill("core", "Core {{REPOS_ROOT}}", ["REPOS_ROOT"], ["shared.md"], selectable=False)
        self.make_shared_asset("shared.md", "Shared")
        self.write(self.config_file(), "_source_id=test/skills\n")
        result = self.deploy_fails("--all", pattern="require variables not set in config: REPOS_ROOT")
        self.assertIn("core -> REPOS_ROOT", result.output)
        self.assertFalse((self.skills_dir / "public").exists())
        self.make_config()
        self.deploy_ok("--all")
        for path in (
            self.skills_dir / "middle" / "SKILL.md",
            self.skills_dir / "core" / "SKILL.md",
            self.skills_dir / "shared.md",
        ):
            self.assertTrue(path.is_file())

    def test_dependency_cycles_fail_before_mutation(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["beta"])
        self.make_skill("beta", "Beta", skill_deps=["alpha"], selectable=False)
        self.make_config()
        self.deploy_fails("--all", pattern="Skill dependency cycle detected: alpha beta alpha")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_dependency_cycle_is_named_in_dependency_order(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["beta"])
        self.make_skill("beta", "Beta", skill_deps=["gamma"], selectable=False)
        self.make_skill("gamma", "Gamma", skill_deps=["alpha"], selectable=False)
        self.make_config()
        self.deploy_fails("--all", pattern="Skill dependency cycle detected: alpha beta gamma alpha\n")

    def test_a_dependency_cycle_names_only_the_skills_in_the_cycle(self) -> None:
        self.make_source_json()
        self.make_skill("aardvark", "Aardvark", skill_deps=["beta"])
        self.make_skill("beta", "Beta", skill_deps=["gamma"], selectable=False)
        self.make_skill("gamma", "Gamma", skill_deps=["beta"], selectable=False)
        self.make_config()
        result = self.deploy_fails("--all", pattern="Skill dependency cycle detected: beta gamma beta\n")
        self.assertNotIn("aardvark", result.output)

    def test_invalid_bundles_fail_before_mutation(self) -> None:
        cases: dict[str, dict[str, Any]] = {
            "contains duplicate member 'alpha'": {"operations": {"members": ["alpha", "alpha"]}},
            "references unknown member 'ghost'": {"operations": {"members": ["ghost"]}},
            "must contain a non-empty string members array": {"operations": {"members": []}},
            "Bundle name 'Bad_Name' is invalid": {"Bad_Name": {"members": ["alpha"]}},
        }
        for message, bundles in cases.items():
            with self.subTest(message=message):
                self.make_source_json(bundles=bundles)
                self.make_skill("alpha", "Alpha")
                self.make_config()
                self.deploy_fails("--all", pattern=re.escape(message))
                self.assertFalse((self.skills_dir / "alpha").exists())

    def test_deselecting_a_bundle_removes_its_complete_dependency_closure(self) -> None:
        self.bundle_fixture()
        self.deploy_ok("--all")
        self.deploy_ok(stdin="none\n")
        for directory in (self.skills_dir, self.agents_dir):
            for name in ("alpha", "beta", "core"):
                self.assertFalse((directory / name).exists())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual([], entry["requested_bundles"])
        self.assertEqual([], entry["requested_skills"])
        self.assertEqual({}, entry["skills"])

    def test_a_member_retired_from_an_installed_bundle_is_removed_and_the_bundle_kept(self) -> None:
        # Retiring a skill from a bundle (as re-review was) relies on the next deployment removing it.
        self.bundle_fixture()
        self.deploy_ok(stdin=self.selection_number("operations", bundle=True) + "\n")
        self.make_source_json(bundles={"operations": {"members": ["alpha"]}})
        self.remove_skill("beta")
        preview = self.deploy_ok("--all", "--dry-run").output
        self.assertIn("REMOVE (1):\n  beta (deselected or absent from source)\n", preview)
        self.assertTrue((self.skills_dir / "beta" / "SKILL.md").is_file(), "a dry run changes nothing")
        self.deploy_ok("--all")
        for directory in (self.skills_dir, self.agents_dir):
            with self.subTest(directory=directory.name):
                self.assertFalse((directory / "beta").exists())
                self.assertTrue((directory / "alpha" / "SKILL.md").is_file())
        self.assertTrue((self.skills_dir / "core" / "SKILL.md").is_file())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["operations"], entry["requested_bundles"])
        self.assertEqual(["alpha", "core"], sorted(entry["skills"]))
        self.assertNotIn("beta", self.owned("skills"))
        self.assertNotIn("beta", self.owned("wrappers"))

    def test_dependency_remains_while_another_selected_root_needs_it(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", skill_deps=["core"])
        self.make_skill("beta", "Beta", skill_deps=["core"])
        self.make_skill("core", "Core", selectable=False)
        self.make_config()
        self.deploy_ok("--all")
        self.deploy_ok(stdin=self.selection_number("beta") + "\n")
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertTrue((self.skills_dir / "beta" / "SKILL.md").is_file())
        self.assertTrue((self.skills_dir / "core" / "SKILL.md").is_file())

    def test_runtime_adapter_conflicts_are_preserved_and_force_retains_a_backup(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        adapter = self.agents_dir / "alpha" / "SKILL.md"
        self.append(adapter, "\nlocal adapter change\n")
        self.assertIn(
            "alpha (runtime adapter, modified since last deploy)",
            self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")["SKIPPED"],
        )
        self.assertIn("local adapter change", adapter.read_text(encoding="utf-8"))
        result = self.deploy_ok("--all", "--force-item", "alpha")
        self.assertIn(
            "alpha (runtime adapter, forced, previous copy backed up)",
            self.report_groups(result.output, "DEPLOYED")["REPLACED"],
        )
        self.assertNotIn("local adapter change", adapter.read_text(encoding="utf-8"))
        backups = list((self.agents_dir / ".backups").glob("*/alpha/SKILL.md"))
        self.assertEqual(1, len(backups))
        self.assertIn("local adapter change", backups[0].read_text(encoding="utf-8"))

    def test_copilot_personal_skill_shadow_is_reported_without_mutation(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        native = self.home / ".copilot" / "skills" / "alpha" / "SKILL.md"
        self.write(native, "native Copilot skill\n")
        result = self.deploy_ok("--all")
        self.assertIn("Higher-priority GitHub Copilot personal skills shadow generated runtime adapters", result.output)
        self.assertIn(f"check which copy Copilot uses with '{DEPLOY_COMMAND} verify'", result.output)
        self.assertIn(forward(self.home / ".copilot" / "skills" / "alpha"), result.output)
        self.assertEqual("native Copilot skill\n", native.read_text(encoding="utf-8"))
        self.assertTrue((self.agents_dir / "alpha" / "SKILL.md").is_file())

    def test_older_and_newer_manifest_versions_fail_closed_without_modification(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        for version, message in ((5, "unsupported schema version '5'"), (99, "newer than supported version 8")):
            with self.subTest(version=version):
                data = self.manifest()
                data["manifest_version"] = version
                self.write_manifest(data)
                before = hashlib.sha256(self.manifest_file.read_bytes()).hexdigest()
                self.deploy_fails("--all", pattern=re.escape(message))
                self.assertEqual(before, hashlib.sha256(self.manifest_file.read_bytes()).hexdigest())

    def test_only_the_runtime_adapter_points_to_shared_guidance(self) -> None:
        self.make_source_json(shared_assets={"guide.md": "owner", "table.json": "owner"})
        self.make_shared_asset("guide.md", "# Runtime guidance")
        self.make_shared_asset("table.json", "{}")
        self.make_skill("alpha", "Alpha body", shared_deps=["guide.md", "table.json"])
        self.make_skill("beta", "Beta body")
        self.make_config()
        self.deploy_ok("--all")
        home = forward(self.home)
        self.assertEqual(
            "---\n"
            "name: alpha\n"
            'description: "Test skill alpha"\n'
            "---\n"
            "\n"
            "# Runtime skill adapter\n"
            "\n"
            f"Before anything else, read and apply `{home}/.claude/skills/guide.md`, which maps the skill's "
            "Claude tool, model, and path conventions to this runtime.\n"
            f"Read and follow `{home}/.claude/skills/alpha/SKILL.md` as the authoritative skill instructions.\n"
            f"In that skill, `${{CLAUDE_SKILL_DIR}}` stands for `{home}/.claude/skills/alpha`, its own directory; "
            "substitute it in every path before running a command or reading a file.\n"
            "Use this runtime's native tools for equivalent operations. Do not copy, summarize, "
            "or independently extend the workflow in this adapter.\n",
            (self.agents_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertNotIn("guide.md", (self.agents_dir / "beta" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertNotIn("guide.md", self.skill_text("alpha"), "the authoritative skill is deployed unchanged")

    def adapter(self, name: str) -> frontmatter.Frontmatter:
        return frontmatter.read(self.agents_dir / name / "SKILL.md")

    def test_adapter_carries_the_rendered_description_but_not_allowed_tools(self) -> None:
        self.make_source_json()
        self.make_skill(
            "alpha",
            "Alpha",
            ["REPOS_ROOT"],
            description="Sweep every repo under {{REPOS_ROOT}} — use when asked to tidy branches.",
        )
        self.make_config(repos_root=self.home / "My Repos (work)")
        self.deploy_ok("--all")
        adapter = self.adapter("alpha")
        expected = f"Sweep every repo under {forward(self.home)}/My Repos (work) — use when asked to tidy branches."
        self.assertEqual(expected, adapter.string("description"))
        self.assertEqual(
            ["name", "description"],
            adapter.keys(),
            "Copilot cannot scope allowed-tools; see Granting tools in docs/adding-a-skill.md",
        )
        self.assertEqual(["SKILL.md"], [path.name for path in (self.agents_dir / "alpha").rglob("*")])

    def test_adapter_quotes_a_description_that_plain_yaml_would_misread(self) -> None:
        self.make_source_json()
        description = 'Report: say "done" #1\\ when asked.\nThen stop.'
        self.make_skill("alpha", "Alpha", description=json.dumps(description))
        self.make_config()
        self.deploy_ok("--all")
        self.assertEqual(description, self.adapter("alpha").string("description"))

    def test_user_only_adapter_cannot_be_selected_by_purpose_on_any_runtime(self) -> None:
        self.make_source_json()
        self.make_skill(
            "alpha", "Alpha", description='"Delete stale branches across every repository."', user_only=True
        )
        self.make_config()
        self.deploy_ok("--all")
        adapter = self.adapter("alpha")
        self.assertEqual(
            "Runtime adapter for the authoritative alpha skill, which only the user starts.",
            adapter.string("description"),
        )
        self.assertEqual("true", adapter.string("disable-model-invocation"), "GitHub Copilot CLI's switch")
        self.assertEqual(
            "policy:\n  allow_implicit_invocation: false\n",
            (self.agents_dir / "alpha" / "agents" / "openai.yaml").read_text(encoding="utf-8"),
            "Codex's switch",
        )
        self.make_skill("alpha", "Alpha", description='"Delete stale branches across every repository."')
        self.deploy_ok("--all")
        self.assertEqual("Delete stale branches across every repository.", self.adapter("alpha").string("description"))
        self.assertNotIn("disable-model-invocation", self.adapter("alpha"))
        self.assertFalse((self.agents_dir / "alpha" / "agents").exists())

    def test_description_longer_than_runtimes_accept_stops_before_any_change(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha", description="x" * 1024)
        self.make_skill("beta", "Beta", description="x" * 1025)
        self.make_config()
        self.deploy_fails("--all", pattern="Skill 'beta' description is 1025 characters; .* at most 1024")
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.agents_dir / "alpha").exists())

    def test_description_with_an_xml_tag_stops_before_any_change(self) -> None:
        self.make_source_json()
        self.make_skill(
            "alpha", "Alpha", description='"Compare a < b and c > d, then map x -> y. Use it when sorting."'
        )
        self.make_skill("beta", "Beta", description='"Wrap the answer in <tag> markup. Use it when testing."')
        self.make_config()
        result = self.deploy_fails(
            "--all",
            pattern=re.escape(
                "ERROR: Skill 'beta' description contains the XML tag '<tag>', "
                "which the Agent Skills frontmatter rules forbid"
            ),
        )
        self.assertIn(
            'Remove the tag, or write it without angle brackets; see "Files" in docs/adding-a-skill.md.',
            result.output.splitlines(),
        )
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.agents_dir / "alpha").exists())
        self.make_skill("beta", "Beta", description='"Close the </tag> and <tag attr=\\"x\\"/>. Use it when testing."')
        self.deploy_fails("--all", pattern="description contains the XML tag '</tag>'")
        self.remove_skill("beta")
        self.make_source_json()
        self.deploy_ok("--all")
        self.assertEqual(
            "Compare a < b and c > d, then map x -> y. Use it when sorting.",
            self.adapter("alpha").string("description"),
        )


class SelectionTests(DeployerTestCase):
    def test_invalid_and_out_of_range_selections_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_fails(stdin="x\n", pattern="Invalid selection 'x'")
        self.deploy_fails(stdin="9\n", pattern="Selection '9' is out of range")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_empty_selection_exits_cleanly_without_changes(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        result = self.deploy_ok(stdin="\n")
        self.assertIn("No skills selected.", result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_a_menu_answer_naming_an_item_twice_requests_it_once(self) -> None:
        self.make_source_json(bundles={"b": {"members": ["alpha"]}})
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        bundle, skill = self.selection_number("b", bundle=True), self.selection_number("beta")
        result = self.deploy_ok(stdin=f"{bundle} {bundle} {skill} {skill}\n")
        self.assertIn("Selected 2 items (2 skills):\n", result.output)
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["b"], entry["requested_bundles"])
        self.assertEqual({"b": ["alpha"]}, entry["requested_bundle_members"])
        self.assertEqual(["beta"], entry["requested_skills"])

    def test_an_unknown_include_named_twice_is_named_once(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        result = self.deploy_fails(
            "--all", "--include", "nothing", "--include", "nothing", code=2, pattern="no bundle or skill in this source"
        )
        self.assertIn("--include names no bundle or skill in this source: nothing\n", result.output)

    def test_unknown_arguments_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_fails("--bogus", code=2, pattern="unrecognized arguments: --bogus")
        self.deploy_fails("--force-item", code=2, pattern="argument --force-item: expected one argument")


class OptInTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json(bundles={"pack": {"members": ["fmt"], "opt_in": True}})
        self.make_skill("everyday", "Everyday")
        self.make_skill("lint", "Lint", opt_in=True)
        self.make_skill("fmt", "Format")
        self.make_config()

    def installed(self) -> list[str]:
        return sorted(path.name for path in self.skills_dir.iterdir() if path.is_dir())

    def test_all_skips_opt_in_items_and_the_menu_labels_them(self) -> None:
        self.assertIn("Selected 1 item (1 skill):\n  everyday\n", self.deploy_ok("--all").output)
        self.assertEqual(["everyday"], self.installed())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. pack \(bundle, opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. lint \(opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. everyday$")

    def test_include_adds_opt_in_items_and_later_runs_keep_them(self) -> None:
        self.deploy_ok("--all", "--include", "lint", "--include", "pack")
        self.assertEqual(["everyday", "fmt", "lint"], self.installed())
        self.assertIn("UNCHANGED (3):", self.deploy_ok("--all", "--dry-run").output)
        self.deploy_ok("--all")
        self.assertEqual(["everyday", "fmt", "lint"], self.installed())
        self.deploy_ok(stdin="all\n")
        self.assertEqual(["everyday", "fmt", "lint"], self.installed())

    def test_a_skill_that_joins_a_bundle_keeps_being_deployed_and_the_bundle_completes(self) -> None:
        # Deployed on its own, before the bundle and the skill that joins it existed.
        self.make_source_json()
        self.remove_skill("fmt")
        self.deploy_ok("--all", "--include", "lint")
        self.assertEqual(["everyday", "lint"], self.installed())
        # The next source puts it in an opt-in bundle with a new skill; --include names the bundle now.
        self.make_source_json(bundles={"pack": {"members": ["lint", "fmt"], "opt_in": True}})
        self.make_skill("lint", "Lint")
        self.make_skill("fmt", "Format")
        self.deploy_fails(
            "--all", "--include", "lint", code=2, pattern="--include names no bundle or skill in this source: lint"
        )
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$")
        self.deploy_ok("--all")
        self.assertEqual(["everyday", "fmt", "lint"], self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual(["everyday"], entry["requested_skills"])

    def dependency_member_fixture(self) -> None:
        """An opt-in bundle one of whose members another, everyday, skill depends on."""
        self.make_source_json(bundles={"pack": {"members": ["fmt", "extra"], "opt_in": True}})
        self.make_skill("everyday", "Everyday", skill_deps=["fmt"])
        self.make_skill("extra", "Extra")

    def test_a_bundle_member_installed_only_as_a_dependency_leaves_the_opt_in_bundle_out_of_all(self) -> None:
        self.dependency_member_fixture()
        selected = "Selected 1 item (2 skills):\n  everyday\n    fmt (dependency)\n"
        self.assertIn(selected, self.deploy_ok("--all").output)
        self.assertEqual(["everyday", "fmt"], self.installed())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. pack \(bundle, opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. everyday$")
        dry_run = self.deploy_ok("--all", "--dry-run").output
        deployed = self.deploy_ok("--all").output
        for output, title in ((dry_run, "DRY RUN"), (deployed, "DEPLOYED")):
            with self.subTest(title=title):
                self.assertIn(selected, output)
                self.assertNotIn("pack (bundle)", output)
                self.assertEqual({"UNCHANGED": ["everyday", "fmt"]}, self.report_groups(output, title))
        self.assertEqual(["everyday", "fmt"], self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual([], entry["requested_bundles"])
        self.assertEqual(["everyday"], entry["requested_skills"])

    def test_an_opt_in_bundle_requested_beside_a_skill_depending_on_its_member_stays_in_all(self) -> None:
        self.dependency_member_fixture()
        self.deploy_ok("--all", "--include", "pack")
        self.assertEqual(["everyday", "extra", "fmt"], self.installed())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$")
        selected = "Selected 2 items (3 skills):\n  pack (bundle)\n    extra\n    fmt\n  everyday\n"
        dry_run = self.deploy_ok("--all", "--dry-run").output
        deployed = self.deploy_ok("--all").output
        for output, title in ((dry_run, "DRY RUN"), (deployed, "DEPLOYED")):
            with self.subTest(title=title):
                self.assertIn(selected, output)
                self.assertEqual({"UNCHANGED": ["everyday", "extra", "fmt"]}, self.report_groups(output, title))
        self.assertEqual(["everyday", "extra", "fmt"], self.installed())
        self.assertEqual(["pack"], self.manifest()["sources"]["test/skills"]["requested_bundles"])

    def test_an_opt_in_skill_installed_only_as_a_dependency_is_not_made_a_requested_root(self) -> None:
        self.make_skill("everyday", "Everyday", skill_deps=["lint"])
        self.assertIn(
            "Selected 1 item (2 skills):\n  everyday\n    lint (dependency)\n", self.deploy_ok("--all").output
        )
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[ \] [0-9]+\. lint \(opt-in\)$")
        self.deploy_ok("--all")
        self.assertEqual(["everyday"], self.manifest()["sources"]["test/skills"]["requested_skills"])
        # Once nothing needs it, the dependency goes with the skill that pulled it in.
        self.make_skill("everyday", "Everyday")
        self.deploy_ok("--all")
        self.assertEqual(["everyday"], self.installed())

    def regrouped(
        self,
        bundles: dict[str, Any],
        opt_in_skills: dict[str, str] | None = None,
        preserved: str | None = None,
        installed_since: tuple[str, ...] = (),
        deselected: bool = False,
    ) -> dict[str, list[str]]:
        """Regroup as `regroup` does and return the next --all's report; the dry run and the deployment must select
        and report the same items."""
        self.regroup(bundles, opt_in_skills, preserved, installed_since, deselected=deselected)
        return self.next_all()

    def next_all(self) -> dict[str, list[str]]:
        """Run --all and return its report; the dry run and the deployment must select and report the same items."""
        dry_run = self.report_groups(self.deploy_ok("--all", "--dry-run").output, "DRY RUN")
        deployed = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual(dry_run, deployed)
        return deployed

    def assert_next_all(
        self, unchanged: list[str], installed: tuple[str, ...] = (), removed: tuple[str, ...] = ()
    ) -> None:
        """Run --all as a dry run and then for real, and require each to report these skills unchanged, installed, and
        removed, under its own labels."""
        for arguments, title, labels in (
            (("--all", "--dry-run"), "DRY RUN", ("FRESH INSTALL", "REMOVE")),
            (("--all",), "DEPLOYED", ("INSTALLED", "REMOVED")),
        ):
            with self.subTest(title=title):
                groups = self.report_groups(self.deploy_ok(*arguments).output, title)
                expected = {"UNCHANGED": unchanged, labels[0]: list(installed), labels[1]: list(removed)}
                self.assertEqual(
                    {label: names for label, names in expected.items() if names},
                    {label: [line.split()[0] for line in lines] for label, lines in groups.items()},
                )

    def regroup(
        self,
        bundles: dict[str, Any],
        opt_in_skills: dict[str, str] | None = None,
        preserved: str | None = None,
        installed_since: tuple[str, ...] = (),
        also_included: dict[str, Any] | None = None,
        deselected: bool = False,
    ) -> None:
        """Deploy opt-in bundle pack [fmt, x], and each bundle also_included gives, by request, then regroup the
        source, making each skill opt_in_skills names an opt-in root with the content it gives.

        With `preserved`, the manifest is first rewritten as the first deployer to write version 8 left it after
        preserving that skill through its upgrade of a version 7 manifest, in a deployment that also installed the
        skills `installed_since` names, which the caller made in the source, and, with `deselected`, did not request
        the preserved skill.
        """
        included = {"pack": {"members": ["fmt", "x"], "opt_in": True}, **(also_included or {})}
        self.make_source_json(bundles=included)
        self.make_skill("x", "X")
        self.deploy_ok("--all", *(argument for name in included for argument in ("--include", name)))
        self.assertEqual(sorted(["everyday", "fmt", "x", *installed_since]), self.installed())
        self.assertEqual(
            {name: sorted(bundle["members"]) for name, bundle in included.items()},
            self.manifest()["sources"]["test/skills"]["requested_bundle_members"],
        )
        if preserved is not None:
            self.rewrite_as_preserved_through_the_upgrade(
                preserved, installed_since=installed_since, deselected=deselected
            )
        self.make_source_json(bundles=bundles)
        for name, content in (opt_in_skills or {}).items():
            self.make_skill(name, content, opt_in=True)

    def test_a_requested_bundle_the_source_renames_stays_in_all_under_its_new_name(self) -> None:
        report = self.regrouped({"pack2": {"members": ["fmt", "x"], "opt_in": True}})
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "x"], self.installed())
        self.assertRegex(
            self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. pack2 \(bundle, opt-in\)$"
        )
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack2"], entry["requested_bundles"])
        self.assertEqual({"pack2": ["fmt", "x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday"], entry["requested_skills"])

    def test_a_skill_that_leaves_a_requested_bundle_for_an_opt_in_root_of_its_own_stays_in_all(self) -> None:
        report = self.regrouped({"pack": {"members": ["x"], "opt_in": True}}, {"fmt": "Format"})
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "x"], self.installed())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. fmt \(opt-in\)$")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual({"pack": ["x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "fmt"], entry["requested_skills"])

    def test_a_bundle_request_recorded_before_its_members_were_stands_for_the_bundle_the_source_defines(self) -> None:
        self.deploy_ok("--all", "--include", "pack")
        data = self.manifest()
        del data["sources"]["test/skills"]["requested_bundle_members"]
        self.write_manifest(data)
        report = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt"]}, report)
        self.assertEqual({"pack": ["fmt"]}, self.manifest()["sources"]["test/skills"]["requested_bundle_members"])

    def drop_bundle_members(self, keep_key: bool = False) -> None:
        """Rewrite the entry as a deployer that recorded no bundle members wrote it, or, with keep_key, as a hand edit
        that keeps requested_bundle_members but records no members for the bundle requested."""
        data = self.manifest()
        if keep_key:
            data["sources"]["test/skills"]["requested_bundle_members"] = {}
        else:
            del data["sources"]["test/skills"]["requested_bundle_members"]
        self.write_manifest(data)

    def test_a_bundle_request_recorded_before_its_members_were_stays_in_all_when_the_source_renames_it(self) -> None:
        self.assert_renamed_bundle_stays_without_its_members(keep_key=False)

    def test_a_bundle_request_whose_members_the_entry_lacks_stays_in_all_when_the_source_renames_it(self) -> None:
        # Every deployment records the members of each bundle it requests, so only a hand edit leaves the key without
        # them; an entry that records the key but not a requested bundle's members records that request's no better
        # than an entry without the key, so its installed roots stand for it too.
        self.assert_renamed_bundle_stays_without_its_members(keep_key=True)

    def assert_renamed_bundle_stays_without_its_members(self, keep_key: bool) -> None:
        self.make_source_json(bundles={"pack": {"members": ["fmt", "x"], "opt_in": True}})
        self.make_skill("x", "X")
        self.deploy_ok("--all", "--include", "pack")
        self.drop_bundle_members(keep_key)
        self.make_source_json(bundles={"pack2": {"members": ["fmt", "x"], "opt_in": True}})
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack2 \(bundle, opt-in\)$")
        dry_run = self.report_groups(self.deploy_ok("--all", "--dry-run").output, "DRY RUN")
        deployed = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, dry_run)
        self.assertEqual(dry_run, deployed)
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack2"], entry["requested_bundles"])
        self.assertEqual({"pack2": ["fmt", "x"]}, entry["requested_bundle_members"])

    def test_a_bundle_request_in_a_version_7_manifest_stays_in_all(self) -> None:
        self.make_source_json(bundles={"pack": {"members": ["fmt", "x"], "opt_in": True}})
        self.make_skill("x", "X")
        self.deploy_ok("--all", "--include", "pack")
        # An earlier --migrate-from recorded no bundle request, the source renames the bundle, or neither happened.
        for requested, bundle in (([], "pack"), (["pack"], "pack2"), (["pack"], "pack")):
            with self.subTest(requested=requested, bundle=bundle):
                self.make_source_json(bundles={bundle: {"members": ["fmt", "x"], "opt_in": True}})
                self.rewrite_as_version_7(requested)
                menu = self.deploy("--dry-run", stdin="\n").output
                self.assertRegex(menu, rf"(?m)^  \[\*\] [0-9]+\. {bundle} \(bundle, opt-in\)$")
                dry_run = self.report_groups(self.deploy_ok("--all", "--dry-run").output, "DRY RUN")
                deployed = self.report_groups(self.deploy_ok("--all").output, "DEPLOYED")
                self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, dry_run)
                self.assertEqual(dry_run, deployed)
                entry = self.manifest()["sources"]["test/skills"]
                self.assertEqual([bundle], entry["requested_bundles"])
                self.assertEqual({bundle: ["fmt", "x"]}, entry["requested_bundle_members"])

    def test_a_renamed_bundle_beside_a_skill_preserved_through_the_version_8_upgrade_stays_in_all(self) -> None:
        # everyday's filled-in dependencies name both members and lie on no cycle, since fmt and x record exact ones,
        # but the source declares no dependency of everyday's, so they explain neither.
        report = self.regrouped({"kit": {"members": ["fmt", "x"], "opt_in": True}}, preserved="everyday")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "x"], self.installed())
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. kit \(bundle, opt-in\)$")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x"]}, entry["requested_bundle_members"])

    def test_a_member_moved_out_beside_a_skill_preserved_through_the_version_8_upgrade_stays_in_all(self) -> None:
        report = self.regrouped({"pack": {"members": ["x"], "opt_in": True}}, {"fmt": "Format"}, preserved="everyday")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "x"], self.installed())
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$")
        self.assertRegex(menu, r"(?m)^  \[\*\] [0-9]+\. fmt \(opt-in\)$")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual({"pack": ["x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "fmt"], entry["requested_skills"])

    def test_a_member_moved_out_beside_a_member_preserved_through_the_version_8_upgrade_stays_in_all(self) -> None:
        # fmt's filled-in dependencies name everyday, which is requested on its own, and the member x.
        report = self.regrouped({"pack": {"members": ["fmt"], "opt_in": True}}, {"x": "X"}, preserved="fmt")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "x"], self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual({"pack": ["fmt"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "x"], entry["requested_skills"])

    def test_a_renamed_bundle_beside_a_skill_preserved_by_a_deployment_that_installed_a_new_one_stays_in_all(
        self,
    ) -> None:
        # everyday's filled-in dependencies name fmt and x but not newer, which the version 7 manifest did not own.
        self.make_skill("newer", "Newer")
        report = self.regrouped(
            {"kit": {"members": ["fmt", "x"], "opt_in": True}}, preserved="everyday", installed_since=("newer",)
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "newer", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "newer", "x"], self.installed())
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. kit \(bundle, opt-in\)$")
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "newer"], entry["requested_skills"])

    def test_a_renamed_bundle_beside_a_skill_preserved_by_a_deployment_that_installed_a_new_dependency_stays_in_all(
        self,
    ) -> None:
        # The new skill brought a new opt-in dependency, which newer's exact list records and the source still declares,
        # so helper stays a dependency while everyday's filled-in list explains neither member.
        self.make_skill("newer", "Newer", skill_deps=["helper"])
        self.make_skill("helper", "Helper", opt_in=True)
        report = self.regrouped(
            {"kit": {"members": ["fmt", "x"], "opt_in": True}},
            preserved="everyday",
            installed_since=("helper", "newer"),
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "helper", "newer", "x"]}, report)
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual(["everyday", "newer"], entry["requested_skills"])

    def test_a_member_moved_out_beside_a_member_preserved_by_a_deployment_that_installed_a_new_one_stays_in_all(
        self,
    ) -> None:
        self.make_skill("newer", "Newer")
        report = self.regrouped(
            {"pack": {"members": ["fmt"], "opt_in": True}}, {"x": "X"}, preserved="fmt", installed_since=("newer",)
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "newer", "x"]}, report)
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual({"pack": ["fmt"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "newer", "x"], entry["requested_skills"])

    def test_a_renamed_bundle_beside_a_skill_preserved_by_a_deployment_that_included_a_new_bundle_stays_in_all(
        self,
    ) -> None:
        # That deployment installed np through a bundle request whose members it did not record, so no request on its
        # own reaches np, which everyday's filled-in dependencies do not name; the source declares no dependency of
        # everyday's, so that list explains neither fmt nor x.
        self.make_skill("np", "Np")
        pack2 = {"pack2": {"members": ["np"], "opt_in": True}}
        self.regroup(
            {"kit": {"members": ["fmt", "x"], "opt_in": True}, **pack2},
            preserved="everyday",
            installed_since=("np",),
            also_included=pack2,
        )
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. kit \(bundle, opt-in\)$")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "np", "x"]}, self.next_all())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit", "pack2"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x"], "pack2": ["np"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday"], entry["requested_skills"])

    def test_a_renamed_bundle_beside_a_skill_preserved_beside_a_new_dependency_chain_stays_in_all(self) -> None:
        # newer and the opt-in chain it depends on are new: helper2 is reached from the request only through helper.
        self.make_skill("newer", "Newer", skill_deps=["helper"])
        self.make_skill("helper", "Helper", skill_deps=["helper2"], opt_in=True)
        self.make_skill("helper2", "Helper 2", opt_in=True)
        report = self.regrouped(
            {"kit": {"members": ["fmt", "x"], "opt_in": True}},
            preserved="everyday",
            installed_since=("helper", "helper2", "newer"),
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "helper", "helper2", "newer", "x"]}, report)
        self.assertEqual(["kit"], self.manifest()["sources"]["test/skills"]["requested_bundles"])

    def test_a_renamed_bundle_beside_a_preserved_skill_the_deployment_did_not_select_stays_in_all(self) -> None:
        # Neither everyday nor a skill its filled-in dependencies name is requested on its own; newer is.
        self.make_skill("newer", "Newer")
        report = self.regrouped(
            {"kit": {"members": ["fmt", "x"], "opt_in": True}},
            preserved="everyday",
            installed_since=("newer",),
            deselected=True,
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "newer", "x"]}, report)
        self.assertEqual(["everyday", "fmt", "newer", "x"], self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "newer"], entry["requested_skills"])

    def preserve_beside_a_skill_whose_list_contains_it(self) -> None:
        """Deploy opt-in bundle pack [fmt, x] by request beside newer, which depends on everyday, fmt, x, and helper,
        all requested on their own, and rewrite the manifest as the first deployer to write version 8 left it after
        preserving everyday, in a deployment that installed newer and helper: newer's exact list, with newer, names
        everyday, every skill everyday's filled-in list names, and more."""
        self.make_source_json(bundles={"pack": {"members": ["fmt", "x"], "opt_in": True}})
        self.make_skill("x", "X")
        self.make_skill("helper", "Helper")
        self.make_skill("newer", "Newer", skill_deps=["everyday", "fmt", "helper", "x"])
        self.deploy_ok("--all", "--include", "pack")
        self.assertEqual(["everyday", "fmt", "helper", "newer", "x"], self.installed())
        self.rewrite_as_preserved_through_the_upgrade("everyday", installed_since=("helper", "newer"))
        self.assertEqual(["fmt", "x"], self.owned("skills")["everyday"]["skill_deps"])
        self.assertEqual(["everyday", "fmt", "helper", "x"], self.owned("skills")["newer"]["skill_deps"])

    def test_a_requested_bundle_beside_a_preserved_skill_whose_list_a_new_skills_list_contains_stays_in_all(
        self,
    ) -> None:
        self.preserve_beside_a_skill_whose_list_contains_it()
        self.assertRegex(
            self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$"
        )
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "helper", "newer", "x"]}, self.next_all())
        self.assertEqual({"pack": ["fmt", "x"]}, self.manifest()["sources"]["test/skills"]["requested_bundle_members"])

    def test_a_renamed_bundle_beside_a_preserved_skill_whose_list_a_new_skills_list_contains_stays_in_all(
        self,
    ) -> None:
        # newer's exact list contains everyday's filled-in one, and the source no longer has newer depend on fmt and
        # x, so neither list explains them and they stand for the request.
        self.preserve_beside_a_skill_whose_list_contains_it()
        self.make_source_json(bundles={"kit": {"members": ["fmt", "x"], "opt_in": True}})
        self.make_skill("newer", "Newer", skill_deps=["helper"])
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. kit \(bundle, opt-in\)$")
        self.assertEqual({"UNCHANGED": ["everyday", "fmt", "helper", "newer", "x"]}, self.next_all())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x"]}, entry["requested_bundle_members"])
        self.assertEqual(["everyday", "helper", "newer"], entry["requested_skills"])

    def test_a_renamed_bundle_that_gained_a_member_beside_a_preserved_skill_stays_in_all_and_completes(self) -> None:
        # No dependency the source declares explains fmt or x, so they stand for the request, and --all keeps kit and
        # installs y, the member it gained, as it does for a bundle requested under its current name.
        self.regroup({"kit": {"members": ["fmt", "x", "y"], "opt_in": True}}, preserved="everyday")
        self.make_skill("y", "Y")
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. kit \(bundle, opt-in\)$")
        self.assert_next_all(["everyday", "fmt", "x"], installed=("y",))
        self.assertEqual(["everyday", "fmt", "x", "y"], self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["kit"], entry["requested_bundles"])
        self.assertEqual({"kit": ["fmt", "x", "y"]}, entry["requested_bundle_members"])

    def assert_a_dependency_only_member_stays_unchosen(self, installed_since: tuple[str, ...] = ()) -> None:
        """Deploy everyday, tool, which depends on helper, a member of an opt-in bundle hb nobody requested, and opt-in
        bundle pack [fmt, x] by request, and rewrite the manifest as the first deployer to write version 8 left it after
        preserving everyday, whose filled-in dependencies name every other skill, in a deployment that also installed
        the skills installed_since names, which they therefore leave out. A plain --all and the menu leave hb unchosen
        and install nothing.
        """
        self.make_source_json(
            bundles={
                "hb": {"members": ["helper", "mate"], "opt_in": True},
                "pack": {"members": ["fmt", "x"], "opt_in": True},
            }
        )
        self.make_skill("tool", "Tool", skill_deps=["helper"])
        self.make_skill("helper", "Helper")
        self.make_skill("mate", "Mate")
        self.make_skill("x", "X")
        self.deploy_ok("--all", "--include", "pack")
        installed = sorted({"everyday", "fmt", "helper", "tool", "x", *installed_since})
        self.assertEqual(installed, self.installed())
        requested = self.manifest()["sources"]["test/skills"]["requested_skills"]
        self.rewrite_as_preserved_through_the_upgrade("everyday", installed_since=installed_since)
        self.assertEqual(
            sorted(set(installed) - {"everyday", *installed_since}), self.owned("skills")["everyday"]["skill_deps"]
        )
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[ \] [0-9]+\. hb \(bundle, opt-in\)$")
        for arguments, title in ((("--all", "--dry-run"), "DRY RUN"), (("--all",), "DEPLOYED")):
            with self.subTest(title=title):
                output = self.deploy_ok(*arguments).output
                self.assertNotIn("hb (bundle)", output)
                self.assertEqual({"UNCHANGED": installed}, self.report_groups(output, title))
        self.assertEqual(installed, self.installed())
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual(requested, entry["requested_skills"])

    def test_a_dependency_only_member_beside_a_skill_preserved_through_the_version_8_upgrade_leaves_its_bundle_out(
        self,
    ) -> None:
        # everyday's filled-in dependencies name every other skill, tool and helper among them, and tool's exact list
        # names helper, which the source still declares, so helper stays a dependency.
        self.assert_a_dependency_only_member_stays_unchosen()

    def test_a_dependency_only_member_beside_a_skill_preserved_beside_a_newly_installed_one_leaves_its_bundle_out(
        self,
    ) -> None:
        # everyday's filled-in dependencies leave out newer, which that deployment installed, and still name tool,
        # helper, and more.
        self.make_skill("newer", "Newer")
        self.assert_a_dependency_only_member_stays_unchosen(("newer",))

    def test_a_dependency_only_member_beside_a_skill_preserved_by_the_deployment_that_installed_its_dependent_stays_out(
        self,
    ) -> None:
        # tool and helper are new, so everyday's filled-in dependencies name only fmt and x; tool's exact list names
        # helper, which the source still declares, so helper stays a dependency and hb, which would install mate,
        # stays unchosen.
        self.assert_a_dependency_only_member_stays_unchosen(("helper", "tool"))

    def assert_an_entry_recorded_before_bundle_members_installs_nothing_unchosen(
        self, bundles: dict[str, Any], skill_deps: dict[str, list[str]]
    ) -> None:
        """Deploy the skills skill_deps gives, with those dependencies, and opt-in bundle pack by request, from a source
        whose opt-in bundle hb nobody requests, and drop the bundle members as an earlier release recorded none. A
        plain --all and the menu leave hb unchosen and install nothing."""
        self.make_source_json(bundles={"hb": {"members": bundles["hb"], "opt_in": True}, "pack": bundles["pack"]})
        for name, dependencies in skill_deps.items():
            self.make_skill(name, name.title(), skill_deps=dependencies)
        self.deploy_ok("--all", "--include", "pack")
        installed = self.installed()
        self.assertNotIn("mate", installed)
        self.drop_bundle_members()
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[ \] [0-9]+\. hb \(bundle, opt-in\)$")
        for arguments, title in ((("--all", "--dry-run"), "DRY RUN"), (("--all",), "DEPLOYED")):
            with self.subTest(title=title):
                output = self.deploy_ok(*arguments).output
                self.assertNotIn("hb (bundle)", output)
                self.assertEqual({"UNCHANGED": installed}, self.report_groups(output, title))
        self.assertEqual(["pack"], self.manifest()["sources"]["test/skills"]["requested_bundles"])

    def test_an_entry_recorded_before_bundle_members_does_not_choose_a_requested_skills_opt_in_dependency(
        self,
    ) -> None:
        # r's exact dependencies name helper, and the source still declares them.
        self.assert_an_entry_recorded_before_bundle_members_installs_nothing_unchosen(
            {"hb": ["helper", "mate"], "pack": {"members": ["p1", "p2"], "opt_in": True}},
            {"r": ["helper"], "helper": [], "mate": [], "p1": [], "p2": []},
        )

    def test_an_entry_recorded_before_bundle_members_reads_a_list_by_what_other_requests_reach(self) -> None:
        # r depends on beta and beta on gamma, pack's only member, as the entry records and the source declares, so
        # beta stays a dependency.
        self.assert_an_entry_recorded_before_bundle_members_installs_nothing_unchosen(
            {"hb": ["beta", "mate"], "pack": {"members": ["gamma"], "opt_in": True}},
            {"r": ["beta"], "beta": ["gamma"], "gamma": [], "mate": []},
        )

    def test_an_entry_recorded_before_bundle_members_keeps_out_a_bundle_another_requests_list_reaches_around(
        self,
    ) -> None:
        # a depends on p1 and r on helper, and the source declares both, however the lists compare with each other.
        self.assert_an_entry_recorded_before_bundle_members_installs_nothing_unchosen(
            {"hb": ["helper", "mate"], "pack": {"members": ["p1"], "opt_in": True}},
            {"a": ["p1"], "r": ["helper"], "helper": [], "mate": [], "p1": []},
        )

    def test_an_entry_recorded_before_bundle_members_keeps_out_a_bundle_when_two_requests_reach_around_each_other(
        self,
    ) -> None:
        # r's and s's exact lists each leave out what the other names, and the source declares both.
        self.assert_an_entry_recorded_before_bundle_members_installs_nothing_unchosen(
            {"hb": ["helper", "mate"], "pack": {"members": ["p1"], "opt_in": True}},
            {"r": ["helper"], "s": ["p1"], "helper": [], "mate": [], "p1": []},
        )

    def test_an_entry_recorded_before_bundle_members_were_counts_no_dependency_as_chosen(self) -> None:
        self.dependency_member_fixture()
        self.deploy_ok("--all")
        self.drop_bundle_members()
        menu = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. pack \(bundle, opt-in\)$")
        result = self.deploy_ok("--all").output
        self.assertIn("Selected 1 item (2 skills):\n  everyday\n    fmt (dependency)\n", result)
        self.assertEqual({"UNCHANGED": ["everyday", "fmt"]}, self.report_groups(result, "DEPLOYED"))
        self.assertEqual([], self.manifest()["sources"]["test/skills"]["requested_bundles"])

    def test_a_bundle_member_installed_only_as_a_dependency_in_a_version_7_manifest_stays_unchosen(self) -> None:
        # The upgrade fills in fmt's and everyday's lists with each other, a cycle; the source declares everyday's
        # dependency on fmt and not fmt's on everyday, so fmt stays a dependency and pack, whose extra is not installed,
        # stays unchosen.
        self.dependency_member_fixture()
        self.deploy_ok("--all")
        self.rewrite_as_version_7([])
        self.assertRegex(self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[ \] [0-9]+\. pack \(bundle, opt-in\)$")
        result = self.deploy_ok("--all").output
        self.assertIn("Selected 1 item (2 skills):\n  everyday\n    fmt (dependency)\n", result)
        self.assertEqual({"UNCHANGED": ["everyday", "fmt"]}, self.report_groups(result, "DEPLOYED"))
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual([], entry["requested_bundles"])
        self.assertEqual(["everyday"], entry["requested_skills"])

    def test_a_dependency_the_source_drops_keeps_its_skill_in_an_entry_recorded_before_bundle_members(self) -> None:
        # The entry records everyday's dependency on fmt, but the source no longer declares it, so it explains nothing:
        # an entry that recorded no bundle members cannot tell fmt from a member of a bundle request, and --all keeps
        # it and completes its bundle rather than remove a skill the user may have chosen.
        self.dependency_member_fixture()
        self.deploy_ok("--all")
        self.drop_bundle_members()
        self.make_skill("everyday", "Everyday")
        self.assertRegex(
            self.deploy("--dry-run", stdin="\n").output, r"(?m)^  \[\*\] [0-9]+\. pack \(bundle, opt-in\)$"
        )
        self.assert_next_all(["everyday", "fmt"], installed=("extra",))
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(["pack"], entry["requested_bundles"])
        self.assertEqual({"pack": ["extra", "fmt"]}, entry["requested_bundle_members"])

    def test_a_dependency_the_source_drops_removes_its_skill_in_an_entry_that_records_bundle_members(self) -> None:
        # The same history in an entry that records its bundle members is read as recorded: fmt was no request's.
        self.dependency_member_fixture()
        self.deploy_ok("--all")
        self.make_skill("everyday", "Everyday")
        self.assert_next_all(["everyday"], removed=("fmt",))
        self.assertEqual(["everyday"], self.installed())

    def test_a_bundle_member_installed_only_as_a_dependency_stays_unchosen_when_the_source_regroups_it(self) -> None:
        self.dependency_member_fixture()
        self.deploy_ok("--all")
        self.assertEqual({}, self.manifest()["sources"]["test/skills"]["requested_bundle_members"])
        self.make_source_json(bundles={"pack2": {"members": ["fmt", "extra"], "opt_in": True}})
        result = self.deploy_ok("--all").output
        self.assertIn("Selected 1 item (2 skills):\n  everyday\n    fmt (dependency)\n", result)
        self.assertEqual({"UNCHANGED": ["everyday", "fmt"]}, self.report_groups(result, "DEPLOYED"))
        self.make_source_json(bundles={"pack": {"members": ["extra"], "opt_in": True}})
        self.make_skill("fmt", "Format", opt_in=True)
        result = self.deploy_ok("--all").output
        self.assertIn("Selected 1 item (2 skills):\n  everyday\n    fmt (dependency)\n", result)
        self.assertEqual(["everyday", "fmt"], self.installed())
        self.assertEqual(["everyday"], self.manifest()["sources"]["test/skills"]["requested_skills"])

    def test_menu_selection_installs_an_opt_in_item(self) -> None:
        self.deploy_ok(stdin=f"{self.selection_number('lint')} {self.selection_number('everyday')}\n")
        self.assertEqual(["everyday", "lint"], self.installed())
        self.deploy_ok("--all")
        self.assertEqual(["everyday", "lint"], self.installed())

    def test_include_needs_all_and_a_known_name(self) -> None:
        for arguments, message in (
            (("--include", "lint", "--dry-run"), "--include can only be used with --all"),
            (
                ("--all", "--include", "missing", "--dry-run"),
                "--include names no bundle or skill in this source: missing",
            ),
            (("--all", "--include", "fmt", "--dry-run"), "--include names no bundle or skill in this source: fmt"),
        ):
            with self.subTest(arguments=arguments):
                result = self.deploy_fails(*arguments, code=2, pattern=message)
                self.assertEqual(
                    f"\nERROR: {message}\nRun '{DEPLOY_COMMAND} --help' for usage.\n\n",
                    result.output,
                )


class RepositoryBundleTests(DeployerTestCase):
    def test_the_dotnet_bundle_installs_both_dotnet_skills_as_one_opt_in_root(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        repository = self.repository_source()
        self.deploy_fails(
            "--all",
            "--include",
            "dotnet-format",
            source=repository,
            code=2,
            pattern="--include names no bundle or skill",
        )
        menu = self.deploy("--dry-run", stdin="\n", source=repository).output
        self.assertRegex(menu, r"(?m)^  \[ \] [0-9]+\. dotnet \(bundle, opt-in\)$")
        self.assertNotRegex(menu, r"(?m)^  \[ \] [0-9]+\. dotnet-(format|dependencies)")
        result = self.deploy_from(repository, "--all", "--include", "dotnet")
        self.assertIn("  dotnet (bundle)\n    dotnet-dependencies\n    dotnet-format\n", result.output)
        for name in ("dotnet-format", "dotnet-dependencies"):
            self.assertTrue((self.skills_dir / name / "scripts").is_dir(), name)
            self.assertTrue((self.agents_dir / name / "SKILL.md").is_file(), name)
        entry = self.manifest()["sources"][source_id]
        self.assertEqual(["code-review-operations", "dotnet"], entry["requested_bundles"])
        self.assertIn("dotnet-dependencies", entry["skills"])
        self.assertEqual(["skill-core"], entry["skills"]["dotnet-dependencies"]["skill_deps"])
        # A later --all keeps the bundle without naming it again.
        groups = self.report_groups(self.deploy_from(repository, "--all", "--dry-run").output, "DRY RUN")
        self.assertNotIn("REMOVE", groups, groups)


if __name__ == "__main__":
    unittest.main()
