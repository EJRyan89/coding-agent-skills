from __future__ import annotations

import ast
import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import re
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "ejryan89-skill-core" / "scripts"))

from skill_roots import sibling_scripts

# The extraction the code-review skills run, which must find the pdftotext check reports.
sys.path.insert(0, str(sibling_scripts("ejryan89-code-review-core")))
# The formatter skill, which must find and run the dotnet and dotnet-format check reports.
sys.path.insert(0, str(sibling_scripts("dotnet-format")))

import bounded_process
import github_client
import review_documents
import review_hosts
import review_runtime
import run_dotnet_format
from harness import DEPLOY_COMMAND, REPOSITORY_ROOT, SOURCE_ID, DeployerTestCase, Result, forward
from pdf_tool import MINIMUMS, PdfIdentity, PdfToolRefused, parse_banner

from deployer import platform_support, tools
from deployer.check import _tool_line

XPDF_4_06 = "pdftotext version 4.06\nCopyright 1996-2025 Glyph & Cog, LLC\n"
XPDF_4_00 = "pdftotext version 4.00\nCopyright 1996-2017 Glyph & Cog, LLC\n"
# The banner of a distribution's Poppler, which keeps its upstream version while it backports fixes.
DISTRIBUTION_POPPLER = (
    "pdftotext version 24.02.0\nCopyright 2005-2024 The Poppler Developers - http://poppler.freedesktop.org\n"
    "Copyright 1996-2011, 2022 Glyph & Cog, LLC\n"
)
# What check and each refusal add to the providers' floors: nothing on Windows, and on Linux, where a distribution's
# Poppler keeps a version below its floor as it backports fixes, the Xpdf command-line tools.
LINUX_HINT = (
    "a distribution's Poppler keeps a version below its floor as it backports fixes, so install the Xpdf command-line "
    "tools from https://www.xpdfreader.com/download.html"
)
HINT = "" if sys.platform == "win32" else f"; {LINUX_HINT}"
INSTALL = f"install Xpdf 4.06 or newer or Poppler 26.10.0 or newer{HINT}"
BUNDLE = {"operations": {"members": ["reviewer"]}}
INSTALLED = {
    "shellcheck": ("C:/tools/shellcheck.exe", "ShellCheck - shell script analysis tool\nversion: 0.11.0\n"),
    "gh": ("C:/tools/gh.exe", "gh version 2.97.0 (2026-07-31)\n"),
    "copilot": ("C:/tools/copilot.exe", "GitHub Copilot CLI 1.0.89.\n"),
    "codex": ("C:/tools/codex.exe", "codex-cli 0.160.0\n"),
    "dotnet-format": ("C:/tools/dotnet-format.exe", "5.1.250801+4a851ea9\n"),
    "claude": ("C:/tools/claude.exe", "2.1.300 (Claude Code)\n"),
    "dotnet": ("C:/tools/dotnet.exe", "10.0.301\n"),
    "pdftotext": ("C:/tools/pdftotext.exe", XPDF_4_06),
}
# Xpdf's pdftotext exits 99 after printing its version.
STATUSES = {"C:/tools/pdftotext.exe": 99}
GIT = "C:/tools/git.exe"
PYTHON_COMMAND = "C:/Python/python.exe"
STORE_ALIAS = "C:/Profile/Local/Microsoft/WindowsApps/python.exe"
MSI_PWSH = "C:/Program Files/PowerShell/7/pwsh.exe"
PACKAGED_PWSH = "C:/Profile/Local/Microsoft/WindowsApps/pwsh.exe"


def prompt_input(mode: str) -> str:
    """What `codex debug prompt-input` prints around the sandbox mode it would use, as Codex CLI words it."""
    return (
        '[{"type": "message", "content": [{"text": "<permissions instructions>\\nFilesystem sandboxing defines which '
        f'files can be read or written. `sandbox_mode` is `{mode}`: The sandbox permits reading files."}}]}}]\n'
    )


GIT_VERSION = "git version 2.54.0.windows.1\n"
COMMIT = "71ad8150c0ffee5eed0123456789abcdef012345"


def set_tools(test: DeployerTestCase, name: str, names: list[str], key: str = "tools") -> None:
    path = test.source / "deploy-meta" / f"{name}.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    metadata[key] = names
    path.write_text(json.dumps(metadata), encoding="utf-8")


class Machine:
    """Fake tool locations and version output for one simulated machine."""

    def __init__(
        self,
        missing: tuple[str, ...] = (),
        versions: dict[str, str] | None = None,
        powershell: str = "7.6.6",
        describe: str | None = None,
        python: tuple[int, ...] = (3, 12, 1),
        settings: bool = False,
        python_command: str | None = PYTHON_COMMAND,
        pwsh_command: str | None = None,
        codex_probe: tuple[int, str] = (0, prompt_input("workspace-write")),
    ) -> None:
        # describe is what git describe prints for the deployed commit; None makes it fail, as for an unknown commit.
        # settings says whether this machine's platform has the machine settings check reports, as Windows does: then
        # python_command is what the skills' Python command runs, pwsh_command what pwsh runs, and codex_probe what
        # Codex's sandbox probe prints.
        self.settings = settings
        self.python_command = python_command
        self.pwsh_command = pwsh_command
        self.codex_probe = codex_probe
        self.git = None if "git" in missing else GIT
        self.describe = describe
        self.python = python
        self.git_calls: list[list[str]] = []
        self.installed = {name: entry for name, entry in INSTALLED.items() if name not in missing}
        self.versions = dict(self.installed.values())
        for name, output in (versions or {}).items():
            self.versions[self.installed[name][0]] = output
        self.bash = None if "git-bash" in missing else "C:/Program Files/Git/bin/bash.exe"
        self.powershell = None if "powershell" in missing else "C:/tools/pwsh.exe"
        self.versions["C:/tools/pwsh.exe"] = f"{powershell}\n"

    def patches(self) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch(
                "deployer.platform_support.find_executable",
                side_effect=self.find_executable,
            )
        )
        for flag in ("HAS_APP_EXECUTION_ALIASES", "CODEX_NEEDS_SANDBOX_SETTING"):
            stack.enter_context(mock.patch.object(platform_support, flag, self.settings))
        stack.enter_context(
            mock.patch.object(
                platform_support,
                "is_app_execution_alias",
                side_effect=lambda program: program in (STORE_ALIAS, PACKAGED_PWSH),
            )
        )
        stack.enter_context(
            mock.patch(
                "deployer.platform_support.find_executables",
                side_effect=lambda name: [self.installed[name][0]] if name in self.installed else [],
            )
        )
        stack.enter_context(mock.patch("deployer.platform_support.find_bash", return_value=self.bash))
        stack.enter_context(mock.patch("deployer.platform_support.find_powershell", return_value=self.powershell))
        stack.enter_context(
            mock.patch(
                "deployer.platform_support.run_tool",
                side_effect=lambda arguments, environment=None: self.run_tool(arguments),
            )
        )
        stack.enter_context(mock.patch("deployer.tools.python_version", return_value=self.python))
        stack.enter_context(mock.patch("deployer.tools.probe_pdftotext", side_effect=self.probe_pdftotext))
        return stack

    def find_executable(self, name: str) -> str | None:
        if name == "git":
            return self.git
        if name == platform_support.PYTHON:
            return self.python_command
        if name == "pwsh":
            return self.pwsh_command
        return self.installed.get(name, (None,))[0]

    def probe_pdftotext(self, path: str) -> PdfIdentity:
        """The banner of the program at this resolved path, with the status its provider exits with."""
        [banner] = [output for found, output in self.versions.items() if Path(found).resolve() == Path(path)]
        return parse_banner(banner, 0 if "The Poppler Developers" in banner else 99)

    def run_tool(self, arguments: list[str]) -> platform_support.ToolResult:
        if arguments[1:] == list(tools.CODEX_SANDBOX_PROBE):
            return platform_support.ToolResult(*self.codex_probe)
        if arguments[0] != GIT:
            return platform_support.ToolResult(STATUSES.get(arguments[0], 0), self.versions[arguments[0]])
        if arguments[1:] == ["--version"]:
            return platform_support.ToolResult(0, GIT_VERSION)
        self.git_calls.append(arguments)
        if self.describe is None:
            return platform_support.ToolResult(128, "fatal: No names found, cannot describe anything.\n")
        return platform_support.ToolResult(0, f"{self.describe}\n")


