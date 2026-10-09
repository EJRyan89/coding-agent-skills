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

from deployer import cli, config, names, pipeline, source
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

    def test_runtime_support_names_every_runtime_with_a_known_level_need_and_reason(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test")
        self.make_config()
        partial = {"level": "partial", "needs": ["user-only-start"], "reason": "Start it interactively."}
        cases = [
            ("full", "runtime_support must be an object keyed by runtime"),
            ({"claude-code": "full", "codex": "full"}, "runtime_support does not declare copilot-cli"),
            (
                {"claude-code": "full", "codex": "full", "copilot-cli": "full", "gemini": "full"},
                "runtime_support names unknown runtime 'gemini'",
            ),
            ({"claude-code": "full", "codex": "yes", "copilot-cli": "full"}, 'codex must be "full" or an object'),
            (
                {"claude-code": "full", "codex": "full", "copilot-cli": {**partial, "needs": ["telepathy"]}},
                "copilot-cli needs unknown capability 'telepathy'",
            ),
            (
                {"claude-code": "full", "codex": "full", "copilot-cli": {**partial, "needs": []}},
                "copilot-cli is partial but names no need",
            ),
            (
                {"claude-code": "full", "codex": "full", "copilot-cli": {**partial, "reason": " "}},
                "copilot-cli is partial without a one-line reason",
            ),
            (
                {"claude-code": "full", "codex": {"level": "none"}, "copilot-cli": "full"},
                "codex is none without a one-line reason",
            ),
            (
                {"claude-code": "full", "codex": "full", "copilot-cli": {**partial, "level": "most"}},
                "copilot-cli has level 'most'; an object's level is partial or none",
            ),
        ]
        for value, message in cases:
            with self.subTest(message=message):
                self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"runtime_support": value}))
                self.deploy_fails("--all", pattern=re.escape(f"deploy-meta/alpha.json: {message}"))
        declared = {
            "claude-code": "full",
            "codex": {"level": "none", "reason": "Not on Codex."},
            "copilot-cli": partial,
        }
        self.write(self.source / "deploy-meta" / "alpha.json", json.dumps({"runtime_support": declared}))
        self.deploy_ok("--all")
        loaded = source.discover(self.paths, source.load_source_id(self.paths))
        support = loaded.skills["alpha"].runtime_support
        self.assertEqual(
            {runtime: (value.level, value.needs, value.reason) for runtime, value in (support or {}).items()},
            {
                "claude-code": ("full", (), ""),
                "codex": ("none", (), "Not on Codex."),
                "copilot-cli": ("partial", ("user-only-start",), "Start it interactively."),
            },
        )

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

    def test_names_the_deployer_reserves_for_its_own_files_are_rejected(self) -> None:
        for asset, reason in (
            ("alpha.deploying-bak", "ends in '.deploying-bak'"),
            ("Alpha.Deploying-Bak", "ends in '.deploying-bak'"),
            ("notes.tmp.md", "contains '.tmp.'"),
            ("Notes.TMP.md", "contains '.tmp.'"),
        ):
            with self.subTest(asset=asset):
                self.make_source_json(shared_assets={asset: "owner"})
                self.make_shared_asset(asset, "Shared")
                self.make_skill("alpha", "Test", shared_deps=[asset])
                self.make_config()
                self.deploy_fails(
                    "--all",
                    pattern=re.escape(
                        f"ERROR: shared asset name '{asset}' {reason}, which the deployer reserves for its own files"
                    ),
                )
                self.assertFalse(self.manifest_file.exists())
                (self.source / "skills" / asset).unlink()

    def test_every_reader_of_item_names_refuses_a_reserved_name(self) -> None:
        for name in ("alpha.deploying-bak", "ALPHA.DEPLOYING-BAK", "a.tmp.1", "agent.TMP.md"):
            with self.subTest(name=name):
                self.assertIn("reserves for its own files", names.safe_name_problem(name, "item") or "")
        for name in ("alpha", "notes.md", "deploying-bak.md", "tmp.md", "alpha.deploying-bak.md"):
            with self.subTest(name=name):
                self.assertIsNone(names.safe_name_problem(name, "item"))

    def write_shared_assets(self, value: object) -> None:
        self.make_source_json()
        document = json.loads((self.source / "source.json").read_text(encoding="utf-8"))
        document["shared_assets"] = value
        (self.source / "source.json").write_text(json.dumps(document), encoding="utf-8")

    def test_shared_assets_that_is_not_an_object_fails_discovery(self) -> None:
        self.make_skill("alpha", "Test")
        self.make_config()
        for value in (["shared.md"], "shared.md", None):
            for arguments in (("--all", "--dry-run"), ("check",)):
                with self.subTest(value=value, arguments=arguments):
                    self.write_shared_assets(value)
                    captured = io.StringIO()
                    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                        code = cli.main(list(arguments), self.paths, io.StringIO(""))
                    self.assertEqual(1, code, captured.getvalue())
                    self.assertIn(
                        "ERROR: source.json shared_assets must be an object mapping each asset to its role",
                        captured.getvalue(),
                    )

    def test_a_role_that_is_not_a_string_fails_discovery_naming_the_asset(self) -> None:
        self.make_skill("alpha", "Test")
        self.make_config()
        for role in (1, None, ["owner"]):
            with self.subTest(role=role):
                self.write_shared_assets({"shared.md": role})
                result = self.deploy_fails(
                    "--all",
                    "--dry-run",
                    pattern="ERROR: source.json shared_assets 'shared.md' has a role that is not a string",
                )
                self.assertEqual(1, result.code)


