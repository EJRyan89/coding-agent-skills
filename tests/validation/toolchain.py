"""The validation prerequisites: where each tool is found, its version, and its floor."""

from __future__ import annotations

import re
import sys
import sysconfig
import tempfile
import unittest
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import file_presence
import json_reader
import json_writer
from job_pool import Terminated, refuse_termination, run_process
from validation_support import REPOSITORY_ROOT, continued_lines, find_fences, markdown_section, repository_files

from deployer import platform_support, render, tools

# Commands whose program the commands reader cannot read, by the scope that starts them, and why.
COMMANDS_ALLOWED = {
    "shellcheck_job": "the ShellCheck runner starts the program its caller located",
    "bash_syntax_job": "the Bash syntax runner starts the Bash its caller located",
    "ruff_job": "the ruff runner starts the program find_ruff located",
    "mypy_job": "the mypy runner starts the program find_mypy located",
    "read_tool_version": "runs the version probe of a prerequisite its finder located",
}


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
        json_writer.write(request, inputs)
        result = platform_support.run_tool(command, {**(environment or {}), variable: str(request)})
    refuse_termination(command, result.returncode, result.output)
    lines = [line for line in result.output.splitlines() if line.strip()]
    try:
        found = json_reader.loads(lines[-1]) if result.returncode == 0 and lines else None
    except json_reader.CONTENT_ERRORS:
        found = None
    if not isinstance(found, list):
        raise AssertionError(f"{failure} (exit code {result.returncode}):\n{result.output}")
    return found


# Concise output names one file per line instead of printing each diff, and no cache is written into the tree. Given a
# folder, ruff passes over each file a gitignore names, the home's and a .ignore above the repository included, so a
# setting outside the repository could keep a file from being checked; ruff reads every file under its targets.
RUFF_OPTIONS = ("--output-format", "concise", "--no-cache", "--no-respect-gitignore")
# mypy resolves an import through MYPYPATH ahead of mypy_path, so a stub in a folder it names could stand in for a
# module the repository checks; mypy reads only the paths the configuration names.
MYPY_ENVIRONMENT = {"MYPYPATH": ""}


def shellcheck_job(shellcheck: str, *arguments: str) -> None:
    """Run ShellCheck on `arguments` as a job, reading no rc file and no SHELLCHECK_OPTS; run_process says how a
    failure is raised."""
    run_process(render.shellcheck_command(shellcheck, *arguments), render.SHELLCHECK_ENVIRONMENT)


def bash_syntax_job(bash: str, path: str) -> None:
    """Parse a Bash file with `bash -n` as a job, running none of it."""
    run_process([bash, "-n", path])


def ruff_job(ruff: str, subcommand: Sequence[str], targets: Sequence[str], cwd: Path) -> None:
    """Run a ruff subcommand on targets from cwd as a job, reading every file under them."""
    run_process([ruff, *subcommand, *RUFF_OPTIONS, *targets], cwd=cwd)


def mypy_job(mypy: str, configuration: Path, arguments: Sequence[str], cwd: Path) -> None:
    """Run mypy from cwd as a job, with the one configuration given and no MYPYPATH."""
    run_process([mypy, "--config-file", str(configuration), *arguments], MYPY_ENVIRONMENT, cwd=cwd)


def find_ruff() -> str | None:
    """ruff from this interpreter, where requirements-dev.txt installs it even when its scripts are not on PATH."""
    try:
        from ruff.__main__ import find_ruff_bin  # type: ignore[import-untyped]  # ruff ships no type information

        found: str = find_ruff_bin()
        return found
    except (ImportError, FileNotFoundError):
        return platform_support.find_executable("ruff")


def find_mypy() -> str | None:
    """mypy from this interpreter's scripts directory, where requirements-dev.txt installs it, or else from PATH."""
    scripts = sysconfig.get_path("scripts")
    return platform_support.find_on_path("mypy", scripts) or platform_support.find_executable("mypy")


def find_pytest() -> str | None:
    """pytest from this interpreter's scripts directory, where requirements-dev.txt installs it, or else from PATH.
    The runner starts it as `python -m pytest`, so this interpreter's copy is the one whose version counts."""
    scripts = sysconfig.get_path("scripts")
    return platform_support.find_on_path("pytest", scripts) or platform_support.find_executable("pytest")