class CheckCommandTests(DeployerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.make_source_json(bundles=BUNDLE)
        self.make_skill("reviewer", "Reviewer", skill_deps=["core"])
        self.make_skill("core", "Core", selectable=False)
        self.make_skill("reporter", "Reporter")
        self.make_skill("formatter", "Formatter")
        self.make_skill("plain", "Plain")
        set_tools(self, "core", ["copilot", "gh"])
        set_tools(self, "reporter", ["gh"])
        set_tools(self, "formatter", ["dotnet-format"])

    def check(self, machine: Machine, *arguments: str) -> Result:
        with machine.patches():
            return self.run_cli("check", *arguments)

    def test_everything_installed_is_ready_to_deploy(self) -> None:
        result = self.check(Machine())
        self.assertEqual(0, result.code, result.output)
        # Windows checks rendered Bash with Git for Windows' Bash, and every other platform with the system's.
        bash = "  Bash (needed to deploy; C:/Program Files/Git/bin/bash.exe)\n"
        before, after = ("", bash.replace("Bash", "Git Bash", 1)) if platform_support.WINDOWS else (bash, "")
        self.assertEqual(
            "\n"
            "Source: Test Skills (test/skills)\n"
            "Deployed commit: unknown\n"
            f"Home: {forward(self.home)}\n"
            "\n"
            "=== CHECK ===\n"
            "\n"
            "FOUND (7):\n"
            f"{before}"
            "  Git 2.54.0 (used by every skill)\n"
            f"{after}"
            "  PowerShell 7.6.6 (needed to deploy)\n"
            "  Python 3.12.1 (needed to deploy; 3.11.4 or newer)\n"
            "  ShellCheck 0.11.0 (needed to deploy)\n"
            "  dotnet-format 5.1.250801 (used by formatter)\n"
            "  gh 2.97.0 (used by operations, reporter)\n"
            "\n"
            "OPTIONAL (3):\n"
            "  Claude Code 2.1.300 (used by every skill run in Claude Code)\n"
            "  codex 0.160.0 (used by Codex verification)\n"
            "  copilot 1.0.89 (used by operations, Copilot verification)\n"
            "\n"
            "Ready to deploy.\n"
            "\n",
            result.output,
        )

    def test_with_both_machine_settings_in_place_it_reports_them_found(self) -> None:
        result = self.check(Machine(settings=True))
        self.assertEqual(0, result.code, result.output)
        found = self.report_groups(result.output, "CHECK")["FOUND"]
        self.assertIn(
            "codex sandbox (workspace-write configured; confirm its sandbox starts a command as "
            '"Windows sandbox mode" in docs/codex-support.md shows)',
            [" ".join(line.split()) for line in found],
        )
        self.assertIn(
            f"{platform_support.PYTHON} command (runs {PYTHON_COMMAND}, not an app execution alias)",
            [" ".join(line.split()) for line in found],
        )
        self.assertNotIn("UNUSABLE", self.report_groups(result.output, "CHECK"))

    def test_a_missing_sandbox_setting_and_a_python_alias_are_named_with_the_section_to_read(self) -> None:
        result = self.check(
            Machine(settings=True, python_command=STORE_ALIAS, codex_probe=(0, prompt_input("read-only")))
        )
        self.assertEqual(0, result.code, result.output)
        unusable = [" ".join(line.split()) for line in self.report_groups(result.output, "CHECK")["UNUSABLE"]]
        self.assertEqual(
            [
                "codex sandbox (read-only, so Codex CLI blocks every skill script; set [features] prefer_mxc or "
                '[windows] sandbox in ~/.codex/config.toml; see "Windows sandbox mode" in docs/codex-support.md)',
                f"{platform_support.PYTHON} command (runs the app execution alias {STORE_ALIAS}, which Codex CLI "
                "cannot start; turn off the python.exe and python3.exe aliases under Settings > Apps > Advanced app "
                'settings > App execution aliases; see "Python app execution aliases" in docs/codex-support.md)',
            ],
            unusable,
        )
        self.assertIn("Ready to deploy. Skills cannot use a missing, outdated, or unusable tool\n", result.output)

    def test_a_pwsh_that_is_the_packaged_powershells_alias_is_unusable_and_its_msi_build_found(self) -> None:
        unusable = self.report_groups(self.check(Machine(settings=True, pwsh_command=PACKAGED_PWSH)).output, "CHECK")
        self.assertEqual(
            [
                f"pwsh command (runs the app execution alias {PACKAGED_PWSH}, the packaged PowerShell, which Codex "
                "CLI runs every command through and cannot start; replace the packaged PowerShell with its MSI build: "
                "winget uninstall --id Microsoft.PowerShell, then winget install --id Microsoft.PowerShell "
                '--installer-type wix; see "PowerShell app execution alias" in docs/codex-support.md)'
            ],
            [" ".join(line.split()) for line in unusable["UNUSABLE"]],
        )
        found = self.report_groups(self.check(Machine(settings=True, pwsh_command=MSI_PWSH)).output, "CHECK")
        self.assertIn(
            f"pwsh command (runs {MSI_PWSH}, not an app execution alias)",
            [" ".join(line.split()) for line in found["FOUND"]],
        )
        self.assertNotIn("UNUSABLE", found)

    def test_no_pwsh_line_without_a_pwsh_nor_off_windows(self) -> None:
        # Codex CLI then runs its commands through Windows PowerShell, which its sandbox starts.
        for machine in (Machine(settings=True), Machine(settings=False, pwsh_command=PACKAGED_PWSH)):
            with self.subTest(settings=machine.settings):
                self.assertNotIn("pwsh command", self.check(machine).output)

    def test_a_sandbox_mode_codex_does_not_name_is_unusable_and_no_python_command_is_missing(self) -> None:
        for status, output in ((0, "no permissions here\n"), (1, prompt_input("workspace-write"))):
            with self.subTest(status=status):
                groups = self.report_groups(
                    self.check(Machine(settings=True, python_command=None, codex_probe=(status, output))).output,
                    "CHECK",
                )
                self.assertIn(
                    "codex sandbox (codex debug prompt-input named no sandbox mode; set [features] prefer_mxc or "
                    '[windows] sandbox in ~/.codex/config.toml; see "Windows sandbox mode" in docs/codex-support.md)',
                    [" ".join(line.split()) for line in groups["UNUSABLE"]],
                )
                self.assertIn(
                    f"{platform_support.PYTHON} command (every skill's scripts run {platform_support.PYTHON}; put "
                    "Python on PATH)",
                    [" ".join(line.split()) for line in groups["MISSING"]],
                )

    def test_the_sandbox_is_not_probed_without_a_codex_that_meets_its_floor_nor_off_windows(self) -> None:
        for machine in (
            Machine(settings=True, missing=("codex",)),
            Machine(settings=True, versions={"codex": "codex-cli 0.87.0\n"}),
            Machine(settings=False, codex_probe=(0, prompt_input("read-only"))),
        ):
            with self.subTest(settings=machine.settings, installed=sorted(machine.installed)):
                with machine.patches() as _, mock.patch.object(tools, "codex_sandbox_mode") as probed:
                    result = self.run_cli("check")
                self.assertEqual(0, result.code, result.output)
                probed.assert_not_called()
                self.assertNotIn("codex sandbox", result.output)
        self.assertNotIn("python command", self.check(Machine(settings=False)).output)

    def test_missing_deploy_tool_fails_with_its_install_command(self) -> None:
        result = self.check(Machine(missing=("shellcheck",)))
        self.assertEqual(1, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        hint = "winget install --id koalaman.shellcheck" if platform_support.WINDOWS else "sudo apt install shellcheck"
        self.assertEqual([f"ShellCheck (needed to deploy; {hint})"], groups["MISSING"])
        self.assertTrue(
            result.output.endswith(
                'Install the missing tools before deploying; see "Installing the tools" in\n'
                "docs/installation.md. Open a new terminal afterwards so the tools are on PATH.\n\n"
            ),
            result.output,
        )

    def test_a_python_before_3_11_4_is_outdated_because_at_extracts_with_the_tarfile_data_filter(self) -> None:
        # Python 3.11.4 is the first 3.11 release whose tarfile has extraction filters, which --at extracts with.
        result = self.check(Machine(python=(3, 11, 3)))
        self.assertEqual(1, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["Python 3.11.3 (needed to deploy; needs 3.11.4 or newer)"], groups["OUTDATED"])
        self.assertTrue(
            result.output.endswith(
                'Update the outdated tools before deploying; see "Installing the tools" in\n'
                "docs/installation.md. Open a new terminal afterwards so the tools are on PATH.\n\n"
            ),
            result.output,
        )
        result = self.check(Machine(python=(3, 11, 4)))
        self.assertEqual(0, result.code, result.output)
        found = self.report_groups(result.output, "CHECK")["FOUND"]
        self.assertIn("Python 3.11.4 (needed to deploy; 3.11.4 or newer)", found)

    def test_a_missing_and_an_outdated_deploy_tool_ask_for_both_in_one_lead(self) -> None:
        result = self.check(Machine(missing=("shellcheck",), python=(3, 11, 3)))
        self.assertEqual(1, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["Python 3.11.3 (needed to deploy; needs 3.11.4 or newer)"], groups["OUTDATED"])
        self.assertEqual(1, len(groups["MISSING"]), result.output)
        self.assertTrue(
            result.output.endswith(
                "Install the missing tools and update the outdated ones before deploying; see "
                '"Installing the tools" in\n'
                "docs/installation.md. Open a new terminal afterwards so the tools are on PATH.\n\n"
            ),
            result.output,
        )

    def test_missing_skill_tool_still_allows_deploying(self) -> None:
        result = self.check(Machine(missing=("gh",)))
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(["gh (used by operations, reporter)"], self.report_groups(result.output, "CHECK")["MISSING"])
        self.assertIn("Ready to deploy. Skills cannot use a missing, outdated, or unusable tool\n", result.output)

    def test_optional_and_outdated_tools_are_reported_separately(self) -> None:
        groups = self.report_groups(self.check(Machine(missing=("copilot",))).output, "CHECK")
        self.assertEqual(
            [
                "Claude Code 2.1.300 (used by every skill run in Claude Code)",
                "codex 0.160.0 (used by Codex verification)",
                "copilot (not installed; used by operations, Copilot verification)",
            ],
            groups["OPTIONAL"],
        )
        self.assertNotIn("MISSING", groups)
        result = self.check(Machine(versions={"copilot": "GitHub Copilot CLI 1.0.87.\n"}))
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            ["copilot 1.0.87 (needs 1.0.88 or newer)"], self.report_groups(result.output, "CHECK")["OUTDATED"]
        )

    def test_gh_before_2_48_is_outdated_because_scripts_slurp_paginated_api_output(self) -> None:
        # gh 2.48.0 added `gh api --paginate --slurp`, which repo-cleanup and the code-review scripts rely on.
        result = self.check(Machine(versions={"gh": "gh version 2.47.0 (2024-04-03)\n"}))
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(["gh 2.47.0 (needs 2.48.0 or newer)"], self.report_groups(result.output, "CHECK")["OUTDATED"])
        groups = self.report_groups(
            self.check(Machine(versions={"gh": "gh version 2.48.0 (2024-04-17)\n"})).output, "CHECK"
        )
        self.assertIn("gh 2.48.0 (used by operations, reporter)", groups["FOUND"])
        self.assertNotIn("OUTDATED", groups)

    def test_dotnet_before_9_0_200_is_outdated_because_earlier_sdks_cannot_read_slnx(self) -> None:
        # dotnet-format formats a .slnx solution with the SDK's `dotnet format`; 9.0.200 is the first SDK that reads it.
        set_tools(self, "formatter", ["dotnet"], key="optional_tools")
        groups = self.report_groups(self.check(Machine(versions={"dotnet": "9.0.100\n"})).output, "CHECK")
        self.assertEqual(["dotnet 9.0.100 (needs 9.0.200 or newer)"], groups["OUTDATED"])
        groups = self.report_groups(self.check(Machine(versions={"dotnet": "9.0.200\n"})).output, "CHECK")
        self.assertIn("dotnet 9.0.200 (used by formatter)", groups["OPTIONAL"])

    def test_dotnet_format_requires_the_sdk_and_leaves_the_deprecated_global_tool_optional(self) -> None:
        # The SDK's `dotnet format` formats every .slnx and SDK-style .sln; the global tool only an older .sln.
        metadata = json.loads((REPOSITORY_ROOT / "deploy-meta" / "dotnet-format.json").read_text(encoding="utf-8"))
        self.assertEqual((["dotnet"], ["dotnet-format", "gh"]), (metadata["tools"], metadata["optional_tools"]))
        set_tools(self, "formatter", metadata["tools"])
        set_tools(self, "formatter", metadata["optional_tools"], key="optional_tools")
        groups = self.report_groups(self.check(Machine(missing=("dotnet",))).output, "CHECK")
        self.assertEqual(["dotnet (used by formatter)"], groups["MISSING"])
        self.assertIn("dotnet-format 5.1.250801 (used by formatter)", groups["OPTIONAL"])
        groups = self.report_groups(self.check(Machine(missing=("dotnet-format",))).output, "CHECK")
        self.assertIn("dotnet 10.0.301 (used by formatter)", groups["FOUND"])
        self.assertIn("dotnet-format (not installed; used by formatter)", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)
        self.assertNotIn("OUTDATED", groups)

    def test_pdftotext_reports_separate_provider_floors_and_unusable_probes(self) -> None:
        set_tools(self, "core", ["pdftotext"], key="optional_tools")
        groups = self.report_groups(self.check(Machine()).output, "CHECK")
        self.assertIn("pdftotext Xpdf 4.06 (used by operations)", groups["OPTIONAL"])
        groups = self.report_groups(self.check(Machine(missing=("pdftotext",))).output, "CHECK")
        self.assertIn(f"pdftotext (not installed; used by operations; {INSTALL})", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)
        for banner, provider, version, floor in (
            (XPDF_4_00, "Xpdf", "4.00", "4.06"),
            ("pdftotext version 0.86.1\nCopyright 2005-2020 The Poppler Developers\n", "Poppler", "0.86.1", "26.10.0"),
            (DISTRIBUTION_POPPLER, "Poppler", "24.02.0", "26.10.0"),
            (
                "pdftotext version 26.09.0\nCopyright 2005-2026 The Poppler Developers\n",
                "Poppler",
                "26.09.0",
                "26.10.0",
            ),
        ):
            with self.subTest(provider=provider, version=version):
                groups = self.report_groups(self.check(Machine(versions={"pdftotext": banner})).output, "CHECK")
                self.assertEqual(
                    [
                        f"pdftotext {provider} {version} (no supported pdftotext: skipped C:/tools/pdftotext.exe "
                        f"({provider} {version}, older than {provider} {floor}); {INSTALL})"
                    ],
                    groups["OUTDATED"],
                )
        poppler = "pdftotext version 26.10.0\nCopyright 2005-2026 The Poppler Developers\n"
        groups = self.report_groups(self.check(Machine(versions={"pdftotext": poppler})).output, "CHECK")
        self.assertIn("pdftotext Poppler 26.10.0 (used by operations)", groups["OPTIONAL"])
        groups = self.report_groups(self.check(Machine(versions={"pdftotext": "version 99.99"})).output, "CHECK")
        self.assertIn("provider/version is unidentifiable", groups["UNUSABLE"][0])

    def test_a_failed_pdf_probe_is_not_reported_as_an_optional_usable_tool(self) -> None:
        set_tools(self, "core", ["pdftotext"], key="optional_tools")
        with (
            Machine().patches(),
            mock.patch(
                "deployer.tools.probe_pdftotext",
                side_effect=PdfToolRefused("version probe failed with exit status 1"),
            ),
        ):
            line = _tool_line(tools.SKILL_TOOLS["pdftotext"], True, ["operations"])
        self.assertEqual(
            (
                "pdftotext",
                "UNUSABLE",
                "no supported pdftotext: skipped C:/tools/pdftotext.exe (version probe failed with exit status 1); "
                f"{INSTALL}",
            ),
            (line.name, line.action, line.detail),
        )

    def test_windows_powershell_is_enough_to_deploy(self) -> None:
        groups = self.report_groups(self.check(Machine(powershell="5.1.26100.1")).output, "CHECK")
        self.assertIn(
            "PowerShell 5.1.26100 (enough to deploy; the validation suite needs PowerShell 7)", groups["FOUND"]
        )

    def test_tools_of_uninstalled_opt_in_items_are_optional(self) -> None:
        metadata = self.source / "deploy-meta" / "formatter.json"
        metadata.write_text(
            json.dumps({**json.loads(metadata.read_text(encoding="utf-8")), "opt_in": True}), encoding="utf-8"
        )
        machine = Machine(missing=("dotnet-format",))
        result = self.check(machine)
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertIn("dotnet-format (not installed; used by opt-in formatter)", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)
        self.assertTrue(result.output.endswith("Ready to deploy.\n\n"), result.output)
        self.make_config()
        with machine.patches():
            self.deploy_ok("--all", "--include", "formatter")
        groups = self.report_groups(self.check(machine).output, "CHECK")
        self.assertEqual(["dotnet-format (used by formatter)"], groups["MISSING"])

    def test_a_bundle_member_installed_as_a_dependency_leaves_the_bundles_other_tools_optional(self) -> None:
        self.make_source_json(bundles={"pack": {"members": ["reviewer", "formatter"], "opt_in": True}})
        self.make_skill("plain", "Plain", skill_deps=["reviewer"])
        # dotnet is required by core alone, the dependency of reviewer, so only plain's dependency makes it needed.
        set_tools(self, "core", ["copilot", "dotnet", "gh"])
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all")
        result = self.check(machine)
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        # The formatter is not installed and --all leaves its bundle out, so its tool is not needed.
        self.assertIn("dotnet-format (not installed; used by opt-in pack)", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)
        # The dependency is installed, so the tools it runs are needed, named by the root that installed it.
        self.assertIn("dotnet 10.0.301 (used by opt-in pack, plain)", groups["FOUND"])
        self.assertIn("gh 2.97.0 (used by opt-in pack, plain, reporter)", groups["FOUND"])

    def test_a_member_requested_through_a_bundle_the_source_renames_still_needs_its_tools(self) -> None:
        self.make_source_json(bundles={"pack": {"members": ["reviewer", "formatter"], "opt_in": True}})
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        self.make_source_json(bundles={"pack2": {"members": ["reviewer", "formatter"], "opt_in": True}})
        groups = self.report_groups(self.check(machine).output, "CHECK")
        self.assertEqual(["dotnet-format (used by pack2)"], groups["MISSING"])

    def test_a_member_requested_before_bundle_members_were_recorded_still_needs_its_tools(self) -> None:
        self.make_source_json(bundles={"pack": {"members": ["reviewer", "formatter"], "opt_in": True}})
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        data = self.manifest()
        del data["sources"]["test/skills"]["requested_bundle_members"]
        self.write_manifest(data)
        self.make_source_json(bundles={"pack2": {"members": ["reviewer", "formatter"], "opt_in": True}})
        groups = self.report_groups(self.check(machine).output, "CHECK")
        self.assertEqual(["dotnet-format (used by pack2)"], groups["MISSING"])

    def test_a_member_requested_in_a_version_7_manifest_still_needs_its_tools(self) -> None:
        self.make_source_json(bundles={"pack": {"members": ["reviewer", "formatter"], "opt_in": True}})
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        self.make_source_json(bundles={"pack2": {"members": ["reviewer", "formatter"], "opt_in": True}})
        for requested in ([], ["pack"]):
            with self.subTest(requested=requested):
                self.rewrite_as_version_7(requested)
                groups = self.report_groups(self.check(machine).output, "CHECK")
                self.assertEqual(["dotnet-format (used by pack2)"], groups["MISSING"])

    def test_a_member_requested_beside_a_skill_preserved_through_the_version_8_upgrade_still_needs_its_tools(
        self,
    ) -> None:
        self.make_source_json(bundles={"pack": {"members": ["reviewer", "formatter"], "opt_in": True}})
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        self.make_source_json(bundles={"pack2": {"members": ["reviewer", "formatter"], "opt_in": True}})
        # With reporter, the deployment that preserved plain also installed a skill the version 7 manifest did not own.
        for installed_since in ((), ("reporter",)):
            with self.subTest(installed_since=installed_since):
                self.rewrite_as_preserved_through_the_upgrade("plain", installed_since=installed_since)
                groups = self.report_groups(self.check(machine).output, "CHECK")
                self.assertEqual(["dotnet-format (used by pack2)"], groups["MISSING"])

    def test_a_member_requested_beside_a_preserved_skill_whose_list_a_new_skills_list_contains_still_needs_its_tools(
        self,
    ) -> None:
        # newer's exact list names plain, every skill plain's filled-in list names, and more, but the source no longer
        # has newer depend on pack2's members; as for --all, they stand for the renamed request.
        self.make_source_json(bundles={"pack": {"members": ["formatter", "reporter"], "opt_in": True}})
        self.make_skill("helper", "Helper")
        self.make_skill("newer", "Newer", skill_deps=["core", "formatter", "helper", "plain", "reporter", "reviewer"])
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        self.rewrite_as_preserved_through_the_upgrade("plain", installed_since=("helper", "newer"))
        self.make_source_json(bundles={"pack2": {"members": ["formatter", "reporter"], "opt_in": True}})
        self.make_skill("newer", "Newer", skill_deps=["helper"])
        groups = self.report_groups(self.check(machine).output, "CHECK")
        self.assertEqual(["dotnet-format (used by pack2)"], groups["MISSING"])

    def test_a_member_installed_only_as_a_dependency_beside_requests_reaching_around_it_leaves_its_tools_optional(
        self,
    ) -> None:
        # r depends on helper, as the entry records and the source declares; as for --all, helper stays a dependency,
        # so hb stays unchosen and the formatter's tool optional.
        self.make_source_json(
            bundles={
                "hb": {"members": ["helper", "formatter"], "opt_in": True},
                "pack": {"members": ["p1"], "opt_in": True},
            }
        )
        self.make_skill("r", "R", skill_deps=["helper"])
        self.make_skill("s", "S", skill_deps=["p1"])
        self.make_skill("helper", "Helper")
        self.make_skill("p1", "P1")
        self.make_config()
        machine = Machine(missing=("dotnet-format",))
        with machine.patches():
            self.deploy_ok("--all", "--include", "pack")
        data = self.manifest()
        del data["sources"]["test/skills"]["requested_bundle_members"]
        self.write_manifest(data)
        groups = self.report_groups(self.check(machine).output, "CHECK")
        self.assertIn("dotnet-format (not installed; used by opt-in hb)", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)

    def test_the_runtimes_verify_lists_are_reported_whether_or_not_a_skill_declares_them(self) -> None:
        # deploy.py verify runs Codex CLI and Copilot CLI, so check reports both even when no skill runs either.
        set_tools(self, "core", ["gh"])
        groups = self.report_groups(self.check(Machine(missing=("claude",))).output, "CHECK")
        self.assertEqual(
            [
                "Claude Code (not installed; used by every skill run in Claude Code)",
                "codex 0.160.0 (used by Codex verification)",
                "copilot 1.0.89 (used by Copilot verification)",
            ],
            groups["OPTIONAL"],
        )
        groups = self.report_groups(self.check(Machine(missing=("claude", "codex", "copilot"))).output, "CHECK")
        self.assertEqual(
            [
                "Claude Code (not installed; used by every skill run in Claude Code)",
                "codex (not installed; used by Codex verification)",
                "copilot (not installed; used by Copilot verification)",
            ],
            groups["OPTIONAL"],
        )
        self.assertNotIn("MISSING", groups)

    def test_codex_before_0_88_is_outdated_because_verify_reads_whether_a_skill_is_enabled(self) -> None:
        # Codex CLI 0.88.0 is the first whose app-server skills/list answer says whether each skill is enabled.
        result = self.check(Machine(versions={"codex": "codex-cli 0.87.0\n"}))
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            ["codex 0.87.0 (needs 0.88.0 or newer)"], self.report_groups(result.output, "CHECK")["OUTDATED"]
        )
        groups = self.report_groups(self.check(Machine(versions={"codex": "codex-cli 0.88.0\n"})).output, "CHECK")
        self.assertIn("codex 0.88.0 (used by Codex verification)", groups["OPTIONAL"])
        self.assertNotIn("OUTDATED", groups)

    def test_a_tool_every_user_declares_optional_is_optional(self) -> None:
        # dotnet-format reads a pull request's base through gh only when gh is there, so it declares gh optional.
        set_tools(self, "core", ["copilot"])
        set_tools(self, "reporter", [])
        set_tools(self, "reporter", ["gh"], key="optional_tools")
        set_tools(self, "formatter", ["gh"], key="optional_tools")
        result = self.check(Machine(missing=("gh",)))
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertIn("gh (not installed; used by formatter, reporter)", groups["OPTIONAL"])
        self.assertNotIn("MISSING", groups)
        groups = self.report_groups(self.check(Machine()).output, "CHECK")
        self.assertIn("gh 2.97.0 (used by formatter, reporter)", groups["OPTIONAL"])
        set_tools(self, "reporter", ["gh"])
        set_tools(self, "reporter", [], key="optional_tools")
        groups = self.report_groups(self.check(Machine(missing=("gh",))).output, "CHECK")
        self.assertEqual(["gh (used by formatter, reporter)"], groups["MISSING"])

    def test_git_and_claude_code_versions_are_reported_for_a_bug_report(self) -> None:
        # The bug template asks for check's output alone, so check names the Git and Claude Code versions too, though
        # deploying needs neither. Without Git the skills fail, but deploying still works.
        groups = self.report_groups(self.check(Machine()).output, "CHECK")
        self.assertIn("Git 2.54.0 (used by every skill)", groups["FOUND"])
        self.assertIn("Claude Code 2.1.300 (used by every skill run in Claude Code)", groups["OPTIONAL"])
        result = self.check(Machine(missing=("git", "claude")))
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["Git (used by every skill)"], groups["MISSING"])
        self.assertIn("Claude Code (not installed; used by every skill run in Claude Code)", groups["OPTIONAL"])
        self.assertIn("Ready to deploy. Skills cannot use a missing, outdated, or unusable tool\n", result.output)

    def test_claude_code_before_2_1_295_is_outdated_because_the_reviewer_guard_must_block_when_it_cannot_run(
        self,
    ) -> None:
        # Claude Code 2.1.295 is the first that honors a hook's onFailure: block, which the code-review reviewer's
        # guard declares; an older one lets a call through when the guard cannot start or times out.
        result = self.check(Machine(versions={"claude": "2.1.294 (Claude Code)\n"}))
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["Claude Code 2.1.294 (needs 2.1.295 or newer)"], groups["OUTDATED"])
        self.assertNotIn("Claude Code 2.1.294 (used by every skill run in Claude Code)", groups["OPTIONAL"])
        self.assertIn("Ready to deploy. Skills cannot use a missing, outdated, or unusable tool\n", result.output)
        groups = self.report_groups(self.check(Machine(versions={"claude": "2.1.295 (Claude Code)\n"})).output, "CHECK")
        self.assertIn("Claude Code 2.1.295 (used by every skill run in Claude Code)", groups["OPTIONAL"])
        self.assertNotIn("OUTDATED", groups)

    def test_tools_no_skill_declares_are_not_listed(self) -> None:
        set_tools(self, "formatter", [])
        output = self.check(Machine(missing=("dotnet-format",))).output
        self.assertNotIn("dotnet-format", output)

    def record_commit(self, commit: str = COMMIT) -> None:
        self.make_config()
        with Machine().patches():
            self.deploy_ok("--all")
        data = self.manifest()
        data["sources"][SOURCE_ID]["source_commit"] = commit
        self.write_manifest(data)
        (self.source / ".git").mkdir()

    def deployed_line(self, output: str) -> str:
        return next(line for line in output.splitlines() if line.startswith("Deployed commit:"))

    def test_the_deployed_commit_is_described_from_the_source_checkout(self) -> None:
        self.record_commit()
        machine = Machine(describe="v0.1.0-14-g71ad815")
        result = self.check(machine)
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(f"Deployed commit: v0.1.0-14-g71ad815 ({COMMIT})", self.deployed_line(result.output))
        self.assertEqual([[GIT, "-C", str(self.source), "describe", "--tags", "--always", COMMIT]], machine.git_calls)

    def test_the_deployed_commit_is_the_full_hash_when_git_cannot_describe_it(self) -> None:
        self.record_commit()
        self.assertEqual(f"Deployed commit: {COMMIT}", self.deployed_line(self.check(Machine()).output))
        self.assertEqual(f"Deployed commit: {COMMIT}", self.deployed_line(self.check(Machine(missing=("git",))).output))

    def test_the_deployed_commit_is_the_full_hash_when_the_source_is_no_git_checkout(self) -> None:
        self.record_commit()
        (self.source / ".git").rmdir()
        machine = Machine(describe="v0.1.0")
        self.assertEqual(f"Deployed commit: {COMMIT}", self.deployed_line(self.check(machine).output))
        self.assertEqual([], machine.git_calls)

    def test_the_deployed_commit_is_unknown_when_none_is_recorded(self) -> None:
        self.make_config()
        with Machine().patches():
            self.deploy_ok("--all")
        machine = Machine(describe="v0.1.0")
        self.assertEqual("Deployed commit: unknown", self.deployed_line(self.check(machine).output))
        self.assertEqual([], machine.git_calls)

    def test_check_takes_no_arguments_besides_help(self) -> None:
        result = self.check(Machine(), "--all")
        self.assertEqual(2, result.code)
        self.assertIn(
            f"ERROR: unrecognized arguments: --all\nRun '{DEPLOY_COMMAND} check --help' for usage.", result.output
        )
        result = self.check(Machine(), "--help")
        self.assertEqual(0, result.code)
        self.assertIn("List the tools the deployer and the skills need, with their versions,\n", result.output)


