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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

import job_pool
import shell_targets
import toolchain
from bounded_process import Finished
from job_pool import Terminated
from shell_targets import (
    PowerShellTarget,
    interpreter_problems,
    markdown_shell_check,
    markdown_shell_targets,
    powershell_targets,
    script_analyzer_check,
    shell_script_targets,
    shell_token_problems,
    unclosed_fence_problems,
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
    def setUp(self) -> None:
        super().setUp()
        # The fixtures name Windows' install commands on every platform; test_platform_support pins each platform's.
        patcher = mock.patch.object(platform_support, "INSTALL_HINTS", platform_support.WINDOWS_INSTALL_HINTS)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        # A fence left open runs to the end of the file, so ShellCheck reads it as GitHub shows it.
        self.assertEqual(
            [
                (".claude/skills/a/SKILL.md", 2, "echo three"),
                ("docs/guide.md", 4, "echo one"),
                ("docs/guide.md", 12, "echo two"),
                ("docs/unclosed.md", 2, "echo never\n"),
            ],
            [(target.name, target.first_line, target.text) for target in targets],
        )

    def test_an_unclosed_fence_is_reported_by_file_and_line_and_its_commands_reach_every_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "docs/guide.md": "# Guide\n\n```bash\n# shell" + "check disable=SC2086\nrm -rf $DIR/*\n",
                "docs/ps.md": "```powershell\nGet-Item x\n",
                "docs/closed.md": "```bash\necho closed\n```\n",
                "README.md": "~~~\nplain\n",
                "tests/fixtures/review/notes.md": "```bash\nleft open on purpose\n",
            }
            write_fixture_tree(root, files)
            paths = [root / name for name in files]
            self.assertEqual(
                [
                    "README.md:1 opens a plain fence it never closes; close it",
                    "docs/guide.md:3 opens a bash fence it never closes; close it",
                    "docs/ps.md:1 opens a powershell fence it never closes; close it",
                ],
                unclosed_fence_problems(root, paths),
            )
            # The suppression in the unclosed fence is read, as the rest of the fence is.
            self.assertEqual(
                ["docs/guide.md:4"],
                [problem.split(" ", 1)[0] for problem in shell_targets.shell_suppression_problems(root, paths)],
            )
            self.assertEqual(["docs/ps.md"], [target.name for target in powershell_targets(root, paths)])

    def test_missing_shellcheck_fails_the_markdown_shell_check_with_the_install_command(self) -> None:
        with (
            mock.patch.object(shell_targets, "find_shellcheck", return_value=None),
            self.assertRaises(AssertionError) as raised,
        ):
            markdown_shell_check(REPOSITORY_ROOT, [])
        self.assertIn("ShellCheck was not found: winget install --id koalaman.shellcheck", str(raised.exception))

    def test_a_token_that_may_render_empty_joins_its_option_rather_than_standing_as_an_argument(self) -> None:
        # Windows PowerShell 5.1 drops an empty argument, so --protected "" lets --protected take the next word.
        lone = '--protected "{{REPO_CLEANUP_PROTECTED}}" "t"'
        joined = '--protected="{{REPO_CLEANUP_PROTECTED}}" --regenerable "x{{REPO_CLEANUP_REGENERABLE}}" "t"'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": f"```bash\nrun {lone}\n```\n\n```powershell\nrun {joined}\n"
                    f"run {lone}\n```\n",
                    "skills/alpha/scripts/run.ps1": "run '{{REPO_CLEANUP_REGENERABLE}}'\n",
                },
            )
            problems = [problem for problem in shell_token_problems(root) if "*with_spaces*" not in problem]
            self.assertEqual(
                [
                    f"skills/alpha/SKILL.md:{line} passes {{{{{token}}}}} as an argument of its own in {label}, which "
                    "Windows PowerShell drops when the value is empty; join it to its option, as "
                    f'--name="{{{{{token}}}}}"'
                    for line, token, label in (
                        (2, "REPO_CLEANUP_PROTECTED", "Bash"),
                        (7, "REPO_CLEANUP_PROTECTED", "PowerShell"),
                    )
                ]
                + [
                    "skills/alpha/scripts/run.ps1:1 passes {{REPO_CLEANUP_REGENERABLE}} as an argument of its own in "
                    "PowerShell, which Windows PowerShell drops when the value is empty; join it to its option, as "
                    '--name="{{REPO_CLEANUP_REGENERABLE}}"'
                ],
                problems,
            )

    def test_an_interpreter_token_stands_unquoted_as_a_command_word_but_still_needs_its_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {"skills/alpha/SKILL.md": "```bash\n{{PYTHON}} -V\n```\n\n```pwsh\n{{PYTHON}} -V\n```\n"},
            )
            self.assertEqual(
                [
                    "skills/alpha/SKILL.md:2 carries {{PYTHON}} in Bash, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                    "skills/alpha/SKILL.md:6 carries {{PYTHON}} in PowerShell, but no tests/deployer test named "
                    "*with_spaces* renders and executes it there",
                ],
                shell_token_problems(root),
            )

    def test_python_named_by_hand_in_shipped_markdown_shell_or_a_shell_grant_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "---\nname: alpha\n"
                    'allowed-tools: ["Bash(python -B \\"${CLAUDE_SKILL_DIR}/scripts/*)", '
                    '"PowerShell({{PYTHON}} -B \\"${CLAUDE_SKILL_DIR}/scripts/*)", "Read"]\n'
                    "---\n"
                    "\n"
                    "Prose may name `python`, `python3`, or `python -c` in a span.\n"
                    "\n"
                    "```bash\n"
                    "python -B x.py\n"
                    '{{PYTHON}} -B "x.py"\n'
                    "python3 -c pass\n"
                    "echo pythonic python.exe ./python\n"
                    "```\n"
                    "\n"
                    "```pwsh\n"
                    "& python script.py\n"
                    "```\n"
                    "\n"
                    "```text\n"
                    "python x.py\n"
                    "```\n",
                    "skills/alpha/references/guide.md": "```bash\npython3 x.py\n```\n",
                    # A script runs from the checkout in its suite, unrendered, so it names the interpreter itself.
                    "skills/alpha/scripts/run.sh": "#!/usr/bin/env bash\npython x.py\n",
                },
            )
            doc = '"Template values" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    f"skills/alpha/SKILL.md:9 names Python by hand in Bash; write {{{{PYTHON}}}}; see {doc}",
                    f"skills/alpha/SKILL.md:11 names Python by hand in Bash; write {{{{PYTHON}}}}; see {doc}",
                    f"skills/alpha/SKILL.md:16 names Python by hand in PowerShell; write {{{{PYTHON}}}}; see {doc}",
                    'skills/alpha/SKILL.md grants Bash(python -B "${CLAUDE_SKILL_DIR}/scripts/*), which names Python '
                    f"by hand; write {{{{PYTHON}}}}; see {doc}",
                    f"skills/alpha/references/guide.md:2 names Python by hand in Bash; write {{{{PYTHON}}}}; see {doc}",
                ],
                interpreter_problems(root),
            )

    def test_a_prose_code_span_that_runs_python_named_by_hand_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_tree(
                root,
                {
                    "skills/alpha/SKILL.md": "---\nname: alpha\n---\n"
                    "\n"
                    "1. Run `python -B .github/scripts/gen.py --check` from the root, then `python3 -m pip list`.\n"
                    '2. Run `{{PYTHON}} -B "x.py"`, which renders per platform.\n'
                    "3. Recommend no `python -c`, `python`, `pythonic x`, or `python.exe x.py`.\n"
                    "\n"
                    "```text\n"
                    "`python x.py` in a fence is no code span\n"
                    "```\n",
                    "skills/alpha/references/guide.md": "Then run ` python x.py `.\n",
                },
            )
            doc = '"Template values" in docs/adding-a-skill.md'
            self.assertEqual(
                [
                    "skills/alpha/SKILL.md:5 names Python by hand in the code span "
                    f"`python -B .github/scripts/gen.py --check`; write {{{{PYTHON}}}}; see {doc}",
                    "skills/alpha/SKILL.md:5 names Python by hand in the code span "
                    f"`python3 -m pip list`; write {{{{PYTHON}}}}; see {doc}",
                    "skills/alpha/references/guide.md:1 names Python by hand in the code span "
                    f"` python x.py `; write {{{{PYTHON}}}}; see {doc}",
                ],
                interpreter_problems(root),
            )

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
        with mock.patch.object(shell_targets, "find_psscriptanalyzer", return_value=None) as missing:
            self.assertIn(
                f"  - PSScriptAnalyzer: {install}",
                missing_prerequisites((("PSScriptAnalyzer", "PSScriptAnalyzer", missing),)),
            )
            with self.assertRaises(AssertionError) as raised:
                script_analyzer_check(REPOSITORY_ROOT, [])
        self.assertIn(f"PSScriptAnalyzer was not found: {install}", str(raised.exception))
        self.assertEqual(
            [f"  - PSScriptAnalyzer 1.24.0 is older than the floor 1.25.0 in docs/dependency-updates.md: {install}"],
            outdated_prerequisites({"PSScriptAnalyzer": (1, 24, 0)}, (3, 11, 4)),
        )
        self.assertEqual([], outdated_prerequisites({"PSScriptAnalyzer": (1, 25, 0)}, (3, 11, 4)))

    def test_suppression_scan_finds_directives_attributes_and_configuration_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directive = "# shellcheck disable=SC2086"
            files = {
                "skills/alpha/scripts/run.sh": f"#!/usr/bin/env bash\n\n{directive}\necho $1\n",
                "skills/alpha/scripts/combined.bash": "# shellcheck source=lib.sh disable=SC1091\n. lib.sh\n",
                # Selecting a shell, or following a source, turns no check off.
                "skills/alpha/scripts/clean.sh": "# shellcheck shell=bash\n# shellcheck source=lib.sh\n. lib.sh\n",
                "skills/alpha/scripts/run.ps1": "function Get-It {\n"
                "    [Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]\n"
                "    param()\n}\n",
                "skills/alpha/scripts/short.ps1": "[SuppressMessage('PSAvoidGlobalVars', '')]\nparam()\n",
                # An attribute list holds the attribute after another one, on the same line or the next.
                "skills/alpha/scripts/listed.ps1": "[CmdletBinding(), "
                "Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '')]\nparam()\n",
                "skills/alpha/scripts/wrapped.ps1": "[CmdletBinding(SupportsShouldProcess),\n"
                " SuppressMessage('PSAvoidGlobalVars', '')]\nparam()\n",
                "skills/alpha/scripts/plain.ps1": "[CmdletBinding(), OutputType([string])]\nparam()\n",
                "docs/guide.md": f"Text naming {directive} in prose.\n\n```bash\n\n{directive}\necho $1\n```\n\n"
                "```powershell\n[System.Diagnostics.CodeAnalysis.SuppressMessage('X', '')]\nparam()\n```\n\n"
                "```powershell\n[CmdletBinding(), SuppressMessageAttribute('X', '')]\nparam()\n```\n",
                "docs/clean.md": '```bash\necho "$1"\n```\n',
                ".shellcheckrc": "disable=SC2086\n",
                "tools/PSScriptAnalyzerSettings.psd1": "@{ ExcludeRules = @('PSAvoidUsingWriteHost') }\n",
                # deployer/render.py holds the one sanctioned header, in Python.
                "deployer/render.py": f'HEADER = "{directive}"\n',
            }
            write_fixture_tree(root, files)
            fix = "; fix the cause instead"
            self.assertEqual(
                [
                    f".shellcheckrc configures ShellCheck{fix}",
                    f"docs/guide.md:5 suppresses a rule with a ShellCheck disable directive{fix}",
                    f"docs/guide.md:10 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"docs/guide.md:15 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"skills/alpha/scripts/combined.bash:1 suppresses a rule with a ShellCheck disable directive{fix}",
                    f"skills/alpha/scripts/listed.ps1:1 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"skills/alpha/scripts/run.ps1:2 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"skills/alpha/scripts/run.sh:3 suppresses a rule with a ShellCheck disable directive{fix}",
                    f"skills/alpha/scripts/short.ps1:1 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"skills/alpha/scripts/wrapped.ps1:2 suppresses a rule with SuppressMessageAttribute{fix}",
                    f"tools/PSScriptAnalyzerSettings.psd1 configures PSScriptAnalyzer{fix}",
                ],
                shell_targets.shell_suppression_problems(root, [root / name for name in files]),
            )