class LinkedWorktreeTestCase(DeployerTestCase):
    """A source checkout made a git repository with a linked worktree, for the classes below."""

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

    def deploy_fails_from(self, directory: Path, *arguments: str) -> Result:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = pipeline.run(list(arguments), Paths(directory, self.home), stdin=io.StringIO(""))
        self.assertNotEqual(0, code, captured.getvalue())
        return Result(code, captured.getvalue())


class SourceCheckoutTests(LinkedWorktreeTestCase):
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


class OtherCheckoutTests(LinkedWorktreeTestCase):
    """The manifest records the checkout each source deploys from; another checkout must take the source over."""

    def deployed_clone(self) -> Path:
        self.make_source_json()
        self.make_skill("alpha", "Alpha from the first clone")
        self.make_config()
        self.deploy_ok("--all")
        clone = self.snapshot_source("Second Clone (2)")
        skill = clone / "skills" / "alpha" / "SKILL.md"
        skill.write_bytes(skill.read_bytes().replace(b"first", b"second"))
        return clone

    def recorded_source_dir(self) -> str:
        return forward(Path(self.manifest()["sources"]["test/skills"]["source_dir"]))

    def refusal(self, clone: Path, gone: str = "") -> str:
        return (
            f"ERROR: Source 'test/skills' is deployed from {forward(self.source)}{gone}, not from this checkout, "
            f"{forward(clone)}.\n"
            f"Deploy from {forward(self.source)}; or, if this checkout replaces it, rerun with --take-over-source to "
            "record this checkout as its source.\n"
            'See "Deploying from another checkout" in docs/recovery.md.\n'
        )

    def assert_refused_unchanged(self, clone: Path, *arguments: str, gone: str = "") -> None:
        manifest = self.manifest_file.read_bytes()
        result = self.deploy_fails_from(clone, *arguments)
        self.assertIn(self.refusal(clone, gone), result.output)
        self.assertEqual(manifest, self.manifest_file.read_bytes())
        self.assertIn("Alpha from the first clone", self.skill_text("alpha"))
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_a_second_clone_of_the_same_source_is_refused_before_any_change(self) -> None:
        clone = self.deployed_clone()
        self.assert_refused_unchanged(clone, "--all")
        self.deploy_ok("--all")

    def test_a_recorded_checkout_that_no_longer_exists_is_named_as_gone(self) -> None:
        clone = self.deployed_clone()
        shutil.rmtree(self.source)
        self.assert_refused_unchanged(clone, "--all", gone=", which no longer exists")

    def test_take_over_source_records_the_new_checkout_and_deploys_from_it(self) -> None:
        clone = self.deployed_clone()
        result = self.deploy_from(clone, "--all", "--take-over-source")
        self.assertIn(
            f"Taking over source 'test/skills' from {forward(self.source)}: 1 skill, 0 shared assets, "
            "1 runtime adapter, 0 agents.\n"
            f"This checkout, {forward(clone)}, is recorded as their source when this deployment commits.\n",
            result.output,
        )
        self.assertEqual(forward(clone), self.recorded_source_dir())
        self.assertIn("Alpha from the second clone", self.skill_text("alpha"))
        self.assertNotIn("Taking over", self.deploy_from(clone, "--all").output)
        result = self.deploy_fails_from(self.source, "--all")
        self.assertIn(f"ERROR: Source 'test/skills' is deployed from {forward(clone)}, not from this", result.output)

    def test_an_uninstall_removes_every_kind_and_keeps_the_source_entry_and_its_checkout(self) -> None:
        """As docs/installation.md's "Uninstalling" says: a later clone must still take the source over."""
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_shared_asset("shared.md")
        self.make_agent("reviewer")
        self.make_skill("alpha", "Alpha from the first clone", shared_deps=["shared.md"], agent_deps=["reviewer"])
        self.make_config()
        self.deploy_ok("--all")
        installed = [
            self.skills_dir / "alpha",
            self.skills_dir / "shared.md",
            self.agents_dir / "alpha",
            self.claude_agents_dir / "reviewer.md",
        ]
        self.assertTrue(all(path.exists() for path in installed))
        self.deploy_ok(stdin="none\n")
        self.assertEqual([], [path for path in installed if path.exists()])
        entry = self.manifest()["sources"]["test/skills"]
        self.assertEqual(forward(self.source), self.recorded_source_dir())
        self.assertEqual(
            {"skills": {}, "shared": {}, "wrappers": {}, "agents": {}},
            {key: entry[key] for key in ("skills", "shared", "wrappers", "agents")},
        )
        clone = self.snapshot_source("Second Clone (2)")
        self.assert_refused_unchanged_after_uninstall(clone)
        self.deploy_from(clone, "--all", "--take-over-source")
        self.assertEqual(forward(clone), self.recorded_source_dir())

    def assert_refused_unchanged_after_uninstall(self, clone: Path) -> None:
        manifest = self.manifest_file.read_bytes()
        result = self.deploy_fails_from(clone, "--all")
        self.assertIn(self.refusal(clone), result.output)
        self.assertEqual(manifest, self.manifest_file.read_bytes())
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_take_over_source_from_the_recorded_checkout_changes_nothing_about_it(self) -> None:
        self.deployed_clone()
        result = self.deploy_ok("--all", "--take-over-source")
        self.assertNotIn("Taking over", result.output)
        self.assertEqual(forward(self.source), self.recorded_source_dir())

    def test_migration_from_a_second_clone_needs_take_over_source(self) -> None:
        self.make_source_json("test/old")
        self.make_skill("beta", "Beta")
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.remove_skill("beta")
        clone = self.deployed_clone()
        self.make_skill("beta", "Beta")
        shutil.copytree(self.source / "skills" / "beta", clone / "skills" / "beta")
        shutil.copy2(self.source / "deploy-meta" / "beta.json", clone / "deploy-meta" / "beta.json")
        self.assert_refused_unchanged(clone, "--migrate-from", "test/old")
        self.assertIn("beta", self.owned("skills", "test/old"))
        result = self.deploy_from(clone, "--migrate-from", "test/old", "--take-over-source")
        self.assertIn("Moved ownership from 'test/old' to 'test/skills'.", result.output)
        self.assertIn("beta", self.owned("skills"))
        self.assertEqual(forward(clone), self.recorded_source_dir())

    def test_take_over_source_with_nothing_to_migrate_still_records_the_checkout(self) -> None:
        self.make_source_json("test/old")
        self.make_skill("beta", "Beta")
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.remove_skill("beta")
        clone = self.deployed_clone()
        manifest = self.manifest_file.read_bytes()
        self.assert_refused_unchanged(clone, "--migrate-from", "test/old")
        self.assertEqual(manifest, self.manifest_file.read_bytes())
        result = self.deploy_from(clone, "--migrate-from", "test/old", "--take-over-source")
        self.assertIn("No intersecting ownership entries to migrate from 'test/old'.", result.output)
        self.assertIn(f"Recorded this checkout, {forward(clone)}, as the source of 'test/skills'.", result.output)
        self.assertEqual(forward(clone), self.recorded_source_dir())
        self.assertIn("beta", self.owned("skills", "test/old"))
        self.assertEqual(["alpha"], list(self.owned("skills")))
        self.assertNotIn("Taking over", self.deploy_from(clone, "--all").output)
        self.assertIn("Alpha from the second clone", self.skill_text("alpha"))

    def test_nothing_to_migrate_from_the_recorded_checkout_changes_nothing(self) -> None:
        self.make_source_json("test/old")
        self.make_skill("beta", "Beta")
        self.make_config("test/old")
        self.deploy_from(self.snapshot_source("old"), "--all")
        self.remove_skill("beta")
        self.deployed_clone()
        manifest = self.manifest_file.read_bytes()
        result = self.deploy_ok("--migrate-from", "test/old", "--take-over-source")
        self.assertIn("No intersecting ownership entries to migrate from 'test/old'.", result.output)
        self.assertNotIn("Recorded this checkout", result.output)
        self.assertEqual(manifest, self.manifest_file.read_bytes())

    def test_a_linked_worktree_is_refused_even_with_take_over_source(self) -> None:
        linked = self.make_linked_worktree()
        self.deploy_ok("--all")
        result = self.deploy_fails_from(linked, "--all", "--take-over-source")
        self.assertIn(f"ERROR: {forward(linked)} is a linked git worktree.", result.output)
        self.assertEqual(forward(self.source), self.recorded_source_dir())

    def test_a_dry_run_from_a_second_clone_is_allowed(self) -> None:
        clone = self.deployed_clone()
        self.deploy_from(clone, "--all", "--dry-run")

    def test_take_over_source_cannot_be_combined_with_a_dry_run(self) -> None:
        self.deployed_clone()
        self.deploy_fails(
            "--all", "--take-over-source", "--dry-run", pattern="--take-over-source cannot be combined with --dry-run"
        )


class CanaryHomeTests(LinkedWorktreeTestCase):
    """--canary-home deploys into a throwaway home, which is allowed from a linked worktree."""

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

    def test_dry_run_migration_and_take_over_cannot_use_a_canary_home(self) -> None:
        linked = self.make_linked_worktree()
        for extra, pattern in (
            (("--dry-run",), "--canary-home cannot be combined with --dry-run"),
            (("--migrate-from", "test/old"), "--canary-home cannot be combined with --migrate-from"),
            (("--take-over-source",), "--canary-home cannot be combined with --take-over-source"),
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