class DeployWarningTests(DeployerTestCase):
    def test_deploying_warns_about_missing_tools_of_selected_skills(self) -> None:
        self.make_source_json()
        self.make_skill("reporter", "Reporter")
        self.make_skill("plain", "Plain")
        set_tools(self, "reporter", ["gh", "copilot"])
        self.make_config()
        with Machine(missing=("gh", "copilot")).patches():
            output = self.deploy_ok("--all", "--dry-run").output
            self.assertIn(
                "\nWARNING: Selected skills use tools that are not installed:\n"
                "  gh (used by reporter)\n"
                "Those skills will fail until the tools are installed.\n"
                f"Run '{DEPLOY_COMMAND} check' for details.\n",
                output,
            )
            self.assertNotIn("copilot", output)
            menu_selection = self.selection_number("plain")
            self.assertNotIn("WARNING", self.deploy_ok("--dry-run", stdin=f"{menu_selection}\n").output)
        with Machine().patches():
            self.assertNotIn("WARNING", self.deploy_ok("--all", "--dry-run").output)

    def test_deploying_does_not_warn_about_a_missing_optional_tool(self) -> None:
        self.make_source_json()
        self.make_skill("reporter", "Reporter")
        set_tools(self, "reporter", ["gh"], key="optional_tools")
        self.make_config()
        with Machine(missing=("gh",)).patches():
            self.assertNotIn("WARNING", self.deploy_ok("--all", "--dry-run").output)


