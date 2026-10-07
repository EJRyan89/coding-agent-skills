"""The validation prerequisites: where each tool is found, its version, and its floor."""

from __future__ import annotations

import re
import shutil
import sys
import sysconfig
import unittest
from collections.abc import Callable
from pathlib import Path

from validation_support import REPOSITORY_ROOT

from deployer import platform_support, tools


def find_git_bash() -> str:
    found = platform_support.find_bash()
    if found:
        return found
    raise AssertionError(
        "Git Bash was not found. Install Git for Windows or set GIT_BASH; "
        "Git Bash is required for repository validation."
    )


def find_shellcheck() -> str | None:
    return platform_support.find_executable("shellcheck")


# PSScriptAnalyzer is a PowerShell module, so it is found through pwsh and versioned from its module manifest.
NEWEST_SCRIPT_ANALYZER = (
    "Get-Module -ListAvailable PSScriptAnalyzer | Sort-Object Version -Descending | "
    "Select-Object -First 1 -ExpandProperty Path"
)
MODULE_VERSION = re.compile(r"^\s*ModuleVersion\s*=\s*['\"]([0-9.]+)['\"]", re.MULTILINE | re.IGNORECASE)


def find_psscriptanalyzer() -> str | None:
    """The manifest of the newest PSScriptAnalyzer pwsh can load, or None when pwsh lists none."""
    result = platform_support.run_tool(
        [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", NEWEST_SCRIPT_ANALYZER]
    )
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    found = lines[-1] if result.returncode == 0 and lines else ""
    return found if found.casefold().endswith(".psd1") else None


def read_module_version(manifest: str) -> tuple[int, ...] | None:
    """A PowerShell module's ModuleVersion, read from its manifest, which may be UTF-8 or UTF-16."""
    try:
        data = Path(manifest).read_bytes()
    except OSError:
        return None
    text = data.decode("utf-16") if data[:2] in (b"\xff\xfe", b"\xfe\xff") else data.decode("utf-8-sig", "replace")
    match = MODULE_VERSION.search(text)
    return tools.parse_version(match.group(1)) if match else None


def find_powershell() -> str:
    found = shutil.which("pwsh")
    if found:
        return found
    raise AssertionError("PowerShell 7 (pwsh) was not found in PATH. It is required for repository validation.")


def find_ruff() -> str | None:
    """ruff from this interpreter, where requirements-dev.txt installs it even when its scripts are not on PATH."""
    try:
        from ruff.__main__ import find_ruff_bin  # type: ignore[import-untyped]  # ruff ships no type information

        return find_ruff_bin()
    except (ImportError, FileNotFoundError):
        return platform_support.find_executable("ruff")


def find_mypy() -> str | None:
    """mypy from this interpreter's scripts directory, where requirements-dev.txt installs it, or else from PATH."""
    return shutil.which("mypy", path=sysconfig.get_path("scripts")) or platform_support.find_executable("mypy")


PREREQUISITES: tuple[tuple[str, str, Callable[[], str | None]], ...] = (
    ("Git Bash", "Git Bash", find_git_bash),
    ("ShellCheck", "ShellCheck", find_shellcheck),
    ("PSScriptAnalyzer", "PSScriptAnalyzer", find_psscriptanalyzer),
    ("PowerShell 7 (pwsh)", "PowerShell", find_powershell),
    ("ruff", "ruff", find_ruff),
    ("mypy", "mypy", find_mypy),
)


def missing_prerequisites(
    prerequisites: tuple[tuple[str, str, Callable[[], str | None]], ...] = PREREQUISITES,
) -> list[str]:
    """Describe every required tool that cannot be found, with its install hint."""
    missing: list[str] = []
    for label, hint_key, finder in prerequisites:
        try:
            found = finder()
        except AssertionError:
            found = None
        if not found:
            missing.append(f"  - {label}: {platform_support.install_hint(hint_key)}")
    return missing


# The oldest release of each validation tool the suite is known to work with. docs/dependency-updates.md owns these,
# and CI installs Python, ShellCheck, PSScriptAnalyzer, ruff, and mypy at their floors. Git Bash has no floor: the
# shell scripts need no Bash 4. ruff's and mypy's floors are their pins in requirements-dev.txt, because a newer
# release can change the formatting style or report new errors in an unchanged tree.
DEPENDENCY_DOC = "docs/dependency-updates.md"
VALIDATION_FLOORS: dict[str, tuple[int, ...]] = {
    "Python": tools.MINIMUM_PYTHON,
    "ShellCheck": (0, 9, 0),
    "PSScriptAnalyzer": (1, 25, 0),
    "PowerShell 7 (pwsh)": (7, 0),
    "ruff": (0, 16, 10),
    "mypy": (2, 4, 0),
}


def read_tool_version(path: str) -> tuple[int, ...] | None:
    if path.casefold().endswith(".psd1"):
        return read_module_version(path)
    result = platform_support.run_tool([path, "--version"])
    return tools.parse_version(result.output) if result.returncode == 0 else None


def tool_versions(
    prerequisites: tuple[tuple[str, str, Callable[[], str | None]], ...] = PREREQUISITES,
    read_version: Callable[[str], tuple[int, ...] | None] = read_tool_version,
) -> dict[str, tuple[int, ...] | None]:
    """The version of each required tool that is found, or None where it cannot be read."""
    versions: dict[str, tuple[int, ...] | None] = {}
    for label, _, finder in prerequisites:
        try:
            found = finder()
        except AssertionError:
            found = None
        if found:
            versions[label] = read_version(found)
    return versions


def outdated_prerequisites(versions: dict[str, tuple[int, ...] | None], python: tuple[int, ...]) -> list[str]:
    """Describe every found tool older than its floor, or whose version cannot be read, with its install hint."""
    hints = {label: hint_key for label, hint_key, _ in PREREQUISITES}
    found = {"Python": python, **versions}
    outdated: list[str] = []
    for label, floor in VALIDATION_FLOORS.items():
        if label not in found:
            continue
        version = found[label]
        hint = f": {platform_support.install_hint(hints[label])}" if label in hints else ""
        if version is None:
            outdated.append(
                f"  - {label}: its version could not be read; the floor is {tools.format_version(floor)} in "
                f"{DEPENDENCY_DOC}{hint}"
            )
        elif version < floor:
            outdated.append(
                f"  - {label} {tools.format_version(version)} is older than the floor "
                f"{tools.format_version(floor)} in {DEPENDENCY_DOC}{hint}"
            )
    return outdated


def report_prerequisite_problems(versions: dict[str, tuple[int, ...] | None]) -> bool:
    missing = missing_prerequisites()
    outdated = outdated_prerequisites(versions, tools.python_version())
    if missing:
        print("Repository validation cannot run; required tools were not found:", file=sys.stderr)
        for line in missing:
            print(line, file=sys.stderr)
    if outdated:
        print("Repository validation cannot run; required tools are older than their floors:", file=sys.stderr)
        for line in outdated:
            print(line, file=sys.stderr)
    if missing or outdated:
        for line in platform_support.INSTALL_HELP:
            print(line, file=sys.stderr)
    return bool(missing or outdated)


class ToolchainPolicies(unittest.TestCase):
    def test_validation_floors_are_the_documented_versions(self) -> None:
        self.assertEqual(
            {
                "Python": (3, 11),
                "ShellCheck": (0, 9, 0),
                "PSScriptAnalyzer": (1, 25, 0),
                "PowerShell 7 (pwsh)": (7, 0),
                "ruff": (0, 16, 10),
                "mypy": (2, 4, 0),
            },
            VALIDATION_FLOORS,
        )
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for row in (
            "| Python | 3.11 |",
            "| ShellCheck | 0.9.0 |",
            "| PSScriptAnalyzer | 1.25.0 |",
            "| PowerShell 7 (`pwsh`) | 7.0 |",
            "| ruff | 0.16.10 |",
            "| mypy | 2.4.0 |",
        ):
            with self.subTest(row=row):
                self.assertIn(row, document)

    def test_ci_exercises_each_floor(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        self.assertIn("choco install shellcheck --version 0.9.0 ", workflow)
        self.assertIn("Install-Module PSScriptAnalyzer -RequiredVersion 1.25.0 -Scope CurrentUser -Force", workflow)
        # The floor runs on every event; the latest release only on the schedule, where it cannot block a pull request.
        self.assertIn(
            "python-version: ${{ github.event_name == 'schedule' && fromJSON('[\"3.11\", \"3.x\"]') "
            "|| fromJSON('[\"3.11\"]') }}",
            workflow,
        )
        self.assertIn("if: github.event_name == 'schedule' && matrix.python-version == '3.x'", workflow)
        # Both matrix entries install the pinned development dependencies, so ruff and mypy run at their floors.
        self.assertIn("python -m pip install -r requirements-dev.txt", workflow)
        self.assertIn("python -m mypy --version", workflow)
        requirements = (REPOSITORY_ROOT / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(["ruff==0.16.10", "mypy==2.4.0"], [line for line in requirements if "==" in line])
        dependabot = (REPOSITORY_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertRegex(
            dependabot, r"(?m)^  - package-ecosystem: pip\n    directory: /\n    schedule:\n      interval: weekly$"
        )
        # The aggregate job keeps the single required status check context that branch protection names.
        self.assertRegex(workflow, r"(?m)^  validate:\n(?:    .*\n)*?    needs: suite$")