class ProgramEndingFixtures(unittest.TestCase):
    """Each program a shell or analyzer job starts is recognized when it is ended before its first line of output."""

    BASH = "C:/Program Files/Git/bin/bash.exe"
    SHELLCHECK = "C:/tools/shell check/shellcheck.exe"
    SCRIPTS = ("skills/alpha/scripts/a.sh", "skills/alpha/scripts/b.sh", "skills/alpha/scripts/c.sh")

    def static_shell_check(self, statuses: dict[int, Finished], shellcheck: str | None = SHELLCHECK) -> list[list[str]]:
        """Run the static shell checks over SCRIPTS, each start ending as statuses gives by its index, else with 0."""
        started: list[list[str]] = []

        def run(arguments: list[str], *_: object, **__: object) -> Finished:
            started.append(arguments)
            return statuses.get(len(started) - 1, Finished(0, b"", b""))

        with (
            mock.patch.object(
                shell_targets, "shell_script_targets", return_value=[REPOSITORY_ROOT / name for name in self.SCRIPTS]
            ),
            mock.patch.object(shell_targets, "find_git_bash", return_value=self.BASH),
            mock.patch.object(shell_targets, "find_shellcheck", return_value=shellcheck),
            mock.patch.object(job_pool, "run_bounded", side_effect=run),
        ):
            shell_targets.static_shell_check()
        return started

    def test_each_syntax_check_and_shellcheck_start_as_their_own_programs(self) -> None:
        self.assertEqual(
            [
                *([self.BASH, "-n", name] for name in self.SCRIPTS),
                [self.SHELLCHECK, "--severity=warning", *self.SCRIPTS],
            ],
            self.static_shell_check({}),
            "no shell above a check, whose status a Bash above it would read as exit 0 when Windows ends it",
        )

    # The status is the one Windows set on a Git Bash before it printed anything; the fixtures read it as Windows does,
    # on every platform, and test_platform_support holds how a signal ends a process elsewhere. A Bash above the check
    # read that end as exit 0 and `set -e` went on, so the script was never parsed and the checks passed.
    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_syntax_check_windows_ends_fails_the_checks_and_names_the_status_and_the_script(self) -> None:
        with self.assertRaises(Terminated) as raised:
            self.static_shell_check({1: Finished(2_147_483_652, b"", b"")})
        self.assertEqual(
            "bash.exe was ended with status 0x80000004 before its first line of output: "
            f"{self.BASH} -n {self.SCRIPTS[1]}",
            str(raised.exception),
        )

    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_check_ended_after_its_output_began_or_shellcheck_ended_fails_the_checks(self) -> None:
        with self.assertRaises(AssertionError) as raised:
            self.static_shell_check({0: Finished(2_147_483_652, b"", b"partial\n")})
        self.assertIn(
            f"(status 0x80000004, ended after its output began): {self.BASH} -n {self.SCRIPTS[0]}",
            str(raised.exception),
        )
        with self.assertRaises(Terminated) as ended:
            self.static_shell_check({len(self.SCRIPTS): Finished(2_147_483_652, b"", b"")})
        self.assertTrue(str(ended.exception).startswith("shellcheck.exe was ended with status 0x80000004"))

    def test_a_failed_syntax_check_stops_the_checks_and_shellcheck_missing_fails_before_any_start(self) -> None:
        with self.assertRaises(AssertionError) as raised:
            self.static_shell_check({1: Finished(2, b"", b"syntax error near unexpected token\n")})
        self.assertIn(f"Command failed with exit code 2: {self.BASH} -n {self.SCRIPTS[1]}", str(raised.exception))
        self.assertIn("syntax error near unexpected token", str(raised.exception))
        with self.assertRaises(AssertionError) as missing:
            self.assertEqual([], self.static_shell_check({}, shellcheck=None))
        self.assertTrue(str(missing.exception).startswith("ShellCheck was not found: "))

    # A Bash suite is Git Bash's own program, so the status Windows ended it with is the one the runner reads.
    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_bash_suite_starts_with_no_shell_above_it_and_its_ending_names_the_status(self) -> None:
        bash = "C:/Program Files/Git/bin/bash.exe"
        started: list[list[str]] = []

        def ended(arguments: list[str], *_: object, **__: object) -> Finished:
            started.append(arguments)
            return Finished(2_147_483_652, b"START suite.sh 7\n", b"")

        with (
            mock.patch.object(shell_targets, "find_git_bash", return_value=bash),
            mock.patch.object(job_pool, "run_bounded", side_effect=ended),
            self.assertRaises(AssertionError) as raised,
        ):
            shell_targets.run_bash_suite("skills/alpha/scripts/test_alpha.sh")
        self.assertEqual([[bash, "skills/alpha/scripts/test_alpha.sh"]], started)
        self.assertIn("(status 0x80000004, ended after its output began)", str(raised.exception))

    # A Git Bash that Windows ends reaches a Bash above it as exit 0 or as the status's low byte, 4, which hid the
    # endings CI legs met; this suite ends its own bash through Windows with the status a CI runner ended one with.
    @unittest.skipUnless(sys.platform == "win32", "only Windows ends a process with a status of its choosing")
    def test_a_bash_suite_that_windows_ends_is_named_with_the_status_windows_set(self) -> None:
        python = platform_support.to_shell_path(sys.executable)
        ending = (
            "import ctypes, sys; k = ctypes.windll.kernel32; k.OpenProcess.restype = ctypes.c_void_p; "
            "k.TerminateProcess(ctypes.c_void_p(k.OpenProcess(1, False, int(sys.argv[1]))), ctypes.c_uint(0x80000004))"
        )
        with tempfile.TemporaryDirectory() as temporary:
            suite = Path(temporary) / "test_ended.sh"
            suite.write_text(
                f"echo started\n'{python}' -c '{ending}' \"$(</proc/$$/winpid)\"\nsleep 5\necho 'not ended'\n",
                encoding="utf-8",
            )
            with self.assertRaises(AssertionError) as raised:
                shell_targets.run_bash_suite(platform_support.to_shell_path(str(suite)))
        self.assertIn("(status 0x80000004, ended after its output began)", str(raised.exception))
        self.assertIn("started", str(raised.exception))
        self.assertNotIn("not ended", str(raised.exception))

    # The status is the one Windows set on Git Bash before it printed anything; the fixtures read it as Windows does, on
    # every platform, and test_platform_support holds how a signal ends a process elsewhere.
    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_the_analyzer_and_markdown_shellcheck_report_an_ending_before_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "tools").mkdir()
            (root / "tools" / "run.ps1").write_text("Get-Date\n", encoding="utf-8")
            (root / "docs").mkdir()
            (root / "docs" / "guide.md").write_text("```bash\necho 'hello'\n```\n", encoding="utf-8")
            files = [root / "tools" / "run.ps1", root / "docs" / "guide.md"]
            with (
                mock.patch.object(shell_targets, "find_psscriptanalyzer", return_value="C:/m/PSScriptAnalyzer.psd1"),
                mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
                mock.patch.object(shell_targets, "find_shellcheck", return_value="C:/tools/shellcheck.exe"),
                mock.patch.object(
                    platform_support, "run_tool", return_value=platform_support.ToolResult(2_147_483_652, "")
                ),
            ):
                for check, program in ((script_analyzer_check, "pwsh.exe"), (markdown_shell_check, "shellcheck.exe")):
                    with self.subTest(program=program), self.assertRaises(Terminated) as raised:
                        check(root, files)
                    self.assertTrue(
                        str(raised.exception).startswith(
                            f"{program} was ended with status 0x80000004 before its first line of output: C:/tools/"
                        )
                    )
            # Output before the ending is the program's own report, a failed check as before.
            with (
                mock.patch.object(shell_targets, "find_shellcheck", return_value="C:/tools/shellcheck.exe"),
                mock.patch.object(
                    platform_support, "run_tool", return_value=platform_support.ToolResult(2_147_483_652, "partial\n")
                ),
                self.assertRaises(AssertionError),
            ):
                markdown_shell_check(root, files)