class InstallationGuideTests(unittest.TestCase):
    def test_the_section_install_messages_name_exists(self) -> None:
        guide = (REPOSITORY_ROOT / "docs" / "installation.md").read_text(encoding="utf-8")
        self.assertIn("Installing the tools", re.findall(r"^## (.+)$", guide, re.MULTILINE))

    def test_the_sections_the_setting_lines_name_exist(self) -> None:
        guide = (REPOSITORY_ROOT / "docs" / "codex-support.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (.+)$", guide, re.MULTILINE)
        for section in ("Windows sandbox mode", "Python app execution aliases", "PowerShell app execution alias"):
            with self.subTest(section=section):
                self.assertIn(section, headings)


class StandardCommandTests(unittest.TestCase):
    def test_standard_commands_are_the_documented_set(self) -> None:
        # "Commands skills may run" in docs/adding-a-skill.md lists these; the platform's part comes from
        # platform_support: Python as python, Windows PowerShell, and cygpath on Windows, and python3 elsewhere.
        platform = {"python", "powershell", "cygpath"} if platform_support.WINDOWS else {"python3"}
        self.assertEqual(
            frozenset(
                {
                    *platform,
                    "git",
                    "bash",
                    "sh",
                    "pwsh",
                    "awk",
                    "basename",
                    "cat",
                    "cmp",
                    "comm",
                    "cp",
                    "curl",
                    "cut",
                    "date",
                    "diff",
                    "dirname",
                    "env",
                    "expr",
                    "find",
                    "grep",
                    "gzip",
                    "head",
                    "ls",
                    "mkdir",
                    "mktemp",
                    "mv",
                    "od",
                    "paste",
                    "readlink",
                    "realpath",
                    "rm",
                    "rmdir",
                    "sed",
                    "seq",
                    "sleep",
                    "sort",
                    "stat",
                    "tail",
                    "tar",
                    "tee",
                    "touch",
                    "tr",
                    "uniq",
                    "wc",
                    "xargs",
                }
            ),
            tools.STANDARD_COMMANDS,
        )


