from __future__ import annotations

import hashlib
import json
import re
import unittest
from typing import Any

from harness import DeployerTestCase, forward

from deployer import frontmatter

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
        self.assertEqual(7, self.manifest()["manifest_version"])
        self.assertEqual(["operations"], entry["requested_bundles"])
        self.assertEqual([], entry["requested_skills"])
        self.assertEqual(["alpha", "beta", "core"], sorted(entry["selected_skills"]))
        self.assertEqual(["alpha", "beta"], sorted(entry["wrappers"]))
        rerun = self.deploy("--dry-run", stdin="\n").output
        self.assertRegex(rerun, r"(?m)^  \[\*\] [0-9]+\. operations \(bundle\)$")

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
        self.assertEqual([], entry["selected_skills"])

    def test_a_member_retired_from_an_installed_bundle_is_removed_and_the_bundle_kept(self) -> None:
        # Retiring a skill from a bundle (as re-review was, #116) relies on the next deployment removing it.
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
        self.assertEqual(["alpha", "core"], sorted(entry["selected_skills"]))
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
        self.assertIn("check which copy Copilot uses with 'python deploy.py verify'", result.output)
        self.assertIn(forward(self.home / ".copilot" / "skills" / "alpha"), result.output)
        self.assertEqual("native Copilot skill\n", native.read_text(encoding="utf-8"))
        self.assertTrue((self.agents_dir / "alpha" / "SKILL.md").is_file())

    def test_older_and_newer_manifest_versions_fail_closed_without_modification(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_ok("--all")
        for version, message in ((5, "unsupported schema version '5'"), (99, "newer than supported version 7")):
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

    def test_unknown_arguments_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_config()
        self.deploy_fails("--bogus", pattern="unrecognized arguments: --bogus")
        self.deploy_fails("--force-item", pattern="argument --force-item: expected one argument")


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

    def test_menu_selection_installs_an_opt_in_item(self) -> None:
        self.deploy_ok(stdin=f"{self.selection_number('lint')} {self.selection_number('everyday')}\n")
        self.assertEqual(["everyday", "lint"], self.installed())
        self.deploy_ok("--all")
        self.assertEqual(["everyday", "lint"], self.installed())

    def test_include_needs_all_and_a_known_name(self) -> None:
        self.deploy_fails("--include", "lint", "--dry-run", pattern="--include can only be used with --all")
        self.deploy_fails(
            "--all",
            "--include",
            "missing",
            "--dry-run",
            pattern="--include names no bundle or skill in this source: missing",
        )
        self.deploy_fails(
            "--all", "--include", "fmt", "--dry-run", pattern="--include names no bundle or skill in this source: fmt"
        )


if __name__ == "__main__":
    unittest.main()
