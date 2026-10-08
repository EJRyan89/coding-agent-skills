"""Fixture tests for tests/validation/shell_targets.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import shell_targets
import toolchain
from shell_targets import (
    PowerShellTarget,
    markdown_shell_check,
    markdown_shell_targets,
    powershell_targets,
    script_analyzer_check,
    shell_script_targets,
    shell_token_problems,
)
from toolchain import PREREQUISITES, find_psscriptanalyzer, missing_prerequisites, outdated_prerequisites
from validation_support import REPOSITORY_ROOT, write_fixture_tree

from deployer import platform_support


class SkillScriptTargetFixtures(unittest.TestCase):
    FILES: ClassVar[dict[str, str]] = {
        "skills/alpha/SKILL.md": "",
        "skills/alpha/scripts/run.sh": "",
        "skills/group/inner/SKILL.md": "",
        "skills/group/inner/scripts/tool.bash": "",
        ".claude/skills/local/SKILL.md": "",
        ".claude/skills/local/scripts/local.sh": "",
        ".claude/skills/local/scripts/local.ps1": "",
        # Shell outside a repository skill's scripts/ is not a skill script; tools/ and tests/ hold no skill.
        ".claude/hooks/hook.sh": "",
        "tools/run.sh": "",
    }

    def test_shellcheck_reads_every_shipped_and_repository_skill_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, self.FILES)
            self.assertEqual(
                [
                    ".claude/skills/local/scripts/local.sh",
                    "skills/alpha/scripts/run.sh",
                    "skills/group/inner/scripts/tool.bash",
                ],
                [path.relative_to(root).as_posix() for path in shell_script_targets(root)],
            )

    def test_psscriptanalyzer_reads_a_repository_skills_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(root, {**self.FILES, ".claude/hooks/hook.ps1": ""})
            targets = powershell_targets(root, sorted(path for path in root.rglob("*") if path.is_file()))
        self.assertEqual([".claude/skills/local/scripts/local.ps1"], [target.name for target in targets])


class ShellTargetsFixtures(unittest.TestCase):
    def test_markdown_bash_fences_outside_skills_and_tests_are_shellcheck_targets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "docs/guide.md": (
                    "# Guide\n\n```bash\necho one\n```\n\n```python\nprint()\n```\n\n```sh\necho two\n```\n"
                ),
                ".claude/skills/a/SKILL.md": "```shell\necho three\n```\n",
                "docs/empty.md": "```bash\n\n```\n",
                "docs/unclosed.md": "```bash\necho never\n",
                "skills/a/SKILL.md": "```bash\necho {{TOKEN}}\n```\n",
                "tests/fixture.md": "```bash\ncd x\n```\n",
                "tools/run.sh": "echo script\n",
            }
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            targets = markdown_shell_targets(root, [root / name for name in files])
        self.assertEqual(
            [
                (".claude/skills/a/SKILL.md", 2, "echo three"),
                ("docs/guide.md", 4, "echo one"),
                ("docs/guide.md", 12, "echo two"),
            ],
            [(target.name, target.first_line, target.text) for target in targets],
        )

    def test_missing_shellcheck_fails_the_markdown_shell_check_with_the_install_command(self) -> None:
        with (
            mock.patch.object(shell_targets, "find_shellcheck", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            markdown_shell_check(REPOSITORY_ROOT, [])
        self.assertIn("ShellCheck was not found: winget install --id koalaman.shellcheck", str(raised.exception))

    def test_shell_token_policy_detects_unquoted_and_unexecuted_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative_path: str, text: str) -> None:
                (root / relative_path).parent.mkdir(parents=True, exist_ok=True)
                (root / relative_path).write_text(text, encoding="utf-8")

            write(
                "skills/alpha/SKILL.md",
                "Prose may say {{BARE}}.\n"
                "\n"
                "```bash\n"
                'python -B run.py "{{QUOTED}}" \'{{SINGLE}}\' "a \\" {{STILL_QUOTED}}"\n'
                "cd {{UNQUOTED}}/x  # but a comment may say {{IN_COMMENT}}\n"
                "```\n"
                "\n"
                "```text\n"
                "{{NOT_SHELL}}\n"
                "```\n"
                "\n"
                "```pwsh\n"
                'Set-Location "{{QUOTED}}"; Write-Output `{{ESCAPED}}\n'
                "```\n",
            )
            write("skills/alpha/scripts/run.sh", '#!/usr/bin/env bash\necho "{{QUOTED}}"\necho {{SCRIPT}}\n')
            write("skills/alpha/scripts/run.ps1", "Write-Output '{{PS_ONLY}}'\n")
            write("skills/alpha/scripts/tool.py", "ROOT = {{PYTHON}}\n")
            write(
                "tests/deployer/test_rendering.py",
                "class Fixtures:\n"
                # Renders each Bash token, runs it with Git Bash, and compares the output with the spaced value.
                "    def test_quoted_executes_with_spaces(self):\n"
                '        self.make_skill("alpha", "```bash\\nprintf \\"{{QUOTED}}\\"\\n```")\n'
                '        self.write("run.sh", "{{SINGLE}} {{STILL_QUOTED}} {{UNQUOTED}} {{SCRIPT}}")\n'
                '        repos = self.root / "Repos With Spaces"\n'
                "        self.make_config(repos_root=repos)\n"
                "        bash = platform_support.find_bash()\n"
                "        result = platform_support.run_tool([bash, 'run.sh'])\n"
                "        self.assertEqual(forward(repos), result.output.strip())\n"
                "\n"
                # The name does not say with_spaces.
                "    def test_powershell_executes(self):\n"
                '        self.make_skill("alpha", "{{PS_ONLY}}")\n'
                '        repos = self.root / "Repos With Spaces"\n'
                "        result = platform_support.run_tool([platform_support.find_pwsh(path), 'run.ps1'])\n"
                "        self.assertEqual(repos, result.output)\n"
                "\n"
                # The token sits only in a comment.
                "    def test_ps_only_in_a_comment_with_spaces(self):\n"
                "        # Renders {{PS_ONLY}}.\n"
                '        self.make_skill("alpha", "Write-Output \'x\'")\n'
                '        repos = self.root / "Repos With Spaces"\n'
                "        result = platform_support.run_tool([platform_support.find_pwsh(path), 'run.ps1'])\n"
                "        self.assertEqual(repos, result.output)\n"
                "\n"
                # The output is compared with a value without a space.
                "    def test_escaped_compared_with_a_plain_value_with_spaces(self):\n"
                '        self.make_skill("alpha", "{{ESCAPED}} {{QUOTED}}")\n'
                '        repos = self.root / "Repos With Spaces"\n'
                "        pwsh = platform_support.find_pwsh(path)\n"
                "        result = platform_support.run_tool([pwsh, 'run.ps1'])\n"
                '        self.assertEqual("plain", result.output)\n'
                "\n"
                # PowerShell is found but nothing runs.
                "    def test_quoted_renders_in_powershell_with_spaces(self):\n"
                '        self.make_skill("alpha", "{{QUOTED}}")\n'
                '        repos = self.root / "Repos With Spaces"\n'
                "        platform_support.find_pwsh(path)\n"
                "        self.assertEqual(repos, self.rendered())\n",
            )
            self.assertEqual(
                [
                    "skills/alpha/SKILL.md:5 has {{UNQUOTED}} outside quotes in Bash",
                    "skills/alpha/SKILL.md:13 has {{ESCAPED}} outside quotes in PowerShell",
                    "skills/alpha/scripts/run.sh:3 has {{SCRIPT}} outside quotes in Bash",
                    "skills/alpha/SKILL.md:13 carries {{ESCAPED}} in PowerShell, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                    "skills/alpha/SKILL.md:13 carries {{QUOTED}} in PowerShell, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                    "skills/alpha/scripts/run.ps1:1 carries {{PS_ONLY}} in PowerShell, but no tests/deployer test "
                    "named *with_spaces* renders and executes it there",
                ],
                shell_token_problems(root),
            )

    def test_script_analyzer_names_each_file_fence_and_rule_it_finds(self) -> None:
        unused = "function Get-Answer {\n    $unused = 1\n    'answer'\n}\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "tools" / "clean.ps1").write_text("function Get-Answer {\n    'answer'\n}\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "clean.md").write_text("# Clean\n\n```powershell\nGet-Date\n```\n", encoding="utf-8")
            clean = [root / "tools" / "clean.ps1", root / "docs" / "clean.md"]
            script_analyzer_check(root, clean)
            (root / "tools" / "bad file.ps1").write_text(unused, encoding="utf-8")
            (root / "docs" / "bad.md").write_text(f"# Bad\n\nProse.\n\n```pwsh\n{unused}```\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as raised:
                script_analyzer_check(root, [*clean, root / "tools" / "bad file.ps1", root / "docs" / "bad.md"])
        message = str(raised.exception)
        self.assertIn("tools/bad file.ps1:2: PSUseDeclaredVarsMoreThanAssignments (Warning)", message)
        # The fence opens on line 5, so the fragment's second line is the file's seventh.
        self.assertIn("docs/bad.md:7: PSUseDeclaredVarsMoreThanAssignments (Warning)", message)
        self.assertNotIn("clean", message)
        self.assertIn("never suppress", message)

    def test_script_analyzer_targets_scripts_and_powershell_fences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            files = {
                "tools/reader.ps1": "Get-Date\n",
                "tests/fixtures/run.ps1": "Get-Date\n",
                "skills/alpha/scripts/run.ps1": "Get-Location\n",
                "elsewhere/ignored.ps1": "Get-Date\n",
                "skills/alpha/SKILL.md": "# Alpha\n\n```bash\nls\n```\n\n```PowerShell\nGet-Item '{{X}}'\n```\n",
                "docs/guide.md": "```ps1\nGet-Date\n```\n\n```pwsh\n\n```\n",
                "tests/fixtures/notes.md": "```powershell\nGet-Date\n```\n",
                "notes.txt": "```powershell\nGet-Date\n```\n",
            }
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding="utf-8")
            targets = powershell_targets(root, [root / name for name in files])
        self.assertEqual(
            [
                PowerShellTarget("docs/guide.md", 2, None, "Get-Date"),
                PowerShellTarget("skills/alpha/SKILL.md", 8, None, "Get-Item '{{X}}'"),
                PowerShellTarget("skills/alpha/scripts/run.ps1", 1, root / "skills/alpha/scripts/run.ps1", None),
                PowerShellTarget("tests/fixtures/run.ps1", 1, root / "tests/fixtures/run.ps1", None),
                PowerShellTarget("tools/reader.ps1", 1, root / "tools/reader.ps1", None),
            ],
            targets,
        )

    def test_missing_script_analyzer_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("PSScriptAnalyzer", "PSScriptAnalyzer", find_psscriptanalyzer), PREREQUISITES)
        install = "pwsh -Command 'Install-Module PSScriptAnalyzer -Scope CurrentUser -Force'"
        # pwsh answers but lists no module: None, not an error.
        with (
            mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
            mock.patch.object(platform_support, "run_tool", return_value=platform_support.ToolResult(0, "\n")),
        ):
            self.assertIsNone(find_psscriptanalyzer())
        with mock.patch.object(shell_targets, "find_psscriptanalyzer", return_value=None):
            self.assertIn(
                f"  - PSScriptAnalyzer: {install}",
                missing_prerequisites((("PSScriptAnalyzer", "PSScriptAnalyzer", shell_targets.find_psscriptanalyzer),)),
            )
            with self.assertRaises(AssertionError) as raised:
                script_analyzer_check(REPOSITORY_ROOT, [])
        self.assertIn(f"PSScriptAnalyzer was not found: {install}", str(raised.exception))
        self.assertEqual(
            [f"  - PSScriptAnalyzer 1.24.0 is older than the floor 1.25.0 in docs/dependency-updates.md: {install}"],
            outdated_prerequisites({"PSScriptAnalyzer": (1, 24, 0)}, (3, 11, 0)),
        )
        self.assertEqual([], outdated_prerequisites({"PSScriptAnalyzer": (1, 25, 0)}, (3, 11, 0)))


if __name__ == "__main__":
    unittest.main()
