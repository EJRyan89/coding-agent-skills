from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import unittest
from unittest import mock

from harness import REPOSITORY_ROOT, SOURCE_ID, DeployerTestCase, Result, forward

from deployer import cli, platform_support, tools

BUNDLE = {"operations": {"members": ["reviewer"]}}
INSTALLED = {
    "shellcheck": ("C:/tools/shellcheck.exe", "ShellCheck - shell script analysis tool\nversion: 0.11.0\n"),
    "gh": ("C:/tools/gh.exe", "gh version 2.97.0 (2026-07-31)\n"),
    "copilot": ("C:/tools/copilot.exe", "GitHub Copilot CLI 1.0.89.\n"),
    "codex": ("C:/tools/codex.exe", "codex-cli 0.160.0\n"),
    "dotnet-format": ("C:/tools/dotnet-format.exe", "5.1.250801+4a851ea9\n"),
    "claude": ("C:/tools/claude.exe", "2.1.291 (Claude Code)\n"),
    "dotnet": ("C:/tools/dotnet.exe", "10.0.301\n"),
}
GIT = "C:/tools/git.exe"
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
    ) -> None:
        # describe is what git describe prints for the deployed commit; None makes it fail, as for an unknown commit.
        self.git = None if "git" in missing else GIT
        self.describe = describe
        self.git_calls: list[list[str]] = []
        self.installed = {name: entry for name, entry in INSTALLED.items() if name not in missing}
        self.versions = {path: output for path, output in self.installed.values()}
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
                side_effect=lambda name: self.git if name == "git" else self.installed.get(name, (None,))[0],
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
        stack.enter_context(mock.patch("deployer.tools.python_version", return_value=(3, 12, 1)))
        return stack

    def run_tool(self, arguments: list[str]) -> platform_support.ToolResult:
        if arguments[0] != GIT:
            return platform_support.ToolResult(0, self.versions[arguments[0]])
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
        captured = io.StringIO()
        with machine.patches(), contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = cli.main(["check", *arguments], self.paths)
        return Result(code, captured.getvalue())

    def test_everything_installed_is_ready_to_deploy(self) -> None:
        result = self.check(Machine())
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(
            "\n"
            "Source: Test Skills (test/skills)\n"
            "Deployed commit: unknown\n"
            f"Home: {forward(self.home)}\n"
            "\n"
            "=== CHECK ===\n"
            "\n"
            "FOUND (7):\n"
            "  Git 2.54.0 (used by every skill)\n"
            "  Git Bash (needed to deploy; C:/Program Files/Git/bin/bash.exe)\n"
            "  PowerShell 7.6.6 (needed to deploy)\n"
            "  Python 3.12.1 (needed to deploy; 3.11 or newer)\n"
            "  ShellCheck 0.11.0 (needed to deploy)\n"
            "  dotnet-format 5.1.250801 (used by formatter)\n"
            "  gh 2.97.0 (used by operations, reporter)\n"
            "\n"
            "OPTIONAL (3):\n"
            "  Claude Code 2.1.291 (used by every skill run in Claude Code)\n"
            "  codex 0.160.0 (used by Codex verification)\n"
            "  copilot 1.0.89 (used by operations, Copilot verification)\n"
            "\n"
            "Ready to deploy.\n"
            "\n",
            result.output,
        )

    def test_missing_deploy_tool_fails_with_its_install_command(self) -> None:
        result = self.check(Machine(missing=("shellcheck",)))
        self.assertEqual(1, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["ShellCheck (needed to deploy; winget install --id koalaman.shellcheck)"], groups["MISSING"])
        self.assertTrue(
            result.output.endswith(
                'Install the missing tools before deploying; see "Installing the tools" in\n'
                "docs/installation.md. Open a new terminal afterwards so the tools are on PATH.\n\n"
            ),
            result.output,
        )

    def test_missing_skill_tool_still_allows_deploying(self) -> None:
        result = self.check(Machine(missing=("gh",)))
        self.assertEqual(0, result.code, result.output)
        self.assertEqual(["gh (used by operations, reporter)"], self.report_groups(result.output, "CHECK")["MISSING"])
        self.assertIn("Ready to deploy. Skills that use a missing or outdated tool will fail\n", result.output)

    def test_optional_and_outdated_tools_are_reported_separately(self) -> None:
        groups = self.report_groups(self.check(Machine(missing=("copilot",))).output, "CHECK")
        self.assertEqual(
            [
                "Claude Code 2.1.291 (used by every skill run in Claude Code)",
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
        self.assertNotIn("OUTDATED", groups)

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
        self.assertIn("Claude Code 2.1.291 (used by every skill run in Claude Code)", groups["OPTIONAL"])
        result = self.check(Machine(missing=("git", "claude")))
        self.assertEqual(0, result.code, result.output)
        groups = self.report_groups(result.output, "CHECK")
        self.assertEqual(["Git (used by every skill)"], groups["MISSING"])
        self.assertIn("Claude Code (not installed; used by every skill run in Claude Code)", groups["OPTIONAL"])
        self.assertIn("Ready to deploy. Skills that use a missing or outdated tool will fail\n", result.output)

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
            "ERROR: unrecognized arguments: --all\nRun 'python deploy.py check --help' for usage.", result.output
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
                "Run 'python deploy.py check' for details.\n",
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


class StandardCommandTests(unittest.TestCase):
    def test_standard_commands_are_the_documented_set(self) -> None:
        # "Commands skills may run" in docs/adding-a-skill.md lists these; the Windows part comes from platform_support.
        self.assertEqual(
            frozenset(
                {
                    "python",
                    "git",
                    "bash",
                    "sh",
                    "pwsh",
                    "powershell",
                    "awk",
                    "basename",
                    "cat",
                    "cmp",
                    "comm",
                    "cp",
                    "curl",
                    "cut",
                    "cygpath",
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

    def test_minimum_python_is_3_11_in_both_places(self) -> None:
        specification = importlib.util.spec_from_file_location("deploy_entry", REPOSITORY_ROOT / "deploy.py")
        if specification is None or specification.loader is None:
            self.fail("deploy.py loads as a module")
        entry = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(entry)
        self.assertEqual((3, 11), entry.MINIMUM_PYTHON)
        self.assertEqual((3, 11), tools.MINIMUM_PYTHON)
        captured = io.StringIO()
        with contextlib.redirect_stderr(captured), self.assertRaises(SystemExit) as raised:
            entry.require_supported_python((3, 10, 9))
        self.assertEqual(1, raised.exception.code)
        self.assertEqual(
            "\nERROR: Python 3.11 or newer is required; this is Python 3.10.\n"
            'See "Installing the tools" in docs/installation.md.\n\n',
            captured.getvalue(),
        )
        entry.require_supported_python((3, 11, 0))
        entry.require_supported_python(sys.version_info)


if __name__ == "__main__":
    unittest.main()
