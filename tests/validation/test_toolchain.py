"""Fixture tests for tests/validation/toolchain.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "skill-core" / "scripts"))

import toolchain
from git_test_base import GitTestCase
from job_pool import Terminated
from toolchain import (
    PREREQUISITES,
    find_psscriptanalyzer,
    find_ruff,
    missing_prerequisites,
    outdated_prerequisites,
    powershell_json,
    read_tool_version,
    requirements_problems,
    tool_versions,
)

from deployer import platform_support


class ToolchainFixtures(GitTestCase):
    def setUp(self) -> None:
        super().setUp()
        # The fixtures name Windows' install commands on every platform; test_platform_support pins each platform's.
        patcher = mock.patch.object(platform_support, "INSTALL_HINTS", platform_support.WINDOWS_INSTALL_HINTS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prerequisite_check_reports_every_missing_tool(self) -> None:
        def raises() -> str:
            raise AssertionError("not found")

        self.assertEqual(
            [
                "  - Git Bash: winget install --id Git.Git",
                "  - PowerShell 7 (pwsh): winget install --id Microsoft.PowerShell",
            ],
            missing_prerequisites(
                (
                    ("Git Bash", "Git Bash", raises),
                    ("ShellCheck", "ShellCheck", lambda: "C:/tools/shellcheck.exe"),
                    ("PowerShell 7 (pwsh)", "PowerShell", lambda: None),
                )
            ),
        )
        self.assertEqual([], missing_prerequisites((("ShellCheck", "ShellCheck", lambda: "shellcheck"),)))

    def test_requirements_hold_only_comments_blank_lines_and_the_listed_pins(self) -> None:
        packages = ("ruff", "mypy")
        clean = "# Development dependencies.\n\nruff==0.16.10\n  # Indented comment.\nmypy==2.4.0\n"
        self.assertEqual([], requirements_problems(clean, packages))
        for added in (
            "requests\n",
            "requests==2.32.0\n",
            "--extra-index-url https://example.invalid/simple\n",
            "--index-url https://example.invalid/simple\n",
            "-r other.txt\n",
            "-c constraints.txt\n",
            "-e git+https://example.invalid/pkg.git#egg=pkg\n",
            "pkg @ https://example.invalid/pkg.whl\n",
            "ruff>=0.16.10\n",
            "mypy==2.4.0 --hash=sha256:00\n",
            "mypy==2.4.0; python_version > '3.11'\n",
            "mypy==2.4.0 # a trailing comment\n",
            "Ruff==0.16.10\n",
        ):
            with self.subTest(added=added):
                problems = requirements_problems(clean + added, packages)
                self.assertTrue(any("line 6 is not name==version" in problem for problem in problems), problems)
        self.assertEqual(
            ["mypy is pinned 0 times, not once"], requirements_problems(clean.replace("mypy==2.4.0\n", ""), packages)
        )
        self.assertEqual(
            ["ruff is pinned 2 times, not once"], requirements_problems(clean + "ruff==0.17.0\n", packages)
        )

    def test_missing_ruff_is_reported_with_the_install_command(self) -> None:
        self.assertIn(("ruff", "ruff", find_ruff), PREREQUISITES)
        # Neither the interpreter's ruff package nor one on PATH: no import or command error, only None.
        with (
            mock.patch.dict(sys.modules, {"ruff": None, "ruff.__main__": None}),
            mock.patch.object(platform_support, "find_executable", return_value=None),
        ):
            self.assertIsNone(find_ruff())
        self.assertEqual(
            ["  - ruff: python -m pip install -r requirements-dev.txt"],
            missing_prerequisites((("ruff", "ruff", lambda: None),)),
        )

    def test_script_analyzer_is_found_by_its_manifest_and_versioned_from_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "PSScriptAnalyzer with spaces" / "1.25.0" / "PSScriptAnalyzer.psd1"
            manifest.parent.mkdir(parents=True)
            listed = platform_support.ToolResult(0, f"WARNING: unrelated\n{manifest}\n")
            with (
                mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
                mock.patch.object(platform_support, "run_tool", return_value=listed),
            ):
                self.assertEqual(str(manifest), find_psscriptanalyzer())
            body = "@{\n    RootModule = 'PSScriptAnalyzer.psm1'\n    ModuleVersion = '1.25.0'\n"
            body += "    PowerShellVersion = '5.1'\n}\n"
            manifest.write_text(body, encoding="utf-8-sig")
            self.assertEqual((1, 25, 0), read_tool_version(str(manifest)))
            manifest.write_text(body, encoding="utf-16")
            self.assertEqual((1, 25, 0), read_tool_version(str(manifest)))
            manifest.write_text("@{ PowerShellVersion = '5.1' }\n", encoding="utf-8")
            self.assertIsNone(read_tool_version(str(manifest)))

    def test_prerequisite_check_reports_every_tool_older_than_its_floor(self) -> None:
        self.assertEqual(
            [
                "  - Python 3.11.3 is older than the floor 3.11.4 in docs/dependency-updates.md",
                "  - ShellCheck 0.8.0 is older than the floor 0.9.0 in docs/dependency-updates.md: "
                "winget install --id koalaman.shellcheck",
                "  - PowerShell 7 (pwsh): its version could not be read; the floor is 7.0 in "
                "docs/dependency-updates.md: winget install --id Microsoft.PowerShell",
                "  - ruff 0.17.0 is older than the floor 0.17.1 in docs/dependency-updates.md: "
                "python -m pip install -r requirements-dev.txt",
            ],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 8, 0), "PowerShell 7 (pwsh)": None, "ruff": (0, 17, 0)},
                (3, 11, 3),
            ),
        )
        self.assertEqual(
            [],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 5, 3), "ruff": (0, 17, 1)},
                (3, 11, 4),
            ),
        )
        # A missing tool is reported by missing_prerequisites, not again here.
        self.assertEqual([], outdated_prerequisites({}, (3, 14, 7)))

    def test_tool_versions_reads_each_found_tool(self) -> None:
        def raises() -> str:
            raise AssertionError("not found")

        read = {"C:/tools/shellcheck.exe": (0, 11, 0), "C:/tools/pwsh.exe": None}
        self.assertEqual(
            {"ShellCheck": (0, 11, 0), "PowerShell 7 (pwsh)": None},
            tool_versions(
                (
                    ("Git Bash", "Git Bash", raises),
                    ("ShellCheck", "ShellCheck", lambda: "C:/tools/shellcheck.exe"),
                    ("PowerShell 7 (pwsh)", "PowerShell", lambda: "C:/tools/pwsh.exe"),
                ),
                read.__getitem__,
            ),
        )

    # The status is the one Windows set on Git Bash before it printed anything; the fixtures read it as Windows does, on
    # every platform, and test_platform_support holds how a signal ends a process elsewhere.
    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_probe_ended_before_output_is_named_rather_than_read_as_a_missing_module(self) -> None:
        with (
            mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
            mock.patch.object(
                platform_support, "run_tool", return_value=platform_support.ToolResult(2_147_483_652, "")
            ),
        ):
            with self.assertRaises(Terminated) as raised:
                find_psscriptanalyzer()
            self.assertTrue(
                str(raised.exception).startswith(
                    "pwsh.exe was ended with status 0x80000004 before its first line of output: C:/tools/pwsh.exe "
                )
            )
            prerequisite = (("PSScriptAnalyzer", "PSScriptAnalyzer", find_psscriptanalyzer),)
            self.assertEqual(
                [f"  - PSScriptAnalyzer: could not be looked for, because {raised.exception}; run validation again"],
                missing_prerequisites(prerequisite),
            )
            self.assertEqual({}, tool_versions(prerequisite, read_version=lambda path: (1, 25, 0)))


class PowerShellJsonRunner(GitTestCase):
    def run_script(self, result: platform_support.ToolResult) -> tuple[list[object], list[dict[str, str]]]:
        """The runner's list for a fake pwsh's result, and each environment it was started with."""
        seen: list[dict[str, str]] = []

        def fake(command: list[str], environment: dict[str, str]) -> platform_support.ToolResult:
            seen.append(environment)
            self.assertEqual(["C:/tools/pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"], command[:5])
            self.assertEqual(["a", 1], json.loads(Path(environment["INPUTS"]).read_text(encoding="utf-8")))
            return result

        with (
            mock.patch.object(toolchain, "find_powershell", return_value="C:/tools/pwsh.exe"),
            mock.patch.object(platform_support, "run_tool", side_effect=fake),
        ):
            return powershell_json("Get-Thing", ["a", 1], "INPUTS", "Could not run", {"FLOOR": "1.0"}), seen

    def test_the_last_line_s_list_is_the_result_and_the_inputs_reach_the_script(self) -> None:
        found, seen = self.run_script(platform_support.ToolResult(0, 'warming up\n[{"line": 3}]\n\n'))
        self.assertEqual([{"line": 3}], found)
        self.assertEqual("1.0", seen[0]["FLOOR"])

    def test_a_failed_exit_or_output_that_is_not_a_list_is_a_failure_naming_the_status(self) -> None:
        for result in (
            platform_support.ToolResult(1, "[]\n"),
            platform_support.ToolResult(0, '{"line": 3}\n'),
            platform_support.ToolResult(0, "not json\n"),
            platform_support.ToolResult(0, ""),
        ):
            with self.subTest(output=result.output), self.assertRaises(AssertionError) as raised:
                self.run_script(result)
            self.assertTrue(str(raised.exception).startswith(f"Could not run (exit code {result.returncode}):\n"))

    @mock.patch.object(platform_support, "WINDOWS", True)
    def test_a_pwsh_ended_before_output_is_a_termination(self) -> None:
        with self.assertRaises(Terminated):
            self.run_script(platform_support.ToolResult(2_147_483_652, ""))


if __name__ == "__main__":
    unittest.main()