class CodexSandboxProbeTests(unittest.TestCase):
    """The sandbox mode Codex CLI would use, read from the documented probe that starts no model."""

    def mode(self, result: platform_support.ToolResult | Exception) -> tuple[str | None, list[list[str]]]:
        calls: list[list[str]] = []

        def run(arguments: list[str], environment: dict[str, str] | None = None) -> platform_support.ToolResult:
            calls.append(arguments)
            if isinstance(result, Exception):
                raise result
            return result

        with mock.patch.object(platform_support, "run_tool", side_effect=run):
            return tools.codex_sandbox_mode("C:/tools/codex.exe"), calls

    def test_the_probe_runs_the_documented_command_and_reads_the_mode(self) -> None:
        for mode in ("workspace-write", "read-only", "danger-full-access"):
            with self.subTest(mode=mode):
                found, calls = self.mode(platform_support.ToolResult(0, prompt_input(mode)))
                self.assertEqual(mode, found)
                self.assertEqual(
                    [["C:/tools/codex.exe", "debug", "prompt-input", "-c", "sandbox_mode=workspace-write"]], calls
                )

    def test_a_failed_or_silent_probe_names_no_mode(self) -> None:
        for result in (
            platform_support.ToolResult(1, prompt_input("workspace-write")),
            platform_support.ToolResult(0, "error: unknown subcommand 'debug'\n"),
        ):
            with self.subTest(result=result):
                self.assertIsNone(self.mode(result)[0])

    def test_an_unfinished_probe_stops_with_the_deadline_rather_than_naming_no_mode(self) -> None:
        timed_out = platform_support.ToolTimedOut("C:/tools/codex.exe", 600)
        with self.assertRaises(platform_support.ToolTimedOut) as raised:
            self.mode(timed_out)
        self.assertIs(timed_out, raised.exception)


