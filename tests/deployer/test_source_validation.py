from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from harness import DeployerTestCase, Result, forward

from deployer import config, pipeline, source
from deployer.errors import DeployError
from deployer.paths import Paths


def make_junction(link, target) -> None:
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


class ConfigValidationTests(DeployerTestCase):
    def test_spaces_in_configured_path_expand(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", 'for d in "{{REPOS_ROOT}}"/*/; do echo "$d"; done', ["REPOS_ROOT"])
        self.make_config(repos_root=self.root / "My Projects")
        self.deploy_ok("--all")
        self.assertIn(forward(self.root / "My Projects"), self.skill_text("alpha"))
        self.assertNotIn("{{REPOS_ROOT}}", self.skill_text("alpha"))

    def test_disallowed_character_in_path_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.write(self.config_file(), f"_source_id=test/skills\nREPOS_ROOT={forward(self.root)}/A&B\n")
        self.deploy_fails("--all", pattern="contains disallowed character '&' at position")

    def other_home(self, name: str) -> Path:
        home = self.root / name
        for directory in (".claude/skills", ".claude/deployer/config", ".claude/deployer/staging"):
            (home / directory).mkdir(parents=True)
        self.make_config()
        shutil.copy2(self.config_file(), home / self.config_file().relative_to(self.home))
        return home

    def deploy_into(self, home: Path, source: Path | None = None) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(["--all"], Paths(source or self.source, home), stdin=io.StringIO(""))
        return Result(code, captured.getvalue())

    def test_a_home_path_outside_the_derived_allowlist_is_refused_before_any_change(self) -> None:
        # Rendering escapes only the contexts it knows, so the allowlist guards derived values as it guards
        # configured ones.
        self.make_source_json()
        self.make_skill("alpha", "Home {{HOME}}", ["HOME"])
        for name, character in (("O'Neil", "'"), ("Tom & Jerry", "&")):
            with self.subTest(home=name):
                home = self.other_home(name)
                result = self.deploy_into(home)
                self.assertEqual(1, result.code, result.output)
                position = forward(home).index(character)
                self.assertIn(
                    f"ERROR: HOME (the home folder) contains disallowed character '{character}' at position "
                    f"{position}\n"
                    "Derived paths may contain only letters, digits, spaces, and / : . @ _ ( ) -.\n"
                    "Skills cannot be deployed into a home folder whose path has other characters.\n",
                    result.output,
                )
                self.assertNotIn("configure", result.output)
                self.assertEqual([], sorted(path.name for path in (home / ".claude" / "skills").iterdir()))

    def test_a_source_checkout_path_outside_the_derived_allowlist_is_refused(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Source {{SOURCE_ROOT}}", ["SOURCE_ROOT"])
        self.make_config()
        checkout = self.snapshot_source("checkout #2")
        result = self.deploy_into(self.home, checkout)
        self.assertEqual(1, result.code, result.output)
        self.assertIn(
            "ERROR: SOURCE_ROOT (the source checkout) contains disallowed character '#' at position "
            f"{forward(checkout).index('#')}\n"
            "Derived paths may contain only letters, digits, spaces, and / : . @ _ ( ) -.\n"
            "Move the checkout to a path without other characters, then deploy from there.\n",
            result.output,
        )
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_home_path_with_letters_of_any_script_and_spaces_deploys(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Home {{HOME}}", ["HOME"])
        home = self.other_home("José Łukasz (home)")
        result = self.deploy_into(home)
        self.assertEqual(0, result.code, result.output)
        self.assertIn(
            f"Home {forward(home)}", (home / ".claude" / "skills" / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        )

    def test_missing_config_is_reported_and_nothing_is_touched(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.deploy_fails("--all", pattern="No config found")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_unknown_token_fails_staging(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{UNKNOWN_VAR}}")
        self.make_config()
        result = self.deploy_fails("--all", pattern="Unexpanded tokens found in staged output")
        self.assertIn("alpha/SKILL.md:", result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_crlf_config_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.repos.mkdir()
        self.config_file().write_bytes(f"_source_id=test/skills\r\nREPOS_ROOT={forward(self.repos)}\r\n".encode())
        self.deploy_fails("--all", pattern="CRLF")

    def test_duplicate_config_key_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test", ["REPOS_ROOT"])
        self.make_config(extra=f"REPOS_ROOT={forward(self.root)}/repos2\n")
        self.deploy_fails("--all", pattern="Duplicate config key: REPOS_ROOT")

    def test_shell_active_characters_are_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test", ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\nREPOS_ROOT=$(whoami)\n")
        self.deploy_fails("--all", pattern="shell-active")

    def test_unrecognized_config_key_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.make_config(extra="GH_ORG=someone\n")
        self.deploy_fails("--all", pattern="Config key GH_ORG is not a recognized variable")

    def test_config_source_id_mismatch_is_rejected(self) -> None:
        self.make_source_json("real/source")
        self.make_skill("alpha", "Test")
        self.repos.mkdir()
        self.write(self.config_file("real/source"), f"_source_id=wrong/source\nREPOS_ROOT={forward(self.repos)}\n")
        self.deploy_fails("--all", pattern="does not match source.json")

    def test_repos_root_filesystem_root_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\nREPOS_ROOT=C:/\n")
        self.deploy_fails("--all", pattern="must not be a filesystem root")

    def test_unsupported_platform_is_rejected_before_any_work(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.make_config()
        with mock.patch("deployer.platform_support.sys.platform", "linux"):
            self.deploy_fails("--all", pattern="supports Windows only")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_missing_required_variable_names_the_skill(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Root {{REPOS_ROOT}}", ["REPOS_ROOT"])
        self.write(self.config_file(), "_source_id=test/skills\n")
        result = self.deploy_fails("--all", pattern="require variables not set in config: REPOS_ROOT")
        self.assertIn("  alpha -> REPOS_ROOT", result.output)


class MetadataValidationTests(DeployerTestCase):
    def test_invalid_skill_name_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("INVALID_NAME", "Test")
        self.make_config()
        self.deploy_fails("--all", pattern="does not match naming grammar")

    def test_skill_name_of_64_characters_is_accepted(self) -> None:
        name = "a" * 64
        self.make_source_json()
        self.make_skill(name, "Test")
        self.make_config()
        self.deploy_ok("--all")
        self.assertTrue((self.skills_dir / name / "SKILL.md").is_file())

    def test_skill_name_of_65_characters_is_rejected(self) -> None:
        name = "a" * 65
        self.make_source_json()
        self.make_skill(name, "Test")
        self.make_config()
        self.deploy_fails("--all", pattern=f"Skill name '{name}' exceeds 64 characters")
        self.assertFalse((self.skills_dir / name).exists())

    def test_skill_names_use_lowercase_digits_and_inner_hyphens(self) -> None:
        for name in ("a", "alpha-2", "2-alpha"):
            with self.subTest(name=name):
                self.make_source_json()
                self.make_skill(name, "Test")
                self.make_config()
                self.deploy_ok("--all")
                self.remove_skill(name)
                self.deploy_ok("--all")
        for name in ("-alpha", "alpha-", "alpha_beta", "alpha.beta", "Alpha"):
            with self.subTest(name=name):
                self.make_source_json()
                self.make_skill(name, "Test")
                self.make_config()
                self.deploy_fails("--all", pattern=f"Skill name '{re.escape(name)}' does not match naming grammar")
                self.remove_skill(name)

    def test_bundle_name_length_is_limited_to_64_characters(self) -> None:
        self.make_skill("alpha", "Test")
        self.make_config()
        self.make_source_json(bundles={"b" * 64: {"members": ["alpha"]}})
        self.deploy_ok("--all")
        self.make_source_json(bundles={"b" * 65: {"members": ["alpha"]}})
        self.deploy_fails("--all", pattern=f"Bundle name '{'b' * 65}' is invalid")

    def test_windows_reserved_skill_name_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("con", "Test")
        self.make_config()
        self.deploy_fails("--all", pattern="reserved name")

    def test_frontmatter_name_must_match_directory(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Test")
        skill_md = directory / "SKILL.md"
        self.write(skill_md, skill_md.read_text(encoding="utf-8").replace("name: alpha", "name: wrong"))
        self.make_config()
        self.deploy_fails("--all", pattern="frontmatter name 'wrong' does not match directory")

    def test_frontmatter_name_may_be_quoted(self) -> None:
        self.make_source_json()
        skill_md = self.make_skill("alpha", "Test") / "SKILL.md"
        self.make_config()
        for declared in ('"alpha"', "'alpha'", '"al\\u0070ha"', "alpha  # comment"):
            with self.subTest(declared=declared):
                self.write(skill_md, f"---\nname: {declared}\ndescription: Test\n---\n\nBody.\n")
                result = self.deploy("--all", "--dry-run")
                self.assertEqual(0, result.code, result.output)

    def test_keys_the_deployer_does_not_read_may_be_structured(self) -> None:
        self.make_source_json()
        skill_md = self.make_skill("alpha", "Test") / "SKILL.md"
        self.write(
            skill_md,
            "---\nname: alpha\ndescription: >-\n  Folded across\n  two lines.\n"
            'hooks:\n  PreToolUse:\n    - matcher: "Bash"\n---\n\nBody.\n',
        )
        self.make_config()
        result = self.deploy("--all", "--dry-run")
        self.assertEqual(0, result.code, result.output)

    def test_unreadable_frontmatter_is_rejected_with_its_reason(self) -> None:
        self.make_source_json()
        skill_md = self.make_skill("alpha", "Test") / "SKILL.md"
        self.make_config()
        cases = {
            "---\nname: alpha\n": "frontmatter is not closed",
            "# No frontmatter\n": "no frontmatter",
            "---\nname: alpha\nname: alpha\n---\n": "name appears more than once",
            "---\nname:\n  nested: alpha\n---\n": "name: a nested mapping is not supported",
            "---\nname: [alpha]\n---\n": "name must be a single value",
        }
        for text, reason in cases.items():
            with self.subTest(reason=reason):
                self.write(skill_md, text)
                self.deploy_fails("--all", pattern=re.escape(f"Skill 'alpha' frontmatter cannot be read: {reason}"))

    def test_skill_without_metadata_is_rejected(self) -> None:
        self.make_source_json()
        (self.source / "skills" / "orphan").mkdir()
        self.write(
            self.source / "skills" / "orphan" / "SKILL.md", '---\nname: orphan\ndescription: "Orphan skill"\n---\n'
        )
        self.make_config()
        self.deploy_fails("--all", pattern="has no matching deploy-meta/orphan.json")

    def test_duplicate_flattened_skill_names_are_rejected(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Alpha content")
        (self.source / "skills" / "category" / "alpha").mkdir(parents=True)
        shutil.copy(directory / "SKILL.md", self.source / "skills" / "category" / "alpha" / "SKILL.md")
        self.make_config()
        self.deploy_fails("--all", pattern="Duplicate skill directory name 'alpha'")

    def test_skill_directory_containing_nested_skill_md_is_rejected(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Alpha content")
        (directory / "nested").mkdir()
        self.write(directory / "nested" / "SKILL.md", "---\nname: nested\n---\n")
        self.make_config()
        self.deploy_fails("--all", pattern="contains another SKILL.md")

    def test_invalid_metadata_shape_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"required_vars": "HOME"}))
        self.make_config()
        self.deploy_fails("--all", pattern="deploy-meta/alpha.json has an invalid metadata shape")

    def test_declared_tools_must_be_a_list_of_known_tools(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.make_config()
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"tools": "gh"}))
        self.deploy_fails("--all", pattern="deploy-meta/alpha.json has an invalid metadata shape")
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"tools": ["gh", "jq"]}))
        self.deploy_fails(
            "--all", pattern=r"Skill 'alpha' declares unknown tool 'jq' \(known tools: copilot, dotnet-format, gh\)"
        )
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"tools": ["gh", "copilot", "gh"]}))
        self.deploy_ok("--all")

    def test_optional_tools_are_known_tools_a_skill_does_not_also_require(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.make_config()
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"optional_tools": "gh"}))
        self.deploy_fails("--all", pattern="deploy-meta/alpha.json has an invalid metadata shape")
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"optional_tools": ["jq"]}))
        self.deploy_fails(
            "--all", pattern=r"Skill 'alpha' declares unknown tool 'jq' \(known tools: copilot, dotnet-format, gh\)"
        )
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"tools": ["gh"], "optional_tools": ["gh"]}))
        self.deploy_fails("--all", pattern="Skill 'alpha' declares tool 'gh' both required and optional")
        self.write(
            self.source / "deploy-meta" / "alpha.json",
            json.dumps({"tools": ["dotnet-format"], "optional_tools": ["gh"]}),
        )
        self.deploy_ok("--all")

    def test_opt_in_is_a_flag_on_menu_items_only(self) -> None:
        self.make_config()
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"opt_in": "yes"}))
        self.deploy_fails("--all", pattern="deploy-meta/alpha.json has an invalid metadata shape")
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"opt_in": True, "selectable": False}))
        self.deploy_fails("--all", pattern="Skill 'alpha' is opt-in but not selectable; only menu items can be opt-in")
        self.make_skill("alpha", "Test", opt_in=True)
        self.make_source_json(bundles={"pack": {"members": ["alpha"]}})
        self.deploy_fails(
            "--all", pattern="Skill 'alpha' is opt-in but belongs to bundle 'pack'; mark the bundle opt-in instead"
        )
        self.make_skill("alpha", "Test")
        self.make_source_json(bundles={"pack": {"members": ["alpha"], "opt_in": "yes"}})
        self.deploy_fails("--all", pattern="Bundle 'pack' opt_in must be true or false")

    def test_windows_junction_in_source_is_rejected(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Alpha content")
        outside = self.root / "outside-source"
        outside.mkdir()
        self.write(outside / "data.txt", "outside\n")
        make_junction(directory / "external", outside)
        self.make_config()
        self.deploy_fails("--all", pattern="(?i)Reparse point found")
        self.assertFalse((self.skills_dir / "alpha").exists())


class SharedAssetValidationTests(DeployerTestCase):
    def test_missing_owned_shared_asset_is_rejected(self) -> None:
        self.make_source_json(shared_assets={"nonexistent.md": "owner"})
        self.make_skill("alpha", "Test")
        self.make_config()
        self.deploy_fails("--all", pattern="declares 'nonexistent.md' as owned but file not found")

    def test_undeclared_shared_dependency_is_rejected(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test", shared_deps=["nonexistent-dep.md"])
        self.make_config()
        self.deploy_fails("--all", pattern="declares shared_dep 'nonexistent-dep.md' but it is not in source.json")

    def test_skill_and_shared_asset_names_cannot_collide(self) -> None:
        self.make_source_json(shared_assets={"alpha": "owner"})
        self.make_skill("alpha", "Skill alpha", category="category")
        self.make_shared_asset("alpha", "Shared alpha")
        self.make_config()
        self.deploy_fails("--all", pattern="collides with a skill of the same name")

    def test_unsafe_shared_asset_name_is_rejected(self) -> None:
        self.make_source_json(shared_assets={"../escape.md": "owner"})
        self.make_skill("alpha", "Test")
        self.make_config()
        self.deploy_fails("--all", pattern="shared asset name '../escape.md' contains path separator")


class SourceCheckoutTests(DeployerTestCase):
    def git(self, *arguments: str) -> None:
        empty = self.root / "empty.gitconfig"
        empty.touch()
        environment = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": str(empty),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        }
        subprocess.run(["git", "-C", str(self.source), *arguments], check=True, capture_output=True, env=environment)

    def make_linked_worktree(self) -> Path:
        self.make_source_json()
        self.make_skill("alpha", "Worktree fixture")
        self.make_config()
        self.git("init", "-q", "-b", "main")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "fixture")
        linked = self.root / "Linked Tree (task)"
        self.git("worktree", "add", "-q", "-b", "task", str(linked))
        return linked

    def test_deploying_from_a_linked_worktree_is_refused_before_any_change(self) -> None:
        linked = self.make_linked_worktree()
        result = self.deploy_fails_from(linked, "--all")
        self.assertIn(f"ERROR: {forward(linked)} is a linked git worktree.", result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse(self.manifest_file.exists())
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_a_dry_run_from_a_linked_worktree_and_a_deploy_from_the_main_checkout_are_allowed(self) -> None:
        linked = self.make_linked_worktree()
        self.deploy_from(linked, "--all", "--dry-run")
        self.deploy_from(self.source, "--all")
        manifest = json.loads(self.manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(forward(self.source), forward(manifest["sources"]["test/skills"]["source_dir"]))

    def test_a_git_file_without_a_common_directory_is_not_a_linked_worktree(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Submodule-style fixture")
        self.make_config()
        module = self.root / "modules" / "source"
        module.mkdir(parents=True)
        self.write(self.source / ".git", f"gitdir: {forward(module)}\n")
        self.assertFalse(source.is_linked_worktree(self.source))
        self.deploy_ok("--all")

    def deploy_fails_from(self, directory: Path, *arguments: str) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), Paths(directory, self.home), stdin=io.StringIO(""))
        self.assertNotEqual(0, code, captured.getvalue())
        return Result(code, captured.getvalue())


class CanaryHomeTests(DeployerTestCase):
    """--canary-home deploys into a throwaway home, which is allowed from a linked worktree."""

    git = SourceCheckoutTests.git
    make_linked_worktree = SourceCheckoutTests.make_linked_worktree

    def setUp(self) -> None:
        super().setUp()
        self.canary = self.root / "Canary Home (1)"
        self.canary.mkdir()

    def run_from(self, directory: Path, *arguments: str) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), Paths(directory, self.home), stdin=io.StringIO(""))
        return Result(code, captured.getvalue())

    def assert_untouched(self, result: Result, pattern: str) -> None:
        self.assertNotEqual(0, result.code, result.output)
        self.assertRegex(result.output, pattern)
        self.assertEqual([], list(self.canary.iterdir()))
        self.assertFalse(self.manifest_file.exists())

    def test_a_linked_worktree_deploys_into_an_empty_temporary_home(self) -> None:
        linked = self.make_linked_worktree()
        self.make_skill("beta", "Repositories under {{REPOS_ROOT}}", ["REPOS_ROOT"])
        shutil.copytree(self.source / "skills" / "beta", linked / "skills" / "beta")
        shutil.copy2(self.source / "deploy-meta" / "beta.json", linked / "deploy-meta" / "beta.json")
        # The real home's configuration is never read: this one would fail to parse.
        self.write(self.config_file(), "not a configuration\n")
        result = self.run_from(linked, "--canary-home", str(self.canary), "--all")
        self.assertEqual(0, result.code, result.output)
        manifest = json.loads(
            (self.canary / ".claude" / "skills" / ".deploy-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(forward(linked), forward(manifest["sources"]["test/skills"]["source_dir"]))
        self.assertTrue((self.canary / ".agents" / "skills" / "alpha" / "SKILL.md").is_file())
        rendered = (self.canary / ".claude" / "skills" / "beta" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"Repositories under {forward(self.canary)}/repos", rendered)
        self.assertTrue((self.canary / "repos").is_dir())
        self.assertTrue((self.canary / ".deploy-canary-home").is_file())
        self.assertEqual([], list(self.canary.rglob("*.config")))
        self.assertEqual(b"not a configuration\n", self.config_file().read_bytes())
        self.assertFalse(self.manifest_file.exists())
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_a_marked_canary_home_takes_a_second_source(self) -> None:
        linked = self.make_linked_worktree()
        self.assertEqual(0, self.run_from(linked, "--canary-home", str(self.canary), "--all").code)
        other = self.root / "other"
        (other / "skills" / "gamma").mkdir(parents=True)
        (other / "deploy-meta").mkdir()
        (other / "source.json").write_text(json.dumps({"id": "test/other"}), encoding="utf-8")
        self.write(other / "skills" / "gamma" / "SKILL.md", '---\nname: gamma\ndescription: "Gamma"\n---\n\nGamma\n')
        (other / "deploy-meta" / "gamma.json").write_text("{}", encoding="utf-8")
        result = self.run_from(other, "--canary-home", str(self.canary), "--all")
        self.assertEqual(0, result.code, result.output)
        manifest = json.loads(
            (self.canary / ".claude" / "skills" / ".deploy-manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual({"test/skills", "test/other"}, set(manifest["sources"]))

    def test_a_directory_outside_the_temporary_directory_is_refused_before_any_change(self) -> None:
        linked = self.make_linked_worktree()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        with mock.patch("tempfile.gettempdir", return_value=str(elsewhere)):
            result = self.run_from(linked, "--canary-home", str(self.canary), "--all")
        self.assert_untouched(result, "--canary-home must be inside the temporary directory")

    def test_the_temporary_directory_itself_is_refused(self) -> None:
        linked = self.make_linked_worktree()
        with mock.patch("tempfile.gettempdir", return_value=str(self.canary)):
            result = self.run_from(linked, "--canary-home", str(self.canary), "--all")
        self.assert_untouched(result, "--canary-home must be inside the temporary directory")

    def test_a_directory_with_unmarked_content_is_refused(self) -> None:
        linked = self.make_linked_worktree()
        (self.canary / ".claude").mkdir()
        result = self.run_from(linked, "--canary-home", str(self.canary), "--all")
        self.assertNotEqual(0, result.code, result.output)
        self.assertIn(
            "--canary-home must be an empty directory or one an earlier --canary-home deployment used", result.output
        )
        self.assertEqual([".claude"], [entry.name for entry in self.canary.iterdir()])

    def test_a_relative_missing_or_file_path_and_a_junction_are_refused(self) -> None:
        linked = self.make_linked_worktree()
        target = self.root / "target"
        target.mkdir()
        junction = self.root / "junction"
        make_junction(junction, target)
        (self.root / "file").write_text("", encoding="utf-8")
        for value, pattern in (
            ("relative-home", "--canary-home must be an absolute path"),
            (str(self.root / "missing"), "--canary-home must be an existing directory"),
            (str(self.root / "file"), "--canary-home must be an existing directory"),
            (str(junction), "--canary-home must not be a symlink or junction"),
        ):
            with self.subTest(value=value):
                self.assert_untouched(self.run_from(linked, "--canary-home", value, "--all"), pattern)
        self.assertEqual([], list(target.iterdir()))

    def test_dry_run_and_migration_cannot_use_a_canary_home(self) -> None:
        linked = self.make_linked_worktree()
        for extra, pattern in (
            (("--dry-run",), "--canary-home cannot be combined with --dry-run"),
            (("--migrate-from", "test/old"), "--canary-home cannot be combined with --migrate-from"),
        ):
            with self.subTest(extra=extra):
                self.assert_untouched(self.run_from(linked, "--canary-home", str(self.canary), *extra), pattern)

    def test_every_configured_variable_has_a_canary_folder(self) -> None:
        self.assertEqual({"REPOS_ROOT": "repos"}, config.CANARY_DIRECTORIES)
        self.assertEqual(set(config.CONFIGURED_VARIABLES), set(config.CANARY_DIRECTORIES))

    def test_without_the_flag_a_linked_worktree_is_still_refused(self) -> None:
        linked = self.make_linked_worktree()
        result = self.run_from(linked, "--all")
        self.assert_untouched(result, "is a linked git worktree")


class DerivedAllowlistTests(unittest.TestCase):
    def test_derived_paths_admit_letters_of_any_script_and_path_punctuation_only(self) -> None:
        source = Path("C:/src")
        for name in ("Jos\u00e9", "\u0141ukasz M\u00fcller", "\u7530\u4e2d", "first.last@corp", "a_b-c (2)"):
            with self.subTest(allowed=name):
                home = f"D:/profiles/{name}"
                self.assertEqual(home, config.derived_values(Path(home), source)["HOME"])
        for character in "'\"$`&;#%!^=+,~[]{}":
            with self.subTest(refused=character):
                with self.assertRaisesRegex(DeployError, re.escape(f"disallowed character '{character}'")):
                    config.derived_values(Path(f"D:/profiles/a{character}b"), source)
                with self.assertRaisesRegex(DeployError, re.escape(f"disallowed character '{character}'")):
                    config.derived_values(Path("D:/profiles/a"), Path(f"C:/src/a{character}b"))
        self.assertEqual({"HOME", "SOURCE_ROOT"}, set(config.derived_values(Path("D:/profiles/a"), source)))


if __name__ == "__main__":
    unittest.main()