PREREQUISITES: tuple[tuple[str, str, Callable[[], str | None]], ...] = (
    (platform_support.BASH_NAME, platform_support.BASH_NAME, find_git_bash),
    ("ShellCheck", "ShellCheck", find_shellcheck),
    ("PSScriptAnalyzer", "PSScriptAnalyzer", find_psscriptanalyzer),
    ("PowerShell 7 (pwsh)", "PowerShell", find_powershell),
    ("ruff", "ruff", find_ruff),
    ("mypy", "mypy", find_mypy),
    ("pytest", "pytest", find_pytest),
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
# CI installs PSScriptAnalyzer, ruff, mypy, and pytest at their floors on every leg, Python at the newest release of
# its floor's minor version, and ShellCheck at its floor on the Windows legs; the Linux job takes Ubuntu's archive
# ShellCheck unpinned, which this floor must not pass. PowerShell 7 and Bash arrive unpinned with each runner image,
# and the macOS legs install Homebrew's Bash. The shell scripts need no Bash 4, but the suites hold tools/bash_syntax.py
# and the hub guard to the Bash validation finds, and the reader reads Bash as 5.2 and later do, where macOS's own
# /bin/bash is 3.2. ruff's, mypy's, and pytest's floors are their pins in requirements-dev.txt, because a newer release
# can change the formatting style, report new errors, or collect and report tests differently in an unchanged tree.
DEPENDENCY_DOC = "docs/dependency-updates.md"
VALIDATION_FLOORS: dict[str, tuple[int, ...]] = {
    "Python": tools.MINIMUM_PYTHON,
    platform_support.BASH_NAME: (5, 2),
    "ShellCheck": (0, 9, 0),
    "PSScriptAnalyzer": (1, 25, 0),
    "PowerShell 7 (pwsh)": (7, 0),
    "ruff": (0, 17, 3),
    "mypy": (2, 4, 1),
    "pytest": (9, 2, 0),
}


# The Python packages validation and CI take from PyPI, which "Development dependencies" in the dependency document
# lists, and no others.
DEVELOPMENT_PACKAGES = ("ruff", "mypy", "pytest")
# The packages only the mutation gate's workflow installs, from requirements-mutation.txt, beside those: mutmut, which
# makes the mutants, and coverage.py, which measures which suites reach each line.
MUTATION_PACKAGES = ("mutmut", "coverage")
REQUIREMENT_PIN = re.compile(r"(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[0-9][0-9A-Za-z.+!-]*)")
# Each set's hand-edited input, and the lock pip-tools compiles from it, which pins every package the set installs,
# transitive ones included, each with the sha256 hash of every file PyPI holds for its version. CI installs a lock
# with --require-hashes, so nothing it does not pin, and no file whose hash it does not hold, is installed. The
# mutation input takes the development input whole and the development lock as a constraint, so its lock is the
# mutation job's one install, and a package both locks hold is pinned alike in each.
DEVELOPMENT_INPUT = "requirements-dev.in"
DEVELOPMENT_LOCK = "requirements-dev.txt"
MUTATION_INPUT = "requirements-mutation.in"
MUTATION_LOCK = "requirements-mutation.txt"
MUTATION_INCLUDES = (f"-r {DEVELOPMENT_INPUT}", f"-c {DEVELOPMENT_LOCK}")
LOCK_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
# How CI runs pip: every download checked against the lock's hashes, and no source distribution, whose build would
# run its own code where a Python has no wheel.
HASH_CHECKED_INSTALL = "--require-hashes --only-binary :all:"


@dataclass(frozen=True)
class Locked:
    """One package a lock pins: its version and the hashes pip accepts for it."""

    version: str
    hashes: frozenset[str]


def package_name(name: str) -> str:
    """A package's name as PyPI compares it, so `Typing_Extensions` and `typing-extensions` are one package."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirements_problems(text: str, packages: tuple[str, ...], includes: tuple[str, ...] = ()) -> list[str]:
    """Every line of a requirements input that is not a comment, a blank line, one of `includes` as written, or
    `name==version` for one of `packages`, so an unpinned package, another index, another included file, or an
    install from a repository fails, and every package it does not pin exactly once."""
    problems: list[str] = []
    pinned: list[str] = []
    for number, line in enumerate(text.splitlines(), start=1):
        content = line.strip()
        if not content or content.startswith("#") or content in includes:
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


def locked_requirements(text: str) -> tuple[dict[str, Locked], list[str]]:
    """Each package a lock pins, by name, and every requirement in it that is not `name==version` with sha256 hashes
    and nothing else: an unhashed or unpinned package, a marker, an option, an included file, or an install from a URL
    fails, and so does a package pinned twice. A requirement continues across lines that end in a backslash; a comment
    line, such as pip-tools' `# via` lines between two requirements, is skipped."""
    requirements: list[str] = []
    parts: list[str] = []
    for line in text.splitlines():
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        parts.append(content.removesuffix("\\").strip())
        if not content.endswith("\\"):
            requirements.append(" ".join(parts))
            parts = []
    if parts:
        requirements.append(" ".join(parts))
    locked: dict[str, Locked] = {}
    problems: list[str] = []
    for requirement in requirements:
        pin, *options = requirement.split()
        match = REQUIREMENT_PIN.fullmatch(pin)
        if match is None or not options or not all(LOCK_HASH.fullmatch(option) for option in options):
            problems.append(f"{requirement!r} is not name==version with sha256 hashes and nothing else")
            continue
        name = package_name(match.group("name"))
        if name in locked:
            problems.append(f"{name} is locked more than once")
        locked[name] = Locked(match.group("version"), frozenset(options))
    return locked, problems


def lock_problems(lock: str, inputs: str, shared: Mapping[str, Locked] | None = None) -> list[str]:
    """Report a lock that holds a requirement locked_requirements refuses, that pins a package its inputs pin at
    another version or not at all, or that pins a package `shared` locks at another version or with other hashes."""
    locked, problems = locked_requirements(lock)
    for line in inputs.splitlines():
        match = REQUIREMENT_PIN.fullmatch(line.strip())
        if match is None:
            continue
        name, version = package_name(match.group("name")), match.group("version")
        if name not in locked:
            problems.append(f"the input pins {name}=={version}, which the lock does not hold")
        elif locked[name].version != version:
            problems.append(f"the input pins {name}=={version}, which the lock pins at {locked[name].version}")
    for name, entry in (shared or {}).items():
        if locked.get(name) != entry:
            problems.append(f"{name} is not locked as the development lock locks it, at {entry.version} and its hashes")
    return problems


# A word that runs pip: pip, pip3, or pip3.11, as a name or the last part of a path, with or without .exe, as in
# `python -m pip`, `pip3`, or `uv pip`.
PIP_PROGRAM = re.compile(r"(?:.*[/\\])?pip(?:3(?:\.\d+)*)?(?:\.exe)?", re.IGNORECASE)
# What ends one command and starts the next on a line, in Bash and PowerShell: a list operator, a pipe, or `;`.
COMMAND_BREAK = re.compile(r"&&|\|\||[;|&]")


def pip_installs(workflow: str) -> list[tuple[int, list[str], str]]:
    """Each command of a workflow that runs pip with an install among its words after it, as the number of its first
    line, the words after the pip, and the command, however its options and spacing fall, onto however many lines a
    trailing backslash or backtick continues it, and wherever among the commands a list operator, a pipe, or `;` joins
    on one line it stands."""
    found: list[tuple[int, list[str], str]] = []
    for number, line in continued_lines(workflow):
        for command in COMMAND_BREAK.split(line):
            words = command.split()
            for index, word in enumerate(words):
                if PIP_PROGRAM.fullmatch(word.strip("'\"")) and "install" in words[index + 1 :]:
                    found.append((number, words[index + 1 :], command))
                    break
    return found


# pip reads its options with optparse, which takes a long option by any prefix that names it alone and a short option's
# value attached or as the next word, after any short flags clustered before it. --requirement shares `--require` with
# --require-hashes and --require-virtualenv, so a prefix names it alone from `--requirem` on, and a shorter one is
# ambiguous, which pip refuses. Of pip install's short options, these take a value, which ends a cluster.
REQUIREMENT_OPTION = "--requirement"
REQUIREMENT_PREFIX = "--requirem"
PIP_SHORT_VALUES = frozenset("cefirt")


def _clustered_requirement(word: str) -> tuple[str, str] | None:
    """The short flags before a cluster's -r and the value attached after it, or None when no -r is in the cluster
    before an option that takes the rest of the word as its value."""
    for index, letter in enumerate(word[1:], start=1):
        if letter == "r":
            return word[:index], word[index + 1 :]
        if letter in PIP_SHORT_VALUES:
            return None
    return None


def install_words(words: list[str]) -> list[str]:
    """The words after a pip, unquoted, with each requirements file named in a form optparse reads written as
    `-r <file>`: `-r<file>`, `--requirement=<file>`, a prefix of --requirement that names it alone, with its value
    attached or after it, and -r after short flags clustered before it, as `-qr <file>` writes it."""
    read: list[str] = []
    for word in (word.strip("'\"") for word in words):
        name, equals, value = word.partition("=")
        if name.startswith(REQUIREMENT_PREFIX) and REQUIREMENT_OPTION.startswith(name):
            read += ["-r", value] if equals else ["-r"]
        elif word.startswith("-") and not word.startswith("--") and (cut := _clustered_requirement(word)) is not None:
            flags, attached = cut
            read += [flags] if flags != "-" else []
            read += ["-r", attached] if attached else ["-r"]
        else:
            read.append(word)
    return read


def names_requirements(words: list[str]) -> bool:
    """Whether the words after a pip name a requirements file, in any form install_words reads, or by a prefix of
    --requirement too short to name it alone, which pip refuses but which a reader must not take for no file."""
    names = (word.strip("'\"").partition("=")[0] for word in words)
    return "-r" in install_words(words) or any(
        name.startswith("--") and len(name) > 2 and REQUIREMENT_OPTION.startswith(name) for name in names
    )


def checks_hashes(words: list[str], locks: tuple[str, ...]) -> bool:
    """Whether the words after a pip read `install --require-hashes --only-binary :all:` and then only `-r <lock>` for
    one of `locks`, each requirements file in any form install_words reads."""
    words = install_words(words)
    flags = ["install", *HASH_CHECKED_INSTALL.split()]
    files = words[len(flags) :]
    pairs = list(zip(files[::2], files[1::2], strict=False))
    return (
        words[: len(flags)] == flags
        and bool(pairs)
        and not len(files) % 2
        and all(flag == "-r" and path in locks for flag, path in pairs)
    )


def hash_install_problems(workflow: str, locks: tuple[str, ...]) -> list[str]:
    """Report each line of a workflow whose pip install does not check hashes, may build a source distribution, or
    installs anything but a lock: pip must be followed by `install --require-hashes --only-binary :all:` and then only
    `-r <lock>` for one of `locks`, so a workflow with no locks may run no pip install at all."""
    return [
        f"line {number} does not install only {list(locks)} with {HASH_CHECKED_INSTALL}: {line.strip()!r}"
        for number, words, line in pip_installs(workflow)
        if not checks_hashes(words, locks)
    ]


def fence_install_problems(markdown: str, locks: tuple[str, ...]) -> list[str]:
    """Report each pip install in a Markdown fence that installs from a requirements file other than as CI installs
    a lock, so a documented install of a lock checks hashes and builds no source distribution, as hash_install_problems
    holds a workflow's. A fence's pip install that names no requirements file, such as one of pip-tools, is not
    one."""
    lines = markdown.splitlines()
    body = {index + 1 for fence in find_fences(lines) for index in fence.body()}
    return [
        f"line {number} does not install only {list(locks)} with {HASH_CHECKED_INSTALL}: {line.strip()!r}"
        for number, words, line in pip_installs(markdown)
        if number in body and names_requirements(words) and not checks_hashes(words, locks)
    ]


def floor_copy_problems(text: str, pattern: str, floor: str) -> list[str]:
    """Report a document that names no copy of a floor where `pattern`, whose one group is the version, finds it, and
    each copy that names a version other than the floor."""
    found = re.findall(pattern, text)
    if not found:
        return [f"no copy of the floor matches {pattern!r}"]
    return [f"{version} is named where the floor is {floor}" for version in found if version != floor]


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
        if version is not None and version >= floor:
            continue
        # The install hint only of a tool reported, so a tool at its floor needs none.
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


# Homebrew commands that install a package by naming it, each followed by the rest of its command on the line: up to a
# list operator, a pipe, or a Markdown cell or code span's end.
BREW_INSTALL = re.compile(r"\bbrew\s+(?:install|reinstall|upgrade)\b(?P<arguments>[^\n|`;&]*)")
# The packages Homebrew no longer has, as a brew command names them, bare or by their tap. Homebrew dropped its
# PowerShell cask, and with no formula of that name a bare `brew install powershell` fails the same way; the preview
# cask that remains installs pwsh-preview, which the deployer and the skills never look for.
REMOVED_BREW_PACKAGES = {"powershell": "install Microsoft's .pkg from PowerShell's GitHub releases instead"}


def removed_install_problems(text: str) -> list[str]:
    """Report each line of `text` that installs, with Homebrew, a package REMOVED_BREW_PACKAGES names: a document,
    an install hint, or a workflow that holds one sends a person or a runner to a command that fails."""
    problems: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in BREW_INSTALL.finditer(line):
            for word in match["arguments"].split():
                package = word.strip("'\"").rsplit("/", 1)[-1]
                if package in REMOVED_BREW_PACKAGES:
                    problems.append(
                        f"line {number}: {match.group(0).strip()!r} installs Homebrew's {package}, which Homebrew no "
                        f"longer has; {REMOVED_BREW_PACKAGES[package]}"
                    )
    return problems


class ToolchainPolicies(unittest.TestCase):
    def test_validation_floors_are_the_documented_versions(self) -> None:
        self.assertIn(platform_support.BASH_NAME, ("Git Bash", "Bash"))
        self.assertEqual(
            {
                "Python": (3, 11, 4),
                platform_support.BASH_NAME: (5, 2),
                "ShellCheck": (0, 9, 0),
                "PSScriptAnalyzer": (1, 25, 0),
                "PowerShell 7 (pwsh)": (7, 0),
                "ruff": (0, 17, 3),
                "mypy": (2, 4, 1),
                "pytest": (9, 2, 0),
            },
            VALIDATION_FLOORS,
        )
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for row in (
            "| Python | 3.11.4 |",
            "| Git Bash, or Bash on Linux and macOS | 5.2 |",
            "| ShellCheck | 0.9.0 |",
            "| PSScriptAnalyzer | 1.25.0 |",
            "| PowerShell 7 (`pwsh`) | 7.0 |",
            "| ruff | 0.17.3 |",
            "| mypy | 2.4.1 |",
            "| pytest | 9.2.0 |",
        ):
            with self.subTest(row=row):
                self.assertIn(row, document)

    def test_the_mutation_gate_installs_its_pinned_tools_and_nothing_else(self) -> None:
        requirements = (REPOSITORY_ROOT / MUTATION_INPUT).read_text(encoding="utf-8")
        self.assertEqual(
            ["mutmut==3.8.1", "coverage==7.17.2"], [line for line in requirements.splitlines() if "==" in line]
        )
        self.assertEqual(("mutmut", "coverage"), MUTATION_PACKAGES)
        self.assertEqual(("-r requirements-dev.in", "-c requirements-dev.txt"), MUTATION_INCLUDES)
        self.assertEqual([], requirements_problems(requirements, MUTATION_PACKAGES, MUTATION_INCLUDES))
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        self.assertIn("| mutmut | 3.8.1 | `requirements-mutation.in` |", document)
        self.assertIn("| coverage.py | 7.17.2 | `requirements-mutation.in` |", document)

    def test_every_package_installed_is_locked_with_hashes_and_installed_with_hash_checking(self) -> None:
        self.assertEqual(
            ("requirements-dev.in", "requirements-dev.txt", "requirements-mutation.in", "requirements-mutation.txt"),
            (DEVELOPMENT_INPUT, DEVELOPMENT_LOCK, MUTATION_INPUT, MUTATION_LOCK),
        )
        development = (REPOSITORY_ROOT / DEVELOPMENT_INPUT).read_text(encoding="utf-8")
        development_lock = (REPOSITORY_ROOT / DEVELOPMENT_LOCK).read_text(encoding="utf-8")
        self.assertEqual([], lock_problems(development_lock, development))
        locked, _ = locked_requirements(development_lock)
        mutation = (REPOSITORY_ROOT / MUTATION_INPUT).read_text(encoding="utf-8")
        mutation_lock = (REPOSITORY_ROOT / MUTATION_LOCK).read_text(encoding="utf-8")
        self.assertEqual([], lock_problems(mutation_lock, development + mutation, locked))
        # Every workflow is read, and one that installs no lock may run no pip install.
        expected = {"validate.yml": (("requirements-dev.txt",), 3), "mutation.yml": (("requirements-mutation.txt",), 3)}
        workflows = file_presence.matching(REPOSITORY_ROOT / ".github" / "workflows", "*.y*ml")
        self.assertLessEqual(set(expected), {workflow.name for workflow in workflows})
        for workflow in workflows:
            locks, installs = expected.get(workflow.name, ((), 0))
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertEqual(installs, len(pip_installs(text)))
                self.assertEqual([], hash_install_problems(text, locks))

    def test_every_documented_install_of_a_lock_checks_hashes_as_ci_does(self) -> None:
        locks = (DEVELOPMENT_LOCK, MUTATION_LOCK)
        documented: set[str] = set()
        for path in repository_files(REPOSITORY_ROOT):
            if path.suffix != ".md":
                continue
            name = path.relative_to(REPOSITORY_ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            with self.subTest(document=name):
                self.assertEqual([], fence_install_problems(text, locks))
            # With no lock allowed, each install from a requirements file in a fence is reported, so this counts them.
            if fence_install_problems(text, ()):
                documented.add(name)
        self.assertLessEqual({"CONTRIBUTING.md", DEPENDENCY_DOC}, documented)
        # The command validation prints for a missing or outdated development dependency installs the lock the same
        # way, on each platform.
        for hints in (platform_support.WINDOWS_INSTALL_HINTS, platform_support.LINUX_INSTALL_HINTS):
            for package in DEVELOPMENT_PACKAGES:
                with self.subTest(hint=hints[package]):
                    self.assertEqual(1, len(pip_installs(hints[package])))
                    self.assertEqual([], hash_install_problems(hints[package], (DEVELOPMENT_LOCK,)))

    def test_no_document_or_install_hint_names_a_package_homebrew_removed(self) -> None:
        for path in repository_files(REPOSITORY_ROOT):
            if path.suffix != ".md":
                continue
            with self.subTest(document=path.relative_to(REPOSITORY_ROOT).as_posix()):
                self.assertEqual([], removed_install_problems(path.read_text(encoding="utf-8")))
        platforms = {
            "Windows": (platform_support.WINDOWS_INSTALL_HINTS, platform_support.WINDOWS_INSTALL_HELP),
            "Linux": (platform_support.LINUX_INSTALL_HINTS, platform_support.LINUX_INSTALL_HELP),
            "macOS": (platform_support.MACOS_INSTALL_HINTS, platform_support.MACOS_INSTALL_HELP),
        }
        for platform, (hints, help_lines) in platforms.items():
            with self.subTest(platform=platform):
                self.assertEqual([], removed_install_problems("\n".join([*hints.values(), *help_lines])))

    def test_each_documented_copy_of_a_floor_is_the_floor_and_the_dependency_document_names_it(self) -> None:
        catalogue = {**tools.SKILL_TOOLS, **tools.VERIFY_TOOLS}
        floors = {name: tools.format_version(tool.minimum) for name, tool in catalogue.items() if tool.minimum}
        python = tools.format_version(tools.MINIMUM_PYTHON)
        shellcheck = tools.format_version(VALIDATION_FLOORS["ShellCheck"])
        found_python = r"Python (\d+\.\d+\.\d+)"
        copilot = r"Copilot CLI (?:older than )?(\d+(?:\.\d+)+)"
        dotnet = r"\.NET SDK (\S+) or newer"
        # Each copy of a floor outside its declaration: the part of the dependency document that names the copy (a
        # floor's row, or the runtime tool floors section), the document that holds it, the pattern whose group is
        # the version, and the floor it must be.
        copies = (
            ("| Python |", "CLAUDE.md", found_python, python),
            ("| Python |", "CONTRIBUTING.md", found_python, python),
            ("| Python |", "README.md", found_python, python),
            ("| Python |", "docs/installation.md", found_python, python),
            ("| Python |", "docs/code-review-operations.md", found_python, python),
            ("| Python |", "docs/design.md", r"the Python floor is (\d+\.\d+\.\d+)", python),
            ("| ShellCheck |", "docs/installation.md", r"CI uses (\d+\.\d+\.\d+)", shellcheck),
            ("## Runtime tool floors", "docs/installation.md", r"GitHub CLI \(`gh`\) (\S+) or newer", floors["gh"]),
            ("## Runtime tool floors", "docs/installation.md", r"Codex CLI (\S+) or newer", floors["codex"]),
            ("## Runtime tool floors", "docs/codex-support.md", r"Codex CLI before (\S+) ", floors["codex"]),
            ("## Runtime tool floors", "docs/installation.md", copilot, floors["copilot"]),
            ("## Runtime tool floors", "docs/copilot-support.md", copilot, floors["copilot"]),
            ("## Runtime tool floors", "docs/code-review-operations.md", copilot, floors["copilot"]),
            ("## Runtime tool floors", "docs/code-review-operations-contract.md", copilot, floors["copilot"]),
            ("## Runtime tool floors", "docs/installation.md", dotnet, floors["dotnet"]),
            ("## Runtime tool floors", "docs/skills.md", dotnet, floors["dotnet"]),
        )
        dependencies = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        for part, document, pattern, expected in copies:
            if part.startswith("|"):
                named = next(line for line in dependencies.splitlines() if line.startswith(part))
            else:
                named = markdown_section(dependencies, part) or ""
            with self.subTest(document=document, pattern=pattern):
                text = (REPOSITORY_ROOT / document).read_text(encoding="utf-8")
                self.assertEqual([], floor_copy_problems(text, pattern, expected))
                self.assertIn("the README" if document == "README.md" else f"`{document}`", named)

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
        # The floor runs on every event; the latest release only on the schedule, where it cannot block a pull request,
        # on the Windows, Linux, and macOS legs.
        self.assertEqual(
            3,
            workflow.count(
                "python-version: ${{ github.event_name == 'schedule' && fromJSON('[\"3.11\", \"3.x\"]') "
                "|| fromJSON('[\"3.11\"]') }}"
            ),
        )
        self.assertIn("if: github.event_name == 'schedule' && matrix.python-version == '3.x'", workflow)
        # Both matrix entries install the pinned development dependencies, so ruff, mypy, and pytest run at their
        # floors.
        self.assertIn("python -m pip install --require-hashes --only-binary :all: -r requirements-dev.txt", workflow)
        self.assertIn("python -m mypy --version", workflow)
        requirements = (REPOSITORY_ROOT / DEVELOPMENT_INPUT).read_text(encoding="utf-8")
        self.assertEqual(
            ["ruff==0.17.3", "mypy==2.4.1", "pytest==9.2.0"],
            [line for line in requirements.splitlines() if "==" in line],
        )
        # Every other line is a comment or blank, and the packages are the ones the dependency document lists.
        self.assertEqual(("ruff", "mypy", "pytest"), DEVELOPMENT_PACKAGES)
        self.assertEqual([], requirements_problems(requirements, DEVELOPMENT_PACKAGES))
        document = (REPOSITORY_ROOT / DEPENDENCY_DOC).read_text(encoding="utf-8")
        self.assertIn(
            "pinned in `requirements-dev.txt`, and no others: `ruff`, the formatter and linter, `mypy`, the type "
            "checker, and `pytest`, the test runner",
            document,
        )
        dependabot = (REPOSITORY_ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
        self.assertRegex(
            dependabot, r"(?m)^  - package-ecosystem: pip\n    directory: /\n    schedule:\n      interval: weekly$"
        )
        # The aggregate job keeps the single required status check context that branch protection names, over the
        # Windows legs, the Linux job, and the known-failure issues' job alike.
        self.assertRegex(workflow, r"(?m)^  validate:\n(?:    .*\n)*?    needs: \[suite, linux, known-failures\]$")