class VersionTests(unittest.TestCase):
    def test_verify_runtimes_are_catalogued_with_their_floors(self) -> None:
        from deployer import discovery

        self.assertEqual({"codex", "copilot"}, set(discovery.RUNTIMES))
        self.assertEqual(set(discovery.RUNTIMES), set(tools.VERIFY_TOOLS))
        self.assertEqual(
            {"codex": (0, 88, 0), "copilot": (1, 0, 88)},
            {name: tool.minimum for name, tool in tools.VERIFY_TOOLS.items()},
        )
        self.assertIs(tools.SKILL_TOOLS["copilot"], tools.VERIFY_TOOLS["copilot"])
        self.assertNotIn("codex", tools.SKILL_TOOLS, "no skill runs Codex CLI")

    def test_the_runtime_tool_floors_table_lists_every_floor_the_catalogue_checks(self) -> None:
        # docs/dependency-updates.md owns every floor, so its table and the catalogue both name these.
        floors = {"gh": "2.48.0", "codex": "0.88.0", "copilot": "1.0.88", "dotnet": "9.0.200", "claude": "2.1.295"}
        reported = {tool.name: tool for tool in tools.REPORTED_TOOLS}
        catalogued = {
            name: ".".join(map(str, tool.minimum))
            for name, tool in {**tools.SKILL_TOOLS, **tools.VERIFY_TOOLS, **reported}.items()
            if tool.minimum
        }
        self.assertEqual(floors, catalogued)
        text = (REPOSITORY_ROOT / "docs" / "dependency-updates.md").read_text(encoding="utf-8")
        section = text.split("\n## Runtime tool floors\n", 1)[1].split("\n## ", 1)[0]
        self.assertEqual(floors, dict(re.findall(r"(?m)^\| `([a-z-]+)` \| ([0-9.]+) \|", section)))
        self.assertIs(MINIMUMS, tools.SKILL_TOOLS["pdftotext"].provider_minimum)
        self.assertEqual({"Xpdf": (4, 6), "Poppler": (26, 10, 0)}, MINIMUMS)
        # Each provider's floor as the vendor numbers it.
        for row in ("| `pdftotext` (Xpdf) | 4.06 |", "| `pdftotext` (Poppler) | 26.10.0 |"):
            self.assertIn(row, section)

    def test_the_pdftotext_entry_sets_nothing_the_probe_does_not_read(self) -> None:
        # pdf_tool owns the probe's arguments and each provider's exit statuses, so the entry names only where to
        # find the tool, its floors, and what pdf_tool's refusals tell the user to install; every other field is the
        # default.
        entry = tools.SKILL_TOOLS["pdftotext"]
        default = tools.Tool("pdftotext", "pdftotext", entry.locate)
        changed = {
            field.name
            for field in dataclasses.fields(tools.Tool)
            if getattr(entry, field.name) != getattr(default, field.name)
        }
        self.assertEqual({"provider_minimum", "install_hint"}, changed)
        self.assertEqual(INSTALL, entry.install_hint)
        self.assertNotIn("version_statuses", {field.name for field in dataclasses.fields(tools.Tool)})
        with (
            mock.patch("deployer.platform_support.find_executables", return_value=["C:/tools/pdftotext.exe"]),
            mock.patch("deployer.platform_support.run_tool") as run_tool,
            mock.patch("deployer.tools.probe_pdftotext", side_effect=lambda path: parse_banner(XPDF_4_06, 99)),
        ):
            probe = tools.probe(entry)
        run_tool.assert_not_called()
        resolved = str(Path("C:/tools/pdftotext.exe").resolve())
        self.assertEqual((resolved, (4, 6), None, ()), (probe.path, probe.version, probe.refusal, probe.skipped))

    def test_parse_version_reads_the_first_dotted_number(self) -> None:
        for output, expected in (
            ("gh version 2.97.0 (2026-07-31)", (2, 97, 0)),
            ("codex-cli 0.160.0", (0, 160, 0)),
            ("ShellCheck - shell script analysis tool\nversion: 0.9.0\nlicense: GNU", (0, 9, 0)),
            ("GitHub Copilot CLI 1.0.89.\nRun 'copilot update'", (1, 0, 89)),
            ("7.6", (7, 6)),
            ("no version here", None),
        ):
            with self.subTest(output=output):
                self.assertEqual(expected, tools.parse_version(output))

    def test_minimum_python_is_3_11_4_in_both_places(self) -> None:
        # 3.11.4 is the first 3.11 release whose tarfile has extraction filters, which --at extracts with.
        specification = importlib.util.spec_from_file_location("deploy_entry", REPOSITORY_ROOT / "deploy.py")
        if specification is None or specification.loader is None:
            self.fail("deploy.py loads as a module")
        entry = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(entry)
        self.assertEqual((3, 11, 4), entry.MINIMUM_PYTHON)
        self.assertEqual((3, 11, 4), tools.MINIMUM_PYTHON)
        for version, shown in (((3, 10, 9), "3.10.9"), ((3, 11, 3), "3.11.3")):
            captured = io.StringIO()
            with (
                self.subTest(version=version),
                contextlib.redirect_stderr(captured),
                self.assertRaises(SystemExit) as raised,
            ):
                entry.require_supported_python(version)
            self.assertEqual(1, raised.exception.code)
            self.assertEqual(
                f"\nERROR: Python 3.11.4 or newer is required; this is Python {shown}.\n"
                'See "Installing the tools" in docs/installation.md.\n\n',
                captured.getvalue(),
            )
        entry.require_supported_python((3, 11, 4))
        entry.require_supported_python(sys.version_info)

    def test_deploy_py_and_its_console_setup_parse_as_python_3_7(self) -> None:
        # deploy.py imports ejryan89-skill-core's console module, and the module imports flat_text, before its version
        # check, so an older Python reaches the check only while these files keep to a grammar it reads.
        for path in (
            REPOSITORY_ROOT / "deploy.py",
            REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts" / "console.py",
            REPOSITORY_ROOT / "skills" / "ejryan89-skill-core" / "scripts" / "flat_text.py",
        ):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 7))
        with self.assertRaises(SyntaxError):
            ast.parse("if (found := 1):\n    pass\n", feature_version=(3, 7))


