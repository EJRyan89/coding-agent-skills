"""Fixture tests for tests/validation/toolchain.py.

Each policy fails on a violating input and passes on a conforming one.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import toolchain
from toolchain import (
    PREREQUISITES,
    find_psscriptanalyzer,
    find_ruff,
    missing_prerequisites,
    outdated_prerequisites,
    read_tool_version,
    tool_versions,
)

from deployer import platform_support


class ToolchainFixtures(unittest.TestCase):
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
                "  - Python 3.10.12 is older than the floor 3.11 in docs/dependency-updates.md",
                "  - ShellCheck 0.8.0 is older than the floor 0.9.0 in docs/dependency-updates.md: "
                "winget install --id koalaman.shellcheck",
                "  - PowerShell 7 (pwsh): its version could not be read; the floor is 7.0 in "
                "docs/dependency-updates.md: winget install --id Microsoft.PowerShell",
                "  - ruff 0.16.9 is older than the floor 0.16.10 in docs/dependency-updates.md: "
                "python -m pip install -r requirements-dev.txt",
            ],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 8, 0), "PowerShell 7 (pwsh)": None, "ruff": (0, 16, 9)},
                (3, 10, 12),
            ),
        )
        self.assertEqual(
            [],
            outdated_prerequisites(
                {"Git Bash": None, "ShellCheck": (0, 9, 0), "PowerShell 7 (pwsh)": (7, 5, 3), "ruff": (0, 16, 10)},
                (3, 11, 0),
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


if __name__ == "__main__":
    unittest.main()
