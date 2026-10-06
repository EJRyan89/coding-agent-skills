from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from harness import REPOSITORY_ROOT, DeployerTestCase, forward

from deployer import fsops, lock, platform_support
from deployer.paths import Paths


def probe_returning(alive: bool, start_time: int | None):
    return lambda pid: platform_support.ProcessStatus(alive, start_time)


class RenderedExecutableTests(DeployerTestCase):
    def test_rendered_bash_validates_and_executes_with_spaces(self) -> None:
        self.make_source_json()
        directory = self.make_skill(
            "alpha", "Rendered script fixture\n\n```bash\nprintf '%s\\n' \"{{REPOS_ROOT}}\"\n```", ["REPOS_ROOT"]
        )
        self.write(directory / "run.sh", "#!/usr/bin/env bash\nprintf '%s\\n' \"{{REPOS_ROOT}}\"\n")
        repos = self.root / "Repos With Spaces"
        self.make_config(repos_root=repos)
        self.deploy_ok("--all")
        bash = platform_support.find_bash()
        if bash is None:
            self.fail("Git Bash is a validation prerequisite")
        result = platform_support.run_tool([bash, forward(self.skills_dir / "alpha" / "run.sh")])
        self.assertEqual(0, result.returncode, result.output)
        self.assertEqual(forward(repos), result.output.strip())

    def test_source_root_renders_the_deploying_clone_and_executes_with_spaces(self) -> None:
        self.make_source_json()
        directory = self.make_skill(
            "alpha", "Source fixture\n\n```bash\nprintf '%s\\n' \"{{SOURCE_ROOT}}\"\n```", ["SOURCE_ROOT"]
        )
        self.write(directory / "run.sh", "#!/usr/bin/env bash\nprintf '%s\\n' \"{{SOURCE_ROOT}}\"\n")
        self.make_config()
        clone = self.snapshot_source("Clone With Spaces (dev)")
        self.deploy_from(clone, "--all")
        self.assertIn(f'"{forward(clone)}"', self.skill_text("alpha"))
        bash = platform_support.find_bash()
        if bash is None:
            self.fail("Git Bash is a validation prerequisite")
        result = platform_support.run_tool([bash, forward(self.skills_dir / "alpha" / "run.sh")])
        self.assertEqual(0, result.returncode, result.output)
        self.assertEqual(forward(clone), result.output.strip())

    def test_skill_directory_commands_run_as_claude_substitutes_them_and_as_the_adapter_states_with_spaces(
        self,
    ) -> None:
        self.home = self.root / "Home With Spaces (dev)"
        for directory in (self.skills_dir, self.home / ".claude" / "deployer" / "config"):
            directory.mkdir(parents=True)
        self.make_source_json()
        directory = self.make_skill(
            "alpha", 'Directory fixture\n\n```bash\npython -B "${CLAUDE_SKILL_DIR}/scripts/where.py" "a b"\n```'
        )
        self.write(
            directory / "scripts" / "where.py",
            "import sys\nfrom pathlib import Path\n\nprint(Path(__file__).resolve().parents[1].name, sys.argv[1])\n",
        )
        self.make_config()
        self.deploy_ok("--all")
        skill = self.skill_text("alpha")
        self.assertEqual((directory / "SKILL.md").read_text(encoding="utf-8"), skill, "deployed without rendering")
        adapter = (self.agents_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        stated = re.search(r"`\$\{CLAUDE_SKILL_DIR\}` stands for `([^`]+)`", adapter)
        if stated is None:
            self.fail(adapter)
        self.assertEqual(forward(self.skills_dir / "alpha"), stated.group(1))
        fence = re.search(r"```bash\n(.*?)\n```", skill, re.DOTALL)
        if fence is None:
            self.fail(skill)
        command = fence.group(1)
        bash = platform_support.find_bash()
        if bash is None:
            self.fail("Git Bash is a validation prerequisite")
        # Claude Code substitutes the native absolute path; Codex and Copilot use the one the adapter states.
        for value in (str(self.skills_dir / "alpha"), stated.group(1)):
            with self.subTest(value=value):
                result = platform_support.run_tool([bash, "-c", command.replace("${CLAUDE_SKILL_DIR}", value)])
                self.assertEqual(0, result.returncode, result.output)
                self.assertEqual("alpha a b", result.output.strip())

    def test_bundled_skill_directory_paths_resolve_in_the_deployed_layout(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        self.deploy_from(self.repository_source(), "--all", "--include", "dotnet-format")
        referenced = 0
        for document in sorted(self.skills_dir.glob("*/**/*.md")):
            skill = document.relative_to(self.skills_dir).parts[0]
            # A backslash ends a path too: allowed-tools patterns are JSON-quoted, so their quotes are escaped.
            for match in re.finditer(r"\$\{CLAUDE_SKILL_DIR\}/([^\"`\s\\]+)", document.read_text(encoding="utf-8")):
                referenced += 1
                path = match.group(1)
                with self.subTest(document=document.relative_to(self.skills_dir).as_posix(), path=path):
                    if "*" in path:
                        # An allowed-tools pattern such as scripts/*) must name a directory that is deployed.
                        self.assertTrue((self.skills_dir / skill / path.split("*")[0]).resolve().is_dir())
                    else:
                        self.assertTrue((self.skills_dir / skill / path).resolve().exists())
        self.assertGreater(referenced, 20, "the bundled skills name their scripts through ${CLAUDE_SKILL_DIR}")
        for adapter in sorted(self.agents_dir.glob("*/SKILL.md")):
            with self.subTest(adapter=adapter.parent.name):
                self.assertIn(
                    f"`${{CLAUDE_SKILL_DIR}}` stands for `{forward(self.skills_dir / adapter.parent.name)}`",
                    adapter.read_text(encoding="utf-8"),
                )

    def test_bundled_templates_render_and_pass_real_executable_checks(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        repository = self.repository_source()
        self.deploy_from(repository, "--all", "--include", "dotnet-format")
        for relative in (
            "audit-ai-config/scripts/audit_ai_config.py",
            "dotnet-format/scripts/dotnet_format_targets.py",
            "repo-cleanup/scripts/repo_cleanup.py",
            "skill-core/scripts/console.py",
            "skill-core/scripts/frontmatter.py",
            "skill-core/scripts/github_client.py",
            "skill-core/scripts/skill_roots.py",
            "update-coding-agent-skills/scripts/update.sh",
        ):
            with self.subTest(relative=relative):
                self.assertEqual(
                    (REPOSITORY_ROOT / "skills" / relative).read_bytes(),
                    (self.skills_dir / relative).read_bytes(),
                )
        # analyze-skill-cost imports the reader from skill-core and ships no copy of its own.
        self.assertFalse((self.skills_dir / "analyze-skill-cost" / "scripts" / "frontmatter.py").exists())
        # Every skill that runs gh imports skill-core's client; none ships a gh runner of its own.
        for script in sorted(self.skills_dir.glob("*/scripts/*.py")):
            if script.parent.parent.name != "skill-core" and not script.name.startswith("test_"):
                with self.subTest(script=script.name):
                    self.assertNotRegex(script.read_text(encoding="utf-8"), r'\[\s*"gh"')
        for name in ("analyze-skill-cost", "audit-ai-config", "repo-cleanup", "update-coding-agent-skills"):
            with self.subTest(skill=name):
                self.assertNotIn("{{", self.skill_text(name))
        self.assertIn(f'"{forward(repository)}"', self.skill_text("update-coding-agent-skills"))
        self.assertTrue((self.skills_dir / "runtime-compatibility.md").is_file())
        # The code-review bundle pulls in code-review-core, which ships the reviewer subagent.
        self.assertEqual(
            (REPOSITORY_ROOT / "agents" / "code-review-reviewer.md").read_bytes(),
            (self.claude_agents_dir / "code-review-reviewer.md").read_bytes(),
        )
        self.assertEqual(["code-review-reviewer.md"], sorted(self.owned("agents", source_id)))
        self.assertIn(
            f"read and apply `{forward(self.home)}/.claude/skills/runtime-compatibility.md`",
            (self.agents_dir / "review-prs" / "SKILL.md").read_text(encoding="utf-8"),
        )
        self.assertNotIn("runtime-compatibility", self.skill_text("review-prs"), "Claude runs skip the shared read")
        owned = self.owned("skills", source_id)
        self.assertEqual(sorted(path.stem for path in (REPOSITORY_ROOT / "deploy-meta").glob("*.json")), sorted(owned))
        # skill-core is a hidden dependency: installed beside its dependents, with no adapter for any runtime.
        self.assertFalse((self.agents_dir / "skill-core").exists())
        # Every entry point finds skill-core where the deployer put it, and prints under a legacy code page.
        entry_points = sorted(
            path.relative_to(REPOSITORY_ROOT / "skills")
            for path in (REPOSITORY_ROOT / "skills").glob("*/scripts/*.py")
            if not path.name.startswith("test_")
            and re.search(r'^if __name__ == "__main__":', path.read_text(encoding="utf-8"), re.MULTILINE)
        )
        self.assertEqual(15, len(entry_points), entry_points)
        for entry_point in entry_points:
            with self.subTest(entry_point=entry_point.as_posix()):
                hook = entry_point.name == "review_guard.py"  # a hook reads its event on stdin and takes no options
                result = subprocess.run(
                    [sys.executable, "-B", str(self.skills_dir / entry_point), *([] if hook else ["--help"])],
                    input=b"{}" if hook else b"",
                    capture_output=True,
                    env={**os.environ, "PYTHONIOENCODING": "cp1252"},
                    check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr.decode("utf-8", "replace"))

    def test_an_installation_without_skill_core_adds_it_on_update(self) -> None:
        # The installations before #27 have no skill-core; updating one installs it with the skills that import it.
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        repository = self.repository_source()
        core = self.root / "skill-core"
        shutil.move(repository / "skills" / "skill-core", core)
        metadata = {path: path.read_bytes() for path in (repository / "deploy-meta").glob("*.json")}
        (repository / "deploy-meta" / "skill-core.json").unlink()
        for path, text in metadata.items():
            document = json.loads(text)
            if "skill-core" in document.get("skill_deps", []):
                document["skill_deps"].remove("skill-core")
                path.write_text(json.dumps(document), encoding="utf-8")
        self.deploy_from(repository, "--all", "--include", "dotnet-format")
        self.assertFalse((self.skills_dir / "skill-core").exists())
        shutil.move(core, repository / "skills" / "skill-core")
        for path, text in metadata.items():
            path.write_bytes(text)
        groups = self.report_groups(self.deploy_from(repository, "--all", "--dry-run").output, "DRY RUN")
        self.assertEqual(["skill-core"], groups.get("FRESH INSTALL"), groups)

    def test_shellcheck_failure_in_script_blocks_deployment(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "ShellCheck fixture")
        self.write(directory / "run.sh", "#!/usr/bin/env bash\ncd $1 || exit\n")
        self.make_config()
        result = self.deploy_fails("--all", pattern="ShellCheck failed for rendered content: alpha/run.sh")
        self.assertIn("SC2086", result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_shellcheck_failure_in_markdown_block_names_the_block(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Markdown ShellCheck fixture\n\n```bash\ncd $1 || exit\n```")
        self.make_config()
        result = self.deploy_fails("--all", pattern="ShellCheck failed for rendered Bash block: alpha/SKILL.md block 1")
        self.assertNotRegex(result.output, r"ShellCheck failed.*(blocks|deploy-render)")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_shell_fence_is_validated_like_a_bash_fence(self) -> None:
        self.make_source_json()
        self.make_skill(
            "alpha",
            "Shell fence fixture\n\n```shell\nprintf '%s\\n' \"{{REPOS_ROOT}}\"\n```",
            ["REPOS_ROOT"],
        )
        tricky = self.root / "re.pos (test) v1"
        self.make_config(repos_root=tricky)
        checked: list[tuple[str, str]] = []
        real = platform_support.run_tool

        def record(arguments: list[str], environment: dict[str, str] | None = None):
            tool = Path(arguments[0]).stem.casefold()
            for argument in arguments[1:]:
                if argument.endswith(".sh") and "blocks" in argument:
                    checked.append((tool, Path(argument).read_text(encoding="utf-8")))
            return real(arguments, environment)

        with mock.patch("deployer.platform_support.run_tool", side_effect=record):
            self.deploy_ok("--all")
        rendered = f"printf '%s\\n' \"{forward(tricky)}\"\n"
        self.assertEqual(["bash", "shellcheck"], sorted(tool for tool, text in checked if text.endswith(rendered)))

    def test_shellcheck_failure_in_shell_fence_names_the_block(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Shell fence fixture\n\n```shell\ncd {{REPOS_ROOT}}/$1 || exit\n```", ["REPOS_ROOT"])
        self.make_config()
        self.deploy_fails("--all", pattern="ShellCheck failed for rendered Bash block: alpha/SKILL.md block 1")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_bash_syntax_error_in_shell_fence_blocks_deployment(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Shell fence fixture\n\n```shell\nif then\n```")
        self.make_config()
        self.deploy_fails("--all", pattern="Rendered Bash block syntax validation failed: alpha/SKILL.md block 1")

    def test_bash_syntax_error_blocks_deployment(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Syntax fixture")
        self.write(directory / "run.sh", "#!/usr/bin/env bash\nif then\n")
        self.make_config()
        self.deploy_fails("--all", pattern="Rendered Bash syntax validation failed: alpha/run.sh")

    def test_unclosed_bash_fence_blocks_deployment(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Fence fixture\n\n```bash\necho open")
        self.make_config()
        self.deploy_fails("--all", pattern="Unclosed Bash code fence in alpha/SKILL.md")

    def test_missing_shellcheck_blocks_rendered_bash(self) -> None:
        self.make_source_json()
        self.make_skill(
            "alpha", "Missing ShellCheck fixture\n\n```bash\nprintf '%s\\n' \"{{REPOS_ROOT}}\"\n```", ["REPOS_ROOT"]
        )
        self.make_config()
        real = platform_support.find_executable
        with mock.patch(
            "deployer.platform_support.find_executable",
            side_effect=lambda name: None if name == "shellcheck" else real(name),
        ):
            result = self.deploy_fails("--all", pattern="Tools required to validate the rendered skills were not found")
        self.assertIn("  - ShellCheck (lints rendered Bash): winget install --id koalaman.shellcheck", result.output)
        self.assertIn("open a new terminal", result.output)
        self.assertIn('For Chocolatey, Scoop, or direct downloads, see "Installing the tools" in', result.output)
        self.assertNotIn("Git Bash (", result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_every_missing_validation_tool_is_reported_at_once(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Missing tools fixture\n\n```bash\necho ok\n```")
        self.write(directory / "scripts" / "check.ps1", "Write-Output 'ok'\n")
        self.make_config()
        with (
            mock.patch("deployer.platform_support.find_bash", return_value=None),
            mock.patch("deployer.platform_support.find_executable", return_value=None),
            mock.patch("deployer.platform_support.find_powershell", return_value=None),
        ):
            result = self.deploy_fails("--all", pattern="Tools required to validate the rendered skills were not found")
        for line in (
            "  - Git Bash (checks rendered Bash syntax): winget install --id Git.Git",
            "  - ShellCheck (lints rendered Bash): winget install --id koalaman.shellcheck",
            "  - PowerShell (parses rendered .ps1 files): winget install --id Microsoft.PowerShell",
        ):
            self.assertIn(line, result.output)
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_content_without_shell_scripts_needs_no_validation_tools(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Plain instructions only")
        self.make_config()
        with (
            mock.patch("deployer.platform_support.find_bash", return_value=None),
            mock.patch("deployer.platform_support.find_executable", return_value=None),
            mock.patch("deployer.platform_support.find_powershell", return_value=None),
        ):
            self.deploy_ok("--all")
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())

    def test_values_with_parentheses_and_dots_expand(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Path is {{REPOS_ROOT}}/sub", ["REPOS_ROOT"])
        tricky = self.root / "re.pos (test)"
        self.make_config(repos_root=tricky)
        self.deploy_ok("--all")
        self.assertIn(f"{forward(tricky)}/sub", self.skill_text("alpha"))

    def test_python_files_are_expanded(self) -> None:
        self.make_source_json()
        directory = self.make_skill("alpha", "Python fixture", ["REPOS_ROOT"])
        self.write(directory / "scripts" / "tool.py", 'ROOT = "{{REPOS_ROOT}}"\n')
        self.make_config()
        self.deploy_ok("--all")
        self.assertEqual(
            f'ROOT = "{forward(self.repos)}"\n',
            (self.skills_dir / "alpha" / "scripts" / "tool.py").read_text(encoding="utf-8"),
        )

    def test_value_unsafe_for_shell_context_is_refused_before_rendering(self) -> None:
        # The derived-value allowlist refuses the quote before any context sees it; render's own escaping, which
        # would refuse it in this shell fence too, is covered in test_render_contexts.py.
        self.home = self.root / "o'home"
        for directory in ("skills", "deployer/config"):
            (self.home / ".claude" / directory).mkdir(parents=True)
        self.make_source_json()
        self.make_skill("alpha", 'Shell fixture\n\n```bash\ncd "{{HOME}}" || exit\n```', ["HOME"])
        self.make_config()
        self.deploy_fails("--all", pattern="HOME \\(the home folder\\) contains disallowed character '''")
        self.assertFalse((self.skills_dir / "alpha").exists())

    def test_only_declared_variables_are_substituted(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Home {{HOME}} but repos {{REPOS_ROOT}}", ["HOME"])
        self.make_config()
        self.deploy_fails("--all", pattern="Unexpanded tokens found")


class LockTests(DeployerTestCase):
    def write_lock(self, info: dict) -> Path:
        directory = self.home / ".claude" / "deployer" / ".deploy.lock.d"
        directory.mkdir(parents=True)
        self.write(directory / "token", str(info.get("token", "old-token")))
        self.write(directory / "info.json", json.dumps(info))
        return directory

    def fixture(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha content")
        self.make_config()

    def test_pid_reuse_is_detected_and_stale_lock_reclaimed(self) -> None:
        self.fixture()
        self.write_lock({"pid": 4242, "token": "old-token", "start_time": 1})
        result = self.deploy_ok("--all", probe=probe_returning(True, 2))
        self.assertRegex(result.output, "Stale lock detected \\(PID 4242 was reused\\)")
        self.assertTrue((self.skills_dir / "alpha" / "SKILL.md").is_file())
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_dead_lock_holder_is_reclaimed(self) -> None:
        self.fixture()
        self.write_lock({"pid": 4242, "token": "old-token", "start_time": 1})
        result = self.deploy_ok("--all", probe=probe_returning(False, None))
        self.assertIn("Stale lock detected (PID 4242 not running)", result.output)

    def test_matching_process_identity_blocks_contention(self) -> None:
        self.fixture()
        self.write_lock({"pid": 4242, "token": "active-token", "start_time": 7})
        self.deploy_fails(
            "--all", pattern="Another deployment is running \\(PID 4242\\)", probe=probe_returning(True, 7)
        )
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertTrue((self.home / ".claude" / "deployer" / ".deploy.lock.d" / "info.json").is_file())

    def test_unverifiable_live_lock_fails_closed(self) -> None:
        self.fixture()
        self.write_lock({"pid": 4242, "token": "x", "start_time": None})
        self.deploy_fails("--all", pattern="process identity cannot be verified", probe=probe_returning(True, 7))
        info = self.home / ".claude" / "deployer" / ".deploy.lock.d" / "info.json"
        self.write(info, json.dumps({"pid": 4242, "token": "x", "start_time": 7}))
        self.deploy_fails("--all", pattern="start time cannot be read", probe=probe_returning(True, None))

    def test_lock_without_metadata_or_pid_fails_closed(self) -> None:
        self.fixture()
        directory = self.home / ".claude" / "deployer" / ".deploy.lock.d"
        directory.mkdir(parents=True)
        self.deploy_fails("--all", pattern="Lock exists but has no metadata")
        self.write(directory / "info.json", json.dumps({"token": "x"}))
        self.deploy_fails("--all", pattern="Lock metadata is malformed \\(no PID\\)")

    def test_failed_lock_initialization_removes_the_new_lock(self) -> None:
        self.fixture()
        real_write = fsops.write_atomic

        def failing_write(path: Path, content: bytes) -> None:
            if path.name == "info.json":
                raise OSError("synthetic metadata write failure")
            real_write(path, content)

        with mock.patch("deployer.fsops.write_atomic", side_effect=failing_write):
            self.deploy_fails("--all", pattern="Failed to initialize deployment lock: synthetic metadata write failure")
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.deploy_ok("--all")

    def assert_failed_write_changes_nothing(self, fails: Callable[[Path], bool], message: str) -> None:
        self.fixture()
        self.deploy_ok("--all")
        manifest = self.manifest_file.read_bytes()
        self.make_skill("alpha", "Updated alpha")
        real_write = fsops.write_file

        def failing_write(path: Path, content: bytes) -> None:
            if fails(path):
                raise OSError(message)
            real_write(path, content)

        with mock.patch("deployer.fsops.write_file", side_effect=failing_write):
            self.deploy_fails("--all", pattern=message)
        self.assertIn("Alpha content", self.skill_text("alpha"))
        self.assertEqual(manifest, self.manifest_file.read_bytes())
        self.assertEqual([], list((self.home / ".claude" / "deployer" / "staging").iterdir()))
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())
        self.deploy_ok("--all")
        self.assertIn("Updated alpha", self.skill_text("alpha"))

    def test_a_failed_staging_write_changes_nothing(self) -> None:
        self.assert_failed_write_changes_nothing(
            lambda path: "staging" in path.parts and path.name == "SKILL.md", "synthetic staging write failure"
        )

    def test_a_failed_journal_creation_changes_nothing(self) -> None:
        self.assert_failed_write_changes_nothing(
            lambda path: path.name == "journal.jsonl", "synthetic journal creation failure"
        )

    def test_double_contention_cleans_renamed_stale_lock(self) -> None:
        self.fixture()
        lock_dir = self.write_lock({"pid": 999999, "token": "stale-token", "start_time": 1})
        real_make_directory = fsops.make_directory
        calls = {"count": 0}

        def contended(path: Path) -> None:
            if path == lock_dir:
                calls["count"] += 1
                if calls["count"] == 2:
                    real_make_directory(path)
                    raise FileExistsError(str(path))
            real_make_directory(path)

        with mock.patch("deployer.fsops.make_directory", side_effect=contended):
            self.deploy_fails(
                "--all", pattern="Failed to acquire lock after stale reclaim", probe=probe_returning(False, None)
            )
        self.assertEqual([], list((self.home / ".claude" / "deployer").glob(".deploy.lock.stale.*")))
        self.assertTrue(lock_dir.is_dir())

    def test_manifest_ownership_is_loaded_after_lock_acquisition(self) -> None:
        self.make_source_json("test/source-a")
        self.make_skill("alpha", "Identical content")
        self.make_config("test/source-a")
        source_a = self.snapshot_source("source-a")
        self.make_source_json("test/source-b")
        self.make_config("test/source-b")
        real_acquire = lock.acquire
        state = {"nested": False}

        def acquire_after_competitor(paths: Paths, probe=platform_support.process_status):
            if not state["nested"]:
                state["nested"] = True
                self.deploy_from(source_a, "--all")
            return real_acquire(paths, probe)

        with mock.patch("deployer.lock.acquire", side_effect=acquire_after_competitor):
            self.deploy_fails("--all", pattern="owned by source 'test/source-a'")
        self.assertIn("alpha", self.owned("skills", "test/source-a"))
        self.assertNotIn("alpha", self.owned("skills", "test/source-b"))


class DryRunTests(DeployerTestCase):
    def test_dry_run_reports_actions_without_touching_disk(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Test {{HOME}}", ["HOME"])
        self.make_config()
        result = self.deploy_ok("--all", "--dry-run")
        self.assertIn("=== DRY RUN ===", result.output)
        self.assertEqual({"FRESH INSTALL": ["alpha"]}, self.report_groups(result.output, "DRY RUN"))
        self.assertFalse((self.skills_dir / "alpha").exists())
        self.assertFalse(self.manifest_file.exists())
        self.assertEqual([], list((self.home / ".claude" / "deployer" / "staging").iterdir()))
        self.assertFalse((self.home / ".claude" / "deployer" / ".deploy.lock.d").exists())

    def test_dry_run_reports_updates_conflicts_and_removals(self) -> None:
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "edit\n")
        self.remove_skill("beta")
        result = self.deploy_ok("--all", "--dry-run")
        groups = self.report_groups(result.output, "DRY RUN")
        self.assertEqual(["alpha (modified since last deploy)"], groups["CONFLICT"])
        self.assertEqual(["beta (deselected or absent from source)"], groups["REMOVE"])

    def test_dry_run_distinguishes_unchanged_items_from_updates(self) -> None:
        self.make_source_json(shared_assets={"shared.md": "owner"})
        self.make_shared_asset("shared.md")
        self.make_skill("alpha", "Alpha", shared_deps=["shared.md"])
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        result = self.deploy_ok("--all", "--dry-run")
        self.assertEqual(
            {"UNCHANGED": ["alpha", "beta", "shared.md (shared asset)"]},
            self.report_groups(result.output, "DRY RUN"),
        )
        self.append(self.source / "skills" / "beta" / "SKILL.md", "New instruction\n")
        self.make_shared_asset("shared.md", "# Revised shared asset")
        result = self.deploy_ok("--all", "--dry-run")
        groups = self.report_groups(result.output, "DRY RUN")
        self.assertEqual({"UPDATE": ["beta"], "EXISTS": ["shared.md (shared asset)"], "UNCHANGED": ["alpha"]}, groups)

    def test_dry_run_groups_actions_with_attention_first_and_sorts_each_group(self) -> None:
        self.make_source_json()
        for name in ("zeta", "beta", "alpha", "mid"):
            self.make_skill(name, name)
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "mid" / "SKILL.md", "local edit\n")
        self.append(self.source / "skills" / "zeta" / "SKILL.md", "New instruction\n")
        self.make_skill("new", "New skill")
        result = self.deploy_ok("--all", "--dry-run")
        report = result.output[result.output.index("=== DRY RUN ===") :]
        self.assertEqual(
            "=== DRY RUN ===\n"
            "\n"
            "CONFLICT (1):\n"
            "  mid (modified since last deploy)\n"
            "\n"
            "UPDATE (1):\n"
            "  zeta\n"
            "\n"
            "FRESH INSTALL (1):\n"
            "  new\n"
            "\n"
            "UNCHANGED (2):\n"
            "  alpha\n"
            "  beta\n"
            "\n"
            "Replace conflicting items with --force-item NAME or --force (backups are kept).\n"
            "\n",
            report,
        )

    def test_deployment_summary_lists_items_alphabetically(self) -> None:
        self.make_source_json()
        for name in ("zeta", "beta", "alpha"):
            self.make_skill(name, name)
        self.make_config()
        result = self.deploy_ok("--all")
        self.assertIn(
            "=== DEPLOYED ===\n\nINSTALLED (3):\n  alpha\n  beta\n  zeta\n\nRun ID: ",
            result.output,
        )

    def test_deployment_report_folds_adapters_unless_they_need_attention(self) -> None:
        self.make_source_json()
        for name in ("alpha", "beta", "gamma", "omega"):
            self.make_skill(name, name)
        self.make_config()
        self.deploy_ok("--all")
        self.append(self.skills_dir / "alpha" / "SKILL.md", "local edit\n")
        self.append(self.source / "skills" / "beta" / "SKILL.md", "New instruction\n")
        self.append(self.source / "skills" / "omega" / "SKILL.md", "New instruction\n")
        self.append(self.agents_dir / "omega" / "SKILL.md", "local adapter edit\n")
        self.remove_skill("gamma")
        self.make_skill("delta", "delta")
        output = self.deploy_ok("--all").output
        report = output[output.index("=== DEPLOYED ===") : output.index("Run ID: ")]
        self.assertTrue(output.endswith("/.claude/skills/.deploy-manifest.json\n\n"), output)
        self.assertEqual(
            "=== DEPLOYED ===\n"
            "\n"
            "SKIPPED (2):\n"
            "  alpha (modified since last deploy)\n"
            "  omega (runtime adapter, modified since last deploy)\n"
            "\n"
            "REMOVED (1):\n"
            "  gamma (deselected or absent from source)\n"
            "\n"
            "UPDATED (2):\n"
            "  beta\n"
            "  omega\n"
            "\n"
            "INSTALLED (1):\n"
            "  delta\n"
            "\n"
            "Replace skipped items with --force-item NAME or --force (backups are kept).\n"
            "\n",
            report,
        )
        self.assertNotIn("Applying...", output)

    def test_force_hint_appears_only_when_force_can_resolve_the_problem(self) -> None:
        dry_hint = "Replace conflicting items with --force-item NAME or --force (backups are kept)."
        deploy_hint = "Replace skipped items with --force-item NAME or --force (backups are kept)."
        self.make_source_json()
        self.make_skill("alpha", "Alpha")
        self.make_skill("beta", "Beta")
        self.make_config()
        self.deploy_ok("--all")
        shutil.rmtree(self.skills_dir / "alpha")
        self.write(self.skills_dir / "alpha", "not a directory\n")
        preview = self.deploy_ok("--all", "--dry-run").output
        self.assertEqual(["alpha (destination is not a directory)"], self.report_groups(preview, "DRY RUN")["CONFLICT"])
        self.assertNotIn(dry_hint, preview)
        result = self.deploy_ok("--all").output
        self.assertIn("alpha (destination is not a directory)", self.report_groups(result, "DEPLOYED")["SKIPPED"])
        self.assertNotIn(deploy_hint, result)
        self.append(self.skills_dir / "beta" / "SKILL.md", "local edit\n")
        self.assertIn(dry_hint, self.deploy_ok("--all", "--dry-run").output)
        self.assertIn(deploy_hint, self.deploy_ok("--all").output)

    def test_report_lines_for_this_repository_fit_in_80_columns(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        repository = self.repository_source()
        self.deploy_from(repository, "--all", "--include", "dotnet-format")
        for directory in (*self.skills_dir.iterdir(), *self.agents_dir.iterdir()):
            if directory.is_dir():
                self.append(directory / "SKILL.md", "local edit\n")
        outputs = (
            self.deploy_from(repository, "--all", "--dry-run").output,
            self.deploy_from(repository, "--all").output,
            self.deploy_from(repository, "--dry-run", stdin="none\n").output,
            self.deploy_from(repository, stdin="none\n").output,
        )
        self.assertIn("KEEP", self.report_groups(outputs[2], "DRY RUN"))
        for output in outputs:
            for line in output.splitlines():
                if not line.startswith(("Config: ", "Home: ", "Manifest: ")):
                    self.assertLessEqual(len(line), 80, line)

    def test_redirected_output_with_non_ascii_diff_does_not_crash(self) -> None:
        source_id = json.loads((REPOSITORY_ROOT / "source.json").read_text(encoding="utf-8"))["id"]
        self.make_config(source_id)
        self.write(self.skills_dir / "repo-cleanup" / "SKILL.md", "Locally edited → copy\n")
        result = platform_support.run_tool(
            [sys.executable, "-B", str(REPOSITORY_ROOT / "deploy.py"), "--all", "--dry-run"],
            {"HOME": str(self.home), "USERPROFILE": str(self.home), "PYTHONIOENCODING": "cp1252"},
        )
        self.assertEqual(0, result.returncode, result.output)
        self.assertIn(
            "repo-cleanup (unmanaged, differs)", self.report_groups(result.output, "DRY RUN")["BOOTSTRAP DIFF"]
        )
        self.assertIn("Locally edited → copy", result.output)


if __name__ == "__main__":
    unittest.main()