class LocatorFixture(unittest.TestCase):
    """A sanitized fixture: PATH is one empty folder, and the local application data folder is a fresh one with an
    empty WinGet Links folder, so the only programs `check` and a skill find are the ones a test installs. Off
    Windows, where no package manager links commands into a folder of its own, `links` is the folder PATH names
    after the first, the place the locator looks once the first has no such program."""

    def setUp(self) -> None:
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="shared locator ")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.path_folder = root / "on path"
        self.path_folder.mkdir()
        if platform_support.WINDOWS:
            self.links = root / "local app data" / "Microsoft" / "WinGet" / "Links"
            variables = {
                "PATH": str(self.path_folder),
                "PATHEXT": ".com;.exe",
                "LOCALAPPDATA": str(root / "local app data"),
            }
        else:
            self.links = root / "later on path"
            variables = {"PATH": f"{self.path_folder}{os.pathsep}{self.links}"}
        self.links.mkdir(parents=True)
        environment = mock.patch.dict(os.environ, variables)
        environment.start()
        self.addCleanup(environment.stop)

    def install(self, folder: Path, name: str) -> Path:
        """A program as this platform's package manager installs it: `<name>.exe` on Windows, and elsewhere an
        executable file named `name`."""
        if platform_support.WINDOWS:
            program = folder / f"{name}.exe"
            program.write_bytes(b"")
            return program
        program = folder / name
        program.write_bytes(b"")
        program.chmod(0o755)
        return program


