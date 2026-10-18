"""The validation prerequisites: where each tool is found, its version, and its floor."""

from __future__ import annotations

import json
import re
import sys
import sysconfig
import tempfile
import unittest
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from job_pool import Terminated, refuse_termination
from validation_support import REPOSITORY_ROOT

from deployer import platform_support, tools


def find_git_bash() -> str:
    found = platform_support.find_bash()
    if found:
        return found
    raise AssertionError(
        f"{platform_support.BASH_MISSING} {platform_support.BASH_NAME} is required for repository validation."
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
    """The manifest of the newest PSScriptAnalyzer pwsh can load, or None when pwsh lists none.

    Raises Terminated when pwsh was ended before it wrote anything, which says nothing about the module.
    """
    probe = [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", NEWEST_SCRIPT_ANALYZER]
    result = platform_support.run_tool(probe)
    refuse_termination(probe, result.returncode, result.output)
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
    found = platform_support.find_executable("pwsh")
    if found:
        return found
    raise AssertionError("PowerShell 7 (pwsh) was not found in PATH. It is required for repository validation.")


def powershell_json(
    script: str, inputs: object, variable: str, failure: str, environment: Mapping[str, str] | None = None
) -> list[Any]:
    """The JSON list the last line of a PowerShell script's output holds, the script given inputs as JSON in a
    throwaway file whose path it finds in the environment variable named `variable`, beside `environment`.

    Every policy that asks PowerShell to read something runs it here, so each refuses a termination, an exit status
    other than 0, and output that is not a list the same way. Raises Terminated when pwsh was ended before it wrote
    anything, and AssertionError opening with `failure`, the exit status, and the output otherwise.
    """
    command = [find_powershell(), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script]
    with tempfile.TemporaryDirectory(prefix="powershell-json-") as directory:
        request = Path(directory) / "inputs.json"
        request.write_text(json.dumps(inputs), encoding="utf-8")
        result = platform_support.run_tool(command, {**(environment or {}), variable: str(request)})
    refuse_termination(command, result.returncode, result.output)
    lines = [line for line in result.output.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if result.returncode == 0 and lines else None
    except json.JSONDecodeError:
        found = None
    if not isinstance(found, list):
        raise AssertionError(f"{failure} (exit code {result.returncode}):\n{result.output}")
    return found


def find_ruff() -> str | None:
    """ruff from this interpreter, where requirements-dev.txt installs it even when its scripts are not on PATH."""
    try:
        from ruff.__main__ import find_ruff_bin  # type: ignore[import-untyped]  # ruff ships no type information

        return find_ruff_bin()
    except (ImportError, FileNotFoundError):
        return platform_support.find_executable("ruff")


def find_mypy() -> str | None:
    """mypy from this interpreter's scripts directory, where requirements-dev.txt installs it, or else from PATH."""
    scripts = sysconfig.get_path("scripts")
    return platform_support.find_on_path("mypy", scripts) or platform_support.find_executable("mypy")


PREREQUISITES: tuple[tuple[str, str, Callable[[], str | None]], ...] = (
    (platform_support.BASH_NAME, platform_support.BASH_NAME, find_git_bash),
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
        except Terminated as exc:
            missing.append(f"  - {label}: could not be looked for, because {exc}; run validation again")
            continue
        if not found:
            missing.append(f"  - {label}: {platform_support.install_hint(hint_key)}")
    return missing


# The oldest release of each validation tool the suite is known to work with. docs/dependency-updates.md owns these.
# CI installs PSScriptAnalyzer, ruff, and mypy at their floors on every leg, Python at the newest release of its floor's
# minor version, and ShellCheck at its floor on the Windows legs; the Linux job takes Ubuntu's archive ShellCheck
# unpinned, which this floor must not pass. PowerShell 7 arrives unpinned with each runner image. Git Bash has no floor:
# the shell scripts need no Bash 4. ruff's and mypy's floors are their pins in requirements-dev.txt, because a newer
# release can change the formatting style or report new errors in an unchanged tree.
DEPENDENCY_DOC = "docs/dependency-updates.md"
VALIDATION_FLOORS: dict[str, tuple[int, ...]] = {
    "Python": tools.MINIMUM_PYTHON,
    "ShellCheck": (0, 9, 0),
    "PSScriptAnalyzer": (1, 25, 0),
    "PowerShell 7 (pwsh)": (7, 0),
    "ruff": (0, 17, 1),
    "mypy": (2, 4, 0),
}


# The Python packages validation and CI take from PyPI, which "Development dependencies" in the dependency document
# lists, and no others.
DEVELOPMENT_PACKAGES = ("ruff", "mypy")
REQUIREMENT_PIN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==[0-9][0-9A-Za-z.+!-]*")


def requirements_problems(text: str, packages: tuple[str, ...]) -> list[str]:
    """Every line of a requirements file that is not a comment, a blank line, or `name==version` for one of `packages`,
    so an unpinned package, another index, an included file, or an install from a repository fails, and every package
    it does not pin exactly once."""
    problems: list[str] = []
    pinned: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        match = REQUIREMENT_PIN.fullmatch(content)
        if match is None or match.group("name") not in packages:
            problems.append(f"line {number} is not name==version for one of {list(packages)}: {content!r}")
        else:
            pinned.append(match.group("name"))
    problems += [
        f"{package} is pinned {pinned.count(package)} times, not once"
        for package in packages
        if pinned.count(package) != 1
    ]
    return problems


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
        except (AssertionError, Terminated):  # missing_prerequisites reports either
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
                "Python": (3, 11, 4),
                "ShellCheck": (0, 9, 0),
                "PSScriptAnalyzer": (1, 25, 0),
                "PowerShell 7 (pwsh)": (7, 0),
                "ruff": (0, 17, 1),
                "mypy": (2, 4, 0),
            },
            VALIDATION_FLOORS,
        )
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for row in (
            "| Python | 3.11.4 |",
            "| ShellCheck | 0.9.0 |",
            "| PSScriptAnalyzer | 1.25.0 |",
            "| PowerShell 7 (`pwsh`) | 7.0 |",
            "| ruff | 0.17.1 |",
            "| mypy | 2.4.0 |",
        ):
            with self.subTest(row=row):
                self.assertIn(row, document)

    def test_ci_exercises_each_floor(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github/workflows/validate.yml").read_text(encoding="utf-8")
        # The workflow pins each tool once in its env, which the install command and the cache key both read.
        self.assertRegex(
            workflow, r"(?m)^env:\n  SHELLCHECK_VERSION: '0\.9\.0'\n  PSSCRIPTANALYZER_VERSION: '1\.25\.0'$"
        )
        self.assertIn("choco install shellcheck --version $env:SHELLCHECK_VERSION ", workflow)
        self.assertIn(
            "Install-Module PSScriptAnalyzer -RequiredVersion $env:PSSCRIPTANALYZER_VERSION -Scope CurrentUser -Force",
            workflow,
        )
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
        requirements = (REPOSITORY_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        self.assertEqual(["ruff==0.17.1", "mypy==2.4.0"], [line for line in requirements.splitlines() if "==" in line])
        # Every other line is a comment or blank, and the packages are the two the dependency document lists.
        self.assertEqual(("ruff", "mypy"), DEVELOPMENT_PACKAGES)
        self.assertEqual([], requirements_problems(requirements, DEVELOPMENT_PACKAGES))
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        self.assertIn(
            "pinned in `requirements-dev.txt`, and no others: `ruff`, the formatter and linter, and `mypy`", document
        )
        dependabot = (REPOSITORY_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertRegex(
            dependabot, r"(?m)^  - package-ecosystem: pip\n    directory: /\n    schedule:\n      interval: weekly$"
        )
        # The aggregate job keeps the single required status check context that branch protection names, over the
        # Windows legs and the Linux job alike.
        self.assertRegex(workflow, r"(?m)^  validate:\n(?:    .*\n)*?    needs: \[suite, linux\]$")