class SharedLocatorTests(LocatorFixture):
    """`check` and the PDF extraction choose pdftotext alike, among every one the locator finds, so on one fixture
    they report the same parser, skip, refusal, or absence. A portable WinGet install leaves a parser in the Links
    folder; Git Bash puts Git's bundled Xpdf 4.0 on PATH ahead of the Windows PATH, as the fixture's PATH folder
    does."""

    def setUp(self) -> None:
        super().setUp()
        self.banners: dict[Path, str] = {}
        self.probed: list[str] = []
        self.started: list[list[str]] = []

    def parser(self, folder: Path, banner: str) -> Path:
        program = self.install(folder, "pdftotext")
        self.banners[program.resolve()] = banner
        return program

    def probe(self, path: str) -> PdfIdentity:
        self.probed.append(path)
        banner = self.banners[Path(path)]
        return parse_banner(banner, 0 if "The Poppler Developers" in banner else 99)

    def start(self, command: list[str], **_: object) -> None:
        self.started.append(command)
        raise OSError("the fixture's pdftotext is no program")

    def check_and_extract(self) -> tuple[tuple[str, str, str], str]:
        """check's pdftotext line, and why the extraction stopped: the fixture's program cannot start, so a
        supported one is refused only once the extraction runs it."""
        with (
            mock.patch("deployer.tools.probe_pdftotext", side_effect=self.probe),
            mock.patch.object(review_documents, "probe_pdftotext", side_effect=self.probe),
            mock.patch.object(review_documents, "streaming", side_effect=self.start),
        ):
            line = _tool_line(tools.SKILL_TOOLS["pdftotext"], True, ["operations"])
            with self.assertRaises(review_documents.ExtractionRefused) as refused:
                review_documents.extract("pdf", b"%PDF-1.4 fixture")
        return (line.name, line.action, line.detail), str(refused.exception)

    def test_a_supported_pdftotext_only_in_wingets_links_folder_is_found_by_both_and_run_at_the_checked_path(
        self,
    ) -> None:
        program = self.parser(self.links, XPDF_4_06)
        line, refusal = self.check_and_extract()
        self.assertEqual(("pdftotext Xpdf 4.06", "OPTIONAL", "used by operations"), line)
        self.assertEqual("pdftotext could not be started", refusal)
        self.assertEqual([program.resolve()] * 2, [Path(path).resolve() for path in self.probed])
        self.assertEqual([str(program.resolve())], [command[0] for command in self.started])

    def test_an_unsupported_pdftotext_only_there_gets_the_same_refusal_from_both(self) -> None:
        program = self.parser(self.links, XPDF_4_00)
        line, refusal = self.check_and_extract()
        unsupported = f"no supported pdftotext: skipped {program} (Xpdf 4.00, older than Xpdf 4.06); {INSTALL}"
        self.assertEqual(("pdftotext Xpdf 4.00", "OUTDATED", unsupported), line)
        self.assertEqual(unsupported, refusal)
        self.assertEqual(2, len(self.probed))
        self.assertEqual([], self.started)

    def test_a_supported_pdftotext_behind_gits_bundled_one_is_chosen_by_both_and_the_skip_reported(self) -> None:
        bundled = self.parser(self.path_folder, XPDF_4_00)
        supported = self.parser(self.links, XPDF_4_06)
        line, refusal = self.check_and_extract()
        self.assertEqual(
            (
                "pdftotext Xpdf 4.06",
                "OPTIONAL",
                f"used by operations; skipped {bundled} (Xpdf 4.00, older than Xpdf 4.06)",
            ),
            line,
        )
        self.assertEqual("pdftotext could not be started", refusal)
        self.assertEqual([bundled, supported] * 2, [Path(path) for path in self.probed])
        self.assertEqual([str(supported)], [command[0] for command in self.started])

    def test_only_unsupported_pdftotexts_get_the_same_refusal_from_both_naming_each(self) -> None:
        bundled = self.parser(self.path_folder, XPDF_4_00)
        linked = self.parser(self.links, "pdftotext version 0.86.1\nCopyright 2005-2020 The Poppler Developers\n")
        line, refusal = self.check_and_extract()
        unsupported = (
            f"no supported pdftotext: skipped {bundled} (Xpdf 4.00, older than Xpdf 4.06), {linked} (Poppler 0.86.1, "
            f"older than Poppler 26.10.0); {INSTALL}"
        )
        self.assertEqual(("pdftotext Xpdf 4.00", "OUTDATED", unsupported), line)
        self.assertEqual(unsupported, refusal)
        self.assertEqual([], self.started)

    def test_with_no_pdftotext_in_either_both_say_it_is_not_installed_and_name_the_floors(self) -> None:
        line, refusal = self.check_and_extract()
        self.assertEqual(("pdftotext", "OPTIONAL", f"not installed; used by operations; {INSTALL}"), line)
        self.assertEqual(f"pdftotext, which extracts a PDF's text, is not installed; {INSTALL}", refusal)
        self.assertEqual(([], []), (self.probed, self.started))


class SkillToolLocatorTests(LocatorFixture):
    """Each tool a skill runs by name is the one `check` reports, found by the same locator on the same fixture and
    started by that absolute path: only in WinGet's Links folder, and on PATH, which comes first."""

    def started(self, module: Any, run: Callable[[], object]) -> str:
        """The program `run` starts through `module`'s bounded runner, which the fixture replaces."""
        finished = bounded_process.Finished(0, b"", b"")
        with mock.patch.object(module, "run_bounded", return_value=finished) as bounded:
            run()
        return str(bounded.call_args.args[0][0])

    def skill_program(self, name: str) -> str | None:
        """The program the skill that runs `name` finds, and, where it starts one, the one it starts."""
        if name == "gh":
            return self.started(github_client, lambda: github_client.subprocess_runner(["gh", "--version"]))
        if name in ("dotnet", "dotnet-format"):
            found = run_dotnet_format.Services().which(name)
            started = self.started(
                run_dotnet_format, lambda: run_dotnet_format.subprocess_runner([name, "--version"], Path.cwd(), 60)
            )
            self.assertEqual(found, started)
            return found
        if name == "copilot":
            return review_hosts.find_copilot()
        return None

    def test_a_tool_only_in_wingets_links_folder_is_the_one_check_reports_and_the_skill_runs(self) -> None:
        for name in ("gh", "dotnet", "dotnet-format", "copilot"):
            with self.subTest(name=name):
                program = self.install(self.links, name)
                self.assertEqual(str(program), {**tools.SKILL_TOOLS, **tools.VERIFY_TOOLS}[name].locate())
                self.assertEqual(str(program), self.skill_program(name))
                on_path = self.install(self.path_folder, name)
                self.assertEqual(str(on_path), {**tools.SKILL_TOOLS, **tools.VERIFY_TOOLS}[name].locate())
                self.assertEqual(str(on_path), self.skill_program(name), "PATH comes first")

    def test_a_runtime_cli_check_reports_is_one_the_code_review_skills_detect(self) -> None:
        catalogue = {tool.name: tool for tool in (*tools.VERIFY_TOOLS.values(), *tools.REPORTED_TOOLS)}
        for name, runtime in (("copilot", "copilot-cli"), ("codex", "codex"), ("claude", "claude-code")):
            with self.subTest(name=name):
                self.assertIsNone(catalogue[name].locate())
                program = self.install(self.links, name)
                self.assertEqual(str(program), catalogue[name].locate())
                self.assertEqual(runtime, review_runtime.resolve_runtime("auto"))
